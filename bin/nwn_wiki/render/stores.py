"""Store rendering for the wiki.

The stores index, the per-blueprint store page and the per-placement store
instance page, plus the shared buy/sell summary, inventory and opener helpers
the area and creature pages reuse.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from nwn_wiki.db.index import _store_instance_slug
from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.chrome import page, write
from nwn_wiki.htmlgen.escape import E, nwn_html, nwn_text
from nwn_wiki.htmlgen.links import link
from nwn_wiki.items import (
    _CREATURE_WEAPON_BASEITEMS,
    _TOC_GROUPS,
    _item_category,
    _item_category_label,
    item_gp_value,
)
from nwn_wiki.lookups import SPELL_INFO, STOCK_BASEITEMS, baseitem_name
from nwn_wiki.render.conversations import _caller_html
from nwn_wiki.render.items import _items_col_flags, _items_row, _items_table_head


def _buy_limit_str(mbp: Any) -> str:
    if mbp is None:
        return "—"
    if mbp == -1:
        return "No limit"
    if mbp == 0:
        return "Won't buy"
    return f"{mbp:,} gp"


def _store_buy_base_types(store: dict, field: str) -> list[int]:
    """BaseItem rows listed under a store's WillOnlyBuy / WillNotBuy list."""
    out: list[int] = []
    for s in list_items(store.get(field)):
        bi = fld(s, "BaseItem")
        if bi is not None:
            out.append(int(bi))
    return out


def _store_buy_summary(store: dict) -> tuple[str, bool]:
    """Describe which item types a store will buy from the player.

    Returns (html_label, buys_anything). NWN store buy rules:
      * WillOnlyBuy non-empty → store buys ONLY those base item types.
      * else WillNotBuy non-empty → buys everything EXCEPT those types.
      * else → buys all types.

    Builders make a store refuse all sales by pointing WillOnlyBuy at an
    out-of-range sentinel base item (a row absent from stock baseitems.2da, e.g.
    309) that no real merchandise uses. When every WillOnlyBuy row is such a
    sentinel the store buys nothing, so buys_anything is False and callers show
    N/A for the buy-back rate rather than a misleading percentage."""
    def _labels(rows: list[int]) -> str:
        return ", ".join(E(baseitem_name(r) or f"BaseItem #{r}")
                         for r in sorted(set(rows)))

    only = _store_buy_base_types(store, "WillOnlyBuy")
    if only:
        if not any(r in STOCK_BASEITEMS for r in only):
            return ('<em class="muted">Nothing</em>', False)
        return (f"Only: {_labels(only)}", True)
    nope = _store_buy_base_types(store, "WillNotBuy")
    if nope:
        return (f"All except: {_labels(nope)}", True)
    return ("All types", True)


_CREATURE_STORES_TABLE_HEAD = (
    '<table class="data"><thead><tr>'
    "<th>Name</th><th>Items</th><th>Area</th>"
    '<th title="Price store charges you to buy items (% of base)">Sells At</th>'
    '<th title="Item types the store will buy from you">Buys</th>'
    '<th title="Price store pays you for items (% of base); N/A if the store buys nothing">Buys At</th>'
    '<th title="Maximum the store will pay for any single item">Max Buy</th>'
    '<th title="Highest base item value in stock (before markup)">Max GP</th>'
    '<th title="Average base item value in stock (before markup)">Avg GP</th>'
    "</tr></thead><tbody>"
)


