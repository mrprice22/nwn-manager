"""items/most-equipped.html — every worn item, ranked by how much it is worn.

The wiki knows what each character wears (``rec["equipped"]``) but never
inverted that: no page answered "who wears this, and how many of them?". This
one does, in a single table ordered by equipped copies.

**Rows are keyed by blueprint resref.** A player who reworks an item at the
forge changes the instance, not its TemplateResRef, so every player variant of
an item collapses into the one row that links to the one item page. That is
deliberate: giving forge variants their own pages would mint links that break
the next time a player touches the anvil. The properties shown are therefore the
*blueprint's* -- the row matches the page it links to -- and a copy whose worn
property set differs is flagged ``modified`` instead.

Depth matters: the page is written to ``items/`` so it can reuse the item page's
own link builders (:func:`~nwn_wiki.render.items.itemprop_cells`,
:func:`~nwn_wiki.render.items.item_type_cell`), whose hrefs are relative to that
directory.
"""

from __future__ import annotations

from pathlib import Path

from nwn_wiki import state
from nwn_wiki.gff import fld, list_items
from nwn_wiki.htmlgen.blocks import items_layout, toc_sidebar
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E, nwn_text
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.itemprops import itemprop_format, itemprop_oneliner
from nwn_wiki.lookups import baseitem_label
from nwn_wiki.players.model import class_shorthand
from nwn_wiki.render.characters import _is_gear, _item_name, char_link
from nwn_wiki.render.items import item_type_cell, itemprop_cells
from nwn_wiki.render.leaderboards import dedupe
from nwn_wiki.render.players import player_link

PAGE_REL = "items/most-equipped.html"


def _prop_sig(props: list) -> tuple[str, ...]:
    """Order-independent fingerprint of a property list.

    A sorted tuple rather than a frozenset: the forge can add a *second* copy of
    a property a blueprint already has, and a set would call that unmodified.
    """
    return tuple(sorted(itemprop_oneliner(p) for p in props or []))


def collect_equipped(db) -> "tuple[list[dict], list[dict]]":
    """Worn items, most-worn first — the shared source for the page and the JSON.

    Returns one dict per blueprint resref::

        {resref, name, item, copies, chars: [{rec, copies, modified}],
         modified_copies, has_page}

    ``item`` is always a blueprint struct carrying at least one property.
    Returns ``(rows, dropped)``: gear with no module blueprint, or with a
    blueprint that grants nothing, is not equipment a player chose -- it is the
    engine's starting outfit (nw_cloth001 and friends), which every new
    character spawns wearing and which would otherwise top the ranking with an
    unlinkable, propertyless row. Those are reported separately rather than
    listed, so both the page and the JSON show only gear worth ranking.
    """
    rows: dict[str, dict] = {}
    for rec in dedupe(state._CHARACTERS):
        for inst in rec.get("equipped") or []:
            if not _is_gear(inst):
                continue
            rr = (inst.get("resref") or "").lower()
            if not rr:
                continue
            bp = db.items.get(rr)
            row = rows.get(rr)
            if row is None:
                row = rows[rr] = {
                    "resref": rr,
                    "name": nwn_text(db.item_name(rr)) if bp else _item_name(inst),
                    "item": bp,
                    "copies": 0,
                    "modified_copies": 0,
                    "has_page": rr in state._ITEM_PAGES,
                    "_chars": {},
                }
            modified = bp is not None and _prop_sig(inst.get("properties")) != _prop_sig(
                list_items(bp.get("PropertiesList")))
            row["copies"] += 1
            if modified:
                row["modified_copies"] += 1
            slot = row["_chars"].setdefault(
                rec["slug"], {"rec": rec, "copies": 0, "modified": False})
            slot["copies"] += 1
            slot["modified"] = slot["modified"] or modified

    out, dropped = [], []
    for row in rows.values():
        row["chars"] = sorted(
            row.pop("_chars").values(),
            key=lambda c: (-c["copies"], c["rec"]["name"].lower()),
        )
        bp = row["item"]
        if bp is None:
            row["reason"] = "no blueprint in the module"
            dropped.append(row)
        elif not list_items(bp.get("PropertiesList")):
            row["reason"] = "blueprint grants no properties"
            dropped.append(row)
        else:
            out.append(row)
    out.sort(key=lambda r: (-r["copies"], r["name"].lower()))
    dropped.sort(key=lambda r: (-r["copies"], r["name"].lower()))
    return out, dropped


def _name_cell(row: dict) -> str:
    """Item name plus its resref, linked when the item has a page."""
    rr = row["resref"]
    tail = f' <code class="muted">[{E(rr)}]</code>'
    if row["has_page"]:
        return f'<a href="{E(rr)}.html">{E(row["name"])}</a>' + tail
    return E(row["name"]) + tail


def _type_cell(db, row: dict) -> str:
    """Category (linked to its index section) over the raw base item row."""
    if row["item"] is None:
        return '<span class="muted">—</span>'
    # baseitem_label already carries its own muted "(row N)" suffix -- kept
    # because CEP/HAK relabel base item rows and the number is the authority.
    return (item_type_cell(db, row["resref"], row["item"])
            + f'<br>{baseitem_label(fld(row["item"], "BaseItem"))}')


