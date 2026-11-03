"""Overland map: a pixel-art world map built from the module's areas.

The look borrows from the overland view of 1990s 4X games: a tile grid of
grassland, forest, hills, mountains and sea, with towns, towers and caves drawn
as little sprites on top. All the art is original and procedural; it is drawn
in the browser by ``wiki_assets/overland.js``. This module only builds the world
*model* (a terrain grid, the area each cell belongs to, the sites and roads)
and embeds it in the page as JSON.

How the world is built:

* **Exterior areas are land.** Each exterior area (ARE ``Flags`` bit 0x1 clear)
  becomes a patch of terrain, sized from its tile dimensions and painted from
  its tileset (rural → grassland with copses, forest → forest, …).
* **Interior areas are sites.** Each interior is drawn as an icon on the first
  exterior a player reaches it from: a crypt sits in the field its door is in.
  Interiors that no exterior leads to (teleport-only dungeons) get a small
  rocky island of their own.
* **Layout** is the same directional force layout as the connection diagram
  (``layout.layout_areas``), run over exteriors only. Two exteriors joined
  through a chain of interiors (a cave passage) count as neighbours.
* **Teleport hubs.** An area whose Comments carry ``@overland-hub`` (a hub
  like a "Well of" teleport chamber) is left off the map; instead every area
  its conversations teleport to is called out with a glowing marker and its
  name.
* **Coastlines.** Area boxes are joined by land corridors along their
  connections, then smoothed with a few cellular-automata passes, so connected
  areas become continents and unconnected ones islands.

Everything is seeded (``--seed`` plus CRC32 of resrefs), so the output is
byte-stable for ``wiki-golden``.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from collections import deque

from nwn_wiki.gff import fld
from nwn_wiki.htmlgen.escape import E, nwn_first_color, nwn_text
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.layout import layout_areas
from nwn_wiki.lookups import tileset_name


#: Layout units per map cell. layout_areas sizes boxes at 1.5 units per NWN
#: tile, so 2.0 makes a cell about 1.3 tiles: a 16×16 area is a 12×12 patch.
CELL = 2.0

#: Roads longer than this many cells are left off: the layout cannot put every
#: pair of linked areas side by side, and a long straight road across half the
#: map reads as noise. The land bridge still joins the two.
ROAD_MAX = 18.0

#: Enclosed water smaller than this many cells is filled in as land.
LAKE_MIN = 45

#: Terrain codes shared with overland.js. Water codes are produced here from
#: distance to land; land codes come from the area's tileset.
OCEAN, SHALLOW, LAKE = "~", "w", "l"
GRASS, FOREST, HILLS, MOUNTAIN = "g", "f", "h", "m"
DESERT, TUNDRA, SWAMP, ROCK = "d", "t", "s", "r"
LAND_CODES = {GRASS, FOREST, HILLS, MOUNTAIN, DESERT, TUNDRA, SWAMP, ROCK}
_KIND_BY_NAME = {"grass": GRASS, "forest": FOREST, "hills": HILLS,
                 "mountain": MOUNTAIN, "mountains": MOUNTAIN,
                 "desert": DESERT, "tundra": TUNDRA, "snow": TUNDRA,
                 "swamp": SWAMP, "rock": ROCK, "cave": ROCK}

# Exterior tileset → (base terrain, [(scattered terrain, share), ...]).
# Shares are approximate: they threshold smooth noise, so scatter forms clumps.
_PROFILE: dict[str, tuple[str, list[tuple[str, float]]]] = {
    "ttr01": (GRASS, [(MOUNTAIN, 0.07), (HILLS, 0.2), (FOREST, 0.2)]),
    "tdr01": (GRASS, [(MOUNTAIN, 0.07), (HILLS, 0.2), (FOREST, 0.2)]),
    "ttf01": (FOREST, [(HILLS, 0.08), (GRASS, 0.18)]),
    "tts01": (TUNDRA, [(MOUNTAIN, 0.1), (FOREST, 0.12), (HILLS, 0.12)]),
    "tti01": (TUNDRA, [(MOUNTAIN, 0.15), (HILLS, 0.1)]),
    "ttd01": (DESERT, [(HILLS, 0.12)]),
    "ttz01": (MOUNTAIN, [(HILLS, 0.35)]),
    "tcn01": (GRASS, [(FOREST, 0.06)]),
    "tcn02": (GRASS, [(FOREST, 0.06)]),
    "twc01": (GRASS, [(HILLS, 0.15)]),
    "ttu01": (ROCK, []),
    "ttm01": (SWAMP, [(FOREST, 0.15)]),
}
_DEFAULT_PROFILE = (GRASS, [(HILLS, 0.12), (FOREST, 0.15)])
# Island for an interior-only component (reached only by teleport): bare rock
# for an underground root, a green wooded isle otherwise.
_ISLAND_ROCK = (ROCK, [])
_ISLAND_GREEN = (GRASS, [(FOREST, 0.25), (HILLS, 0.1)])

# Exterior tilesets whose area is itself a settlement: drawn with a site icon.
_EXTERIOR_SITE = {"tcn01": "town", "tcn02": "town", "twc01": "castle"}

# Interior tileset → site icon (sprite names in overland.js). Checked against
# area names in the homers_lotr corpus: tin01 is houses and inns, tdc01 crypts
# and tombs, tic01 large town buildings, tde01 dungeons, tdm01 caves.
_SITE_ICON = {
    "tin01": "house", "tic01": "house", "tcn01": "town", "tcn02": "town",
    "tdc01": "crypt", "tde01": "dungeon", "tii01": "dungeon",
    "twc01": "castle", "tni02": "castle",
    "tdm01": "cave", "ttu01": "cave", "tib01": "cave", "til01": "cave",
    "tid01": "tower", "tds01": "sewer", "tni01": "mine",
}

# Words in an area's name that say what it is, checked before the tileset.
_NAME_ICON = [
    (re.compile(r"\b(mines?|mineshaft|quarry)\b", re.I), "mine"),
    (re.compile(r"\b(sewers?)\b", re.I), "sewer"),
    (re.compile(r"\b(towers?|spire|observatory)\b", re.I), "tower"),
    (re.compile(r"\b(crypts?|tombs?|barrows?|graves?|mausoleum|catacombs?)\b", re.I), "crypt"),
    (re.compile(r"\b(caves?|caverns?|grotto|den|lair)\b", re.I), "cave"),
    (re.compile(r"\b(dungeons?|prisons?|pit|labyrinth|deeps|oubliette)\b", re.I), "dungeon"),
    (re.compile(r"\b(keep|castle|fortress|citadel|throne|barracks|palace)\b", re.I), "castle"),
    (re.compile(r"\b(inn|tavern|house|home|hut|cottage|shop|smithy|barn|bank)\b", re.I), "house"),
]
SITE_ICONS = ("town", "castle", "house", "tower", "crypt", "mine", "cave",
              "dungeon", "sewer", "portal")

# Exterior tilesets, for areas with no Flags field at all.
_EXTERIOR_TILESETS = {"ttr01", "tdr01", "ttf01", "tts01", "ttd01", "ttz01",
                      "tcn01", "tcn02", "twc01", "ttu01", "ttm01", "tno01"}

_DIRECTIVE_RE = re.compile(r"@overland-(terrain|icon)\s+([A-Za-z]+)")
_HUB_RE = re.compile(r"@overland-hub\b", re.I)


# ---------------------------------------------------------------------------
# Deterministic noise
# ---------------------------------------------------------------------------

def _seed_of(s: str, salt: int = 0) -> int:
    return (zlib.crc32(s.encode("utf-8")) ^ (salt * 0x9E3779B1)) & 0xFFFFFFFF


def _hash01(ix: int, iy: int, seed: int) -> float:
    h = (ix * 374761393 + iy * 668265263 + seed * 2246822519) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((h ^ (h >> 16)) & 0xFFFF) / 65535.0


def _noise(x: float, y: float, seed: int, freq: float) -> float:
    """Smooth value noise in [0, 1]."""
    x *= freq
    y *= freq
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    fx = fx * fx * (3 - 2 * fx)
    fy = fy * fy * (3 - 2 * fy)
    a = _hash01(ix, iy, seed)
    b = _hash01(ix + 1, iy, seed)
    c = _hash01(ix, iy + 1, seed)
    d = _hash01(ix + 1, iy + 1, seed)
    return (a + (b - a) * fx) + ((c + (d - c) * fx) - (a + (b - a) * fx)) * fy


# ---------------------------------------------------------------------------
# Area classification
# ---------------------------------------------------------------------------

def _tileset(a: dict) -> str:
    return (fld(a, "Tileset", "") or "").lower()


def _is_exterior(a: dict) -> bool:
    flags = fld(a, "Flags")
    if flags is None:
        return _tileset(a) in _EXTERIOR_TILESETS
    return not (int(flags) & 0x1)


def _is_underground(a: dict) -> bool:
    return bool(int(fld(a, "Flags", 0) or 0) & 0x2)


def _directives(a: dict) -> dict[str, str]:
    return {k: v.lower() for k, v in _DIRECTIVE_RE.findall(fld(a, "Comments", "") or "")}


def _is_hub(a: dict) -> bool:
    return bool(_HUB_RE.search(fld(a, "Comments", "") or ""))


def _profile(a: dict) -> tuple[str, list[tuple[str, float]]]:
    forced = _KIND_BY_NAME.get(_directives(a).get("terrain", ""))
    if forced:
        return (forced, [])
    return _PROFILE.get(_tileset(a), _DEFAULT_PROFILE)


def _site_icon(a: dict, name: str = "") -> str:
    forced = _directives(a).get("icon", "")
    if forced in SITE_ICONS:
        return forced
    for rx, icon in _NAME_ICON:
        if rx.search(name):
            return icon
    icon = _SITE_ICON.get(_tileset(a))
    if icon:
        return icon
    return "dungeon" if _is_underground(a) else "tower"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def build_overland(db: "Db", seed: int = 1) -> dict | None:
    """Build the overland world model. Returns None when there is nothing to
    draw. The result is JSON-ready apart from ``areas``/``sites`` hrefs, which
    are page-relative and filled in by :func:`render_overland_html`."""
    visible = sorted(r for r in db.areas if r not in db.hidden_areas)
    # Teleport hubs come off the map; what they teleport to is called out.
    hubs = [r for r in visible if _is_hub(db.areas[r])]
    visible = [r for r in visible if r not in set(hubs)]
    if not visible:
        return None
    vis_set = set(visible)
    hub_dest: dict[str, str] = {}   # destination → first hub that leads there
    for tr in db.conv_transitions:
        if tr["src"] in hubs and tr["dst_area"] in vis_set:
            hub_dest.setdefault(tr["dst_area"], tr["src"])

    # Undirected adjacency over every walkable/scripted transition, plus a
    # record of where (in the source area) each transition sits.
    adj: dict[str, set[str]] = {r: set() for r in visible}
    exit_point: dict[tuple[str, str], tuple[float, float]] = {}

    def link(a: str, b: str) -> None:
        if a in vis_set and b in vis_set and a != b:
            adj[a].add(b)
            adj[b].add(a)

    for tr in db.transitions:
        a, b = tr["src_area"], tr["dst_area"]
        link(a, b)
        if a in vis_set and b in vis_set and (a, b) not in exit_point:
            x, y = tr.get("src_x"), tr.get("src_y")
            if x is not None and y is not None:
                exit_point[(a, b)] = (float(x), float(y))
    for tr in db.script_transitions:
        link(tr["src_area"], tr["dst_area"])

    exteriors = [r for r in visible if _is_exterior(db.areas[r])]

    # Multi-source BFS: each interior's parent is the nearest exterior, `via`
    # is the area it was reached from (its site clusters next to via's site).
    parent: dict[str, str] = {r: r for r in exteriors}
    via: dict[str, str] = {}
    q = deque(exteriors)
    while q:
        u = q.popleft()
        for v in sorted(adj[u]):
            if v not in parent:
                parent[v] = parent[u]
                via[v] = u
                q.append(v)

    # Interior-only components: the best-connected interior becomes the root
    # of a rocky island; the rest of the component are sites on it.
    island_roots: list[str] = []
    for r in visible:
        if r in parent:
            continue
        comp, cq = [r], deque([r])
        seen = {r}
        while cq:
            u = cq.popleft()
            for v in sorted(adj[u]):
                if v not in seen:
                    seen.add(v)
                    comp.append(v)
                    cq.append(v)
        root = max(sorted(comp), key=lambda n: len(adj[n]))
        island_roots.append(root)
        parent[root] = root
        rq = deque([root])
        while rq:
            u = rq.popleft()
            for v in sorted(adj[u]):
                if v not in parent:
                    parent[v] = root
                    via[v] = u
                    rq.append(v)

    nodes = sorted(exteriors + island_roots)
    edge_set: set[tuple[str, str]] = set()
    for u in visible:
        for v in adj[u]:
            pu, pv = parent[u], parent[v]
            if pu != pv:
                edge_set.add((min(pu, pv), max(pu, pv)))
    edges = sorted(edge_set)

    positions, sizes = layout_areas(nodes, edges, db=db, seed=seed)

    # --- Grid frame -------------------------------------------------------
    margin = 5
    min_x = min(positions[n][0] - sizes[n][0] / 2 for n in nodes)
    min_y = min(positions[n][1] - sizes[n][1] / 2 for n in nodes)
    max_x = max(positions[n][0] + sizes[n][0] / 2 for n in nodes)
    max_y = max(positions[n][1] + sizes[n][1] / 2 for n in nodes)
    W = int(math.ceil((max_x - min_x) / CELL)) + 2 * margin
    H = int(math.ceil((max_y - min_y) / CELL)) + 2 * margin

    # Box of each node in (fractional) cell coordinates: x0, y0, x1, y1.
    boxes: dict[str, tuple[float, float, float, float]] = {}
    for n in nodes:
        cx, cy = positions[n]
        w, h = sizes[n]
        x0 = (cx - w / 2 - min_x) / CELL + margin
        y0 = (cy - h / 2 - min_y) / CELL + margin
        boxes[n] = (x0, y0, x0 + w / CELL, y0 + h / CELL)

    def box_dist(n: str, x: float, y: float) -> float:
        x0, y0, x1, y1 = boxes[n]
        dx = max(x0 - x, 0.0, x - x1)
        dy = max(y0 - y, 0.0, y - y1)
        return math.hypot(dx, dy)

    # --- Land mask and ownership -----------------------------------------
    land = [[False] * W for _ in range(H)]
    core = [[False] * W for _ in range(H)]   # never eroded by smoothing
    owner = [[-1] * W for _ in range(H)]
    best = [[1e9] * W for _ in range(H)]
    node_idx = {n: i for i, n in enumerate(nodes)}
    reach = 3.0
    for n in nodes:
        x0, y0, x1, y1 = boxes[n]
        s = _seed_of(n, seed)
        for cy in range(max(0, int(y0 - reach)), min(H, int(y1 + reach) + 1)):
            for cx in range(max(0, int(x0 - reach)), min(W, int(x1 + reach) + 1)):
                px, py = cx + 0.5, cy + 0.5
                d = box_dist(n, px, py)
                # Wobbly coast: grow/shrink the box edge by up to ~1.5 cells.
                wob = _noise(px, py, s, 0.35) * 2.2 - 0.9
                if d <= 0.0 and min(px - x0, x1 - px, py - y0, y1 - py) > 1.0:
                    core[cy][cx] = True
                if d < wob or d <= 0.0 and wob > -0.6:
                    land[cy][cx] = True
                # Ragged borders between neighbouring areas: signed distance
                # (negative inside, capped) so a neighbour can push a few
                # cells into this box where the noise favours it.
                sd = d if d > 0.0 else -min(px - x0, x1 - px, py - y0, y1 - py, 3.0)
                score = sd - _noise(px, py, s ^ 0x5bd1e995, 0.3) * 3.0
                if score < best[cy][cx]:
                    best[cy][cx] = score
                    owner[cy][cx] = node_idx[n]

    def centre(n: str) -> tuple[float, float]:
        x0, y0, x1, y1 = boxes[n]
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    def toward_edge(n: str, tx: float, ty: float) -> tuple[float, float]:
        cx, cy = centre(n)
        x0, y0, x1, y1 = boxes[n]
        dx, dy = tx - cx, ty - cy
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return cx, cy
        sx = (x1 - x0) / 2 / abs(dx) if dx else float("inf")
        sy = (y1 - y0) / 2 / abs(dy) if dy else float("inf")
        k = min(sx, sy) * 0.9
        return cx + dx * k, cy + dy * k

    # Land bridges along connections, so neighbours share a continent.
    for a, b in edges:
        (ax, ay), (bx, by) = centre(a), centre(b)
        ex_a = toward_edge(a, bx, by)
        ex_b = toward_edge(b, ax, ay)
        steps = int(math.hypot(ex_b[0] - ex_a[0], ex_b[1] - ex_a[1]) * 2) + 1
        for i in range(steps + 1):
            t = i / steps
            px = ex_a[0] + (ex_b[0] - ex_a[0]) * t
            py = ex_a[1] + (ex_b[1] - ex_a[1]) * t
            rad = 1.3 + _noise(px, py, seed, 0.3) * 0.9
            for cy in range(int(py - rad), int(py + rad) + 1):
                for cx in range(int(px - rad), int(px + rad) + 1):
                    if 0 <= cx < W and 0 <= cy < H and \
                            math.hypot(cx + 0.5 - px, cy + 0.5 - py) <= rad:
                        land[cy][cx] = True

    # Cellular-automata smoothing (majority rule), keeping each area's core.
    for _ in range(3):
        nxt = [row[:] for row in land]
        for cy in range(H):
            for cx in range(W):
                if core[cy][cx]:
                    continue
                n8 = 0
                for dy in (-1, 0, 1):
                    yy = cy + dy
                    if 0 <= yy < H:
                        row = land[yy]
                        for dx in (-1, 0, 1):
                            xx = cx + dx
                            if (dx or dy) and 0 <= xx < W and row[xx]:
                                n8 += 1
                if n8 >= 5:
                    nxt[cy][cx] = True
                elif n8 <= 2:
                    nxt[cy][cx] = False
        land = nxt
    for cy in (0, H - 1):
        for cx in range(W):
            land[cy][cx] = False
    for cy in range(H):
        land[cy][0] = land[cy][W - 1] = False

    # Fill enclosed water smaller than a real lake (the gaps the layout leaves
    # between neighbouring boxes); drop specks of land with no core cell.
    def components(want: bool):
        seen = [[False] * W for _ in range(H)]
        for sy in range(H):
            for sx in range(W):
                if seen[sy][sx] or land[sy][sx] != want:
                    continue
                comp = []
                cq = deque([(sx, sy)])
                seen[sy][sx] = True
                while cq:
                    x, y = cq.popleft()
                    comp.append((x, y))
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if 0 <= nx < W and 0 <= ny < H and not seen[ny][nx] \
                                and land[ny][nx] == want:
                            seen[ny][nx] = True
                            cq.append((nx, ny))
                yield comp

    lake_cells: set[tuple[int, int]] = set()
    for comp in list(components(False)):
        touches_edge = any(x in (0, W - 1) or y in (0, H - 1) for x, y in comp)
        if touches_edge:
            continue
        if len(comp) < LAKE_MIN:
            for x, y in comp:
                land[y][x] = True
        else:
            lake_cells.update(comp)
    for comp in list(components(True)):
        if len(comp) < 4 and not any(core[y][x] for x, y in comp):
            for x, y in comp:
                land[y][x] = False

    # Land cells beyond every box's reach (bridges, smoothing growth) take the
    # owner of the nearest owned land cell.
    oq = deque()
    for cy in range(H):
        for cx in range(W):
            if not land[cy][cx]:
                owner[cy][cx] = -1
            elif owner[cy][cx] >= 0:
                oq.append((cx, cy))
    while oq:
        x, y = oq.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < W and 0 <= ny < H and land[ny][nx] and owner[ny][nx] < 0:
                owner[ny][nx] = owner[y][x]
                oq.append((nx, ny))

    # --- Terrain ------------------------------------------------------------
    # Water depth: shallow within 2 cells of land.
    dist = [[-1] * W for _ in range(H)]
    dq = deque()
    for cy in range(H):
        for cx in range(W):
            if land[cy][cx]:
                dist[cy][cx] = 0
                dq.append((cx, cy))
    while dq:
        x, y = dq.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < W and 0 <= ny < H and dist[ny][nx] < 0:
                dist[ny][nx] = dist[y][x] + 1
                dq.append((nx, ny))

    def island_profile(n: str):
        return _ISLAND_ROCK if _is_underground(db.areas[n]) else _ISLAND_GREEN

    profiles = {n: (island_profile(n) if n in island_roots else _profile(db.areas[n]))
                for n in nodes}
    terrain = [[OCEAN] * W for _ in range(H)]
    for cy in range(H):
        for cx in range(W):
            if not land[cy][cx]:
                if (cx, cy) in lake_cells:
                    terrain[cy][cx] = LAKE
                elif dist[cy][cx] <= 2:
                    terrain[cy][cx] = SHALLOW
                continue
            n = nodes[owner[cy][cx]]
            base, mix = profiles[n]
            kind = base
            s = _seed_of(n, seed)
            for i, (alt, share) in enumerate(mix):
                if _noise(cx + 0.5, cy + 0.5, s + 101 * (i + 1), 0.28) > 1.0 - share * 1.6:
                    kind = alt
                    break
            terrain[cy][cx] = kind

    # --- Sites ------------------------------------------------------------
    occupied: set[tuple[int, int]] = set()

    def free_cell(x: float, y: float, own: int) -> tuple[int, int]:
        """Nearest land cell to (x, y) owned by `own` with no site on or
        beside it; relaxes to any free land cell, then to (x, y) itself."""
        ix, iy = int(x), int(y)
        for strict in (2, 1, 0):
            for r in range(0, 9):
                ring = []
                for dy in range(-r, r + 1):
                    for dx in range(-r, r + 1):
                        if max(abs(dx), abs(dy)) != r:
                            continue
                        cx, cy = ix + dx, iy + dy
                        if not (0 <= cx < W and 0 <= cy < H) or not land[cy][cx]:
                            continue
                        if strict and owner[cy][cx] != own:
                            continue
                        if (cx, cy) in occupied:
                            continue
                        if strict == 2 and any((cx + ax, cy + ay) in occupied
                                               for ax in (-1, 0, 1) for ay in (-1, 0, 1)):
                            continue
                        ring.append((math.hypot(dx, dy), cy, cx))
                if ring:
                    _, cy, cx = min(ring)
                    return cx, cy
        return max(0, min(W - 1, ix)), max(0, min(H - 1, iy))

    def exit_cell(p: str, child: str) -> tuple[float, float] | None:
        """Where, inside p's patch, the transition toward `child` sits."""
        pt = exit_point.get((p, child))
        a = db.areas.get(p)
        if pt is None or a is None or p not in boxes:
            return None
        w = float(fld(a, "Width", 8) or 8) * 10.0
        h = float(fld(a, "Height", 8) or 8) * 10.0
        x0, y0, x1, y1 = boxes[p]
        fx = min(max(pt[0] / w, 0.05), 0.95)
        fy = min(max(1.0 - pt[1] / h, 0.05), 0.95)   # NWN +Y is north
        return x0 + (x1 - x0) * fx, y0 + (y1 - y0) * fy

    site_list: list[dict] = []
    site_at: dict[str, tuple[int, int]] = {}

    def add_site(area: str, icon: str, x: float, y: float, own: int) -> None:
        cx, cy = free_cell(x, y, own)
        occupied.add((cx, cy))
        site_at[area] = (cx, cy)
        site_list.append({"area": area, "icon": icon, "x": cx, "y": cy})

    # Settlements first (they claim the middle of their own patch), then
    # island roots, then interiors in BFS order so chains cluster outward.
    for n in nodes:
        a = db.areas[n]
        if n in island_roots:
            add_site(n, _site_icon(a, nwn_text(db.area_name(n))), *centre(n), node_idx[n])
        elif _tileset(a) in _EXTERIOR_SITE or "icon" in _directives(a):
            icon = _directives(a).get("icon") or _EXTERIOR_SITE[_tileset(a)]
            if icon in SITE_ICONS:
                add_site(n, icon, *centre(n), node_idx[n])
    order = sorted((r for r in visible if r not in node_idx),
                   key=lambda r: (_bfs_depth(r, via), r))
    for r in order:
        p = parent[r]
        v = via[r]
        own = node_idx[p]
        if v in node_idx:
            pos = exit_cell(v, r) or centre(v)
        elif v in site_at:
            pos = (site_at[v][0] + 0.5, site_at[v][1] + 0.5)
        else:
            pos = centre(p)
        add_site(r, _site_icon(db.areas[r], nwn_text(db.area_name(r))), pos[0], pos[1], own)

    # Hub destinations: mark the area's own icon, or plant a gateway stone in
    # the middle of an exterior that has none.
    callouts = []
    for d in sorted(hub_dest):
        if d not in site_at:
            c = centre(d) if d in boxes else None
            if c is None:
                continue
            add_site(d, "portal", c[0], c[1], node_idx[d])
        x, y = site_at[d]
        callouts.append({"area": d, "x": x, "y": y, "hub": hub_dest[d]})

    # --- Roads --------------------------------------------------------------
    roads = []
    for a, b in edges:
        (ax, ay), (bx, by) = centre(a), centre(b)
        pa = exit_cell(a, b) or toward_edge(a, bx, by)
        pb = exit_cell(b, a) or toward_edge(b, ax, ay)
        if math.hypot(pb[0] - pa[0], pb[1] - pa[1]) > ROAD_MAX:
            continue
        # Through an interior (cave passage, gatehouse) rather than direct.
        direct = b in adj[a]
        roads.append([round(pa[0], 2), round(pa[1], 2),
                      round(pb[0], 2), round(pb[1], 2), 0 if direct else 1])

    # --- Start view: centre of the biggest connected group -------------------
    comp_of: dict[str, int] = {}
    nadj: dict[str, set[str]] = {n: set() for n in nodes}
    for a, b in edges:
        nadj[a].add(b)
        nadj[b].add(a)
    groups: list[list[str]] = []
    for n in nodes:
        if n in comp_of:
            continue
        g, gq = [], deque([n])
        comp_of[n] = len(groups)
        while gq:
            u = gq.popleft()
            g.append(u)
            for v in sorted(nadj[u]):
                if v not in comp_of:
                    comp_of[v] = len(groups)
                    gq.append(v)
        groups.append(g)
    main = max(groups, key=len)
    sx = sum(centre(n)[0] for n in main) / len(main)
    sy = sum(centre(n)[1] for n in main) / len(main)

    # Run-length encode the owner grid row-major: [owner, run, owner, run, …].
    rle: list[int] = []
    prev, run = None, 0
    for cy in range(H):
        for cx in range(W):
            o = owner[cy][cx]
            if o == prev:
                run += 1
            else:
                if prev is not None:
                    rle += [prev, run]
                prev, run = o, 1
    rle += [prev, run]

    label_at = {n: [round(c, 1) for c in centre(n)] for n in nodes}
    for n in nodes:
        if n in site_at:
            sx_, sy_ = site_at[n]
            label_at[n] = [sx_ + 0.5, sy_ + 1.7]

    return {
        "w": W, "h": H,
        "start": [round(sx, 1), round(sy, 1)],
        "terrain": ["".join(row) for row in terrain],
        "owner": rle,
        "nodes": nodes,
        "label_at": label_at,
        "sites": site_list,
        "callouts": callouts,
        "hubs": hubs,
        "roads": roads,
        "seed": seed,
    }