def _creature_store_section(db: "Db", creature_resref: str,
                             filter_area: str | None, root_rel: str) -> str:
    """Return an HTML <h2>Stores</h2> + table for stores this creature opens,
    or empty string if none found. filter_area restricts to one area (instance
    pages); None returns all areas (blueprint pages)."""
    rows: list[str] = []
    seen_slugs: set[str] = set()

    for area_rr, store_list in sorted(
        db.area_stores.items(),
        key=lambda kv: nwn_text(db.area_name(kv[0])).lower(),
    ):
        if filter_area and area_rr != filter_area:
            continue
        if area_rr in db.hidden_areas:
            continue
        for inst in store_list:
            tag = fld(inst, "Tag", "")
            if not tag:
                continue
            openers = db.store_tag_openers.get(tag.lower(), [])
            matched = any(
                o.get("resref", "").lower() == creature_resref.lower()
                and area_rr in (o.get("areas") or (
                    [o["area"]] if "area" in o else []
                ))
                for o in openers
            )
            if not matched:
                continue

            slug = _store_instance_slug(area_rr, inst)
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            rr = fld(inst, "ResRef", "")
            name = db.store_name(rr) if rr in db.stores else (tag or rr)
            name_cell = link(f"{root_rel}/stores/{slug}.html", name)

            pages = list_items(inst.get("StoreList"))
            n_items = sum(len(list_items(p.get("ItemList"))) for p in pages)

            area_cell = link(f"{root_rel}/areas/{area_rr}.html", db.area_name(area_rr))

            mu_val = fld(inst, "MarkUp", None)
            md_val = fld(inst, "MarkDown", None)
            buy_html, buys_any = _store_buy_summary(inst)
            mu_str = f"{mu_val}%" if mu_val is not None else "—"
            md_str = (f"{md_val}%" if md_val is not None else "—") if buys_any else "N/A"
            mbp_raw = fld(inst, "MaxBuyPrice", None)

            max_gp, _, _, avg_gp = _store_item_gp_stats(db, pages)
            max_gp_str = f"{max_gp:,}" if max_gp > 0 else "—"
            avg_gp_str = f"{avg_gp:,.0f}" if avg_gp > 0 else "—"
            rows.append(
                f"<tr>"
                f"<td>{name_cell}</td>"
                f"<td>{n_items}</td>"
                f"<td>{area_cell}</td>"
                f"<td>{mu_str}</td>"
                f"<td>{buy_html}</td>"
                f"<td>{md_str}</td>"
                f"<td>{'N/A' if not buys_any else _buy_limit_str(mbp_raw)}</td>"
                f"<td>{max_gp_str}</td>"
                f"<td>{avg_gp_str}</td>"
                f"</tr>"
            )

    if not rows:
        return ""
    return (
        "<h2>Stores</h2>"
        + _CREATURE_STORES_TABLE_HEAD
        + "\n".join(rows)
        + "</tbody></table>"
    )


_STORE_TABLE_HEAD = (
    '<table class="data"><thead><tr>'
    "<th>Name</th><th>ResRef</th><th>Tag</th><th>Items</th>"
    "<th>Area</th>"
    '<th title="Price store charges you to buy items (% of base)">Sells At</th>'
    '<th title="Price store pays you for items (% of base)">Buys At</th>'
    '<th title="Maximum the store will pay for any single item">Max Buy</th>'
    "</tr></thead><tbody>"
)


def _store_opener_html(db: Db, opener: dict, root_rel: str = "..") -> str:
    """Compact one-liner describing what opens a store, for table cells."""
    kind = opener.get("kind", "")
    rr = opener.get("resref", "")
    via_dlg = opener.get("via_dialog", "")

    if kind in ("creature", "creature-instance", "creature-event"):
        if kind == "creature-instance":
            area_rr = opener.get("area", "")
            idx = opener.get("idx", 0)
            can_rr = db.canonical_for_inst.get((area_rr, idx), rr)
            name = db.canonical_creature_name(can_rr) if can_rr else (rr or "(NPC)")
            url = f"{root_rel}/creatures/{can_rr}.html" if can_rr else "#"
            npc_html = f'<a href="{E(url)}">{nwn_html(name)}</a>'
        else:
            can_rr = db.canonical_for_bp.get(rr, rr) if rr else rr
            name = db.canonical_creature_name(can_rr) if can_rr in db.canonical_creatures else (rr or "NPC")
            npc_html = (link(f"{root_rel}/creatures/{can_rr}.html", name)
                        if can_rr in db.canonical_creatures else nwn_html(name))
        if via_dlg and via_dlg in db.dialogs:
            conv = link(f"{root_rel}/conversations/{via_dlg}.html", "conversation")
            return f"NPC {npc_html} (via {conv})"
        return f"NPC {npc_html}"

    if kind in ("trigger-event", "trigger-event-instance"):
        inst_tag = opener.get("tag") or rr or "trigger"
        areas = opener.get("areas", [])
        area_html = ""
        if areas and areas[0] in db.areas:
            area_html = " in " + link(f"{root_rel}/areas/{areas[0]}.html",
                                      db.area_name(areas[0]))
        return f'Trigger <code>{E(inst_tag)}</code>{area_html}'

    if kind in ("placeable-event", "placeable-event-instance"):
        inst_tag = opener.get("tag") or rr or "placeable"
        return f'Placeable <code>{E(inst_tag)}</code>'

    # Fall back to full caller html for any other kind.
    return _caller_html(db, opener, root_rel=root_rel)


