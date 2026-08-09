"""Directional force-directed layout for the wiki's area overview map.

Places every area box on a 2D canvas: a BFS seeding pass that uses
transition-direction hints to put adjacent areas on the correct cardinal
side of each other, followed by a short force-directed simulation that
resolves residual overlaps.

Pure stdlib plus :mod:`nwn_wiki.gff` field access. No wiki state is read or
written here -- the caller passes the ``Db`` in and gets positions back.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from nwn_wiki.gff import fld


# ---------------------------------------------------------------------------
# Directional area layout
# ---------------------------------------------------------------------------
#
# NWN areas are tile grids: the in-game X axis runs east, the Y axis runs
# north. A trigger or door at X≈Width*10 sits on the east edge of its area —
# meaning the area it links to "is to the east" in the player's mental map.
# We exploit that to place adjacent areas on the correct side of each other,
# so the resulting overview map looks like a real overworld instead of a
# random force-directed blob.

def _transition_dir(tr: dict, src_w: float, src_h: float) -> tuple[float, float] | None:
    """Unit vector pointing out of the source area in the direction the
    transition exits. Returns None if the transition has no usable position
    or is well inside the area (we don't want noise near the centre to push
    nodes around)."""
    x, y = tr.get("src_x"), tr.get("src_y")
    if x is None or y is None or src_w <= 0 or src_h <= 0:
        return None
    # Distance from each edge (in metres).
    d_w = x                  # west
    d_e = src_w - x          # east
    d_s = y                  # south
    d_n = src_h - y          # north
    # Closest edge wins; ignore transitions deep inside the area.
    edges = [("w", d_w, (-1.0, 0.0)),
             ("e", d_e, (1.0, 0.0)),
             ("s", d_s, (0.0, -1.0)),
             ("n", d_n, (0.0, 1.0))]
    edges.sort(key=lambda e: e[1])
    name, dist, vec = edges[0]
    # Threshold = 25% of the smaller dimension. Anything further from any
    # edge is ambiguous (e.g. internal door inside a building).
    thresh = 0.25 * min(src_w, src_h)
    if dist > thresh:
        return None
    return vec


def layout_areas(
    node_ids: list[str],
    edges: list[tuple[str, str]],
    *,
    db: "Db | None" = None,
    iterations: int = 600,
    seed: int = 1,
) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    """Place areas on a 2D canvas so the resulting map respects in-game
    orientation.

    Phase 1: BFS from the most-connected node in each connected component,
    seeding positions from transition-direction hints so adjacents start near
    each other in the correct cardinal direction.  Same-direction siblings are
    spread perpendicular to avoid initial overlap.  Disconnected components are
    packed in a grid below the main component.

    Phase 2: A short force-directed simulation (weak repulsion + strong spring
    + directional bias) fine-tunes the layout.

    Returns (positions, sizes). Sizes are in the same units as positions
    (one unit ≈ one tile, so a 16×10 area becomes a 16×10 box). Positions
    are box centres.
    """
    if not node_ids:
        return {}, {}
    rng = random.Random(seed)

    # Box size in tile units. 1.5× the area's tile dims gives enough room for
    # text labels while keeping boxes visually proportionate on the map.
    box_scale = 1.5
    sizes: dict[str, tuple[float, float]] = {}
    for nid in node_ids:
        a = db.areas.get(nid, {}) if db else {}
        w = float(fld(a, "Width", 8) or 8)
        h = float(fld(a, "Height", 8) or 8)
        sizes[nid] = (max(w, 4.0) * box_scale, max(h, 4.0) * box_scale)

    avg_dim = sum(w + h for w, h in sizes.values()) / max(1, 2 * len(sizes))
    node_set = set(node_ids)

    # Per-edge desired direction (averaged over both endpoints' transitions).
    edge_dirs: dict[tuple[str, str], tuple[float, float]] = {}
    if db is not None:
        for tr in db.transitions:
            a, b = tr["src_area"], tr["dst_area"]
            if a not in node_set or not b or b not in node_set or a == b:
                continue
            sw, sh = sizes[a]
            v = _transition_dir(tr, sw * 10.0, sh * 10.0)  # tile→metres
            if v is None:
                continue
            cur = edge_dirs.get((a, b), (0.0, 0.0))
            edge_dirs[(a, b)] = (cur[0] + v[0], cur[1] + v[1])
    # Normalise.
    for k, v in list(edge_dirs.items()):
        m = math.hypot(*v)
        if m > 1e-6:
            edge_dirs[k] = (v[0] / m, v[1] / m)

    # "Ideal" edge length: enough that two boxes don't overlap, plus margin.
    def ideal_len(a: str, b: str) -> float:
        wa, ha = sizes[a]
        wb, hb = sizes[b]
        return 0.5 * (max(wa, ha) + max(wb, hb)) + avg_dim * 0.25

    # Adjacency used by both BFS placement and the force simulation.
    adj: dict[str, set[str]] = {nid: set() for nid in node_ids}
    for a, b in edges:
        if a in node_set and b in node_set and a != b:
            adj[a].add(b)
            adj[b].add(a)
    edge_pairs = [(a, b) for a, b in edges
                  if a in node_set and b in node_set and a != b]

    # --- BFS-based initial placement ---
    # Phase 1: discover connected components, sorted largest-first.
    comp_list: list[list[str]] = []
    disc_seen: set[str] = set()
    disc_remaining: set[str] = set(node_ids)
    while disc_remaining:
        root0 = max(disc_remaining, key=lambda n: len(adj[n]))
        disc_q: list[str] = [root0]; disc_comp: list[str] = [root0]
        disc_seen.add(root0); disc_remaining.discard(root0)
        dh = 0
        while dh < len(disc_q):
            u0 = disc_q[dh]; dh += 1
            for nb0 in adj[u0]:
                if nb0 not in disc_seen:
                    disc_seen.add(nb0); disc_remaining.discard(nb0)
                    disc_q.append(nb0); disc_comp.append(nb0)
        comp_list.append(disc_comp)
    comp_list.sort(key=len, reverse=True)

    def _bfs_place_comp(comp_nodes: list[str]) -> dict[str, list[float]]:
        """BFS-place a connected component, returning positions relative to (0,0)."""
        cpos: dict[str, list[float]] = {}
        cplaced: set[str] = set()
        root = max(comp_nodes, key=lambda n: len(adj[n]))
        cpos[root] = [0.0, 0.0]; cplaced.add(root)
        bfs_q: list[str] = [root]; head = 0
        while head < len(bfs_q):
            u = bfs_q[head]; head += 1
            ux, uy = cpos[u]
            neighbors = [nb for nb in sorted(adj[u]) if nb not in cplaced]
            # Group by quantized direction so same-direction siblings spread
            # perpendicular, avoiding the initial-overlap explosion.
            by_sector: dict[int, list[tuple[str, float, float]]] = defaultdict(list)
            no_hint: list[str] = []
            for nb in neighbors:
                dir_fwd = edge_dirs.get((u, nb))
                dir_rev = edge_dirs.get((nb, u))
                if dir_fwd:
                    ddx, ddy = dir_fwd
                elif dir_rev:
                    ddx, ddy = -dir_rev[0], -dir_rev[1]
                else:
                    no_hint.append(nb); continue
                sector = round(math.atan2(ddy, ddx) / (math.pi / 4)) % 8
                by_sector[sector].append((nb, ddx, ddy))
            used_sectors = set(by_sector.keys())
            for sector, group in by_sector.items():
                angle = sector * math.pi / 4
                mdx, mdy = math.cos(angle), math.sin(angle)
                pdx, pdy = -mdy, mdx
                n = len(group)
                for j, (nb, _, _) in enumerate(group):
                    if nb in cplaced: continue
                    L = ideal_len(u, nb)
                    perp_off = (j - (n - 1) / 2) * (sizes[nb][0] + avg_dim * 0.15)
                    cpos[nb] = [ux + mdx * L + pdx * perp_off,
                                uy + mdy * L + pdy * perp_off]
                    cplaced.add(nb); bfs_q.append(nb)
            avail_angles = [k * math.pi / 4 for k in range(8)
                            if k not in used_sectors]
            for j, nb in enumerate(no_hint):
                if nb in cplaced: continue
                angle = (avail_angles[j % len(avail_angles)] if avail_angles
                         else 2 * math.pi * j / max(1, len(no_hint)))
                L = ideal_len(u, nb)
                cpos[nb] = [ux + math.cos(angle) * L, uy + math.sin(angle) * L]
                cplaced.add(nb); bfs_q.append(nb)
        return cpos

    # Phase 2: place each component; arrange smaller ones in a grid below the main.
    pos: dict[str, list[float]] = {}
    comp_positioned: list[tuple[list[str], dict[str, list[float]], float, float]] = []
    for comp_nodes in comp_list:
        cpos = _bfs_place_comp(comp_nodes)
        # Bounding box of this component relative to its own origin
        cx0 = min(cpos[n][0] - sizes[n][0] / 2 for n in comp_nodes)
        cx1 = max(cpos[n][0] + sizes[n][0] / 2 for n in comp_nodes)
        cy0 = min(cpos[n][1] - sizes[n][1] / 2 for n in comp_nodes)
        cy1 = max(cpos[n][1] + sizes[n][1] / 2 for n in comp_nodes)
        # Normalise to local (0,0) top-left
        for n in comp_nodes:
            cpos[n][0] -= cx0
            cpos[n][1] -= cy0
        comp_positioned.append((comp_nodes, cpos, cx1 - cx0, cy1 - cy0))

    # Main component at origin; secondary components below in a grid.
    # NOTE: layout_areas flips Y at return time (-pos_y → screen coords where
    # +Y = down). Here pre-flip: +Y = north (top of screen), -Y = south (bottom).
    # Main component cpos-Y is normalised so 0 = south edge, ch = north edge.
    # To appear BELOW main on screen, secondary components must use negative
    # pre-flip Y so that after the flip they have positive screen Y.
    sep = avg_dim * 2
    main_nodes, main_cpos, main_w, main_h = comp_positioned[0]
    # Centre the main component horizontally; place with south edge at Y=0.
    for n in main_nodes:
        pos[n] = [main_cpos[n][0] - main_w / 2, main_cpos[n][1]]

    # Secondary components: north edge at -sep (sep below main's south edge),
    # then south edge at -(sep + ch). Track rows, wrap when width exceeds budget.
    # row_top_y is the pre-flip Y of each row's north edge (negative and growing).
    row_top_y = -sep
    row_x = 0.0
    row_max_ch = 0.0
    row_budget = max(main_w, avg_dim * 4)
    for comp_nodes, cpos, cw, ch in comp_positioned[1:]:
        if row_x > 0 and row_x + cw > row_budget:
            row_top_y -= (row_max_ch + sep)
            row_x = 0.0
            row_max_ch = 0.0
        # cpos-Y 0=south ch=north; north edge aligns with row_top_y.
        for n in comp_nodes:
            pos[n] = [row_x + cpos[n][0] - main_w / 2,
                      row_top_y - ch + cpos[n][1]]
        row_x += cw + sep
        row_max_ch = max(row_max_ch, ch)

    # Small jitter to break symmetry before the force simulation.
    for nid in pos:
        pos[nid][0] += rng.uniform(-avg_dim * 0.05, avg_dim * 0.05)
        pos[nid][1] += rng.uniform(-avg_dim * 0.05, avg_dim * 0.05)

    # Temperature: small since BFS starts nodes near-correct — simulation
    # mainly resolves residual overlaps and sharpens directional alignment.
    t = avg_dim * 0.8
    cooling = (t - 0.5) / iterations

    for _ in range(iterations):
        disp = {nid: [0.0, 0.0] for nid in pos}

        # Repulsion — only between boxes whose AABBs are close enough to
        # potentially overlap, with a soft falloff.
        ids = list(pos.keys())
        for i, u in enumerate(ids):
            ux, uy = pos[u]
            uw, uh = sizes[u]
            for v in ids[i + 1:]:
                vx, vy = pos[v]
                vw, vh = sizes[v]
                # AABB overlap distance in each axis (negative = overlap).
                gap_x = abs(ux - vx) - 0.5 * (uw + vw) - avg_dim * 0.15
                gap_y = abs(uy - vy) - 0.5 * (uh + vh) - avg_dim * 0.15
                gap = max(gap_x, gap_y)
                if gap > avg_dim * 0.3:
                    continue  # well clear; no force
                dx = ux - vx
                dy = uy - vy
                d = math.hypot(dx, dy)
                if d < 1e-3:
                    dx = rng.uniform(-0.5, 0.5)
                    dy = rng.uniform(-0.5, 0.5)
                    d = math.hypot(dx, dy) + 1e-3
                # Strong push when overlapping, gentle nudge when close.
                strength = (avg_dim * 1.0) ** 2 / max(d, 1.0)
                if gap < 0:
                    strength *= 6.0
                fx, fy = dx / d * strength, dy / d * strength
                disp[u][0] += fx
                disp[u][1] += fy
                disp[v][0] -= fx
                disp[v][1] -= fy

        # Attractive forces toward an ideal length, with directional bias.
        for a, b in edge_pairs:
            ax, ay = pos[a]
            bx, by = pos[b]
            dx = bx - ax
            dy = by - ay
            d = math.hypot(dx, dy) + 1e-6
            L = ideal_len(a, b)
            # Spring toward L
            f = (d - L) * 0.4
            fx, fy = dx / d * f, dy / d * f
            disp[a][0] += fx
            disp[a][1] += fy
            disp[b][0] -= fx
            disp[b][1] -= fy

            # Directional bias: dst should be at src + L*dir (and vice versa
            # for dst→src direction if recorded).
            for s, t_id, sign in [(a, b, 1.0), (b, a, -1.0)]:
                v = edge_dirs.get((s, t_id))
                if v is None:
                    continue
                # Target offset of t_id from s.
                target_dx = v[0] * L
                target_dy = v[1] * L
                cur_dx = pos[t_id][0] - pos[s][0]
                cur_dy = pos[t_id][1] - pos[s][1]
                err_x = target_dx - cur_dx
                err_y = target_dy - cur_dy
                bias = 0.15
                disp[t_id][0] += err_x * bias
                disp[t_id][1] += err_y * bias
                disp[s][0]    -= err_x * bias * 0.5
                disp[s][1]    -= err_y * bias * 0.5

        # Apply, capped by current temperature.
        for nid in ids:
            dx, dy = disp[nid]
            d = math.hypot(dx, dy) + 1e-9
            scale = min(d, t) / d
            pos[nid][0] += dx * scale
            pos[nid][1] += dy * scale

        t = max(0.5, t - cooling)

    # Flip Y to screen coords (NWN +Y = north; SVG +Y = down).
    return ({nid: (pos[nid][0], -pos[nid][1]) for nid in pos}, sizes)