def _bfs_depth(r: str, via: dict[str, str]) -> int:
    d = 0
    while r in via:
        r = via[r]
        d += 1
    return d


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _area_entry(db: "Db", resref: str, ctx: PageCtx) -> list:
    raw = db.area_name(resref)
    return [resref, nwn_text(raw), nwn_first_color(raw) or "",
            ctx.url(f"areas/{resref}.html"),
            tileset_name(_tileset(db.areas.get(resref, {})))]


def render_overland_html(db: "Db", model: dict | None, ctx: PageCtx,
                         diagram_below: bool = False) -> str:
    """The overland map block (toolbar + canvas viewport + data + script) for
    a page at ``ctx``. Empty model → a short placeholder. ``diagram_below``
    says whether the connection diagram follows (for the no-JS note)."""
    if not model:
        return "<p>(no areas)</p>"
    # Areas table: every node and every site area, indexed by position.
    names = list(model["nodes"])
    seen = set(names)
    for s in model["sites"]:
        if s["area"] not in seen:
            seen.add(s["area"])
            names.append(s["area"])
    for h in model["hubs"]:
        if h not in seen:
            seen.add(h)
            names.append(h)
    idx = {n: i for i, n in enumerate(names)}
    called = {c["area"] for c in model["callouts"]}
    data = {
        "v": 1,
        "w": model["w"], "h": model["h"],
        "seed": model["seed"],
        "start": model["start"],
        "terrain": model["terrain"],
        "owner": model["owner"],
        "areas": [_area_entry(db, n, ctx) for n in names],
        "labels": [[idx[n], *model["label_at"][n]] for n in model["nodes"]
                   if n not in called],
        "sites": [[s["x"], s["y"], s["icon"], idx[s["area"]]] for s in model["sites"]],
        "callouts": [[c["x"], c["y"], idx[c["area"]], idx[c["hub"]]]
                     for c in model["callouts"]],
        "roads": model["roads"],
    }
    hub_hint = ""
    if model["hubs"]:
        hub_names = " / ".join(E(nwn_text(db.area_name(h)).strip()) for h in model["hubs"])
        hub_hint = (f' <span class="overland-callout-key">✦</span> marks a place '
                    f'you can teleport to from {hub_names}.')
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    blob = blob.replace("</", "<\\/")
    return (
        '<div class="overland" id="overland">'
        '<div class="map-toolbar overland-toolbar">'
        '<span class="map-zoom-controls">'
        '<button type="button" class="map-zoom-btn" data-ov="out" title="Zoom out">−</button>'
        '<button type="button" class="map-zoom-btn" data-ov="reset" title="Reset zoom">1:1</button>'
        '<button type="button" class="map-zoom-btn" data-ov="in" title="Zoom in">+</button>'
        '</span>'
        '<button type="button" class="map-zoom-btn" data-ov="find" '
        'title="Search for a place" aria-pressed="false">Find</button>'
        '<button type="button" class="map-zoom-btn" data-ov="labels" '
        'title="Show or hide area names" aria-pressed="true">Names</button>'
        '<button type="button" class="map-zoom-btn" data-ov="full" '
        'title="Fill the window">⤢</button>'
        '</div>'
        '<div class="overland-wrap">'
        '<canvas class="overland-canvas" aria-label="Overland map of the module\'s areas">'
        '</canvas>'
        '<canvas class="overland-minimap" title="Click to jump"></canvas>'
        # Place finder, shown by overland.js (a dead search box without JS
        # would only mislead).
        '<aside class="overland-side" hidden>'
        '<div class="overland-side-head">'
        '<input type="search" class="overland-search" placeholder="Find a place…" '
        'aria-label="Find a place on the map" autocomplete="off" spellcheck="false">'
        '<button type="button" class="overland-side-close" title="Hide the list" '
        'aria-label="Hide the place list">×</button>'
        '</div>'
        '<div class="overland-count" aria-live="polite"></div>'
        '<ul class="overland-results" role="listbox" aria-label="Places"></ul>'
        '</aside>'
        '<div class="overland-tip" hidden></div>'
        '<noscript><p class="overland-noscript">The overland map needs JavaScript'
        + ('; the connection diagram below works without it.'
           if diagram_below else '.') +
        '</p></noscript>'
        '</div>'
        '<p class="map-hint">Drag to pan, scroll to zoom, click an area or '
        'site to open its page; <b>Find</b> searches for a place and flies '
        'the map to it. Icons mark interiors (towns, crypts, mines, '
        'caves…) on the land you enter them from; dotted roads pass through '
        'an interior.' + hub_hint + '</p>'
        f'<script type="application/json" class="overland-data">{blob}</script>'
        f'<script src="{E(ctx.url("assets/overland.js"))}" defer></script>'
        '</div>'
    )