def _props_cell(row: dict) -> str:
    """The blueprint's properties as one linked, comma-separated list."""
    parts = []
    for p in list_items(row["item"].get("PropertiesList")):
        f = itemprop_format(p)
        pname_cell, subtype_cell, cost_cell, _param = itemprop_cells(f)
        bits = [b for b in (pname_cell, subtype_cell, cost_cell) if b]
        parts.append(" ".join(bits))
    cell = ", ".join(parts)
    if row["modified_copies"]:
        cell += (' <span class="muted" title="Worn copies whose property set '
                 'differs from this blueprint — reworked at the forge.">&middot; '
                 f'{row["modified_copies"]} modified</span>')
    return f'<div class="props-cell">{cell}</div>'


def _worn_by_cell(row: dict, ctx: PageCtx) -> str:
    """One bullet per character: name [player]: build shorthand."""
    bullets = []
    for c in row["chars"]:
        rec = c["rec"]
        player = (player_link(ctx, rec["player"]) if rec["player"]
                  else '<span class="muted">—</span>')
        build = class_shorthand(rec.get("classes") or [])
        extra = ""
        if c["copies"] > 1:
            extra += f' <span class="muted">x{c["copies"]}</span>'
        if c["modified"]:
            extra += ' <span class="muted">(modified)</span>'
        bullets.append(
            f"<li>{char_link(rec, ctx)} [{player}]: "
            f"{E(build) if build else '<span class=\"muted\">—</span>'}{extra}</li>"
        )
    return '<ul class="worn-by">' + "".join(bullets) + "</ul>"


def render_most_equipped(db, out: Path) -> None:
    """Write items/most-equipped.html, or nothing when there is no vault.

    The early return is the same predicate ``SiteChrome.has_characters`` gates
    the nav entry on, so the menu cannot offer a page that was never written.
    """
    if not state._CHARACTERS:
        return
    ctx = PageCtx(PAGE_REL)
    rows, dropped = collect_equipped(db)

    # Not a warning: propertyless starting kit is expected on every character.
    # Reported so the count stays visible if it ever grows into something else.
    if dropped:
        state._module_index_summary.append((
            "info",
            f"[nwn-wiki] most-equipped: {len(dropped)} worn item(s) not ranked "
            f"(no properties, or no blueprint) — "
            + ", ".join(r["resref"] for r in dropped[:8])
            + (" …" if len(dropped) > 8 else ""),
        ))
    # A blueprint that IS ranked but has no item page is still a real defect.
    missing = [r["resref"] for r in rows if not r["has_page"]]
    if missing:
        state._module_index_summary.append((
            "warn",
            f"[nwn-wiki] most-equipped: {len(missing)} equipped item(s) have no "
            f"wiki page — {', '.join(sorted(missing)[:10])}"
            + (" …" if len(missing) > 10 else ""),
        ))

    body_rows = []
    for row in rows:
        chars = len(row["chars"])
        body_rows.append(
            "<tr>"
            f"<td>{_name_cell(row)}</td>"
            f"<td>{_type_cell(db, row)}</td>"
            f"<td>{_props_cell(row)}</td>"
            f'<td class="num">{row["copies"]}'
            f'<br><small class="muted">{chars} char{"s" if chars != 1 else ""}</small></td>'
            f"<td>{_worn_by_cell(row, ctx)}</td>"
            "</tr>"
        )

    sidebar = toc_sidebar([
        '<div class="toc-group-heading">Views</div>',
        '<div><a href="index.html">Accessible Items</a></div>',
        '<div><a href="properties/index.html">Browse by Property</a></div>',
        '<div><a href="search.html">Search Items</a></div>',
        f'<div><a href="#most-equipped">Most Equipped'
        f' <span class="muted">({len(rows)})</span></a></div>',
    ])

    total_copies = sum(r["copies"] for r in rows)
    body = (
        '<h1 id="most-equipped">Most Equipped Items</h1>'
        f"<p>{len(rows)} item(s) worn across {total_copies} equipped slot(s) on "
        "the server's characters, most-worn first. "
        '<small class="muted">Rows are keyed by item blueprint: an item reworked '
        "at the forge keeps its blueprint, so every variant is counted here and "
        "the properties shown are the blueprint's. Copies whose properties no "
        'longer match are counted as <em>modified</em>. Starting kit is left '
        "out: an item whose blueprint grants no properties is not gear a player "
        'chose.</small></p>'
    )
    if not body_rows:
        body += '<p class="muted">No character is wearing anything.</p>'
    else:
        body += (
            '<div class="table-scroll">'
            '<table class="data most-equipped"><thead><tr>'
            "<th>Item</th><th>Type</th><th>Properties</th>"
            "<th>Equipped</th><th>Worn by</th>"
            "</tr></thead><tbody>" + "\n".join(body_rows) + "</tbody></table></div>"
        )

    # .wide-page is what main:has(> .wide-page) keys the wider cap off, so the
    # wearer column has room for one character per line.
    write_page(out, ctx, "Most Equipped Items",
               f'<div class="wide-page">{items_layout(sidebar, body)}</div>')
