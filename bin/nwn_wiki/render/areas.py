"""Area rendering for the wiki.

The area index, the per-area detail page's sibling container pages, and the
area→area transition graph the "how do I get there" path sections walk.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path

from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.chrome import page, write
from nwn_wiki.htmlgen.escape import E, nwn_html, nwn_text
from nwn_wiki.htmlgen.links import link
from nwn_wiki.items import item_gp_value
from nwn_wiki.lookups import baseitem_label, placeable_name, tileset_name


def build_area_graph(db: "Db") -> dict[str, list[tuple[str, str, str, bool, bool]]]:
    """Directed area→area edge graph from door/trigger transitions and conv teleports.

    Uses db.conv_transitions (not db.dialog_teleports directly) so that any
    --exclude-conv-option labels (e.g. "[Admin Options]") are already stripped
    out — the same exclusion that applies to the area map SVG edges.

    Each edge is (dst, kind, label, is_fallback, is_dup_tag) where:
      is_fallback=True  — resolved via WP_-prefix-stripping fallback; may not work in-game
      is_dup_tag=True   — LinkedTo tag matches multiple objects; actual destination is ambiguous
    """
    graph: dict[str, list[tuple[str, str, str, bool, bool]]] = {rr: [] for rr in db.areas}
    seen: set[tuple[str, str, str, str]] = set()
    for tr in db.transitions:
        src, dst = tr["src_area"], tr["dst_area"]
        is_dup = tr.get("is_dup_tag", False)
        if dst and src in graph and dst in graph and src != dst:
            key = (src, dst, tr["kind"], tr["label"])
            if key not in seen:
                seen.add(key)
                graph[src].append((dst, tr["kind"], tr["label"], False, is_dup))
        # Add edges to all alt destinations for duplicate-tag transitions.
        if is_dup:
            for alt_dst in tr.get("dst_area_alts", []):
                if alt_dst and alt_dst in graph and src != alt_dst:
                    key = (src, alt_dst, tr["kind"], tr["label"])
                    if key not in seen:
                        seen.add(key)
                        graph[src].append((alt_dst, tr["kind"], tr["label"], False, True))
    for tr in db.conv_transitions:
        src, dst = tr["src"], tr["dst_area"]
        if src not in graph or dst not in graph or src == dst:
            continue
        conv_rr = tr.get("conv_resref", "")
        label = db.dialog_label(conv_rr) if conv_rr else conv_rr
        key = (src, dst, "talk", label)
        if key not in seen:
            seen.add(key)
            graph[src].append((dst, "talk", label, False, False))
    for tr in db.script_transitions:
        src, dst = tr["src_area"], tr["dst_area"]
        if not dst or src not in graph or dst not in graph or src == dst:
            continue
        key = (src, dst, tr["kind"], tr["label"])
        if key not in seen:
            seen.add(key)
            graph[src].append((dst, tr["kind"], tr["label"], tr.get("fallback", False), False))
    return graph


def bfs_shortest_path(
    graph: dict[str, list[tuple[str, str, str, bool, bool]]], src: str, dst: str
) -> list[tuple[str, str, str, str, bool, bool]] | None:
    """BFS shortest path; returns list of (from, to, kind, label, is_fallback, is_dup_tag) or None."""
    if src == dst:
        return []
    came_from: dict[str, str | None] = {src: None}
    edge_in: dict[str, tuple[str, str, bool, bool]] = {}
    q: deque[str] = deque([src])
    while q:
        cur = q.popleft()
        for dest, kind, label, is_fallback, is_dup_tag in graph.get(cur, []):
            if dest in came_from:
                continue
            came_from[dest] = cur
            edge_in[dest] = (kind, label, is_fallback, is_dup_tag)
            if dest == dst:
                steps: list[tuple[str, str, str, str, bool, bool]] = []
                node = dst
                while came_from[node] is not None:
                    prev = came_from[node]
                    k, l, fb, dup = edge_in[node]
                    steps.append((prev, node, k, l, fb, dup))
                    node = prev
                return list(reversed(steps))
            q.append(dest)
    return None


def render_areas_index(db: Db, out: Path, *,
                       area_paths: "dict[str, list | None] | None" = None,
                       path_from_resref: str = "",
                       path_from_name: str = "") -> None:
    # Classify areas by reachability via fallback transitions.
    # An area is "fallback-only reachable" if every incoming transition that
    # could lead to it from another area uses a WP_-prefix-stripped fallback tag
    # (i.e., the script uses "foo" but the waypoint is tagged "WP_foo"), with no
    # confirmed non-fallback route (door, trigger, conversation, or direct-tag script).
    has_real_incoming: set[str] = set()
    has_fallback_incoming: set[str] = set()
    # Areas that are a primary or alternative destination of a dup-tag transition.
    has_dup_tag_incoming: set[str] = set()
    for tr in db.transitions:
        if tr["dst_area"]:
            has_real_incoming.add(tr["dst_area"])
        if tr.get("is_dup_tag"):
            if tr["dst_area"] and tr["dst_area"] in db.areas:
                has_dup_tag_incoming.add(tr["dst_area"])
            for alt in tr.get("dst_area_alts", []):
                if alt in db.areas:
                    has_dup_tag_incoming.add(alt)
    for tr in db.conv_transitions:
        dst = tr.get("dst_area", "")
        if dst and dst in db.areas:
            has_real_incoming.add(dst)
    for tr in db.script_transitions:
        dst = tr.get("dst_area", "")
        if not dst or dst not in db.areas:
            continue
        if tr.get("fallback"):
            has_fallback_incoming.add(dst)
        else:
            has_real_incoming.add(dst)
    has_any_fallback = has_fallback_incoming  # areas with at least one fallback route

    def _dominant_faction_cell(resref: str) -> str:
        faction_votes: dict[int, int] = {}
        for c in db.area_npcs.get(resref, []):
            try:
                fid = int(fld(c, "FactionID"))
            except (TypeError, ValueError):
                continue
            faction_votes[fid] = faction_votes.get(fid, 0) + 1
        for e in db.area_encounters.get(resref, []):
            rr = fld(e, "TemplateResRef", "")
            blueprint = db.encounters.get(rr, {})
            # Instance (.git) CreatureList is what spawns at runtime; blueprint is fallback.
            spawns = list_items(e.get("CreatureList")) or list_items(blueprint.get("CreatureList"))
            for s in spawns:
                srr = fld(s, "ResRef", "")
                bp = db.creatures.get(srr, {})
                try:
                    fid = int(fld(bp, "FactionID"))
                except (TypeError, ValueError):
                    continue
                faction_votes[fid] = faction_votes.get(fid, 0) + 1
        total = sum(faction_votes.values())
        if not total:
            return "<td>None</td>"
        max_count = max(faction_votes.values())
        top_fids = [fid for fid, cnt in faction_votes.items() if cnt == max_count]
        if len(top_fids) > 1:
            return "<td>Tie</td>"
        dominant_fid = top_fids[0]
        pct = round(100 * max_count / total)
        raw_name = db.faction_name(dominant_fid)
        slug = re.sub(r"[^a-z0-9]+", "-", raw_name.lower()).strip("-")
        anchor = f"faction-{dominant_fid}-{slug}" if slug else f"faction-{dominant_fid}"
        href = f"../factions.html#{anchor}"
        return f'<td><a href="{E(href)}">{nwn_html(raw_name)}</a> {pct}%</td>'

    def _area_row(resref: str) -> str:
        a = db.areas[resref]
        npc_count = len(db.area_npcs.get(resref, []))
        enc_count = len(db.area_encounters.get(resref, []))
        store_count = len(db.area_stores.get(resref, []))
        cont_count = len(db.area_containers.get(resref, []))
        return (
            f'<tr>'
            f'<td>{link(f"{resref}.html", db.area_name(resref))}</td>'
            f'<td>{E(tileset_name(fld(a, "Tileset", "")))}</td>'
            f'<td>{E(fld(a, "Width", ""))}×{E(fld(a, "Height", ""))}</td>'
            f'<td>{npc_count}</td>'
            f'<td>{enc_count}</td>'
            + _dominant_faction_cell(resref) +
            f'<td>{store_count}</td>'
            f'<td>{cont_count}</td>'
            f'</tr>'
        )

    sorted_resrefs = sorted(
        (r for r in db.areas if r not in db.hidden_areas),
        key=lambda r: nwn_text(db.area_name(r)).lower(),
    )

    # When --path-from is active, split into reachable vs. no-known-path.
    # The source area itself is always reachable; area_paths[rr] is None
    # when BFS found no route, or a list (possibly empty) when a route exists.
    use_path_split = bool(area_paths is not None and path_from_resref)
    if use_path_split:
        reachable_resrefs = [
            r for r in sorted_resrefs
            if r == path_from_resref or area_paths.get(r) is not None
        ]
        no_path_resrefs = [
            r for r in sorted_resrefs
            if r != path_from_resref and area_paths.get(r) is None
        ]
    else:
        reachable_resrefs = sorted_resrefs
        no_path_resrefs = []

    # Prioritise fallback > dup-tag > normal so each area appears in exactly one bucket.
    normal_resrefs = [r for r in reachable_resrefs
                      if r not in has_any_fallback and r not in has_dup_tag_incoming]
    dup_tag_resrefs = [r for r in reachable_resrefs
                       if r not in has_any_fallback and r in has_dup_tag_incoming]
    suspect_resrefs = [r for r in reachable_resrefs if r in has_any_fallback]

    table_head = (
        '<table class="data"><thead><tr>'
        "<th>Name</th><th>Tileset</th><th>Size</th>"
        "<th>NPCs</th><th>Encounters</th><th>Dominant Faction</th><th>Stores</th><th>Containers</th>"
        "</tr></thead><tbody>"
    )

    body_parts = [
        "<h1>Areas</h1>",
        f"<p>{len(db.areas)} areas total.</p>",
        table_head,
        "\n".join(_area_row(r) for r in normal_resrefs),
        "</tbody></table>",
    ]

    if dup_tag_resrefs:
        body_parts += [
            '<h2 id="dup-dest-tags">Areas with duplicate destination tags</h2>',
            '<p class="warn-dup-tag-note">&#9888; The areas below are destinations of'
            ' transitions whose <code>LinkedTo</code> tag matches multiple objects'
            ' (waypoints, doors, or triggers) across the module. The game engine'
            ' resolves to whichever it finds first, so these routes may send the'
            ' player to an unexpected area. See each area\'s shortest-path section'
            ' for affected steps.</p>',
            table_head,
            "\n".join(_area_row(r) for r in dup_tag_resrefs),
            "</tbody></table>",
        ]

    if suspect_resrefs:
        body_parts += [
            '<h2 id="possibly-inaccessible">Areas with possibly broken routes</h2>',
            '<p class="warn-fallback-note">&#9888; The areas below have at least one'
            ' incoming transition resolved via a waypoint-tag fallback — the script'
            ' references a short tag (e.g. <code>foo</code>) while the actual waypoint'
            ' is tagged <code>WP_foo</code>. That route may not work in-game and could'
            ' indicate a typo in a waypoint tag or script. Other routes into these'
            ' areas may still work normally.</p>',
            table_head,
            "\n".join(_area_row(r) for r in suspect_resrefs),
            "</tbody></table>",
        ]

    if no_path_resrefs:
        src_label = nwn_html(path_from_name) if path_from_name else E(path_from_resref)
        body_parts += [
            f'<h2 id="no-path">No known path from {src_label}</h2>',
            f'<p class="warn-fallback-note">&#9888; The areas below have no known route'
            f' from {src_label} via door, trigger, or conversation teleport. They may be'
            f' accessible by other means (login spawn, admin, DM warp), or their'
            f' transitions may not yet be indexed.</p>',
            table_head,
            "\n".join(_area_row(r) for r in no_path_resrefs),
            "</tbody></table>",
        ]

    # Unresolved transitions: door/trigger LinkedTo targets that don't match any
    # waypoint, door, or trigger tag in the module.
    unresolved: dict[str, list[dict]] = {}
    for tr in db.transitions:
        if not tr["dst_area"] and tr.get("dst_tag"):
            unresolved.setdefault(tr["dst_tag"], []).append(tr)
    if unresolved:
        rows = []
        for tag in sorted(unresolved, key=str.lower):
            refs = unresolved[tag]
            for tr in refs:
                src = tr["src_area"]
                rows.append(
                    f"<tr>"
                    f"<td><code>{E(tag)}</code></td>"
                    f"<td>{link(f'{src}.html', db.area_name(src))}</td>"
                    f"<td>{E(tr['kind'])}</td>"
                    f"<td>{E(tr['label'])}</td>"
                    f"</tr>"
                )
        body_parts += [
            '<h2 id="unresolved-transitions">Unresolved transition targets</h2>',
            '<p class="warn-fallback-note">&#9888; The tags below are referenced by'
            ' door or trigger <code>LinkedTo</code> fields but do not exist as a'
            ' waypoint, door, or trigger tag in any area. Each likely indicates a'
            ' missing waypoint, a typo, or unfinished content.</p>',
            '<table class="data"><thead><tr>'
            '<th>Target tag</th><th>From area</th><th>Kind</th><th>Label</th>'
            '</tr></thead><tbody>',
            "\n".join(rows),
            "</tbody></table>",
        ]

    body = "".join(body_parts)
    write(out / "areas" / "index.html", page("Areas", body, root_rel=".."))


def render_container_page(db: Db, area_resref: str, c: dict, out: Path) -> None:
    """One page per container (a placeable with a non-empty inventory).
    Surfaces the in-area position, lock state, and items it holds — useful
    for tracing the player's loot path through an area without having to
    open the placeable in the toolset."""
    p = c["p"]
    idx = c["idx"]
    tag = fld(p, "Tag", "")
    rr = fld(p, "TemplateResRef", "")
    pname = loc(p.get("LocName")) or tag or rr or "(unnamed container)"
    items = list_items(p.get("ItemList"))

    x = fld(p, "X", 0.0) or 0.0
    y = fld(p, "Y", 0.0) or 0.0
    z = fld(p, "Z", 0.0) or 0.0
    bearing = fld(p, "Bearing", "")
    appearance = fld(p, "Appearance")
    hp = fld(p, "HP", "")
    cur_hp = fld(p, "CurrentHP", "")
    hardness = fld(p, "Hardness", "")
    plot = "yes" if int(fld(p, "Plot", 0) or 0) else "no"
    static = "yes" if int(fld(p, "Static", 0) or 0) else "no"
    useable = "yes" if int(fld(p, "Useable", 0) or 0) else "no"
    lockable = "yes" if int(fld(p, "Lockable", 0) or 0) else "no"
    locked = "yes" if int(fld(p, "Locked", 0) or 0) else "no"
    open_dc = fld(p, "OpenLockDC", "")
    close_dc = fld(p, "CloseLockDC", "")
    key_required = "yes" if int(fld(p, "KeyRequired", 0) or 0) else "no"
    key_name = fld(p, "KeyName", "")
    auto_key = "yes" if int(fld(p, "AutoRemoveKey", 0) or 0) else "no"
    trap_flag = "yes" if int(fld(p, "TrapFlag", 0) or 0) else "no"
    trap_dc = fld(p, "TrapDetectDC", "")
    disarm_dc = fld(p, "DisarmDC", "")
    conv = fld(p, "Conversation", "")
    faction = fld(p, "Faction", "")

    sections = [
        f"<h1>{nwn_html(pname)}</h1>",
        '<dl class="meta">',
        f"<dt>Area</dt><dd>{link(f'../areas/{area_resref}.html', db.area_name(area_resref))}</dd>",
        f"<dt>Tag</dt><dd><code>{E(tag)}</code></dd>",
        f"<dt>ResRef</dt><dd>{E(rr)}</dd>",
        f"<dt>Position (X, Y, Z)</dt><dd>{x:.2f}, {y:.2f}, {z:.2f}</dd>",
        f"<dt>Bearing</dt><dd>{E(bearing)}</dd>",
        f"<dt>Appearance</dt><dd>{E(placeable_name(appearance))} "
        f"<small class=\"muted\">(placeables.2da row {E(appearance) if appearance is not None else ''})</small></dd>",
        f"<dt>HP</dt><dd>{E(cur_hp)} / {E(hp)} (hardness {E(hardness)})</dd>",
        f"<dt>Plot / Static / Useable</dt><dd>{plot} / {static} / {useable}</dd>",
        f"<dt>Lockable</dt><dd>{lockable}</dd>",
        f"<dt>Locked</dt><dd>{locked}</dd>",
        f"<dt>Open lock DC</dt><dd>{E(open_dc)}</dd>",
        f"<dt>Close lock DC</dt><dd>{E(close_dc)}</dd>",
        f"<dt>Key required</dt><dd>{key_required}</dd>",
        f"<dt>Key name</dt><dd>{E(key_name)}</dd>",
        f"<dt>Auto-remove key</dt><dd>{auto_key}</dd>",
        f"<dt>Trap</dt><dd>{trap_flag} (detect DC {E(trap_dc)}, disarm DC {E(disarm_dc)})</dd>",
        f"<dt>Conversation</dt><dd>{E(conv)}</dd>",
        f"<dt>Faction ID</dt><dd>{E(faction)}</dd>",
        '</dl>',
    ]

    desc = loc(p.get("Description"))
    if desc:
        sections.append(f'<p class="desc">{nwn_html(desc)}</p>')

    if (area_resref, idx) in db.random_treasure_containers:
        on_open = (fld(p, "OnOpen", "") or "").lower()
        if not on_open:
            on_open = (fld(db.placeables.get(rr, {}), "OnOpen", "") or "").lower()
        sections.append(
            '<p class="muted"><em>This container generates random treasure via '
            f'script <code>{E(on_open)}</code> when opened. '
            "Specific items are not predetermined and cannot be listed here.</em></p>"
        )

    if items:
        sections.append(f"<h2>Contents ({len(items)})</h2>")
        rows = []
        for it in items:
            irr = fld(it, "TemplateResRef") or fld(it, "InventoryRes") or fld(it, "EquippedRes") or ""
            iname = loc(it.get("LocalizedName"))
            if not iname and irr in db.items:
                iname = db.item_name(irr)
            if not iname:
                iname = irr or "(unknown)"
            stack = fld(it, "StackSize", 1)
            base = baseitem_label(fld(it, "BaseItem"))
            cost = item_gp_value(it) or fld(it, "Cost", "")
            cell = (link(f"../items/{irr}.html", iname)
                    if irr in db.items else nwn_html(iname))
            rows.append(
                f"<tr><td>{cell}</td>"
                f"<td>{E(irr)}</td>"
                f"<td>{base}</td>"
                f"<td>{E(stack)}</td>"
                f"<td>{E(cost)}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Item</th><th>ResRef</th><th>Base item</th><th>Stack</th><th>GP Value</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    title = f"{pname} — {db.area_name(area_resref)}"
    write(out / "containers" / f"{area_resref}-{idx:03d}.html",
          page(title, "\n".join(sections), root_rel=".."))
