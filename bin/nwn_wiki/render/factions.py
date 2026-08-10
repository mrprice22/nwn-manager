"""Faction and journal rendering for the wiki.

The Factions page — per-faction creature rosters (direct placements and
encounter-pool slots), the race x faction matrix and the cross-faction
blueprint alert — plus the raw journal dump.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from nwn_wiki.gff import fld, list_items
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E, nwn_html, nwn_text
from nwn_wiki.htmlgen.links import _race_link, link
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.itemprops import _fmt_hp
from nwn_wiki.lookups import class_name, race_name
from nwn_wiki.render.creatures import creature_max_hp


def render_factions(db: Db, out: Path) -> None:
    ctx = PageCtx("factions.html")
    if not db.fac:
        write_page(out, ctx, "Factions", "<h1>Factions</h1><p>(none)</p>")
        return
    flist = list_items(db.fac.get("FactionList"))

    def _int_fid(raw: Any) -> "int | None":
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _total_levels(classes: list) -> int:
        return sum(int(fld(cl, "ClassLevel", 0) or 0) for cl in classes)

    def _faction_anchor(i: int) -> str:
        raw = db.faction_name(i)
        slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
        return f"faction-{i}-{slug}" if slug else f"faction-{i}"

    # ---- Collect per-faction entries ----
    # kind="instance": directly placed GIT creature
    # kind="spawn":    blueprint slot in an encounter pool
    faction_entries: dict[int, list[dict]] = defaultdict(list)
    crr_fids: dict[str, set[int]] = defaultdict(set)

    for area_rr, insts in db.area_creature_instances.items():
        if area_rr in db.hidden_areas:
            continue
        for inst in insts:
            c = inst["c"]
            fid = _int_fid(fld(c, "FactionID"))
            if fid is None:
                continue
            crr = fld(c, "TemplateResRef", "") or ""
            bp = db.creatures.get(crr, {})
            idx = inst["idx"]
            name = db.creature_instance_name(area_rr, idx) or db.creature_name(crr) or crr
            classes = list_items(c.get("ClassList")) or list_items(bp.get("ClassList"))
            hp = creature_max_hp(c, bp) or 0
            cr = fld(c, "ChallengeRating") or fld(bp, "ChallengeRating") or ""
            race = fld(c, "Race") if fld(c, "Race") not in (None, "") else fld(bp, "Race")
            faction_entries[fid].append({
                "kind": "instance",
                "area": area_rr,
                "crr": crr,
                "idx": idx,
                "name": name,
                "classes": classes,
                "lvls": _total_levels(classes),
                "hp": hp,
                "cr": cr,
                "race": race,
            })
            if crr:
                crr_fids[crr].add(fid)

    for crr, spawns in db.creature_encounter_spawns.items():
        bp = db.creatures.get(crr, {})
        fid = _int_fid(fld(bp, "FactionID"))
        if fid is None:
            continue
        classes = list_items(bp.get("ClassList"))
        hp = creature_max_hp(bp) or 0
        cr = fld(bp, "ChallengeRating") or ""
        name = db.creature_name(crr) or crr
        lvls = _total_levels(classes)
        race = fld(bp, "Race")
        for s in spawns:
            area_rr = s["area"]
            if area_rr in db.hidden_areas:
                continue
            faction_entries[fid].append({
                "kind": "spawn",
                "area": area_rr,
                "crr": crr,
                "enc_rr": s["encounter_resref"],
                "name": name,
                "classes": classes,
                "lvls": lvls,
                "hp": hp,
                "cr": cr,
                "race": race,
            })
            if crr:
                crr_fids[crr].add(fid)

    # Blueprints that appear under more than one faction across all placements/pools
    cross_faction: dict[str, set[int]] = {
        crr: fids for crr, fids in crr_fids.items() if len(fids) > 1
    }

    # ---- Sidebar TOC ----
    toc_parts: list[str] = ['<div class="toc-group-heading">Factions</div>']
    for i, f in enumerate(flist):
        fname = nwn_text(fld(f, "FactionName", "")) or f"Faction {i}"
        n = len(faction_entries.get(i, []))
        cnt = f' <span class="muted">({n})</span>' if n else ""
        toc_parts.append(
            f'<div><a href="#{E(_faction_anchor(i))}">{E(fname)}{cnt}</a></div>'
        )
    _none_entries = faction_entries.get(65535, [])
    if _none_entries:
        toc_parts.append(
            f'<div><a href="#{E(_faction_anchor(65535))}">(None)'
            f' <span class="muted">({len(_none_entries)})</span></a></div>'
        )
    if cross_faction:
        toc_parts.append('<div class="toc-group-heading">Flags</div>')
        toc_parts.append(
            f'<div><a href="#cross-faction">&#x26A0; Cross-Faction'
            f' <span class="muted">({len(cross_faction)})</span></a></div>'
        )
    sidebar = '<aside class="items-toc">' + "".join(toc_parts) + "</aside>"

    # ---- Summary table ----
    sum_rows: list[str] = []
    for i, f in enumerate(flist):
        fname = nwn_text(fld(f, "FactionName", "")) or f"Faction {i}"
        parent_id = fld(f, "FactionParentID")
        try:
            pid = int(parent_id) if parent_id not in (None, "") else -1
        except (TypeError, ValueError):
            pid = -1
        parent_cell = (
            f'<a href="#{E(_faction_anchor(pid))}">{E(db.faction_name(pid))}</a>'
            if 0 <= pid < len(flist) and pid != i else "&#x2014;"
        )
        glob = fld(f, "FactionGlobal", "")
        friendly = db.is_friendly(i)
        entries = faction_entries.get(i, [])
        n_inst = sum(1 for e in entries if e["kind"] == "instance")
        n_spawn = sum(1 for e in entries if e["kind"] == "spawn")
        n_areas = len({e["area"] for e in entries})
        n_bps = len({e["crr"] for e in entries if e["crr"]})
        total_lvls = sum(e["lvls"] for e in entries)
        sum_rows.append(
            "<tr>"
            f'<td><a href="#{E(_faction_anchor(i))}">{E(fname)}</a></td>'
            f"<td>{i}</td>"
            f"<td>{parent_cell}</td>"
            f"<td>{'Yes' if glob else 'No'}</td>"
            f"<td>{'Friendly' if friendly else 'Hostile'}</td>"
            f"<td>{n_inst:,}</td>"
            f"<td>{n_spawn:,}</td>"
            f"<td>{n_bps:,}</td>"
            f"<td>{n_areas:,}</td>"
            f"<td>{total_lvls:,}</td>"
            "</tr>"
        )

    if faction_entries.get(65535):
        _ne = faction_entries[65535]
        sum_rows.append(
            "<tr>"
            f'<td><a href="#{E(_faction_anchor(65535))}">(None)</a></td>'
            f"<td>65535</td>"
            f"<td>&#x2014;</td>"
            f"<td>&#x2014;</td>"
            f"<td>&#x2014;</td>"
            f"<td>{sum(1 for e in _ne if e['kind'] == 'instance'):,}</td>"
            f"<td>{sum(1 for e in _ne if e['kind'] == 'spawn'):,}</td>"
            f"<td>{len({e['crr'] for e in _ne if e['crr']}):,}</td>"
            f"<td>{len({e['area'] for e in _ne}):,}</td>"
            f"<td>{sum(e['lvls'] for e in _ne):,}</td>"
            "</tr>"
        )

    # ---- Race × Faction matrix ----
    # Collect (race_id, faction_id) → count across all entries
    race_faction_counts: dict[tuple, int] = defaultdict(int)
    for fid, entries in faction_entries.items():
        for e in entries:
            rid = e.get("race")
            if rid is None or rid == "":
                rid = None
            race_faction_counts[(rid, fid)] += 1

    all_race_ids = sorted(
        {k[0] for k in race_faction_counts},
        key=lambda r: race_name(r).lower() if r is not None else "",
    )
    factions_with_creatures = sorted(
        {k[1] for k in race_faction_counts},
    )

    rf_header = "<tr><th>Race</th>" + "".join(
        f'<th><a href="#{E(_faction_anchor(fid))}">{E(db.faction_name(fid))}</a></th>'
        for fid in factions_with_creatures
    ) + "<th>Total</th></tr>"

    rf_rows: list[str] = []
    col_totals: dict[int, int] = {fid: 0 for fid in factions_with_creatures}
    for rid in all_race_ids:
        row_total = 0
        cells = ""
        for fid in factions_with_creatures:
            n = race_faction_counts.get((rid, fid), 0)
            col_totals[fid] += n
            row_total += n
            cells += f"<td>{n if n else '&#x2014;'}</td>"
        rname_cell = _race_link(rid, ctx) if rid is not None else E("(unset)")
        rf_rows.append(f"<tr><td>{rname_cell}</td>{cells}<td>{row_total}</td></tr>")

    total_cells = "".join(f"<td>{col_totals[fid]}</td>" for fid in factions_with_creatures)
    grand_total = sum(col_totals.values())
    rf_rows.append(f"<tr><th>Total</th>{total_cells}<th>{grand_total}</th></tr>")

    sections: list[str] = [
        "<h1>Factions</h1>",
        f"<p>{len(flist)} factions defined in repute.fac.</p>",
        '<table class="data"><thead><tr>'
        "<th>Name</th><th>ID</th><th>Parent</th><th>Global</th><th>Friendly?</th>"
        "<th>Placed</th><th>Enc. Pool</th><th>Blueprints</th><th>Areas</th><th>Total Levels</th>"
        "</tr></thead><tbody>",
        "\n".join(sum_rows),
        "</tbody></table>",
        "<h2>Race &times; Faction</h2>",
        '<table class="data"><thead>' + rf_header + "</thead><tbody>",
        "\n".join(rf_rows),
        "</tbody></table>",
    ]

    # ---- Per-faction detail sections ----
    for i, f in enumerate(flist):
        fname = nwn_text(fld(f, "FactionName", "")) or f"Faction {i}"
        friendly = db.is_friendly(i)
        entries = faction_entries.get(i, [])
        anchor = _faction_anchor(i)

        sections.append(f'<h2 id="{E(anchor)}">{E(fname)}</h2>')

        if not entries:
            sections.append(
                "<p><em>No creatures placed in this faction or in any encounter pool.</em></p>"
            )
            continue

        n_inst = sum(1 for e in entries if e["kind"] == "instance")
        n_spawn = sum(1 for e in entries if e["kind"] == "spawn")
        areas_sorted = sorted(
            {e["area"] for e in entries},
            key=lambda a: db.area_name(a).lower(),
        )
        total_lvls = sum(e["lvls"] for e in entries)
        area_links = ", ".join(
            link(f"areas/{a}.html", db.area_name(a)) for a in areas_sorted
        )
        sections.append(
            '<dl class="meta">'
            f"<dt>Attitude</dt><dd>{'Friendly to PC' if friendly else 'Hostile to PC'}</dd>"
            f"<dt>Direct placements</dt><dd>{n_inst:,}</dd>"
            f"<dt>Encounter pool slots</dt><dd>{n_spawn:,}</dd>"
            f"<dt>Total class levels</dt><dd>{total_lvls:,}</dd>"
            f"<dt>Areas</dt><dd>{area_links}</dd>"
            "</dl>"
        )

        by_area: dict[str, list[dict]] = defaultdict(list)
        for e in entries:
            by_area[e["area"]].append(e)

        rows: list[str] = []
        for area_rr in sorted(by_area, key=lambda a: db.area_name(a).lower()):
            # Collapse identical (crr, kind) pairs within the same area into one row.
            grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
            for e in by_area[area_rr]:
                grouped[(e["crr"], e["kind"])].append(e)

            for (crr, kind) in sorted(
                grouped,
                key=lambda k: (db.creature_name(k[0]) or k[0]).lower(),
            ):
                group = grouped[(crr, kind)]
                count = len(group)
                e = group[0]
                classes = e["classes"]
                cls_str = "/".join(
                    f"{class_name(fld(cl, 'Class'))} {fld(cl, 'ClassLevel', '')}"
                    for cl in classes
                ) or "&#x2014;"

                can_crr = db.canonical_for_bp.get(crr, crr)
                disp_name = db.canonical_creature_name(can_crr) or e["name"] or crr
                name_cell = (
                    link(f"creatures/{can_crr}.html", disp_name)
                    if can_crr in db.canonical_creatures else nwn_html(disp_name)
                )
                kind_cell = (
                    '<span class="badge">placed</span>'
                    if kind == "instance" else
                    '<span class="badge">encounter pool</span>'
                )
                area_cell = link(f"areas/{area_rr}.html", db.area_name(area_rr))

                cross_flag = (
                    f' <a href="#cross-faction" class="muted"'
                    f' title="blueprint appears in multiple factions">&#x26A0;</a>'
                    if crr in cross_faction else ""
                )

                race_str = E(race_name(e.get("race")))
                rows.append(
                    "<tr>"
                    f"<td>{name_cell}{cross_flag}</td>"
                    f"<td>{kind_cell}</td>"
                    f"<td>{count}</td>"
                    f"<td>{area_cell}</td>"
                    f"<td>{race_str}</td>"
                    f"<td>{cls_str}</td>"
                    f"<td>{_fmt_hp(e['hp'])}</td>"
                    f"<td>{E(e['cr'])}</td>"
                    "</tr>"
                )

        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Name</th><th>Kind</th><th>Count</th><th>Area</th>"
            "<th>Race</th><th>Classes</th><th>HP</th><th>CR</th>"
            "</tr></thead><tbody>"
            + "\n".join(rows)
            + "</tbody></table>"
        )

    # ---- (None) faction detail section ----
    _none_ent = faction_entries.get(65535, [])
    if _none_ent:
        _anchor_none = _faction_anchor(65535)
        sections.append(f'<h2 id="{E(_anchor_none)}">(None)</h2>')
        _n_inst_n = sum(1 for e in _none_ent if e["kind"] == "instance")
        _n_spawn_n = sum(1 for e in _none_ent if e["kind"] == "spawn")
        _areas_n = sorted({e["area"] for e in _none_ent}, key=lambda a: db.area_name(a).lower())
        _area_links_n = ", ".join(link(f"areas/{a}.html", db.area_name(a)) for a in _areas_n)
        sections.append(
            '<dl class="meta">'
            f"<dt>Direct placements</dt><dd>{_n_inst_n:,}</dd>"
            f"<dt>Encounter pool slots</dt><dd>{_n_spawn_n:,}</dd>"
            f"<dt>Total class levels</dt><dd>{sum(e['lvls'] for e in _none_ent):,}</dd>"
            f"<dt>Areas</dt><dd>{_area_links_n}</dd>"
            "</dl>"
        )
        _by_area_n: dict[str, list[dict]] = defaultdict(list)
        for _e in _none_ent:
            _by_area_n[_e["area"]].append(_e)
        _rows_n: list[str] = []
        for _area_rr in sorted(_by_area_n, key=lambda a: db.area_name(a).lower()):
            _grouped_n: dict[tuple, list[dict]] = defaultdict(list)
            for _e in _by_area_n[_area_rr]:
                _grouped_n[(_e["crr"], _e["kind"])].append(_e)
            for (_crr, _kind) in sorted(
                _grouped_n,
                key=lambda k: (db.creature_name(k[0]) or k[0]).lower(),
            ):
                _grp = _grouped_n[(_crr, _kind)]
                _e0 = _grp[0]
                _cls_str = "/".join(
                    f"{class_name(fld(_cl, 'Class'))} {fld(_cl, 'ClassLevel', '')}"
                    for _cl in _e0["classes"]
                ) or "&#x2014;"
                _can_crr = db.canonical_for_bp.get(_crr, _crr)
                _disp = db.canonical_creature_name(_can_crr) or _e0["name"] or _crr
                _name_cell = (
                    link(f"creatures/{_can_crr}.html", _disp)
                    if _can_crr in db.canonical_creatures else nwn_html(_disp)
                )
                _kind_cell = (
                    '<span class="badge">placed</span>' if _kind == "instance"
                    else '<span class="badge">encounter pool</span>'
                )
                _cross_flag = (
                    f' <a href="#cross-faction" class="muted"'
                    f' title="blueprint appears in multiple factions">&#x26A0;</a>'
                    if _crr in cross_faction else ""
                )
                _rows_n.append(
                    "<tr>"
                    f"<td>{_name_cell}{_cross_flag}</td>"
                    f"<td>{_kind_cell}</td>"
                    f"<td>{len(_grp)}</td>"
                    f"<td>{link(f'areas/{_area_rr}.html', db.area_name(_area_rr))}</td>"
                    f"<td>{E(race_name(_e0.get('race')))}</td>"
                    f"<td>{_cls_str}</td>"
                    f"<td>{_fmt_hp(_e0['hp'])}</td>"
                    f"<td>{E(_e0['cr'])}</td>"
                    "</tr>"
                )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Name</th><th>Kind</th><th>Count</th><th>Area</th>"
            "<th>Race</th><th>Classes</th><th>HP</th><th>CR</th>"
            "</tr></thead><tbody>"
            + "\n".join(_rows_n)
            + "</tbody></table>"
        )

    # ---- Cross-faction alert section ----
    if cross_faction:
        sections.append('<h2 id="cross-faction">&#x26A0; Cross-Faction Creatures</h2>')
        sections.append(
            "<p>The following creature blueprints are associated with more than one faction "
            "across their placements or encounter pool entries. This may indicate that the "
            "blueprint faction was not overridden consistently on all instances and may need "
            "review.</p>"
        )
        cf_rows: list[str] = []
        for crr in sorted(cross_faction, key=lambda r: (db.canonical_creature_name(db.canonical_for_bp.get(r, r)) or r).lower()):
            fids = sorted(cross_faction[crr])
            can_crr = db.canonical_for_bp.get(crr, crr)
            bp_cell = (
                link(f"creatures/{can_crr}.html", db.canonical_creature_name(can_crr))
                if can_crr in db.canonical_creatures else f"<code>{E(crr)}</code>"
            )
            faction_list = ", ".join(
                f'<a href="#{E(_faction_anchor(fid))}">{E(db.faction_name(fid))}</a>'
                f' <small class="muted">({fid})</small>'
                for fid in fids
            )
            cf_rows.append(
                f"<tr><td>{bp_cell}</td><td><code>{E(crr)}</code></td>"
                f"<td>{faction_list}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Creature</th><th>ResRef</th><th>Factions</th>"
            "</tr></thead><tbody>"
            + "\n".join(cf_rows)
            + "</tbody></table>"
        )

    body = "\n".join(sections)
    layout = (
        f'<div class="items-layout">{sidebar}'
        f'<div class="items-content">{body}</div></div>'
    )
    write_page(out, ctx, "Factions", layout)