def _store_inventory_html(db: "Db", pages: list, item_prefix: str = "../items/") -> str:
    """Render store inventory grouped by item category (same scheme as the items index)."""
    all_items: list[tuple[str, dict]] = []
    for p in pages:
        for it in list_items(p.get("ItemList")):
            irr = (fld(it, "TemplateResRef", "")
                   or fld(it, "InventoryRes", "")
                   or fld(it, "EquippedRes", "") or "").strip()
            if not irr:
                continue
            enriched = db.items.get(irr, it)
            all_items.append((irr, enriched))

    if not all_items:
        return '<p class="muted">No items.</p>'

    buckets: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for irr, enriched in all_items:
        cat_key = _item_category(enriched, nwn_text(db.item_name(irr)))
        buckets[cat_key].append((irr, enriched))

    def _cost_key(entry: tuple[str, dict]) -> int:
        return item_gp_value(entry[1])

    for items in buckets.values():
        items.sort(key=_cost_key, reverse=True)

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

    ordered: list[str] = []
    seen: set[str] = set()
    for _, group_cats in _TOC_GROUPS:
        for k in _expand(group_cats):
            if k not in seen:
                ordered.append(k)
                seen.add(k)
    for k in buckets:
        if k not in seen:
            ordered.append(k)
            seen.add(k)

    parts: list[str] = []
    for cat_key in ordered:
        items = buckets.get(cat_key)
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
            _items_row(rr, i, db, show_base, show_stack, prefix=item_prefix, show_ac=show_ac,
                       show_spell_info=show_spell_info)
            for rr, i in items
        )
        parts.append(
            f'<h3 id="{E(slug)}">{E(cat_label)} <small class="muted">({len(items)})</small></h3>'
            + _items_table_head(show_base, show_stack, show_ac,
                                show_spell_info=show_spell_info) + rows_html + "</tbody></table>"
        )

    return "\n".join(parts)


def _store_inventory_toc_html(db: "Db", pages: list) -> str:
    """Sidebar TOC for a store instance page. Returns empty string when store has no items."""
    all_items: list[tuple[str, dict]] = []
    for p in pages:
        for it in list_items(p.get("ItemList")):
            irr = (fld(it, "TemplateResRef", "")
                   or fld(it, "InventoryRes", "")
                   or fld(it, "EquippedRes", "") or "").strip()
            if not irr:
                continue
            all_items.append((irr, db.items.get(irr, it)))

    if not all_items:
        return ""

    buckets: dict[str, list] = defaultdict(list)
    for irr, enriched in all_items:
        buckets[_item_category(enriched, nwn_text(db.item_name(irr)))].append(irr)

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

    toc_parts: list[str] = []
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

    special_keys = creature_weapon_keys + (["creature_item"] if buckets.get("creature_item") else [])
    if special_keys:
        toc_parts.append('<div class="toc-group-heading">Special</div>')
        for cat_key in special_keys:
            if not buckets.get(cat_key):
                continue
            slug = cat_key.replace("_", "-")
            toc_parts.append(
                f'<div><a href="#{E(slug)}">{E(_item_category_label(cat_key))}'
                f' <span class="muted">({len(buckets[cat_key])})</span></a></div>'
            )

    if not toc_parts:
        return ""
    return '<aside class="items-toc">' + "".join(toc_parts) + "</aside>"


