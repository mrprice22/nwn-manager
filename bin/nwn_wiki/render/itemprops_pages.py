"""Browse-by-property pages and the item search page.

The ``items/properties/*`` index and detail pages (every item property, its
subtypes and cost tiers, cross-linked to the items that grant it) plus the
client-side item search page and its JSON index.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from nwn_wiki.gff import fld, list_items
from nwn_wiki.htmlgen.chrome import page, write
from nwn_wiki.htmlgen.escape import E, nwn_text
from nwn_wiki.htmlgen.links import link
from nwn_wiki.itemprops import (
    _cost_anchor,
    _cost_tiers,
    _is_raw_subtype,
    _prop_group,
    _prop_slug,
    _prop_value_num,
    itemprop_format,
)
from nwn_wiki.items import (
    _item_category,
    _item_category_label,
    item_gp_value,
)
from nwn_wiki.lookups import (
    IPROP_DEFS,
    SPELL_INFO,
    _COMBINED_PROP_PAGES,
    _SPELL_PROP_TABLES,
    _iprp_name_spell_info,
    _spell_level_classes,
    baseitem_name,
)

from nwn_wiki import state


def _where_snippet(db: "Db", resref: str) -> str:
    """Short 'where to find' blurb for property/search listing pages."""
    sources: list[str] = []
    for s in db.item_sold_at.get(resref, []):
        sources.append("Sold: " + nwn_text(s["name"]))
    for c in db.item_carried_by.get(resref, []):
        sources.append("Carried by: " + nwn_text(c["cname"]))
    for c in db.item_in_container.get(resref, []):
        sources.append("Container: " + nwn_text(c["pname"]))
    for s in db.item_from_script.get(resref, []):
        sources.append("Script: " + (s.get("label") or s["script"]))
    if not sources:
        return '<span class="muted">—</span>'
    first = E(sources[0])
    if len(sources) == 1:
        return first
    return first + f' <span class="muted">&amp; {len(sources) - 1} more</span>'


# ---------------------------------------------------------------------------
# Browse by property pages
# ---------------------------------------------------------------------------

_PROP_GROUP_ORDER = [
    "Resistances & Immunities",
    "Saving Throws",
    "Ability & Skill Bonuses",
    "AC & Combat",
    "Spell & Magic",
    "Other",
]


def render_items_by_property(db: "Db", out: Path) -> None:
    # Map from property name → list of subtype prefix groups.
    # Subtypes matching a prefix (e.g. "Sneak Attack (+1d6)") are grouped under
    # the prefix key on the index page and shown as sub-sections on the detail page.
    _pname_subtype_groups: dict[str, list[str]] = {
        pdef["name"]: pdef["subtype_prefix_groups"]
        for pdef in IPROP_DEFS.values()
        if pdef.get("subtype_prefix_groups")
    }

    def _group_subtype(pname: str, subtype: str) -> str:
        for prefix in _pname_subtype_groups.get(pname, []):
            if subtype == prefix or subtype.startswith(prefix + " ("):
                return prefix
        return subtype

    # Accumulate per (pname, key_subtype, resref, cost_str) so that an item
    # granting the same property N times shows up once with qty=N.
    # Tuple layout: (resref, name, cost_str, value_num, qty)
    _acc: dict[tuple[str, str, str, str], tuple[str, int, int]] = {}
    # Separate accumulator for prefix-group variants (tracks actual subtype).
    _prefix_acc: dict[tuple[str, str, str, str, str], tuple[str, int, int]] = {}
    # Light property: track param1 color per (resref, cost_str/brightness).
    _light_param: dict[tuple[str, str], str] = {}

    for resref, i in db.items.items():
        if not (resref in db.item_sold_at or resref in db.item_in_container
                or any(e.get("dropable") for e in db.item_carried_by.get(resref, []))
                or resref in db.item_from_script):
            continue
        name = db.item_name(resref)
        if name.startswith("[TLK#") or name == resref:
            continue
        state._current_context = f"item:{resref} ({name})"
        for p in list_items(i.get("PropertiesList")):
            f = itemprop_format(p)
            pname, subtype, cost_str = f["property"], f["subtype"], f["cost"]
            if not pname:
                continue
            if pname == "Immunity: Specific Spell" and not subtype and cost_str:
                subtype, cost_str = cost_str, ""
            # Merge entries whose subtype is an unresolved numeric fallback
            # (e.g. "True Seeing: 1", "True Seeing: 2" → all under "True Seeing").
            key_subtype = "" if _is_raw_subtype(subtype) else _group_subtype(pname, subtype)
            acc_key = (pname, key_subtype, resref, cost_str)
            if acc_key in _acc:
                n, v, c = _acc[acc_key]
                _acc[acc_key] = (n, v, c + 1)
            else:
                _acc[acc_key] = (name, _prop_value_num(cost_str), 1)
            if pname == "Light" and f["param"]:
                _light_param[(resref, cost_str)] = f["param"]
            # When a subtype was grouped under a prefix key, also track the
            # actual subtype so the detail page can break out sub-sections.
            if key_subtype and key_subtype != subtype:
                p_key = (pname, key_subtype, subtype, resref, cost_str)
                if p_key in _prefix_acc:
                    n2, v2, c2 = _prefix_acc[p_key]
                    _prefix_acc[p_key] = (n2, v2, c2 + 1)
                else:
                    _prefix_acc[p_key] = (name, _prop_value_num(cost_str), 1)

    prop_index: dict[tuple[str, str], list[tuple[str, str, str, int, int]]] = defaultdict(list)
    for (pname, key_subtype, resref, cost_str), (name, value_num, qty) in _acc.items():
        prop_index[(pname, key_subtype)].append((resref, name, cost_str, value_num, qty))

    for key in prop_index:
        prop_index[key].sort(key=lambda x: (-x[3], nwn_text(x[1]).lower()))

    sorted_keys = sorted(prop_index.keys(), key=lambda k: (k[0].lower(), k[1].lower()))

    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pname, subtype in sorted_keys:
        groups[_prop_group(pname)].append((pname, subtype))

    # Properties that want subtype-only rows on the index (no cost-tier expansion).
    _index_subtype_only = {
        pdef["name"]
        for pdef in IPROP_DEFS.values()
        if pdef.get("index_by_subtype_only")
    }

    # Build ordered lists of unique property names per group and their subtypes.
    # _pname_anchor: stable anchor for each property name (no subtype suffix).
    def _pname_anchor(pname: str) -> str:
        return "pn-" + _prop_slug(pname, "")

    # unique_pnames[g] = ordered list of property names, preserving first appearance order
    unique_pnames: dict[str, list[str]] = {}
    for g in _PROP_GROUP_ORDER:
        seen: set[str] = set()
        ordered: list[str] = []
        for pname, _ in groups.get(g, []):
            if pname not in seen:
                seen.add(pname)
                ordered.append(pname)
        unique_pnames[g] = ordered

    # subtype_rows[pname] = ordered list of (subtype, slug, cost_tier_list)
    # cost_tier_list = list of (cost_str, anchor, count) — one row per value tier.
    subtype_rows: dict[str, list[tuple[str, str, list[tuple[str, str, int]]]]] = defaultdict(list)
    for pname, subtype in sorted_keys:
        slug = _prop_slug(pname, subtype)
        entries = prop_index[(pname, subtype)]
        tiers = [(c, a, len(items)) for c, a, items in _cost_tiers(entries)]
        subtype_rows[pname].append((subtype, slug, tiers))

    # Re-sort spell-property subtypes by (innate_level desc, name asc)
    # instead of the default alphabetical order from sorted_keys.
    for pname, tbl_name in _SPELL_PROP_TABLES.items():
        if pname not in subtype_rows:
            continue
        def _make_spell_sort_key(tbl: str):
            def key(row: tuple) -> tuple[int, str]:
                subtype, slug, tiers = row
                si = _iprp_name_spell_info(tbl, subtype) if subtype else None
                lvl = si.get("innate_level") if si else None
                return (-(lvl) if lvl is not None else 999, subtype.lower())
            return key
        subtype_rows[pname].sort(key=_make_spell_sort_key(tbl_name))

    # --- property index page ---
    # Sidebar: one link per property name (not per subtype).
    sidebar_parts: list[str] = []
    for g in _PROP_GROUP_ORDER:
        if not unique_pnames.get(g):
            continue
        sidebar_parts.append(f'<div class="toc-group-heading">{E(g)}</div>')
        for pname in unique_pnames[g]:
            total = sum(cnt for _, _, tiers in subtype_rows[pname] for _, _, cnt in tiers)
            sidebar_parts.append(
                f'<div><a href="#{E(_pname_anchor(pname))}">{E(pname)}'
                f' <span class="muted">({total})</span></a></div>'
            )
    sidebar = '<aside class="items-toc items-toc--wide">' + "".join(sidebar_parts) + "</aside>"

    total_props = len(prop_index)
    body = (
        "<h1>Browse by Property</h1>"
        f"<p>{total_props} property types across all accessible items.</p>"
    )
    for g in _PROP_GROUP_ORDER:
        if not unique_pnames.get(g):
            continue
        anchor = re.sub(r"[^a-z0-9]+", "-", g.lower()).strip("-")
        body += f'<h2 id="{E(anchor)}">{E(g)}</h2>'
        for pname in unique_pnames[g]:
            rows = subtype_rows[pname]
            total = sum(cnt for _, _, tiers in rows for _, _, cnt in tiers)
            body += (
                f'<h3 id="{E(_pname_anchor(pname))}">{E(pname)}'
                f' <small class="muted">({total})</small></h3>'
            )
            has_subtypes = any(st for st, _, _ in rows)
            # Determine whether any subtype has multiple cost tiers worth showing.
            any_cost_tiers = any(
                len(tiers) > 1 or (len(tiers) == 1 and tiers[0][0])
                for _, _, tiers in rows
            )
            # Some properties (e.g. On Hit Properties) want one index row per
            # subtype only — cost-tier breakdown belongs on the detail page.
            if has_subtypes and any_cost_tiers and pname not in _index_subtype_only:
                body += (
                    '<table class="data"><thead><tr>'
                    "<th>Subtype</th><th>Value</th><th>Items</th></tr></thead><tbody>"
                )
                for subtype, slug, tiers in rows:
                    label = subtype if subtype else pname
                    for cost_str, cost_anch, cnt in tiers:
                        href = f"{slug}.html#{cost_anch}" if cost_anch else f"{slug}.html"
                        cost_cell = E(cost_str) if cost_str else '<span class="muted">—</span>'
                        body += (
                            f'<tr id="{E(slug + ("-" + cost_anch if cost_anch else ""))}">'
                            f"<td>{link(href, label)}</td>"
                            f"<td>{cost_cell}</td>"
                            f"<td>{cnt}</td></tr>"
                        )
                body += "</tbody></table>"
            elif has_subtypes:
                _spell_tbl = _SPELL_PROP_TABLES.get(pname)
                _show_spell_cols = _spell_tbl is not None and any(
                    v for v in SPELL_INFO.get(_spell_tbl, {}).values() if v
                )
                _spell_head = "<th>Level</th><th>Classes</th>" if _show_spell_cols else ""
                body += (
                    '<table class="data"><thead><tr>'
                    f"<th>Subtype</th>{_spell_head}<th>Items</th></tr></thead><tbody>"
                )
                for subtype, slug, tiers in rows:
                    label = subtype if subtype else pname
                    cnt = sum(c for _, _, c in tiers)
                    _spell_cells = ""
                    if _show_spell_cols:
                        _si = _iprp_name_spell_info(_spell_tbl, subtype) if subtype else None
                        _lvl, _cls = _spell_level_classes(_si)
                        _spell_cells = f"<td>{E(_lvl)}</td><td>{E(_cls)}</td>"
                    _comb = _COMBINED_PROP_PAGES.get(pname)
                    href = f"{_comb}.html#{slug}" if _comb else f"{slug}.html"
                    body += (
                        f'<tr id="{E(slug)}"><td>{link(href, label)}</td>'
                        f"{_spell_cells}<td>{cnt}</td></tr>"
                    )
                body += "</tbody></table>"
            elif any_cost_tiers:
                # No subtypes but has multiple cost tiers — expand rows.
                _, slug, tiers = rows[0]
                body += (
                    '<table class="data"><thead><tr>'
                    "<th>Value</th><th>Items</th></tr></thead><tbody>"
                )
                for cost_str, cost_anch, cnt in tiers:
                    href = f"{slug}.html#{cost_anch}" if cost_anch else f"{slug}.html"
                    label = E(cost_str) if cost_str else E(pname)
                    body += (
                        f'<tr id="{E(slug + ("-" + cost_anch if cost_anch else ""))}">'
                        f"<td>{link(href, label)}</td>"
                        f"<td>{cnt}</td></tr>"
                    )
                body += "</tbody></table>"
            else:
                # Single no-subtype, no-value entry — link directly.
                _, slug, tiers = rows[0]
                cnt = sum(c for _, _, c in tiers)
                body += (
                    '<table class="data"><thead><tr>'
                    "<th>Property</th><th>Items</th></tr></thead><tbody>"
                    f'<tr id="{E(slug)}"><td>{link(f"{slug}.html", pname)}</td>'
                    f"<td>{cnt}</td></tr></tbody></table>"
                )

    layout = (
        f'<div class="items-layout">{sidebar}'
        f'<div class="items-content">{body}</div></div>'
    )
    write(out / "items" / "properties" / "index.html",
          page("Browse by Property", layout, root_rel="../.."))

    # Build prefix-group detail structure:
    # (pname, group_key) → [(actual_subtype, resref, name, cost_str, value_num, qty)]
    _prefix_detail: dict[tuple[str, str], list] = defaultdict(list)
    for (pname, group_key, actual_sub, resref, cost_str), (name, value_num, qty) in _prefix_acc.items():
        _prefix_detail[(pname, group_key)].append((actual_sub, resref, name, cost_str, value_num, qty))
    for key in _prefix_detail:
        _prefix_detail[key].sort(key=lambda x: (x[0], -x[4], nwn_text(x[1]).lower()))

    # --- per-property detail pages ---
    for (pname, subtype), entries in prop_index.items():
        if pname in _COMBINED_PROP_PAGES:
            continue  # rendered as a single combined page below
        slug = _prop_slug(pname, subtype)
        label = pname + (f": {subtype}" if subtype else "")
        tiers = _cost_tiers(entries)
        body = f"<h1>{E(label)}</h1>"

        # Spell level / class info block for Cast Spell and On Hit Cast Spell pages.
        _detail_spell_tbl = _SPELL_PROP_TABLES.get(pname)
        if _detail_spell_tbl and subtype and any(
            v for v in SPELL_INFO.get(_detail_spell_tbl, {}).values() if v
        ):
            _dsi = _iprp_name_spell_info(_detail_spell_tbl, subtype)
            _dlvl, _dcls = _spell_level_classes(_dsi)
            if _dlvl or _dcls:
                body += '<dl class="meta">'
                if _dlvl:
                    body += f"<dt>Spell Level</dt><dd>{E(_dlvl)}</dd>"
                if _dcls:
                    body += f"<dt>Caster Classes</dt><dd>{E(_dcls)}</dd>"
                body += "</dl>"

        detail_key = (pname, subtype)
        if detail_key in _prefix_detail:
            # Prefix-group page: sub-sections per actual subtype variant.
            sub_groups: dict[str, list] = defaultdict(list)
            for actual_sub, resref, name, cost_str, value_num, qty in _prefix_detail[detail_key]:
                sub_groups[actual_sub].append((resref, name, cost_str, value_num, qty))
            body += f"<p>{len(entries)} items across {len(sub_groups)} variants.</p>"
            for actual_sub in sorted(sub_groups.keys()):
                sub_entries = sub_groups[actual_sub]
                sub_anchor = _cost_anchor(actual_sub)
                has_qty = any(e[4] > 1 for e in sub_entries)
                body += (
                    f'<h2 id="{E(sub_anchor)}">{E(actual_sub)}'
                    f' <small class="muted">({len(sub_entries)})</small></h2>'
                    '<table class="data"><thead><tr>'
                    "<th>Name</th>"
                    "<th>Type</th>"
                    + ("<th>Qty</th>" if has_qty else "")
                    + "<th>Where to Find</th>"
                    "</tr></thead><tbody>"
                )
                for resref, name, _, _, qty in sub_entries:
                    _icat = _item_category(db.items[resref], nwn_text(name))
                    itype_link = link(f"../index.html#{_icat.replace('_', '-')}", _item_category_label(_icat))
                    body += (
                        f"<tr><td>{link(f'../{resref}.html', name)}</td>"
                        f"<td>{itype_link}</td>"
                        + (f"<td>{qty}</td>" if has_qty else "")
                        + f"<td>{_where_snippet(db, resref)}</td></tr>"
                    )
                body += "</tbody></table>"
        elif len(tiers) <= 1:
            # Single tier (or no cost) — keep flat table.
            body += f"<p>{len(entries)} items.</p>"
            show_cost = bool(tiers and tiers[0][0])
            has_qty = any(e[4] > 1 for e in entries)
            body += (
                '<table class="data"><thead><tr>'
                "<th>Name</th>"
                "<th>Type</th>"
                + ("<th>Value</th>" if show_cost else "")
                + ("<th>Qty</th>" if has_qty else "")
                + "<th>Where to Find</th>"
                "</tr></thead><tbody>"
            )
            for resref, name, cost_str, _, qty in entries:
                _icat = _item_category(db.items[resref], nwn_text(name))
                itype_link = link(f"../index.html#{_icat.replace('_', '-')}", _item_category_label(_icat))
                body += (
                    f"<tr><td>{link(f'../{resref}.html', name)}</td>"
                    f"<td>{itype_link}</td>"
                    + (f"<td>{E(cost_str)}</td>" if show_cost else "")
                    + (f"<td>{qty}</td>" if has_qty else "")
                    + f"<td>{_where_snippet(db, resref)}</td></tr>"
                )
            body += "</tbody></table>"
        else:
            # Multiple tiers — one sub-table per tier with anchors.
            _is_light = (pname == "Light" and not subtype)
            body += f"<p>{len(entries)} items across {len(tiers)} value tiers.</p>"
            for cost_str, cost_anch, tier_entries in tiers:
                has_qty = any(e[4] > 1 for e in tier_entries)
                tier_label = E(cost_str) if cost_str else "Other"
                h_anchor = f' id="{E(cost_anch)}"' if cost_anch else ""
                body += (
                    f"<h2{h_anchor}>{tier_label}"
                    f' <small class="muted">({len(tier_entries)})</small></h2>'
                    '<table class="data"><thead><tr>'
                    "<th>Name</th>"
                    "<th>Type</th>"
                    + ("<th>Color</th>" if _is_light else "")
                    + ("<th>Qty</th>" if has_qty else "")
                    + "<th>Where to Find</th>"
                    "</tr></thead><tbody>"
                )
                for resref, name, _, _, qty in tier_entries:
                    _icat = _item_category(db.items[resref], nwn_text(name))
                    itype_link = link(f"../index.html#{_icat.replace('_', '-')}", _item_category_label(_icat))
                    _color_cell = ""
                    if _is_light:
                        _color = _light_param.get((resref, cost_str), "")
                        if _color:
                            _ccls = f"nwn-light-color nwn-light-{_color.lower()}"
                            _color_cell = f'<td><span class="{_ccls}">{E(_color)}</span></td>'
                        else:
                            _color_cell = '<td><span class="muted">—</span></td>'
                    body += (
                        f"<tr><td>{link(f'../{resref}.html', name)}</td>"
                        f"<td>{itype_link}</td>"
                        + _color_cell
                        + (f"<td>{qty}</td>" if has_qty else "")
                        + f"<td>{_where_snippet(db, resref)}</td></tr>"
                    )
                body += "</tbody></table>"

        # Build floating sidebar for the detail page.
        sibling_rows = subtype_rows.get(pname, [])
        toc_parts: list[str] = []
        if len(sibling_rows) > 1:
            _sib_spell_tbl = _SPELL_PROP_TABLES.get(pname)
            _sib_has_spell_info = _sib_spell_tbl and any(
                v for v in SPELL_INFO.get(_sib_spell_tbl, {}).values() if v
            )
            if _sib_has_spell_info:
                _cur_lvl_heading: object = object()  # sentinel
                for sib_sub, sib_slug, sib_tiers in sibling_rows:
                    _ssi = _iprp_name_spell_info(_sib_spell_tbl, sib_sub) if sib_sub else None
                    _slvl = _ssi.get("innate_level") if _ssi else None
                    lvl_heading = "Cantrip" if _slvl == 0 else (f"Level {_slvl}" if _slvl is not None else "")
                    if lvl_heading != _cur_lvl_heading:
                        _cur_lvl_heading = lvl_heading
                        if lvl_heading:
                            toc_parts.append(f'<div class="toc-group-heading">{E(lvl_heading)}</div>')
                    sib_label = sib_sub if sib_sub else pname
                    sib_count = sum(c for _, _, c in sib_tiers)
                    cls = ' class="toc-current"' if sib_slug == slug else ''
                    toc_parts.append(
                        f'<div><a href="{sib_slug}.html"{cls}>{E(sib_label)}'
                        f' <span class="muted">({sib_count})</span></a></div>'
                    )
            else:
                toc_parts.append(f'<div class="toc-group-heading">{E(pname)}</div>')
                for sib_sub, sib_slug, sib_tiers in sibling_rows:
                    sib_label = sib_sub if sib_sub else pname
                    sib_count = sum(c for _, _, c in sib_tiers)
                    cls = ' class="toc-current"' if sib_slug == slug else ''
                    toc_parts.append(
                        f'<div><a href="{sib_slug}.html"{cls}>{E(sib_label)}'
                        f' <span class="muted">({sib_count})</span></a></div>'
                    )
        if detail_key in _prefix_detail:
            seen_subs: set[str] = set()
            toc_sub_parts: list[str] = []
            for actual_sub, *_ in _prefix_detail[detail_key]:
                if actual_sub not in seen_subs:
                    seen_subs.add(actual_sub)
                    toc_sub_parts.append(
                        f'<div><a href="#{_cost_anchor(actual_sub)}">{E(actual_sub)}</a></div>'
                    )
            if toc_sub_parts:
                toc_parts.append('<div class="toc-group-heading">On This Page</div>')
                toc_parts.extend(toc_sub_parts)
        elif len(tiers) > 1:
            toc_parts.append('<div class="toc-group-heading">On This Page</div>')
            for cost_str, cost_anch, tier_entries in tiers:
                tier_label = cost_str if cost_str else "Other"
                toc_parts.append(
                    f'<div><a href="#{cost_anch}">{E(tier_label)}'
                    f' <span class="muted">({len(tier_entries)})</span></a></div>'
                )
        back = f'<div><a href="index.html#{E(_pname_anchor(pname))}">← Browse by Property</a></div>'
        detail_sidebar = '<aside class="items-toc">' + back + "".join(toc_parts) + '</aside>'
        layout = f'<div class="items-layout">{detail_sidebar}<div class="items-content">{body}</div></div>'
        write(out / "items" / "properties" / f"{slug}.html",
              page(label, layout, root_rel="../.."))

    # --- combined property pages (one page per property, all subtypes together) ---
    for pname, combined_slug in _COMBINED_PROP_PAGES.items():
        if pname not in subtype_rows:
            continue
        _comb_rows = subtype_rows[pname]  # [(subtype, slug, tiers)], sorted level desc
        _spell_tbl = _SPELL_PROP_TABLES.get(pname)
        _has_spell_info = bool(_spell_tbl and any(
            v for v in SPELL_INFO.get(_spell_tbl, {}).values() if v
        ))

        total_items = sum(sum(c for _, _, c in tiers) for _, _, tiers in _comb_rows)

        # Sidebar ToC: spell level group headings + per-spell anchor links.
        toc_parts: list[str] = []
        toc_parts.append(f'<div><a href="index.html#{E(_pname_anchor(pname))}">← Browse by Property</a></div>')
        _cur_toc_lvl: object = object()
        for sib_sub, sib_slug, sib_tiers in _comb_rows:
            _ssi = _iprp_name_spell_info(_spell_tbl, sib_sub) if _has_spell_info and sib_sub else None
            _slvl = _ssi.get("innate_level") if _ssi else None
            lvl_heading = "Cantrip" if _slvl == 0 else (f"Level {_slvl}" if _slvl is not None else "")
            if lvl_heading != _cur_toc_lvl:
                _cur_toc_lvl = lvl_heading
                if lvl_heading:
                    toc_parts.append(f'<div class="toc-group-heading">{E(lvl_heading)}</div>')
            sib_label = sib_sub if sib_sub else pname
            sib_count = sum(c for _, _, c in sib_tiers)
            toc_parts.append(
                f'<div><a href="#{E(sib_slug)}">{E(sib_label)}'
                f' <span class="muted">({sib_count})</span></a></div>'
            )

        # Main content: h2 per spell level, h3 + flat table per spell.
        body = f"<h1>{E(pname)}</h1>"
        body += f"<p>{total_items} items across {len(_comb_rows)} spells.</p>"
        _cur_body_lvl: object = object()
        for subtype, slug, tiers in _comb_rows:
            _si = _iprp_name_spell_info(_spell_tbl, subtype) if _has_spell_info and subtype else None
            _lvl = _si.get("innate_level") if _si else None
            _dlvl, _dcls = _spell_level_classes(_si)

            lvl_heading = "Cantrip" if _lvl == 0 else (f"Level {_lvl}" if _lvl is not None else "")
            if lvl_heading != _cur_body_lvl:
                _cur_body_lvl = lvl_heading
                if lvl_heading:
                    lvl_anchor = re.sub(r"[^a-z0-9]+", "-", lvl_heading.lower()).strip("-")
                    body += f'<h2 id="{E(lvl_anchor)}">{E(lvl_heading)}</h2>'

            entries = prop_index.get((pname, subtype), [])
            cnt = sum(c for _, _, c in tiers)
            label = subtype if subtype else pname

            body += f'<h3 id="{E(slug)}">{E(label)} <small class="muted">({cnt})</small></h3>'
            if _has_spell_info and (_dlvl or _dcls):
                body += '<dl class="meta">'
                if _dlvl:
                    body += f"<dt>Spell Level</dt><dd>{E(_dlvl)}</dd>"
                if _dcls:
                    body += f"<dt>Caster Classes</dt><dd>{E(_dcls)}</dd>"
                body += "</dl>"

            has_qty = any(e[4] > 1 for e in entries)
            body += (
                '<table class="data"><thead><tr>'
                "<th>Name</th><th>Type</th><th>Uses</th>"
                + ("<th>Qty</th>" if has_qty else "")
                + "<th>Where to Find</th>"
                "</tr></thead><tbody>"
            )
            for resref, name, cost_str, _, qty in entries:
                _icat = _item_category(db.items[resref], nwn_text(name))
                itype_link = link(f"../index.html#{_icat.replace('_', '-')}", _item_category_label(_icat))
                uses_cell = E(cost_str) if cost_str else '<span class="muted">—</span>'
                body += (
                    f"<tr><td>{link(f'../{resref}.html', name)}</td>"
                    f"<td>{itype_link}</td>"
                    f"<td>{uses_cell}</td>"
                    + (f"<td>{qty}</td>" if has_qty else "")
                    + f"<td>{_where_snippet(db, resref)}</td></tr>"
                )
            body += "</tbody></table>"

        combined_sidebar = '<aside class="items-toc">' + "".join(toc_parts) + '</aside>'
        layout = f'<div class="items-layout">{combined_sidebar}<div class="items-content">{body}</div></div>'
        write(out / "items" / "properties" / f"{combined_slug}.html",
              page(pname, layout, root_rel="../.."))


# ---------------------------------------------------------------------------
# Item search page + JSON index
# ---------------------------------------------------------------------------

_SEARCH_JS = r"""(function(){
var N=4,data=[],form=document.getElementById('sf'),results=document.getElementById('sr');
var propSels=[],subSels=[],minVals=[];
for(var i=1;i<=N;i++){
  propSels.push(document.getElementById('fp'+i));
  subSels.push(document.getElementById('fs'+i));
  minVals.push(document.getElementById('fv'+i));
}

fetch('search-index.json').then(function(r){return r.json();}).then(function(d){
  data=d; populateFilters();
});

var _FIXED_VALS={
  'Heavy Armor':1,'Medium Armor':1,'Light Armor':1,'Cloth & Robes':1,
  'Small Shields':1,'Large Shields':1,'Tower Shields':1,
  'Helmets':1,'Amulets':1,'Belts':1,'Boots':1,'Bracers & Gauntlets':1,'Rings':1,'Cloaks':1,
  'Ammunition':1,
  'Wands':1,'Potions':1,'Scrolls':1,'Grenades':1,
  'Books':1,'Magic Rods':1,'Magic Staves':1,'Gems':1,'Dyes':1,'Poisons & Venoms':1,
  'Miscellaneous':1,'Creature Items':1
};

function _addOpts(grp,opts,bisSet){
  var added=0;
  opts.forEach(function(o){
    if(bisSet[o.v]){grp.appendChild(new Option(o.l,o.v));added++;}
  });
  return added;
}

function populateFilters(){
  var bisSet={},props={},subs={};
  data.forEach(function(it){
    bisSet[it.base_item]=1;
    it.properties.forEach(function(p){
      props[p.prop]=1;
      if(p.subtype){if(!subs[p.prop])subs[p.prop]={};subs[p.prop][p.subtype]=1;}
    });
  });
  window._subs=subs;
  var biSel=document.getElementById('fb');

  var GROUPS=[
    {label:'Armor',opts:[
      {l:'Heavy Armor',v:'Heavy Armor'},
      {l:'Medium Armor',v:'Medium Armor'},
      {l:'Light Armor',v:'Light Armor'},
      {l:'Cloth & Robes',v:'Cloth & Robes'},
      {l:'Small Shields',v:'Small Shields'},
      {l:'Large Shields',v:'Large Shields'},
      {l:'Tower Shields',v:'Tower Shields'}
    ]},
    {label:'Weapons',dyn:true,extra:[{l:'Ammunition',v:'Ammunition'}]},
    {label:'Gear',opts:[
      {l:'Amulets',v:'Amulets'},
      {l:'Belts',v:'Belts'},
      {l:'Boots',v:'Boots'},
      {l:'Bracers & Gauntlets',v:'Bracers & Gauntlets'},
      {l:'Rings',v:'Rings'},
      {l:'Cloaks',v:'Cloaks'},
      {l:'Helmets',v:'Helmets'}
    ]},
    {label:'Misc.',opts:[
      {l:'Wands',v:'Wands'},
      {l:'Potions',v:'Potions'},
      {l:'Scrolls',v:'Scrolls'},
      {l:'Grenades',v:'Grenades'},
      {l:'Books',v:'Books'},
      {l:'Magic Rods',v:'Magic Rods'},
      {l:'Magic Staves',v:'Magic Staves'},
      {l:'Gems',v:'Gems'},
      {l:'Dyes',v:'Dyes'},
      {l:'Poisons & Venoms',v:'Poisons & Venoms'},
      {l:'Miscellaneous',v:'Miscellaneous'},
      {l:'Creature Items',v:'Creature Items'}
    ]}
  ];

  var dynWeps=Object.keys(bisSet).filter(function(b){return !_FIXED_VALS[b];}).sort();

  GROUPS.forEach(function(g){
    var grp=document.createElement('optgroup');
    grp.label=g.label;
    var added=0;
    if(g.dyn){
      dynWeps.forEach(function(v){grp.appendChild(new Option(v,v));added++;});
      (g.extra||[]).forEach(function(o){if(bisSet[o.v]){grp.appendChild(new Option(o.l,o.v));added++;}});
    } else {
      added=_addOpts(grp,g.opts,bisSet);
    }
    if(added>0)biSel.appendChild(grp);
  });

  var propOpts=Object.keys(props).sort();
  propSels.forEach(function(sel){
    propOpts.forEach(function(p){sel.appendChild(new Option(p,p));});
  });
}

propSels.forEach(function(sel,idx){
  sel.addEventListener('change',function(){
    var chosen=sel.value,sub=(window._subs||{})[chosen]||{};
    subSels[idx].innerHTML='<option value="">— any —</option>';
    Object.keys(sub).sort().forEach(function(s){subSels[idx].appendChild(new Option(s,s));});
    subSels[idx].disabled=!chosen||!Object.keys(sub).length;
  });
});

form.addEventListener('submit',function(e){
  e.preventDefault();
  var bi=document.getElementById('fb').value;
  var sortBy=document.getElementById('fo').value;
  var asc=document.getElementById('fd').value==='asc';
  var inclNonDrop=document.getElementById('fnd').checked;
  var ppOnly=document.getElementById('fpp').checked;
  var conds=[];
  for(var i=0;i<N;i++){
    var prop=propSels[i].value,sub=subSels[i].value;
    var minv=parseInt(minVals[i].value,10)||0;
    if(prop||sub||minv>0)conds.push({prop:prop,sub:sub,minv:minv});
  }
  var out=[];
  data.forEach(function(it){
    if(!it.accessible&&!inclNonDrop)return;
    if(ppOnly&&!it.pickpocketable)return;
    if(bi){
      if(it.base_item!==bi)return;
    }
    var matched=[];
    var ok=!conds.length||conds.every(function(c){
      var mp=it.properties.filter(function(p){
        if(c.prop&&p.prop!==c.prop)return false;
        if(c.sub&&p.subtype!==c.sub)return false;
        if(c.minv>0&&p.value_num<c.minv)return false;
        return true;
      });
      if(!mp.length)return false;
      matched.push(mp[0]);
      return true;
    });
    if(!ok)return;
    if(!matched.length&&it.properties.length)matched=[it.properties[0]];
    out.push({it:it,matched:matched});
  });
  out.sort(function(a,b){
    var v;
    if(sortBy==='cost')v=a.it.cost-b.it.cost;
    else if(sortBy==='value')v=(a.matched[0]?a.matched[0].value_num:0)-(b.matched[0]?b.matched[0].value_num:0);
    else v=a.it.name.localeCompare(b.it.name);
    return asc?v:-v;
  });
  render(out,!!conds.length,inclNonDrop);
});

function render(rows,showProps,showAccess){
  if(!rows.length){results.innerHTML='<p class="muted">No items match.</p>';return;}
  var cols='<th>Name</th>'
    +(showAccess?'<th>Reachable</th>':'')
    +'<th>Base Item</th>'
    +(showProps?'<th>Matched Properties</th>':'')
    +'<th>GP Value</th>';
  var html='<p><strong>'+rows.length+'</strong> result'+(rows.length!==1?'s':'')+'</p>'
    +'<table class="data"><thead><tr>'+cols+'</tr></thead><tbody>';
  rows.forEach(function(r){
    var props=r.matched.map(function(p){
      var pIdxHref='properties/index.html#'+p.prop_idx_anchor;
      var propLink='<a href="'+esc(pIdxHref)+'">'+esc(p.prop)+'</a>';
      var detail='';
      if(p.subtype||p.cost){
        var dHref=p.detail_href?'properties/'+p.detail_href:'properties/'+p.detail_slug+'.html'+(p.cost_anchor?'#'+p.cost_anchor:'');
        if(p.subtype&&p.cost){
          detail=': <a href="'+esc(dHref)+'">'+esc(p.subtype)+' \u2014 '+esc(p.cost)+'</a>';
        }else if(p.subtype){
          detail=': <a href="'+esc(dHref)+'">'+esc(p.subtype)+'</a>';
        }else{
          detail=' \u2014 <a href="'+esc(dHref)+'">'+esc(p.cost)+'</a>';
        }
      }
      if(p.param){
        var cCls='nwn-light-color nwn-light-'+p.param.toLowerCase();
        detail+=' \u2014 <span class="'+cCls+'">'+esc(p.param)+'</span>';
      }
      return '<span>'+propLink+detail+'</span>';
    }).join('<br>');
    var idxBase=r.it.accessible?'index.html':'inaccessible/index.html';
    var biHref=idxBase+'#'+esc(r.it.base_item_anchor||'');
    var biCell=r.it.base_item_anchor
      ?'<a href="'+biHref+'">'+esc(r.it.base_item)+'</a>'
      :esc(r.it.base_item);
    var dropBadge=(!showAccess&&!r.it.accessible)?' <span class="badge-no-drop" title="Carried by NPCs but not flagged as droppable \u2014 cannot be looted">no drop</span>':'';
    var accessCell=showAccess?(r.it.accessible?'<td>Yes</td>':'<td><span class="muted">No</span></td>'):'';
    html+='<tr><td><a href="'+esc(r.it.url)+'">'+esc(r.it.name)+'</a>'+dropBadge+'</td>'
         +accessCell
         +'<td>'+biCell+'</td>'
         +(showProps?'<td>'+props+'</td>':'')
         +'<td>'+(r.it.cost?r.it.cost.toLocaleString()+' gp':'\u2014')+'</td></tr>';
  });
  results.innerHTML=html+'</tbody></table>';
}

function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
})();"""


def render_items_search(db: "Db", out: Path) -> None:
    index: list[dict] = []
    for resref, i in sorted(db.items.items(),
                            key=lambda kv: nwn_text(db.item_name(kv[0])).lower()):
        carriers = db.item_carried_by.get(resref, [])
        pickpocketable = any(e.get("pickpocketable") for e in carriers)
        accessible = (
            resref in db.item_sold_at
            or resref in db.item_in_container
            or any(e.get("dropable") or e.get("pickpocketable") for e in carriers)
            or resref in db.item_from_script
        )
        if not accessible and not carriers:
            continue
        name = db.item_name(resref)
        if name.startswith("[TLK#") or name == resref:
            continue
        state._current_context = f"item:{resref} ({name})"
        _bi_raw = fld(i, "BaseItem", None)
        bi = -1 if _bi_raw is None else int(_bi_raw)
        cost_val = item_gp_value(i)
        props = []
        for p in list_items(i.get("PropertiesList")):
            f = itemprop_format(p)
            pname = f["property"]
            if not pname:
                continue
            subtype = f["subtype"]
            cost = f["cost"]
            # detail_slug / cost_anchor use original f values (before any swap)
            prop_idx_anchor = "pn-" + _prop_slug(pname, "")
            detail_slug = _prop_slug(pname, f["subtype"])
            cost_anchor = _cost_anchor(f["cost"]) if f["cost"] else ""
            _is_raw_sub = _is_raw_subtype(f["subtype"]) if f["subtype"] else False
            _comb_pg = _COMBINED_PROP_PAGES.get(pname)
            if _comb_pg and f["subtype"] and not _is_raw_sub:
                detail_href = f"{_comb_pg}.html#{detail_slug}"
            else:
                detail_href = ""
            if pname == "Immunity: Specific Spell" and not subtype and cost:
                subtype, cost = cost, ""
            _srch_param = f["param"] if pname == "Light" else ""
            props.append({
                "prop": pname,
                "subtype": subtype,
                "cost": cost,
                **({"param": _srch_param} if _srch_param else {}),
                "value_num": _prop_value_num(f["cost"]),
                "prop_idx_anchor": prop_idx_anchor,
                "detail_slug": detail_slug,
                "cost_anchor": cost_anchor,
                "detail_href": detail_href,
            })
        _cat = _item_category(i, nwn_text(name))
        index.append({
            "resref": resref,
            "name": nwn_text(name),
            "base_item": (baseitem_name(bi) or f"BaseItem #{bi}") if _cat.startswith("weapon_") else _item_category_label(_cat),
            "base_item_id": bi,
            "base_item_anchor": _cat.replace("_", "-"),
            "cost": cost_val,
            "url": f"{resref}.html",
            "properties": props,
            "accessible": accessible,
            "pickpocketable": pickpocketable,
        })

    write(out / "items" / "search-index.json",
          json.dumps(index, ensure_ascii=False, separators=(",", ":")))

    def _prop_row(n: int) -> str:
        return (
            f'<div class="prop-row">'
            f'<label>Property {n}</label>'
            f'<select id="fp{n}" class="prop-sel"><option value="">— any —</option></select>'
            f'<select id="fs{n}" class="subtype-sel" disabled>'
            f'<option value="">— any —</option></select>'
            f'<input id="fv{n}" type="number" min="0" placeholder="min value">'
            f'</div>'
        )

    prop_rows = "".join(_prop_row(n) for n in range(1, 5))

    body = (
        "<h1>Search Items</h1>"
        "<p>All conditions must be satisfied simultaneously. Leave fields blank to skip.</p>"
        '<form id="sf" class="item-search-form">'
        '<div class="search-row">'
        '<label for="fb">Base item</label>'
        '<select id="fb"><option value="">— all —</option></select>'
        '<label for="fo">Sort by</label>'
        '<select id="fo">'
        '<option value="cost">Cost</option>'
        '<option value="value">Property Value</option>'
        '<option value="name">Name</option>'
        '</select>'
        '<select id="fd">'
        '<option value="desc">Descending</option>'
        '<option value="asc">Ascending</option>'
        '</select>'
        '<label class="checkbox-label" title="Include items carried by NPCs that have no Dropable=1 flag — they exist in the module but cannot be looted">'
        '<input type="checkbox" id="fnd"> Include non-droppable items'
        '</label>'
        '<label class="checkbox-label">'
        '<input type="checkbox" id="fpp"> Pickpocketable only'
        '</label>'
        '<button type="submit">Search</button>'
        '</div>'
        f'<div class="prop-rows">{prop_rows}</div>'
        '</form>'
        '<div id="sr"><p class="muted">Use the filters above and click Search.</p></div>'
        f"<script>{_SEARCH_JS}</script>"
    )
    write(out / "items" / "search.html",
          page("Search Items", body, root_rel=".."))
