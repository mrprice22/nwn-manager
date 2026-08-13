"""Area rendering for the wiki.

The area index, the per-area detail page's sibling container pages, and the
area→area transition graph the "how do I get there" path sections walk.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Any

from nwn_wiki.db.index import _store_instance_slug
from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.blocks import meta_dl
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E, nwn_html, nwn_text
from nwn_wiki.htmlgen.links import (_area_link, _conv_link, _creature_link,
                                    _item_link, link, tileset_label)
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.items import item_gp_value
from nwn_wiki.lookups import (
    baseitem_label,
    class_name,
    placeable_name,
    race_name,
    tileset_name,
)
from nwn_wiki.render.creatures import creature_max_hp
from nwn_wiki.render.stores import (
    _buy_limit_str,
    _store_buy_summary,
    _store_item_gp_stats,
    _store_opener_html,
)


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
    write_page(out, PageCtx("areas/index.html"), "Areas", body)


def render_container_page(db: Db, area_resref: str, c: dict, out: Path) -> None:
    """One page per container (a placeable with a non-empty inventory).
    Surfaces the in-area position, lock state, and items it holds — useful
    for tracing the player's loot path through an area without having to
    open the placeable in the toolset."""
    p = c["p"]
    idx = c["idx"]
    ctx = PageCtx(f"containers/{area_resref}-{idx:03d}.html")
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
        meta_dl([
            f"<dt>Area</dt><dd>{_area_link(db, area_resref, ctx)}</dd>",
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
        ], "\n"),
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
            cell = (_item_link(db, irr, ctx, iname)
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
    write_page(out, ctx, title, "\n".join(sections))


_OMIT: object = object()  # sentinel: "no path-from section on this page"


def render_area_page(db: Db, resref: str, out: Path,
                     path_from_name: str = "", path_steps: Any = _OMIT) -> None:
    a = db.areas.get(resref)
    if not a:
        return
    ctx = PageCtx(f"areas/{resref}.html")
    name = db.area_name(resref)
    sections: list[str] = []

    # Header dl
    width = fld(a, "Width", "")
    height = fld(a, "Height", "")
    tileset = fld(a, "Tileset", "")
    tag = fld(a, "Tag", "")
    on_enter = fld(a, "OnEnter", "")
    on_exit = fld(a, "OnExit", "")
    on_hb = fld(a, "OnHeartbeat", "")
    on_user = fld(a, "OnUserDefined", "")

    sections.append(f"<h1>{nwn_html(name)}</h1>")
    event_rows = "".join(
        f'<dt>{label}</dt><dd>{E(val)}</dd>'
        for label, val in [
            ("OnEnter", on_enter), ("OnExit", on_exit),
            ("OnHeartbeat", on_hb), ("OnUserDefined", on_user),
        ]
        if val
    )
    sections.append(meta_dl([
        f'<dt>ResRef</dt><dd>{E(resref)}</dd>',
        f'<dt>Tag</dt><dd>{E(tag)}</dd>',
        f'<dt>Tileset</dt><dd>{tileset_label(tileset)}</dd>',
        f'<dt>Size</dt><dd>{E(width)}×{E(height)} tiles</dd>',
        event_rows,
    ]))

    # Shortest path from the configured source area
    if path_steps is not _OMIT:
        sections.append(f"<h2>Path from {nwn_html(path_from_name)}</h2>")
        if path_steps is None:
            sections.append(
                f'<p class="muted">No path found from {nwn_html(path_from_name)} to this area.</p>'
            )
        else:
            n = len(path_steps)
            hop_word = "hop" if n == 1 else "hops"
            items = []
            has_fallback_step = False
            has_dup_tag_step = False
            for a, b, kind, label, is_fallback, is_dup_tag in path_steps:
                a_link = link(f"{a}.html", db.area_name(a))
                b_link = link(f"{b}.html", db.area_name(b))
                if kind == "door":
                    via = f'use the &#8220;{E(label)}&#8221; door'
                elif kind == "trigger":
                    via = f'step on the &#8220;{E(label)}&#8221; trigger'
                elif kind in ("talk", "convo"):
                    via = E(label)
                else:
                    via = f'{E(kind)}: {E(label)}'
                warn = ""
                if is_fallback:
                    has_fallback_step = True
                    warn += (' <span class="warn-fallback" title="This transition was'
                             ' resolved via a WP_-prefix fallback — the waypoint tag'
                             ' in the script may not match the actual waypoint tag,'
                             ' making this transition possibly broken in-game."'
                             '>&#9888; possibly broken</span>')
                if is_dup_tag:
                    has_dup_tag_step = True
                    warn += (' <span class="warn-dup-tag" title="The destination tag'
                             ' for this transition matches multiple objects in the'
                             ' module. The game engine resolves to whichever it finds'
                             ' first, which may not be this area."'
                             '>&#9888; duplicate tag</span>')
                items.append(
                    f"<li>{a_link} &rarr; {b_link}"
                    f" <em class=\"muted\">[{via}]</em>{warn}</li>"
                )
            fallback_note = (
                '<p class="warn-fallback-note">&#9888; One or more steps use a'
                ' waypoint-tag fallback and may not work in-game — the script'
                ' references a short tag (e.g. <code>foo</code>) while the actual'
                ' waypoint is tagged <code>WP_foo</code>.</p>'
                if has_fallback_step else ""
            )
            dup_tag_note = (
                '<p class="warn-dup-tag-note">&#9888; One or more steps use a'
                ' destination tag that matches multiple objects across the module.'
                ' The game engine picks whichever it finds first, so the actual'
                ' in-game destination may differ from what this path shows.</p>'
                if has_dup_tag_step else ""
            )
            sections.append(
                f"<p><strong>{n} {hop_word}</strong></p>"
                + fallback_note
                + dup_tag_note
                + f"<ol>{''.join(items)}</ol>"
            )

    # Transitions (out)
    out_trans = ([t for t in db.transitions if t["src_area"] == resref]
                 + [t for t in db.script_transitions if t["src_area"] == resref])
    if out_trans:
        sections.append("<h2>Outgoing transitions</h2>")
        has_key_out = any(t.get("key_required") and t.get("key_tag") for t in out_trans)
        if has_key_out:
            tag_to_item_rr: dict[str, str] = {}
            for _rr, _it in db.items.items():
                _tag = (fld(_it, "Tag", "") or "").strip().lower()
                if _tag and _tag not in tag_to_item_rr:
                    tag_to_item_rr[_tag] = _rr
        rows = []
        for t in out_trans:
            dst = t["dst_area"]
            dst_link = (link(f"{dst}.html", db.area_name(dst))
                        if dst else f'<em>(unresolved waypoint <code>{E(t["dst_tag"])}</code>)</em>')
            key_cell = ""
            if has_key_out:
                kt = t.get("key_tag", "")
                if t.get("key_required") and kt:
                    item_rr = tag_to_item_rr.get(kt.lower())
                    key_cell = (f"<td>{_item_link(db, item_rr, ctx)}</td>"
                                if item_rr else f"<td><code>{E(kt)}</code></td>")
                else:
                    key_cell = "<td></td>"
            rows.append(
                f"<tr><td>{E(t['kind'])}</td>"
                f"<td>{E(t['label'])}</td>"
                f"<td>{dst_link}</td>"
                f"<td><code>{E(t['dst_tag'])}</code></td>"
                + key_cell + "</tr>"
            )
        key_th = "<th>Requires Key</th>" if has_key_out else ""
        sections.append(
            '<table class="data"><thead><tr>'
            f"<th>Kind</th><th>Label</th><th>Destination</th><th>Waypoint tag</th>{key_th}"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # Transitions (in)
    in_trans = ([t for t in db.transitions if t["dst_area"] == resref]
                + [t for t in db.script_transitions if t["dst_area"] == resref])
    if in_trans:
        sections.append("<h2>Incoming transitions</h2>")
        has_key_in = any(t.get("key_required") and t.get("key_tag") for t in in_trans)
        if has_key_in:
            tag_to_item_rr = {}
            for _rr, _it in db.items.items():
                _tag = (fld(_it, "Tag", "") or "").strip().lower()
                if _tag and _tag not in tag_to_item_rr:
                    tag_to_item_rr[_tag] = _rr
        rows = []
        for t in in_trans:
            src = t["src_area"]
            key_cell = ""
            if has_key_in:
                kt = t.get("key_tag", "")
                if t.get("key_required") and kt:
                    item_rr = tag_to_item_rr.get(kt.lower())
                    key_cell = (f"<td>{_item_link(db, item_rr, ctx)}</td>"
                                if item_rr else f"<td><code>{E(kt)}</code></td>")
                else:
                    key_cell = "<td></td>"
            rows.append(
                f"<tr><td>{E(t['kind'])}</td>"
                f"<td>{link(f'{src}.html', db.area_name(src))}</td>"
                f"<td>{E(t['label'])}</td>"
                + key_cell + "</tr>"
            )
        key_th = "<th>Requires Key</th>" if has_key_in else ""
        sections.append(
            '<table class="data"><thead><tr>'
            f"<th>Kind</th><th>From</th><th>Label</th>{key_th}"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # NPCs / hostile residents — split by friendliness. Each row is one
    # placement (instance), so we link the name to the per-instance page and
    # separately surface the blueprint it was spawned from.
    insts = db.area_creature_instances.get(resref, [])
    friendly, hostile = [], []
    for inst in insts:
        c = inst["c"]
        fid = fld(c, "FactionID")
        (friendly if db.is_friendly(fid) else hostile).append(inst)

    def npc_table(rows_data: list[dict], heading: str) -> None:
        if not rows_data:
            return
        sections.append(f"<h2>{heading}</h2>")
        rows = []
        for inst in rows_data:
            c = inst["c"]
            idx = inst["idx"]
            rr = fld(c, "TemplateResRef", "") or ""
            disp = db.creature_instance_name(resref, idx) or rr or "(unnamed)"
            classes = list_items(c.get("ClassList"))
            cls_str = "/".join(
                f"{class_name(fld(cl, 'Class'))} {fld(cl, 'ClassLevel', '')}"
                for cl in classes
            )
            bp = db.creatures.get(rr.lower())
            _eff_hp = creature_max_hp(c, bp)
            hp = _eff_hp if _eff_hp is not None else ""
            cr = fld(c, "ChallengeRating", "")
            conv = fld(c, "Conversation", "")
            if not conv and bp:
                conv = fld(bp, "Conversation", "") or ""
            can_rr = db.canonical_for_inst.get((resref, idx), rr)
            name_cell = _creature_link(db, can_rr, ctx, disp)
            bp_cell = (_creature_link(db, can_rr, ctx)
                       if can_rr in db.canonical_creatures else
                       (f"<code>{E(rr)}</code>" if rr else ""))
            rows.append(
                f"<tr><td>{name_cell}</td>"
                f"<td>{E(rr)}</td>"
                f"<td>{E(race_name(fld(c, 'Race')))}</td>"
                f"<td>{E(cls_str)}</td>"
                f"<td>{E(hp)}</td>"
                f"<td>{E(cr)}</td>"
                f"<td>{E(db.faction_name(fld(c, 'FactionID', '')))}</td>"
                f"<td>{_conv_link(db, conv, ctx)}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Name</th><th>ResRef</th>"
            "<th>Race</th><th>Class</th><th>HP</th><th>CR</th>"
            "<th>Faction</th><th>Conversation</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    npc_table(friendly, "NPCs")
    npc_table(hostile, "Hostile residents")

    # Encounters
    encs = db.area_encounters.get(resref, [])
    if encs:
        sections.append("<h2>Encounters</h2>")
        enc_groups: dict[str, dict] = {}
        enc_order: list[str] = []
        for e in encs:
            tag = fld(e, "Tag", "")
            rr = fld(e, "TemplateResRef", "")
            if rr not in enc_groups:
                blueprint = db.encounters.get(rr, {})
                ename = loc(blueprint.get("LocalizedName")) or loc(e.get("LocalizedName")) or rr
                max_c = fld(blueprint, "MaxCreatures", fld(e, "MaxCreatures", ""))
                diff = fld(blueprint, "DifficultyIndex", fld(e, "DifficultyIndex", ""))
                # Instance (.git) CreatureList is what spawns at runtime; blueprint is fallback.
                spawns = list_items(e.get("CreatureList")) or list_items(blueprint.get("CreatureList"))
                spawn_cells = []
                for s in spawns:
                    srr = fld(s, "ResRef", "")
                    cr = fld(s, "CR", "")
                    can_srr = db.canonical_for_bp.get(srr, srr)
                    disp = db.canonical_creature_name(can_srr) if can_srr in db.canonical_creatures else (db.creature_name(srr) if srr in db.creatures else srr)
                    cell = (_creature_link(db, can_srr, ctx, disp)
                            if can_srr in db.canonical_creatures else nwn_html(disp))
                    spawn_cells.append(f"{cell}<small> CR {E(cr)}</small>")
                enc_groups[rr] = {
                    "ename": ename, "max_c": max_c, "diff": diff,
                    "spawn_cells": spawn_cells,
                    "tags": [tag] if tag else [], "count": 1,
                }
                enc_order.append(rr)
            else:
                enc_groups[rr]["count"] += 1
                if tag:
                    enc_groups[rr]["tags"].append(tag)
        rows = []
        for rr in enc_order:
            g = enc_groups[rr]
            tags_str = ", ".join(E(t) for t in g["tags"]) if g["tags"] else ""
            rows.append(
                f"<tr><td>{nwn_html(g['ename'])}</td>"
                f"<td>{E(rr)}</td>"
                f"<td>{tags_str}</td>"
                f"<td>{E(g['max_c'])}</td>"
                f"<td>{E(g['diff'])}</td>"
                f"<td>{', '.join(g['spawn_cells']) if g['spawn_cells'] else '—'}</td>"
                f"<td>{g['count']}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Name</th><th>ResRef</th><th>Tag</th><th>Max</th><th>Diff</th><th>Spawn pool</th><th>Count</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # Script-spawned NPCs (creatures created by OnModuleLoad/OnEnter scripts
    # at waypoint locations rather than placed as instances in the GIT).
    script_spawns = db.area_script_spawns.get(resref, [])
    if script_spawns:
        sections.append("<h2>Script-spawned NPCs</h2>")
        ss_rows = []
        seen_ss: set[str] = set()
        for sp in script_spawns:
            can_rr2 = sp["can_rr"]
            if can_rr2 in seen_ss:
                continue
            seen_ss.add(can_rr2)
            bp2 = db.creatures.get(sp["bp_rr"], {})
            name2 = db.canonical_creature_name(can_rr2) or sp["bp_rr"]
            classes2 = list_items(db.canonical_creatures.get(can_rr2, {}).get("c", {}).get("ClassList")) or list_items(bp2.get("ClassList"))
            cls_str2 = "/".join(
                f"{class_name(fld(cl, 'Class'))} {fld(cl, 'ClassLevel', '')}"
                for cl in classes2
            )
            _eff_hp2 = creature_max_hp(bp2)
            hp2 = _eff_hp2 if _eff_hp2 is not None else ""
            cr2 = fld(bp2, "ChallengeRating", "")
            conv2 = fld(bp2, "Conversation", "")
            ss_rows.append(
                f"<tr><td>{_creature_link(db, can_rr2, ctx, name2)}</td>"
                f"<td><code>{E(sp['bp_rr'])}</code></td>"
                f"<td>{E(race_name(fld(bp2, 'Race')))}</td>"
                f"<td>{E(cls_str2)}</td>"
                f"<td>{E(hp2)}</td>"
                f"<td>{E(cr2)}</td>"
                f"<td>{_conv_link(db, conv2, ctx)}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Name</th><th>ResRef</th><th>Race</th><th>Class</th>"
            "<th>HP</th><th>CR</th><th>Conversation</th>"
            "</tr></thead><tbody>" + "\n".join(ss_rows) + "</tbody></table>"
        )

    # Containers (placeables with non-empty inventory). Detail lives on a
    # per-container page so this row can stay scannable.
    containers = db.area_containers.get(resref, [])
    if containers:
        sections.append(
            f"<h2>Containers <small class=\"muted\">({len(containers)})</small></h2>"
        )
        rows = []
        for c in containers:
            p = c["p"]
            idx = c["idx"]
            tag = fld(p, "Tag", "")
            rr = fld(p, "TemplateResRef", "")
            pname = loc(p.get("LocName")) or tag or rr or "(unnamed)"
            n_items = len(list_items(p.get("ItemList")))
            is_rand = (resref, idx) in db.random_treasure_containers
            x = fld(p, "X", 0.0) or 0.0
            y = fld(p, "Y", 0.0) or 0.0
            z = fld(p, "Z", 0.0) or 0.0
            locked = "yes" if int(fld(p, "Locked", 0) or 0) else "no"
            dc = fld(p, "OpenLockDC", "")
            href = f"../containers/{resref}-{idx:03d}.html"
            rand_tag = ' <small class="muted">+random</small>' if is_rand else ""
            rows.append(
                f"<tr><td>{link(href, pname)}</td>"
                f"<td>{n_items}{rand_tag}</td>"
                f"<td><code>{E(tag)}</code></td>"
                f"<td>{E(rr)}</td>"
                f"<td>{x:.1f}, {y:.1f}, {z:.1f}</td>"
                f"<td>{E(locked)}</td>"
                f"<td>{E(dc)}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Container</th><th>Items</th><th>Tag</th><th>ResRef</th>"
            "<th>X, Y, Z</th><th>Locked</th><th>Open DC</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # Stores in the area
    stores = db.area_stores.get(resref, [])
    if stores:
        sections.append("<h2>Stores</h2>")
        rows = []
        for inst in stores:
            rr = fld(inst, "ResRef", "") or fld(inst, "TemplateResRef", "")
            tag = fld(inst, "Tag", "")
            store_label = db.store_name(rr) if rr in db.stores else (tag or rr)
            slug = _store_instance_slug(resref, inst)
            name_cell = link(f"../stores/{slug}.html", store_label)

            pages = list_items(inst.get("StoreList"))
            n_items = sum(len(list_items(p.get("ItemList"))) for p in pages)

            mu_val = fld(inst, "MarkUp", None)
            md_val = fld(inst, "MarkDown", None)
            buy_html, buys_any = _store_buy_summary(inst)
            mu_str = f"{mu_val}%" if mu_val is not None else "—"
            md_str = (f"{md_val}%" if md_val is not None else "—") if buys_any else "N/A"
            mbp_raw = fld(inst, "MaxBuyPrice", None)

            area_openers = [o for o in db.store_tag_openers.get(tag.lower(), [])
                            if resref in o.get("areas", []) or o.get("area") == resref]
            if area_openers:
                seen_html: set[str] = set()
                opener_parts = []
                for o in area_openers[:3]:
                    h = _store_opener_html(db, o, ctx)
                    if h not in seen_html:
                        seen_html.add(h)
                        opener_parts.append(h)
                opener_cell = "<br>".join(opener_parts)
                if len(area_openers) > 3:
                    opener_cell += f" <em class=\"muted\">(+{len(area_openers)-3} more)</em>"
            else:
                opener_cell = '<em class="muted">unknown</em>'

            max_gp, _, _, avg_gp = _store_item_gp_stats(db, pages)
            max_gp_str = f"{max_gp:,}" if max_gp > 0 else "—"
            avg_gp_str = f"{avg_gp:,.0f}" if avg_gp > 0 else "—"
            rows.append(
                f"<tr>"
                f"<td>{name_cell}</td>"
                f"<td>{n_items}</td>"
                f"<td>{opener_cell}</td>"
                f"<td>{mu_str}</td>"
                f"<td>{buy_html}</td>"
                f"<td>{md_str}</td>"
                f"<td>{'N/A' if not buys_any else _buy_limit_str(mbp_raw)}</td>"
                f"<td>{max_gp_str}</td>"
                f"<td>{avg_gp_str}</td>"
                f"</tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Name</th><th>Items</th><th>Opened By</th>"
            '<th title="Price store charges you to buy items (% of base)">Sells At</th>'
            '<th title="Item types the store will buy from you">Buys</th>'
            '<th title="Price store pays you for items (% of base); N/A if the store buys nothing">Buys At</th>'
            '<th title="Maximum the store will pay for any single item">Max Buy</th>'
            '<th title="Highest base item value in stock (before markup)">Max GP</th>'
            '<th title="Average base item value in stock (before markup)">Avg GP</th>'
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # Conversations reachable from this area — every dlg whose callers
    # include an entity (NPC, placeable, door, trigger, area-event) bound
    # to this area. Useful as a jump-off point when chasing a quest hook.
    area_dlgs: list[tuple[str, list[dict]]] = []
    for dlg_resref in sorted(db.dialogs):
        callers = [c for c in db.dialog_callers.get(dlg_resref, [])
                   if resref in c.get("areas", []) or
                   (c.get("kind") == "area-event" and c.get("resref") == resref)]
        if callers:
            area_dlgs.append((dlg_resref, callers))
    if area_dlgs:
        sections.append("<h2>Conversations triggered here</h2>")
        rows = []
        for dlg_resref, callers in area_dlgs:
            tps = db.dialog_teleports.get(dlg_resref, [])
            tp_str = ""
            if tps:
                dests = sorted({t["area"] for t in tps if t.get("area")})
                tp_str = ", ".join(
                    link(f"{a}.html", db.area_name(a)) for a in dests if a in db.areas)
            def _via_one(c: dict) -> str:
                k = c["kind"]
                if k == "creature":
                    return f"NPC blueprint <code>{E(c['resref'])}</code>"
                if k == "creature-instance":
                    a = c.get("area", "")
                    idx = c.get("idx", 0)
                    can_rr2 = db.canonical_for_inst.get((a, idx), "")
                    nm = db.canonical_creature_name(can_rr2) if can_rr2 else (db.creature_instance_name(a, idx) or c.get("resref", ""))
                    href = f"../creatures/{can_rr2}.html" if can_rr2 else "#"
                    return f'NPC <a href="{E(href)}">{nwn_html(nm)}</a>'
                if k == "placeable":
                    return f"placeable <code>{E(c['resref'])}</code>"
                if k == "door":
                    return f"door <code>{E(c['resref'])}</code>"
                if k in ("placeable-instance", "door-instance"):
                    ent = "placeable" if k == "placeable-instance" else "door"
                    tag = c.get("tag") or ""
                    return f"{ent} placement <code>{E(tag or c.get('resref',''))}</code>"
                if k.endswith("-event-instance"):
                    ent = k.split("-")[0]  # placeable / door / trigger
                    tag = c.get("tag") or ""
                    return (f"<code>{E(c.get('event',''))}</code> on {ent} placement "
                            f"<code>{E(tag or c.get('resref',''))}</code>")
                if k.endswith("-event"):
                    return (f"<code>{E(c.get('event',''))}</code> on "
                            f"<code>{E(c['resref'])}</code>")
                return k
            via = "; ".join(_via_one(c) for c in callers)
            rows.append(
                f"<tr><td>{link(f'../conversations/{dlg_resref}.html', db.dialog_label(dlg_resref))}</td>"
                f"<td><code>{E(dlg_resref)}</code></td>"
                f"<td>{via}</td>"
                f"<td>{tp_str or '—'}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Conversation</th><th>ResRef</th><th>Triggered via</th>"
            "<th>Teleports to</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # Waypoints (just for completeness — useful for checking spawn-walk paths
    # and trigger destinations)
    wps = db.area_waypoints.get(resref, [])
    if wps:
        sections.append("<h2>Waypoints</h2>")
        rows = []
        for w in wps:
            tag = fld(w, "Tag", "")
            rr = fld(w, "TemplateResRef", "")
            rows.append(f"<tr><td><code>{E(tag)}</code></td><td>{E(rr)}</td></tr>")
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Tag</th><th>ResRef</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    write_page(out, ctx, name, "\n".join(sections))
