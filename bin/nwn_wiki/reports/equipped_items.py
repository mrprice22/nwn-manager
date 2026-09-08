"""module-index/equipped_items.json — the worn-gear ranking, machine-readable.

The LLM-facing mirror of ``items/most-equipped.html``: same rows, same ordering,
same blueprint-keyed grouping, built from the very same
:func:`~nwn_wiki.render.most_equipped.collect_equipped` traversal so the page and
the export can never disagree.

Written *after* the pages rather than from ``generate_module_index``: the reports
run before ``cli._load_players``, so at module-index time the character records
this file is made of do not exist yet.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nwn_wiki import state
from nwn_wiki.gff import fld, list_items
from nwn_wiki.itemprops import itemprop_oneliner
from nwn_wiki.items import _item_category
from nwn_wiki.lookups import baseitem_name
from nwn_wiki.players.model import _class_line, class_shorthand
from nwn_wiki.render.most_equipped import collect_equipped
from nwn_wiki.reports.counter_gear import _module_index_url_helpers
from nwn_wiki.util import _try_int, _write_json


def write_equipped_items(db, module_index_dir: Path, module_title: str,
                         wiki_out: Path, base_url: str) -> None:
    """Write equipped_items.json, or nothing when no vault was loaded."""
    if not state._CHARACTERS:
        return
    _iu = _module_index_url_helpers(module_index_dir, wiki_out, base_url)[1]
    now = datetime.now().isoformat(timespec="seconds")

    rows, dropped = collect_equipped(db)
    items = []
    for row in rows:
        bp = row["item"]
        bi_raw = fld(bp, "BaseItem", None)
        bi = -1 if bi_raw is None else _try_int(bi_raw, -1)
        items.append({
            "resref": row["resref"],
            "name": row["name"],
            "base_item": baseitem_name(bi) if bi >= 0 else "",
            "category": _item_category(bp, row["name"]),
            "properties": [itemprop_oneliner(p)
                           for p in list_items(bp.get("PropertiesList"))],
            "equipped_copies": row["copies"],
            "equipped_by_characters": len(row["chars"]),
            "modified_copies": row["modified_copies"],
            "has_wiki_page": row["has_page"],
            "wiki_url": _iu(row["resref"]) if row["has_page"] else "",
            "characters": [{
                "name": c["rec"]["name"],
                "player": c["rec"]["player"],
                "level": c["rec"]["level"],
                "classes": _class_line(c["rec"].get("classes") or []),
                "class_shorthand": class_shorthand(c["rec"].get("classes") or []),
                "copies": c["copies"],
                "modified": c["modified"],
                "wiki_url": _character_url(c["rec"], module_index_dir, wiki_out,
                                           base_url),
            } for c in row["chars"]],
        })

    _write_json(module_index_dir / "equipped_items.json", {
        "generated_at": now,
        "module": module_title,
        "count": len(items),
        "items": items,
        # Worn but not ranked, and why -- the page leaves these out, so the
        # export says plainly what it left out rather than silently shrinking.
        "excluded": [{"resref": r["resref"], "name": r["name"],
                      "equipped_copies": r["copies"], "reason": r["reason"]}
                     for r in dropped],
    })
    state._module_index_summary.append((
        "info",
        f"[nwn-wiki] module-index: equipped_items.json ({len(items)} item(s) "
        f"worn by {len(state._CHARACTERS)} character(s))",
    ))


def _character_url(rec: dict, module_index_dir: Path, wiki_out: Path,
                   base_url: str) -> str:
    """Absolute-or-project-relative URL of a character page.

    ``_module_index_url_helpers`` only builds the four module-side directories,
    so characters/ is derived here the same way it derives items/.
    """
    if base_url:
        return f"{base_url.rstrip('/')}/characters/{rec['slug']}.html"
    project_root = module_index_dir.parent
    try:
        rel_wiki = wiki_out.relative_to(project_root)
    except ValueError:
        rel_wiki = wiki_out
    return f"{str(rel_wiki).rstrip('/')}/characters/{rec['slug']}.html"