def _store_item_gp_stats(db: "Db", pages: list) -> tuple[int, str, str, float]:
    """Scan store inventory pages; return (max_cost, max_irr, max_name, avg_cost).
    Only items with cost > 0 are counted. avg_cost is 0.0 when none qualify."""
    max_cost = 0
    max_irr = ""
    max_name = ""
    total = 0
    count = 0
    for p in pages:
        for it in list_items(p.get("ItemList")):
            cost = item_gp_value(it)
            if cost <= 0:
                continue
            count += 1
            total += cost
            if cost > max_cost:
                max_cost = cost
                max_irr = (fld(it, "TemplateResRef", "")
                           or fld(it, "InventoryRes", "")
                           or fld(it, "EquippedRes", "") or "")
                max_name = loc(it.get("LocalizedName")) or ""
                if not max_name and max_irr in db.items:
                    max_name = db.item_name(max_irr)
                if not max_name:
                    max_name = max_irr or "(unknown)"
    return max_cost, max_irr, max_name, (total / count if count else 0.0)


def render_store_instance_page(db: Db, area_rr: str, inst: dict, out: Path) -> None:
    """Render a per-placement store page showing the instance's actual inventory."""
    rr = fld(inst, "ResRef", "") or ""
    tag = fld(inst, "Tag", "") or rr
    slug = _store_instance_slug(area_rr, inst)

    bp_name = db.store_name(rr) if rr in db.stores else ""
    name = bp_name or nwn_text(loc(inst.get("LocName"))) or tag or rr

    sections = [f"<h1>{nwn_html(name)}</h1>"]

    # Location and blueprint metadata
    area_link = (link(f"../areas/{area_rr}.html", db.area_name(area_rr))
                 if area_rr in db.areas else E(area_rr))
    meta = [
        f"<dt>Area</dt><dd>{area_link}</dd>",
        f"<dt>Tag</dt><dd><code>{E(tag)}</code></dd>",
        f"<dt>ResRef (blueprint)</dt><dd>"
        + (link(f"{rr}.html", rr) if rr in db.stores else E(rr))
        + "</dd>",
    ]
    mu = fld(inst, "MarkUp", None)
    md = fld(inst, "MarkDown", None)
    buy_html, buys_any = _store_buy_summary(inst)
    if mu is not None or md is not None:
        md_disp = (f'{md}%' if md is not None else '—') if buys_any else "N/A"
        meta.append(
            f"<dt>Sells At / Buys At</dt><dd>"
            f"{f'{mu}%' if mu is not None else '—'} / {md_disp}</dd>"
        )
    meta.append(f"<dt>Buys</dt><dd>{buy_html}</dd>")
    mbp = fld(inst, "MaxBuyPrice", None)
    if mbp is not None:
        meta.append(f"<dt>Max Buy Price</dt><dd>{'N/A' if not buys_any else _buy_limit_str(mbp)}</dd>")
    inst_pages = list_items(inst.get("StoreList"))
    max_gp, max_irr, max_name, avg_gp = _store_item_gp_stats(db, inst_pages)
    if max_gp > 0:
        max_item_html = (link(f"../items/{max_irr}.html", max_name)
                         if max_irr in db.items else E(max_name))
        meta.append(f"<dt>Most Valuable Item</dt><dd>{max_item_html} ({max_gp:,} gp)</dd>")
        meta.append(f"<dt>Avg Item Value</dt><dd>{avg_gp:,.0f} gp</dd>")
    sections.extend(['<dl class="meta">', *meta, "</dl>"])

    # Opened By
    openers = db.store_tag_openers.get(tag.lower(), [])
    # Only show openers whose area matches this store instance. All store-opening
    # scripts use GetNearestObjectByTag (area-local), so cross-area openers with
    # the same tag are opening a different instance, not this one.
    display_openers = [o for o in openers if area_rr in o.get("areas", []) or
                       o.get("area") == area_rr]
    if display_openers:
        sections.append("<h2>Opened by</h2><ul>")
        seen_html: set[str] = set()
        for o in display_openers:
            h = _store_opener_html(db, o, root_rel="..")
            if h not in seen_html:
                seen_html.add(h)
                sections.append(f"<li>{h}</li>")
        sections.append("</ul>")
    else:
        sections.append(
            "<h2>Opened by</h2>"
            '<p class="muted">No opener found in static analysis — '
            "may be opened from a runtime-only code path, or may be inaccessible.</p>"
        )

    # Instance inventory with floating sidebar TOC
    inv_toc = _store_inventory_toc_html(db, inst_pages)
    inv_body = "<h2>Inventory</h2>" + _store_inventory_html(db, inst_pages)
    if inv_toc:
        inv_section = (
            '<div class="items-layout">'
            + inv_toc
            + f'<div class="items-content">{inv_body}</div>'
            + '</div>'
        )
    else:
        inv_section = inv_body
    sections.append(inv_section)

    write(out / "stores" / f"{slug}.html",
          page(name, "\n".join(sections), root_rel=".."))


