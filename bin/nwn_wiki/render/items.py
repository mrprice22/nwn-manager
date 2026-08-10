"""Item index and item detail rendering for the wiki.

The accessible/inaccessible item indexes (bucketed by category, with their
grouped table of contents) and the per-item detail page.

Note the name: this is ``nwn_wiki.render.items`` -- the page renderers.  The
item *derivation* helpers (categories, base-item sets, offense/defense
extraction) live in :mod:`nwn_wiki.items`, which this module imports.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E, colorize_damage_words, nwn_html, nwn_text
from nwn_wiki.htmlgen.links import _area_link, _creature_link, link
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.itemprops import (
    _cost_anchor,
    _is_raw_subtype,
    _prop_slug,
    _table_lookup,
    itemprop_format,
)
from nwn_wiki.items import (
    SHIELD_BASEITEMS,
    _ARMOR_BASEITEMS,
    _CREATURE_ITEM_BASEITEMS,
    _CREATURE_WEAPON_BASEITEMS,
    _SCROLL_BASEITEMS,
    _TOC_GROUPS,
    _item_category,
    _item_category_label,
    item_gp_value,
)
from nwn_wiki.lookups import (
    SPELL_INFO,
    WEAPONS,
    _CAST_SPELL_PROP_ID,
    _COMBINED_PROP_PAGES,
    _scroll_cast_spell_info,
    _spell_level_classes,
    _torso_base_ac,
    baseitem_label,
    baseitem_name,
)

from nwn_wiki import state


def _scroll_spell_sort_key(entry: tuple[str, dict], db: "Db | None" = None) -> tuple[int, str]:
    """Sort key for scrolls: (innate_level, spell_name_lower)."""
    rr, i = entry
    display_name = nwn_text(db.item_name(rr)) if db else ""
    info = _scroll_cast_spell_info(i, display_name)
    lvl = info.get("innate_level") if info else None
    for p in list_items(i.get("PropertiesList")):
        pname_id = fld(p, "PropertyName")
        if pname_id is not None and int(pname_id) == _CAST_SPELL_PROP_ID:
            subtype = fld(p, "Subtype")
            if subtype is not None:
                sname = _table_lookup("iprp_spells", int(subtype)) or ""
                return (-(lvl) if lvl is not None else 999, sname.lower())
            break
    # Name fallback: use item display name as spell name for sort
    if display_name and lvl is not None:
        return (-(lvl), display_name.lower())
    return (-(lvl) if lvl is not None else 999, display_name.lower())


def _fmt_cost(raw) -> str:
    """Format an item Cost value with comma separators; returns '' for missing/zero."""
    try:
        v = int(raw)
        return f"{v:,}" if v else ""
    except (TypeError, ValueError):
        return E(str(raw)) if raw not in (None, "") else ""


def _items_col_flags(items: list[tuple[str, dict]]) -> tuple[bool, bool]:
    show_base  = len({baseitem_label(fld(i, "BaseItem"))     for _, i in items}) > 1
    show_stack = len({str(fld(i, "StackSize", "") or "")     for _, i in items}) > 1
    return show_base, show_stack


def _items_table_head(show_base: bool, show_stack: bool, show_ac: bool = False,
                      show_reason: bool = False, show_spell_info: bool = False) -> str:
    cols = ["<th>Name</th>", "<th>ResRef</th>"]
    if show_base:
        cols.append("<th>Base item</th>")
    if show_ac:
        cols.append("<th>Base AC</th>")
    if show_spell_info:
        cols.append("<th>Level</th>")
        cols.append("<th>Classes</th>")
    cols.append("<th>GP Value</th>")
    if show_stack:
        cols.append("<th>Stack</th>")
    if show_reason:
        cols.append("<th>Reason</th>")
    return '<table class="data"><thead><tr>' + "".join(cols) + "</tr></thead><tbody>"


def _items_row(rr: str, i: dict, db: "Db", show_base: bool, show_stack: bool,
               ctx: PageCtx, show_ac: bool = False, reason: str | None = None,
               show_spell_info: bool = False) -> str:
    """One <tr> for item ``rr``; ``ctx`` is the page the table is going into,
    which is what decides how the item link reaches ``items/``."""
    prefix = ctx.dir_url("items")
    cells = [
        f"<td>{link(f'{prefix}{rr}.html', db.item_name(rr))}</td>",
        f"<td>{E(rr)}</td>",
    ]
    if show_base:
        cells.append(f"<td>{baseitem_label(fld(i, 'BaseItem'))}</td>")
    if show_ac:
        _mac = _torso_base_ac(i)
        cells.append(f"<td>{_mac if _mac is not None else ''}</td>")
    if show_spell_info:
        _lvl, _cls = _spell_level_classes(_scroll_cast_spell_info(i, nwn_text(db.item_name(rr))))
        cells.append(f"<td>{E(_lvl)}</td>")
        cells.append(f"<td>{E(_cls)}</td>")
    cells.append(f"<td>{_fmt_cost(fld(i, 'Cost', ''))}</td>")
    if show_stack:
        cells.append(f"<td>{E(fld(i, 'StackSize', ''))}</td>")
    if reason is not None:
        cells.append(f"<td>{reason}</td>")
    return "<tr>" + "".join(cells) + "</tr>"


def _inaccessible_reason_html(rr: str, db: "Db") -> str:
    if db.item_carried_by.get(rr):
        return '<span class="reason-undroppable">Carried but not droppable</span>'
    return '<span class="reason-missing">Not found anywhere</span>'


def render_inaccessible_index(db: Db, inaccessible: list[tuple[str, dict]], out: Path) -> None:
    """Render items/inaccessible/index.html — mirrors the main items index layout.

    The early return is has_inaccessible_items(db) — literally the call
    SiteChrome gates the Inaccessible nav entry on — so the nav links to this
    page exactly when it is written.  ``inaccessible`` is non-empty exactly then
    because render_items_index() fills it with the same _item_access_class().
    """
    if not has_inaccessible_items(db):
        return
    ctx = PageCtx("items/inaccessible/index.html")

    def _item_cost_key(entry: tuple[str, dict]) -> int:
        return item_gp_value(entry[1])

    buckets: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for rr, i in inaccessible:
        buckets[_item_category(i, nwn_text(db.item_name(rr)))].append((rr, i))

    for key in buckets:
        buckets[key].sort(key=_item_cost_key, reverse=True)

    all_weapon_keys = sorted(
        (k for k in buckets if k.startswith("weapon_")),
        key=lambda k: (baseitem_name(int(k[7:])) or "").lower(),
    )
    player_weapon_keys   = [k for k in all_weapon_keys if int(k[7:]) not in _CREATURE_WEAPON_BASEITEMS]
    creature_weapon_keys = [k for k in all_weapon_keys if int(k[7:]) in _CREATURE_WEAPON_BASEITEMS]

    def _expand(cats: list[str]) -> list[str]:
        result: list[str] = []
        for c in cats:
            if c == "WEAPONS":
                result.extend(player_weapon_keys)
            elif c == "CREATURE_WEAPONS":
                result.extend(creature_weapon_keys)
            else:
                result.append(c)
        return result

    ordered_cats: list[str] = []
    for _, group_cats in _TOC_GROUPS:
        ordered_cats.extend(_expand(group_cats))
    ordered_cats.extend(creature_weapon_keys + ["creature_item"])

    toc_parts: list[str] = [
        '<div class="toc-group-heading">Views</div>',
        '<div><a href="../index.html">← Accessible Items</a></div>',
        '<div><a href="../properties/index.html">Browse by Property</a></div>',
        '<div><a href="../search.html">Search Items</a></div>',
    ]
    for group_heading, group_cats_tmpl in _TOC_GROUPS:
        entries = [(ck, buckets[ck]) for ck in _expand(group_cats_tmpl) if buckets.get(ck)]
        if not entries:
            continue
        toc_parts.append(f'<div class="toc-group-heading">{E(group_heading)}</div>')
        for cat_key, cat_items in entries:
            slug = cat_key.replace("_", "-")
            toc_parts.append(
                f'<div><a href="#{E(slug)}">{E(_item_category_label(cat_key))}'
                f' <span class="muted">({len(cat_items)})</span></a></div>'
            )

    special_cat_keys = creature_weapon_keys + (["creature_item"] if buckets.get("creature_item") else [])
    special_entries  = [(ck, buckets[ck]) for ck in special_cat_keys if buckets.get(ck)]
    if special_entries:
        toc_parts.append('<div class="toc-group-heading">Special</div>')
        for cat_key, cat_items in special_entries:
            slug = cat_key.replace("_", "-")
            toc_parts.append(
                f'<div><a href="#{E(slug)}">{E(_item_category_label(cat_key))}'
                f' <span class="muted">({len(cat_items)})</span></a></div>'
            )

    sidebar = '<aside class="items-toc">' + "".join(toc_parts) + "</aside>"

    body = "<h1>Inaccessible Items</h1>"
    body += (
        f"<p>{len(inaccessible)} items. "
        '<small class="muted">These items exist as blueprints in the module but are '
        "not found in any store, container, or droppable creature inventory — "
        "there is no normal in-game path for players to obtain them.</small></p>"
    )

    for cat_key in ordered_cats:
        items = buckets.get(cat_key, [])
        if not items:
            continue
        slug = cat_key.replace("_", "-")
        cat_label = _item_category_label(cat_key)
        show_base, show_stack = _items_col_flags(items)
        show_ac = cat_key.startswith("armor_")
        show_spell_info = cat_key == "scroll" and any(
            v for v in SPELL_INFO.get("iprp_spells", {}).values() if v
        )
        rows_html = "\n".join(
            _items_row(rr, i, db, show_base, show_stack, ctx, show_ac=show_ac,
                       reason=_inaccessible_reason_html(rr, db),
                       show_spell_info=show_spell_info)
            for rr, i in items
        )
        body += (
            f'<h2 id="{E(slug)}">{E(cat_label)}'
            f' <small class="muted">({len(items)})</small></h2>'
            + _items_table_head(show_base, show_stack, show_ac, show_reason=True,
                                show_spell_info=show_spell_info) + rows_html + "</tbody></table>"
        )

    layout = f'<div class="items-layout">{sidebar}<div class="items-content">{body}</div></div>'
    write_page(out, ctx, "Inaccessible Items", layout)


def item_is_accessible(db: Db, rr: str) -> bool:
    """True when players can reach item ``rr`` (store, container, droppable
    carrier or script reward). Everything else lands on the Inaccessible page."""
    return (
        rr in db.item_sold_at
        or rr in db.item_in_container
        or any(e.get("dropable") or e.get("pickpocketable")
               for e in db.item_carried_by.get(rr, []))
        or rr in db.item_from_script
    )


def _item_access_class(db: Db, rr: str) -> str:
    """Which items index ``rr`` belongs to: "broken", "accessible" or
    "inaccessible".

    The single classifier: render_items_index() splits its three lists with it
    and has_inaccessible_items() polls it, so the Inaccessible page's contents
    and the predicate that gates its nav entry cannot drift apart.  Broken =
    TLK-only or completely unnamed; those get their own bucket and never count
    as inaccessible.
    """
    name = db.item_name(rr)
    if name.startswith("[TLK#") or name == rr:
        return "broken"
    return "accessible" if item_is_accessible(db, rr) else "inaccessible"


def has_inaccessible_items(db: Db) -> bool:
    """True when items/inaccessible/index.html will have any rows.

    Drives both render_inaccessible_index()'s early return and the Inaccessible
    nav entry in SiteChrome, so the nav offers the page exactly when it exists.
    """
    return any(_item_access_class(db, rr) == "inaccessible" for rr in db.items)


def render_items_index(db: Db, out: Path) -> None:
    ctx = PageCtx("items/index.html")

    # Classify every item; broken = TLK-only or completely unnamed.
    buckets: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    broken: list[tuple[str, dict]] = []
    inaccessible: list[tuple[str, dict]] = []

    for rr in sorted(db.items.keys(), key=lambda r: nwn_text(db.item_name(r)).lower()):
        i = db.items[rr]
        name = db.item_name(rr)
        cls = _item_access_class(db, rr)
        if cls == "broken":
            broken.append((rr, i))
        elif cls == "accessible":
            buckets[_item_category(i, nwn_text(name))].append((rr, i))
        else:
            inaccessible.append((rr, i))

    total = sum(len(v) for v in buckets.values()) + len(broken)

    def _item_cost_key(entry: tuple[str, dict]) -> int:
        return item_gp_value(entry[1])

    # Split weapon keys into player weapons and creature-only weapons.
    all_weapon_keys = sorted(
        (k for k in buckets if k.startswith("weapon_")),
        key=lambda k: (baseitem_name(int(k[7:])) or "").lower(),
    )
    player_weapon_keys   = [k for k in all_weapon_keys if int(k[7:]) not in _CREATURE_WEAPON_BASEITEMS]
    creature_weapon_keys = [k for k in all_weapon_keys if int(k[7:]) in _CREATURE_WEAPON_BASEITEMS]

    def _expand(cats: list[str]) -> list[str]:
        result: list[str] = []
        for c in cats:
            if c == "WEAPONS":
                result.extend(player_weapon_keys)
            elif c == "CREATURE_WEAPONS":
                result.extend(creature_weapon_keys)
            else:
                result.append(c)
        return result

    ordered_cats: list[str] = []
    for _, group_cats in _TOC_GROUPS:
        ordered_cats.extend(_expand(group_cats))
    ordered_cats.extend(creature_weapon_keys + ["creature_item"])

    # Sort each bucket: scrolls by (innate_level, spell_name), others by decreasing GP cost.
    for key in buckets:
        if key == "scroll":
            buckets[key].sort(key=lambda e: _scroll_spell_sort_key(e, db))
        else:
            buckets[key].sort(key=_item_cost_key, reverse=True)

    # Table of contents — grouped under headings.
    toc_parts: list[str] = [
        '<div class="toc-group-heading">Views</div>',
        '<div><a href="properties/index.html">Browse by Property</a></div>',
        '<div><a href="search.html">Search Items</a></div>',
    ]
    for group_heading, group_cats_tmpl in _TOC_GROUPS:
        entries = [(ck, buckets[ck]) for ck in _expand(group_cats_tmpl) if buckets.get(ck)]
        if not entries:
            continue
        toc_parts.append(f'<div class="toc-group-heading">{E(group_heading)}</div>')
        for cat_key, cat_items in entries:
            slug = cat_key.replace("_", "-")
            toc_parts.append(
                f'<div><a href="#{E(slug)}">{E(_item_category_label(cat_key))}'
                f' <span class="muted">({len(cat_items)})</span></a></div>'
            )

    special_cat_keys = creature_weapon_keys + (["creature_item"] if buckets.get("creature_item") else [])
    special_entries  = [(ck, buckets[ck]) for ck in special_cat_keys if buckets.get(ck)]
    if special_entries or inaccessible or broken:
        toc_parts.append('<div class="toc-group-heading">Special</div>')
        for cat_key, cat_items in special_entries:
            slug = cat_key.replace("_", "-")
            toc_parts.append(
                f'<div><a href="#{E(slug)}">{E(_item_category_label(cat_key))}'
                f' <span class="muted">({len(cat_items)})</span></a></div>'
            )
        if inaccessible:
            toc_parts.append(
                f'<div><a href="inaccessible/index.html">Inaccessible'
                f' <span class="muted">({len(inaccessible)})</span></a></div>'
            )
        if broken:
            toc_parts.append(
                f'<div><a href="#broken">Potentially Broken'
                f' <span class="muted">({len(broken)})</span></a></div>'
            )

    sidebar = '<aside class="items-toc">' + "".join(toc_parts) + "</aside>"

    body = "<h1>Accessible Items</h1>"
    body += (
        f"<p>{total} items obtainable through normal gameplay — sold in stores, "
        "found in containers, dropped by creatures, or granted by scripts. "
        '<small class="muted">Base item names use stock NWN baseitems.2da; '
        "CEP/HAK overrides are common — the row number shown is authoritative. "
        "Armor subtypes (Cloth/Light/Medium/Heavy) are based on total AC bonus "
        "from item properties.</small></p>"
    )

    for cat_key in ordered_cats:
        items = buckets.get(cat_key, [])
        if not items:
            continue
        slug = cat_key.replace("_", "-")
        cat_label = _item_category_label(cat_key)
        show_base, show_stack = _items_col_flags(items)
        show_ac = cat_key.startswith("armor_")
        show_spell_info = cat_key == "scroll" and any(
            v for v in SPELL_INFO.get("iprp_spells", {}).values() if v
        )
        rows_html = "\n".join(
            _items_row(rr, i, db, show_base, show_stack, ctx, show_ac=show_ac,
                       show_spell_info=show_spell_info)
            for rr, i in items
        )
        body += (
            f'<h2 id="{E(slug)}">{E(cat_label)}'
            f' <small class="muted">({len(items)})</small></h2>'
            + _items_table_head(show_base, show_stack, show_ac,
                                show_spell_info=show_spell_info) + rows_html + "</tbody></table>"
        )

    if broken:
        # Broken items: use the resref as the display name since TLK
        # placeholders like "[TLK#1550]" are meaningless to readers.
        broken_sorted = sorted(broken, key=lambda x: x[0].lower())
        def _broken_row(rr: str, i: dict, show_base: bool, show_stack: bool) -> str:
            cells = [
                f"<td>{link(f'{rr}.html', rr)}</td>",
                f"<td>{E(rr)}</td>",
            ]
            if show_base:
                cells.append(f"<td>{baseitem_label(fld(i, 'BaseItem'))}</td>")
            cells.append(f"<td>{_fmt_cost(fld(i, 'Cost', ''))}</td>")
            if show_stack:
                cells.append(f"<td>{E(fld(i, 'StackSize', ''))}</td>")
            return "<tr>" + "".join(cells) + "</tr>"
        show_base, show_stack = _items_col_flags(broken_sorted)
        rows_html = "\n".join(_broken_row(rr, i, show_base, show_stack) for rr, i in broken_sorted)
        if state.BASE_TLK:
            broken_desc = (
                "These items have unresolvable names even with the base game TLK loaded "
                "— their LocalizedName matches their ResRef, their blueprint was not "
                "found in the NWN install’s BIF archives, or they reference a TLK entry "
                "that could not be resolved. They may be test items, placeholders, or "
                "incomplete module entries. The ResRef is shown as the name."
            )
        else:
            broken_desc = (
                "These items were found only in store inventories and their names "
                "come from the base game’s TLK file, which is not loaded. "
                "They are typically stock NWN items (nw_*, x0_*, x2_*) added "
                "directly to store inventories rather than custom module items. "
                "Re-run with --dialog-tlk to resolve their names. "
                "The ResRef is shown as the name since the TLK string is unavailable."
            )
        body += (
            '<h2 id="broken">Potentially Broken'
            f' <small class="muted">({len(broken)})</small></h2>'
            f'<p class="muted">{broken_desc}</p>'
            + _items_table_head(show_base, show_stack) + rows_html + "</tbody></table>"
        )

    layout = f'<div class="items-layout">{sidebar}<div class="items-content">{body}</div></div>'
    write_page(out, ctx, "Accessible Items", layout)

    # Render the inaccessible items on their own page.
    render_inaccessible_index(db, inaccessible, out)


def render_item_page(db: Db, resref: str, out: Path) -> None:
    i = db.items.get(resref)
    if not i:
        return
    ctx = PageCtx(f"items/{resref}.html")
    name = db.item_name(resref)
    # TLK-placeholder names mean the item's name is not available without the
    # base game TLK file; show the resref as the page title instead.
    is_tlk_broken    = name.startswith("[TLK#")
    is_resref_named  = (not is_tlk_broken) and (name == resref)
    is_broken        = is_tlk_broken or is_resref_named
    display_name     = resref if is_broken else name
    accessible = (
        resref in db.item_sold_at
        or resref in db.item_in_container
        or any(e.get("dropable") or e.get("pickpocketable")
               for e in db.item_carried_by.get(resref, []))
        or resref in db.item_from_script
    )
    is_inaccessible = not is_broken and not accessible
    props = list_items(i.get("PropertiesList"))
    is_cursed = bool(int(fld(i, "Cursed", 0) or 0))
    is_plot   = bool(int(fld(i, "Plot",   0) or 0))
    _bi_raw = fld(i, "BaseItem", None)
    _bi = -1 if _bi_raw is None else int(_bi_raw)
    is_creature_item = _bi in _CREATURE_WEAPON_BASEITEMS or _bi in _CREATURE_ITEM_BASEITEMS
    _carriers = db.item_carried_by.get(resref, [])
    _any_droppable = any(e.get("dropable") for e in _carriers)
    _any_pickpocketable = any(e.get("pickpocketable") for e in _carriers)
    if is_cursed:
        _drop_label = "No"
        _drop_reason = " — cursed"
        _drop_tt = "title=\"Cursed items are stuck in a creature&#39;s inventory and will not appear as loot.\""
    elif not _carriers:
        _drop_label = "—"
        _drop_reason = ""
        _drop_tt = "title=\"This item is not carried by any creature in the module.\""
    elif _any_droppable:
        _drop_label = "Yes"
        _drop_reason = ""
        _drop_tt = "title=\"At least one creature carrying this item has it flagged as droppable (Dropable=1).\""
    else:
        _drop_label = "No"
        _drop_reason = " — not flagged droppable"
        _drop_tt = "title=\"No creature carrying this item has it flagged as droppable. It will not appear in any loot bag.\""
    _cat_slug = _item_category(i, nwn_text(display_name)).replace("_", "-")
    if is_broken:
        _type_href = f"index.html#broken"
    elif is_inaccessible:
        _type_href = f"inaccessible/index.html#{_cat_slug}"
    else:
        _type_href = f"index.html#{_cat_slug}"
    _is_scroll = _bi in _SCROLL_BASEITEMS
    _scroll_spell_lvl, _scroll_spell_cls = (
        _spell_level_classes(_scroll_cast_spell_info(i, nwn_text(display_name))) if _is_scroll else ("", "")
    )
    sections = [
        f"<h1>{nwn_html(display_name)}</h1>",
        '<dl class="meta">',
        f"<dt>ResRef</dt><dd>{E(resref)}</dd>",
        f"<dt>Tag</dt><dd>{E(fld(i, 'Tag', ''))}</dd>",
        f"<dt>Base item</dt><dd>{baseitem_label(fld(i, 'BaseItem'))}</dd>",
        *(
            [
                f"<dt>Spell Level</dt><dd>{E(_scroll_spell_lvl)}</dd>",
                f"<dt>Caster Classes</dt><dd>{E(_scroll_spell_cls)}</dd>",
            ] if _is_scroll and (_scroll_spell_lvl or _scroll_spell_cls) else []
        ),
        f"<dt>Type</dt><dd>{link(_type_href, _item_category_label(_item_category(i, nwn_text(display_name))))}</dd>",
        *(
            (lambda _ac: [
                f"<dt>Base AC</dt><dd>{_ac}</dd>",
                f"<dt>Material</dt><dd>{'Cloth' if _ac <= 0 else 'Leather' if _ac <= 3 else 'Metal'}</dd>",
            ])(_torso_base_ac(i))
            if _bi in _ARMOR_BASEITEMS
            else [f"<dt>Base AC</dt><dd>{int((WEAPONS.get(_bi) or {}).get('BaseAC', 0) or 0)}</dd>"]
            if _bi in SHIELD_BASEITEMS
            else []
        ),
        f"<dt>GP Value</dt><dd>{_fmt_cost(fld(i, 'Cost', ''))}</dd>",
        f"<dt>Stack size</dt><dd>{E(fld(i, 'StackSize', ''))}</dd>",
        *([
            '<dt title="Plot items cannot be sold to merchants, but can be '
            'dropped and looted normally.">Plot item</dt><dd>Yes '
            '<small class="muted">— cannot be sold to merchants; drops/loots '
            'normally</small></dd>'
        ] if is_plot else []),
        f"<dt {_drop_tt}>Drops on death</dt><dd>{_drop_label}"
        f"{'<small class=\"muted\">' + _drop_reason + '</small>' if _drop_reason else ''}</dd>",
        '</dl>',
    ]
    # Variant notices
    _base_rr = db.item_is_variant_of.get(resref)
    if _base_rr:
        _base_name = nwn_text(db.item_name(_base_rr))
        sections.append(
            f'<p class="muted"><em><strong>Property variant</strong> — '
            f'this is an in-world customised version of '
            f'{link(f"{_base_rr}.html", _base_name)} ({E(_base_rr)}) '
            f'with different item properties.</em></p>'
        )
    _variants = db.item_variants_of.get(resref, [])
    if _variants:
        _vlinks = ", ".join(
            link(f"{vrr}.html", nwn_text(db.item_name(vrr)) + f" ({E(vrr)})")
            for vrr in _variants
        )
        sections.append(
            f'<p class="muted"><em><strong>{len(_variants)} property variant(s)</strong> '
            f'of this item exist in-world with different item properties: {_vlinks}</em></p>'
        )
    if is_cursed:
        sections.append(
            '<p class="warn-cursed"><strong>Warning: this item is Cursed.</strong> '
            "If a player acquires this item, it cannot be unequipped, dropped, or sold.</p>"
        )
    if is_broken:
        if is_tlk_broken:
            if state.BASE_TLK:
                _broken_msg = (
                    "This item references a base game TLK entry that could not be "
                    "resolved. It may have an out-of-range StrRef or be an incomplete "
                    "module entry."
                )
            else:
                _broken_msg = (
                    "Item name not available: this item's name is stored in the base "
                    "game TLK file which is not loaded. It is typically a stock NWN "
                    "item (nw_*, x0_*, x2_*) embedded directly in a store inventory."
                )
        else:
            _broken_msg = (
                "This item's display name matches its ResRef — it may be a test item "
                "or placeholder."
            )
        sections.append(f'<p class="muted"><em>{_broken_msg}</em></p>')
    if is_creature_item:
        sections.append(
            '<p class="warn-creature"><strong>Note: this is a Creature-only item.</strong> '
            "Items with creature base types cannot be equipped or used by player characters — "
            "they are intended for use by NPCs and monsters only.</p>"
        )
    if _carriers and not _any_droppable and not is_cursed:
        if _any_pickpocketable:
            sections.append(
                '<p class="warn-no-drop"><strong>Note: this item does not drop on death.</strong> '
                "It is carried by creatures, but none have it flagged as droppable "
                "(Dropable=1). It will not appear in any loot bag.</p>"
                '<p class="note-pickpocket"><strong>This item can be pickpocketed.</strong> '
                "At least one creature carrying it has Pickpocketable set — "
                "see the Carried by table below.</p>"
            )
        else:
            sections.append(
                '<p class="warn-no-drop"><strong>Note: this item does not drop on death.</strong> '
                "It is carried by creatures, but none of them have it flagged as droppable "
                "(Dropable=1 on the item instance). It will not appear in any loot bag.</p>"
            )

    # Prefer the identified description (what players see in-game for identified
    # items, which is nearly all module content); fall back to the unidentified
    # Description only when there is no DescIdentified.
    desc = loc(i.get("DescIdentified")) or loc(i.get("Description"))
    if desc:
        sections.append(f'<p class="desc">{nwn_html(desc)}</p>')

    # Conversations this item triggers via tag-based scripting (a script
    # whose resref equals this item's resref or tag, calling
    # ActionStartConversation with a literal dlg resref).
    item_tag = (fld(i, "Tag", "") or "").lower()
    item_dlgs: list[tuple[str, str]] = []  # (script_resref, dlg_resref)
    for cand in {resref.lower(), item_tag}:
        if cand and cand in db.script_dialogs:
            for d in sorted(db.script_dialogs[cand]):
                item_dlgs.append((cand, d))
    if item_dlgs:
        sections.append("<h2>Triggers conversation</h2><ul>")
        for s, d in item_dlgs:
            tps = db.dialog_teleports.get(d, [])
            tp_note = ""
            if tps:
                dests = sorted({t["area"] for t in tps if t.get("area")})
                if dests:
                    tp_note = " — teleports to " + ", ".join(
                        _area_link(db, a, ctx)
                        for a in dests if a in db.areas)
            sections.append(
                f"<li>{link(f'../conversations/{d}.html', db.dialog_label(d))} "
                f"<code>{E(d)}</code> via script <code>{E(s)}</code>{tp_note}</li>"
            )
        sections.append("</ul>")

    if props:
        sections.append("<h2>Properties</h2>")
        rows = []
        debug_rows = []
        for p in props:
            f = itemprop_format(p)
            pname, subtype = f["property"], f["subtype"]
            # Build links into the Browse by Property index/detail pages.
            # Property cell → index section heading; subtype cell → detail page.
            # Fall back to plain text when pname is unresolved (e.g. "Property #7").
            _pname_known = pname and not pname.startswith("Property #")
            _subtype_real = subtype and not _is_raw_subtype(subtype)
            cost_str = f["cost"]
            if _pname_known:
                _idx_anch = f"properties/index.html#pn-{_prop_slug(pname, '')}"
                _detail_slug = _prop_slug(pname, subtype if _subtype_real else "")
                _combined_page = _COMBINED_PROP_PAGES.get(pname)
                if _combined_page:
                    _spell_frag = f"#{_detail_slug}" if _subtype_real else ""
                    _detail_href = f"properties/{_combined_page}.html{_spell_frag}"
                    # Cost links to the same spell section (no tier-based anchor).
                    cost_cell = link(_detail_href, cost_str) if cost_str else ""
                else:
                    _detail_href = f"properties/{_detail_slug}.html"
                    if cost_str:
                        _cost_anch = _cost_anchor(cost_str)
                        _cost_href = f"{_detail_href}#{_cost_anch}" if _cost_anch else _detail_href
                        cost_cell = link(_cost_href, cost_str)
                    else:
                        cost_cell = ""
                if _subtype_real:
                    pname_cell = colorize_damage_words(link(_idx_anch, pname))
                    subtype_cell = colorize_damage_words(link(_detail_href, subtype))
                else:
                    pname_cell = colorize_damage_words(link(_detail_href, pname))
                    subtype_cell = colorize_damage_words(E(subtype))
            else:
                pname_cell = colorize_damage_words(E(pname))
                subtype_cell = colorize_damage_words(E(subtype))
                cost_cell = E(cost_str)
            _param_raw = f["param"]
            if f["property"] == "Light" and _param_raw:
                _pcls = f"nwn-light-color nwn-light-{_param_raw.lower()}"
                param_cell = f'<span class="{_pcls}">{E(_param_raw)}</span>'
            else:
                param_cell = E(_param_raw)
            rows.append(
                f"<tr><td>{pname_cell}</td>"
                f"<td>{subtype_cell}</td>"
                f"<td>{cost_cell}</td>"
                f"<td>{param_cell}</td>"
                f"<td>{E(f['chance'])}</td></tr>"
            )
            debug_rows.append(
                f"<tr><td>{E(fld(p, 'PropertyName', ''))}</td>"
                f"<td>{E(fld(p, 'Subtype', ''))}</td>"
                f"<td>{E(fld(p, 'CostTable', ''))}</td>"
                f"<td>{E(fld(p, 'CostValue', ''))}</td>"
                f"<td>{E(fld(p, 'Param1', ''))}/{E(fld(p, 'Param1Value', ''))}</td>"
                f"<td>{E(fld(p, 'ChanceAppear', ''))}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Property</th><th>Subtype</th><th>Value</th>"
            "<th>Param</th><th>Chance %</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )
        # Raw values, collapsed by default — handy when the human label is
        # missing or wrong because of a custom 2DA.
        sections.append(
            "<details><summary>Raw values</summary>"
            '<table class="data"><thead><tr>'
            "<th>PropertyName</th><th>Subtype</th><th>CostTable</th><th>CostValue</th>"
            "<th>Param1/Val</th><th>Chance</th>"
            "</tr></thead><tbody>" + "\n".join(debug_rows) + "</tbody></table>"
            "</details>"
        )

    # Where to find this item
    sold_at = db.item_sold_at.get(resref, [])
    in_containers = db.item_in_container.get(resref, [])
    carried_by = db.item_carried_by.get(resref, [])
    from_script = db.item_from_script.get(resref, [])

    if sold_at or in_containers or carried_by or from_script:
        sections.append("<h2>Where to find</h2>")
        if sold_at:
            rows = []
            for s in sold_at:
                area_rr = s["area_rr"]
                area_cell = (_area_link(db, area_rr, ctx)
                             if area_rr in db.areas else E(area_rr))
                store_cell = link(f"../stores/{s['slug']}.html", s["name"])
                rows.append(f"<tr><td>{store_cell}</td><td>{area_cell}</td></tr>")
            sections.append(
                "<h3>Sold at</h3>"
                '<table class="data"><thead><tr><th>Store</th><th>Area</th></tr></thead>'
                "<tbody>" + "\n".join(rows) + "</tbody></table>"
            )
        if in_containers:
            rows = []
            for c in in_containers:
                area_rr = c["area_rr"]
                area_cell = (_area_link(db, area_rr, ctx)
                             if area_rr in db.areas else E(area_rr))
                href = f"../containers/{area_rr}-{c['idx']:03d}.html"
                lock_note = ""
                if c["locked"]:
                    lock_note = f' (locked, DC {c["dc"]})' if c["dc"] else " (locked)"
                rows.append(
                    f"<tr><td>{link(href, c['pname'])}{E(lock_note)}</td>"
                    f"<td>{area_cell}</td></tr>"
                )
            sections.append(
                "<h3>Found in containers</h3>"
                '<table class="data"><thead><tr><th>Container</th><th>Area</th></tr></thead>'
                "<tbody>" + "\n".join(rows) + "</tbody></table>"
            )
        if carried_by:
            rows = []
            for c in carried_by:
                area_rr = c["area_rr"]
                area_cell = (_area_link(db, area_rr, ctx)
                             if area_rr in db.areas else E(area_rr))
                crr = c["crr"]
                creature_cell = (_creature_link(db, crr, ctx, c["cname"])
                                 if crr in db.canonical_creatures else E(c["cname"]))
                drops = c.get("dropable", False)
                drop_cell = "Yes" if drops else '<span class="muted">No</span>'
                pp = c.get("pickpocketable", False)
                pp_cell = "Yes" if pp else '<span class="muted">No</span>'
                rows.append(
                    f"<tr><td>{creature_cell}</td><td>{area_cell}</td>"
                    f"<td>{drop_cell}</td><td>{pp_cell}</td></tr>"
                )
            sections.append(
                "<h3>Carried by</h3>"
                '<table class="data"><thead><tr>'
                '<th>Creature</th><th>Area</th>'
                '<th title="Whether this item appears in the loot bag when the creature is killed (Dropable=1 on the item instance)">Drops on death</th>'
                '<th title="Whether this item can be stolen via Pickpocket (Pickpocketable=1 on the item instance)">Pickpocketable</th>'
                "</tr></thead>"
                "<tbody>" + "\n".join(rows) + "</tbody></table>"
            )
        if from_script:
            rows = []
            for src in from_script:
                kind = src["kind"]
                script_rr = src["script"]
                areas = src.get("areas") or []
                area_cells = ", ".join(
                    _area_link(db, a, ctx)
                    for a in areas if a in db.areas
                ) or "—"
                if kind == "module-event":
                    source_cell = E(f"Module event ({src['label']})")
                elif kind == "dialog-action":
                    dlg = src.get("dlg") or ""
                    dlg_label = db.dialog_label(dlg) if dlg else script_rr
                    source_cell = (link(f"../conversations/{dlg}.html", dlg_label)
                                   if dlg in db.dialogs else E(dlg_label))
                elif kind == "creature-event":
                    crr = src.get("crr") or ""
                    can_crr = db.canonical_for_bp.get(crr, crr) if crr else ""
                    cname = db.canonical_creature_name(can_crr) if can_crr else script_rr
                    creature_html = (_creature_link(db, can_crr, ctx, cname)
                                     if can_crr in db.canonical_creatures else E(cname))
                    source_cell = f"{creature_html} <small class=\"muted\">({E(src['label'])})</small>"
                else:  # placeable-event
                    prr = src.get("prr") or ""
                    pname = src.get("label") or prr or script_rr
                    source_cell = E(pname)
                rows.append(
                    f"<tr><td>{source_cell}</td>"
                    f"<td><code>{E(script_rr)}</code></td>"
                    f"<td>{area_cells}</td></tr>"
                )
            sections.append(
                "<h3>Quest / script rewards</h3>"
                '<table class="data"><thead><tr>'
                "<th>Source</th><th>Script</th><th>Area(s)</th>"
                "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
            )
    elif not is_broken:
        sections.append(
            "<h2>Where to find</h2>"
            '<p class="muted">Not found in any store, container, creature inventory, '
            "or script reward — this item may be inaccessible to players.</p>"
        )

    key_for = db.item_is_key_for.get(resref, [])
    if key_for:
        has_dst = any(kf.get("dst_area") for kf in key_for)
        rows = []
        for kf in key_for:
            area_rr = kf["area_rr"]
            area_cell = (_area_link(db, area_rr, ctx)
                         if area_rr in db.areas else E(area_rr))
            if kf["kind"] == "container":
                obj_cell = link(
                    f"../containers/{area_rr}-{kf['idx']:03d}.html", kf["name"]
                )
            else:
                obj_cell = E(kf["name"])
            required = kf.get("required", True)
            type_label = kf["kind"].capitalize() + ("" if required else " (optional)")
            row = (f"<tr><td>{obj_cell}</td>"
                   f"<td>{E(type_label)}</td>"
                   f"<td>{area_cell}</td>")
            if has_dst:
                dst_rr = kf.get("dst_area")
                if dst_rr:
                    dst_cell = (_area_link(db, dst_rr, ctx)
                                if dst_rr in db.areas else E(dst_rr))
                else:
                    dst_cell = ""
                row += f"<td>{dst_cell}</td>"
            row += "</tr>"
            rows.append(row)
        headers = "<th>Opens</th><th>Type</th><th>Area</th>"
        if has_dst:
            headers += "<th>Leads to</th>"
        sections.append(
            "<h2>Key for</h2>"
            f'<table class="data"><thead><tr>{headers}'
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    script_checks = db.item_script_checks.get(resref, [])
    if script_checks:
        rows = []
        for sc in script_checks:
            script_rr = sc["script"]
            script_cell = (link(f"../scripts/{script_rr}.html", script_rr)
                           if script_rr in db.script_paths else
                           f"<code>{E(script_rr)}</code>")
            event_cell = E(sc.get("event", ""))
            kind = sc.get("kind", "")
            if kind == "area-event":
                ctx_cell = "Area event"
            elif kind in ("dialog-action", "dialog-condition"):
                dlg = sc.get("dialog", "")
                label = db.dialog_label(dlg) if dlg else dlg
                suffix = "" if kind == "dialog-action" else " (condition)"
                ctx_cell = (link(f"../conversations/{dlg}.html", label + suffix)
                            if dlg in db.dialogs else E(f"Dialog: {dlg}"))
            elif kind in ("trigger", "trigger-instance"):
                tag = sc.get("tag") or sc.get("resref") or ""
                ctx_cell = E(f"Trigger ({tag})" if tag else "Trigger")
            elif kind in ("placeable", "placeable-instance"):
                ctx_cell = E(sc.get("name") or sc.get("resref") or "Placeable")
            elif kind in ("door", "door-instance"):
                ctx_cell = E(sc.get("name") or sc.get("resref") or "Door")
            else:
                ctx_cell = E(kind)
            areas = sc.get("areas") or []
            area_cells = ", ".join(
                _area_link(db, a, ctx)
                for a in areas if a in db.areas
            ) or "—"
            rows.append(
                f"<tr><td>{script_cell}</td><td>{event_cell}</td>"
                f"<td>{ctx_cell}</td><td>{area_cells}</td></tr>"
            )
        sections.append(
            "<h2>Script checks</h2>"
            '<p class="muted">This item\'s tag is checked via '
            "<code>GetItemPossessedBy</code> in these scripts:</p>"
            '<table class="data"><thead><tr>'
            "<th>Script</th><th>Event</th><th>Context</th><th>Area(s)</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # ---- Related quests ----
    # Collect quests from every dialog and script that checks this item's tag.
    item_quest_map: dict[str, set[int]] = defaultdict(set)  # q_tag → entry_ids
    for sc in db.item_script_checks.get(resref, []):
        dlg = sc.get("dialog", "")
        if dlg:
            for q_tag, eids in db.dialog_quest_grants_rev.get(dlg, {}).items():
                item_quest_map[q_tag].update(eids)
        else:
            script_rr = sc.get("script", "")
            for q_tag, eid_map in db.quest_grants.items():
                for eid, script_set in eid_map.items():
                    if script_rr in script_set:
                        item_quest_map[q_tag].add(eid)
    if item_quest_map:
        rows = []
        for q_tag in sorted(item_quest_map,
                            key=lambda t: db.quest_tag_to_info.get(t, (t, ""))[0].lower()):
            q_name, q_slug = db.quest_tag_to_info.get(q_tag, (q_tag, ""))
            eids = sorted(item_quest_map[q_tag])
            step_str = ", ".join(str(e) for e in eids)
            quest_link = (link(f"../quests/{q_slug}.html", q_name)
                          if q_slug else E(q_name))
            rows.append(f"<tr><td>{quest_link}</td><td><code>{E(q_tag)}</code></td>"
                        f"<td>{E(step_str)}</td></tr>")
        sections.append(
            "<h2>Related quests</h2>"
            '<p class="muted">This item is checked in scripts that grant entries '
            "in these quests:</p>"
            '<table class="data"><thead><tr>'
            "<th>Quest</th><th>Tag</th><th>Step(s)</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    write_page(out, ctx, name, "\n".join(sections))