def render_stores_index(db: Db, out: Path) -> None:
    # The GIT instance is the authoritative source: each placed store can have
    # different MarkUp/MarkDown/MaxBuyPrice/inventory from the UTM blueprint.
    # NWN semantics: MarkUp = % of base the store charges you (Sells At, usually >100)
    #                MarkDown = % of base the store pays you  (Buys At,  usually <100)

    placed_rows: list[tuple[str, str]] = []      # (sort_key, html_row) — has opener
    no_opener_rows: list[tuple[str, str]] = []   # (sort_key, html_row) — no opener found
    placed_resrefs: set[str] = set()

    # Track the single most valuable item across all accessible stores.
    gm_cost: int = 0          # highest item base cost found so far
    gm_irr: str = ""          # item resref
    gm_item_name: str = ""    # item display name
    gm_store_name: str = ""   # store display name
    gm_slug: str = ""         # store instance slug (for link)
    gm_area_rr: str = ""      # area resref (for link)

    _IDX_TABLE_HEAD = (
        '<table class="data"><thead><tr>'
        "<th>Name</th>"
        # "<th>ResRef</th><th>Tag</th>"
        "<th>Items</th>"
        "<th>Area</th><th>Opened By</th>"
        '<th title="Price store charges you to buy items (% of base)">Sells At</th>'
        '<th title="Item types the store will buy from you">Buys</th>'
        '<th title="Price store pays you for items (% of base); N/A if the store buys nothing">Buys At</th>'
        '<th title="Maximum the store will pay for any single item">Max Buy</th>'
        '<th title="Highest base item value in stock (before markup)">Max GP</th>'
        '<th title="Average base item value in stock (before markup)">Avg GP</th>'
        "</tr></thead><tbody>"
    )

    for area_rr in sorted(db.area_stores.keys(),
                          key=lambda a: nwn_text(db.area_name(a)).lower()):
        if area_rr in db.hidden_areas:
            continue
        for inst in db.area_stores[area_rr]:
            rr = fld(inst, "ResRef", "")
            tag = fld(inst, "Tag", "")
            placed_resrefs.add(rr)

            name = db.store_name(rr) if rr in db.stores else (tag or rr)
            slug = _store_instance_slug(area_rr, inst)
            name_cell = link(f"{slug}.html", name)

            pages = list_items(inst.get("StoreList"))
            n_items = sum(len(list_items(p.get("ItemList"))) for p in pages)

            mu_val = fld(inst, "MarkUp", None)
            md_val = fld(inst, "MarkDown", None)
            buy_html, buys_any = _store_buy_summary(inst)
            mu_str = f"{mu_val}%" if mu_val is not None else "—"
            md_str = (f"{md_val}%" if md_val is not None else "—") if buys_any else "N/A"

            area_cell = link(f"../areas/{area_rr}.html", db.area_name(area_rr))

            # Only show openers in the same area. All store-opening scripts use
            # GetNearestObjectByTag (area-local), so a cross-area NPC with the
            # same tag is opening a different instance, not this one.
            all_openers = db.store_tag_openers.get(tag.lower(), [])
            display_openers = [o for o in all_openers
                               if area_rr in o.get("areas", []) or o.get("area") == area_rr]
            if display_openers:
                seen_html: set[str] = set()
                opener_parts = []
                for o in display_openers[:3]:  # cap at 3 in index for readability
                    h = _store_opener_html(db, o, root_rel="..")
                    if h not in seen_html:
                        seen_html.add(h)
                        opener_parts.append(h)
                opener_cell = "<br>".join(opener_parts)
                if len(display_openers) > 3:
                    opener_cell += f" <em class=\"muted\">(+{len(display_openers)-3} more)</em>"
            else:
                opener_cell = '<em class="muted">unknown</em>'

            mbp_raw = fld(inst, 'MaxBuyPrice', None)
            max_gp, max_irr, max_item_name, avg_gp = _store_item_gp_stats(db, pages)
            max_gp_str = f"{max_gp:,}" if max_gp > 0 else "—"
            avg_gp_str = f"{avg_gp:,.0f}" if avg_gp > 0 else "—"
            row = (
                f"<tr>"
                f"<td>{name_cell}</td>"
                # f"<td>{E(rr)}</td>"
                # f"<td>{E(tag)}</td>"
                f"<td>{n_items}</td>"
                f"<td>{area_cell}</td>"
                f"<td>{opener_cell}</td>"
                f"<td>{mu_str}</td>"
                f"<td>{buy_html}</td>"
                f"<td>{md_str}</td>"
                f"<td>{'N/A' if not buys_any else _buy_limit_str(mbp_raw)}</td>"
                f"<td>{max_gp_str}</td>"
                f"<td>{avg_gp_str}</td>"
                f"</tr>"
            )
            mbp_sort = float('inf') if mbp_raw == -1 else (mbp_raw if mbp_raw is not None else 0)
            md_sort = md_val if md_val is not None else 0
            sort_key = (-mbp_sort, -md_sort, nwn_text(name).lower())
            if display_openers:
                placed_rows.append((sort_key, row))
                if max_gp > gm_cost:
                    gm_cost = max_gp
                    gm_irr = max_irr
                    gm_item_name = max_item_name
                    gm_store_name = name
                    gm_slug = slug
                    gm_area_rr = area_rr
            else:
                no_opener_rows.append((sort_key, row))

    placed_rows.sort(key=lambda x: x[0])
    no_opener_rows.sort(key=lambda x: x[0])

    # UTM blueprints that were never placed in any area
    unplaced_rows: list[tuple] = []
    for rr in db.stores.keys():
        if rr in placed_resrefs:
            continue
        s = db.stores[rr]
        pages = list_items(s.get("StoreList"))
        n_items = sum(len(list_items(p.get("ItemList"))) for p in pages)
        mu_val = fld(s, "MarkUp", None)
        md_val = fld(s, "MarkDown", None)
        buy_html, buys_any = _store_buy_summary(s)
        mbp_raw = fld(s, 'MaxBuyPrice', None)
        mbp_sort = float('inf') if mbp_raw == -1 else (mbp_raw if mbp_raw is not None else 0)
        md_sort = md_val if md_val is not None else 0
        u_max_gp, _, _, u_avg_gp = _store_item_gp_stats(db, pages)
        unplaced_rows.append((
            (-mbp_sort, -md_sort, nwn_text(db.store_name(rr)).lower()),
            f"<tr>"
            f"<td>{link(f'{rr}.html', db.store_name(rr))}</td>"
            # f"<td>{E(rr)}</td>"
            # f"<td>{E(fld(s, 'Tag', ''))}</td>"
            f"<td>{n_items}</td>"
            f"<td><em>Not placed</em></td>"
            f"<td>—</td>"
            f"<td>{f'{mu_val}%' if mu_val is not None else '—'}</td>"
            f"<td>{buy_html}</td>"
            f"<td>{(f'{md_val}%' if md_val is not None else '—') if buys_any else 'N/A'}</td>"
            f"<td>{'N/A' if not buys_any else _buy_limit_str(mbp_raw)}</td>"
            f"<td>{f'{u_max_gp:,}' if u_max_gp > 0 else '—'}</td>"
            f"<td>{f'{u_avg_gp:,.0f}' if u_avg_gp > 0 else '—'}</td>"
            f"</tr>"
        ))
    unplaced_rows.sort(key=lambda x: x[0])

    n_placed = len(placed_rows) + len(no_opener_rows)
    body = "<h1>Stores</h1>"
    body += (
        f"<p>{n_placed} placed store instance{'s' if n_placed != 1 else ''}"
        + (f"; {len(no_opener_rows)} with no known opener"
           if no_opener_rows else "")
        + (f"; {len(unplaced_rows)} unplaced blueprint"
           f"{'s' if len(unplaced_rows) != 1 else ''} at bottom"
           if unplaced_rows else "")
        + ". Values shown are per-area instance data (may differ from blueprint defaults).</p>"
    )
    body += _IDX_TABLE_HEAD + "\n".join(r[1] for r in placed_rows) + "</tbody></table>"

    if gm_cost > 0:
        item_html = (link(f"../items/{gm_irr}.html", gm_item_name)
                     if gm_irr in db.items else E(gm_item_name))
        store_html = link(f"{gm_slug}.html", gm_store_name)
        area_html = link(f"../areas/{gm_area_rr}.html", db.area_name(gm_area_rr))
        body += (
            "<h2>Most Valuable Item for Sale</h2>"
            f"<p>{item_html} — <strong>{gm_cost:,} gp</strong> base value, "
            f"sold at {store_html} in {area_html}.</p>"
        )

    if no_opener_rows:
        body += (
            "<h2>Possibly Inaccessible</h2>"
            "<p>These stores are placed in areas but no NPC conversation or trigger "
            "was found that opens them. They may be opened by runtime-only code "
            "(dynamic tag lookup, item scripts) or may be bugged / unreachable.</p>"
            + _IDX_TABLE_HEAD + "\n".join(r[1] for r in no_opener_rows) + "</tbody></table>"
        )

    if unplaced_rows:
        body += (
            "<h2>Unplaced Blueprints</h2>"
            "<p>These store blueprints exist in the module files but are not placed "
            "in any area — they are not accessible to players.</p>"
            + _IDX_TABLE_HEAD + "\n".join(r[1] for r in unplaced_rows) + "</tbody></table>"
        )

    write(out / "stores" / "index.html", page("Stores", body, root_rel=".."))


def render_store_page(db: Db, resref: str, out: Path) -> None:
    s = db.stores.get(resref)
    if not s:
        return
    name = db.store_name(resref)

    # Find all placed instances for this blueprint, excluding hidden areas.
    # If every placement is in a hidden area, suppress the blueprint page entirely.
    all_instances: list[tuple[str, Any]] = []
    for area_rr, store_list in db.area_stores.items():
        for inst in store_list:
            if fld(inst, "ResRef", "") == resref:
                all_instances.append((area_rr, inst))
    instances = [(a, i) for a, i in all_instances if a not in db.hidden_areas]
    if not instances and all_instances:
        return  # all placements are in hidden areas
    instances.sort(key=lambda x: nwn_text(db.area_name(x[0])).lower())

    sections = [f"<h1>{nwn_html(name)}</h1>"]

    if instances:
        inst_rows = []
        for area_rr, inst in instances:
            mu = fld(inst, "MarkUp", None)
            md = fld(inst, "MarkDown", None)
            buy_html, buys_any = _store_buy_summary(inst)
            pages = list_items(inst.get("StoreList"))
            n_items = sum(len(list_items(p.get("ItemList"))) for p in pages)
            inst_tag = fld(inst, "Tag", "") or ""
            slug = _store_instance_slug(area_rr, inst)
            inst_rows.append(
                f"<tr>"
                f"<td>{link(f'../areas/{area_rr}.html', db.area_name(area_rr))}</td>"
                f"<td>{link(f'{slug}.html', inst_tag) if inst_tag else '—'}</td>"
                f"<td>{n_items}</td>"
                f"<td>{f'{mu}%' if mu is not None else '—'}</td>"
                f"<td>{buy_html}</td>"
                f"<td>{(f'{md}%' if md is not None else '—') if buys_any else 'N/A'}</td>"
                f"<td>{'N/A' if not buys_any else _buy_limit_str(fld(inst, 'MaxBuyPrice', None))}</td>"
                f"</tr>"
            )
        sections.append(
            "<h2>Placed Instances</h2>"
            '<table class="data"><thead><tr>'
            '<th>Area</th><th>Tag</th><th>Items</th>'
            '<th title="Price store charges you to buy items (% of base)">Sells At</th>'
            '<th title="Item types the store will buy from you">Buys</th>'
            '<th title="Price store pays you for items (% of base); N/A if the store buys nothing">Buys At</th>'
            '<th title="Maximum the store will pay for any single item">Max Buy</th>'
            "</tr></thead><tbody>" + "\n".join(inst_rows) + "</tbody></table>"
        )
    else:
        sections.append(
            '<p><strong>Warning:</strong> This blueprint is not placed in any area '
            '— it is not accessible to players.</p>'
        )

    pages = list_items(s.get("StoreList"))
    bp_max_gp, bp_max_irr, bp_max_name, bp_avg_gp = _store_item_gp_stats(db, pages)
    bp_max_item_html = ""
    if bp_max_gp > 0:
        bp_max_item_html = (link(f"../items/{bp_max_irr}.html", bp_max_name)
                            if bp_max_irr in db.items else E(bp_max_name))
    _bp_buy_html, _bp_buys_any = _store_buy_summary(s)
    bp_meta = [
        "<h2>Blueprint</h2>",
        '<dl class="meta">',
        f"<dt>ResRef</dt><dd>{E(resref)}</dd>",
        f"<dt>Tag</dt><dd>{E(fld(s, 'Tag', ''))}</dd>",
        f"<dt>Sells At (MarkUp) / Buys At (MarkDown)</dt>"
        f"<dd>{E(fld(s, 'MarkUp', ''))} / "
        f"{E(fld(s, 'MarkDown', '')) if _bp_buys_any else 'N/A'}</dd>",
        f"<dt>Buys</dt><dd>{_bp_buy_html}</dd>",
        f"<dt>MaxBuyPrice</dt><dd>"
        f"{'N/A' if not _bp_buys_any else _buy_limit_str(fld(s, 'MaxBuyPrice', None))}</dd>",
        f"<dt>StoreGold</dt><dd>{E(fld(s, 'StoreGold', ''))}</dd>",
        f"<dt>BlackMarket</dt><dd>{E(fld(s, 'BlackMarket', ''))}</dd>",
    ]
    if bp_max_gp > 0:
        bp_meta.append(f"<dt>Most Valuable Item</dt><dd>{bp_max_item_html} ({bp_max_gp:,} gp)</dd>")
        bp_meta.append(f"<dt>Avg Item Value</dt><dd>{bp_avg_gp:,.0f} gp</dd>")
    bp_meta.append('</dl>')
    sections.extend(bp_meta)
    sections.append("<h2>Blueprint Inventory</h2>" + _store_inventory_html(db, pages))

    write(out / "stores" / f"{resref}.html",
          page(name, "\n".join(sections), root_rel=".."))
