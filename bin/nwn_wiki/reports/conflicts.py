"""Tag-conflict and duplicate-conversation reports.

Scans item and store blueprints for shared tags whose variants differ, and
dialog files whose conversation trees are content-identical, writing the
``item_tag_conflicts.json``, ``store_tag_conflicts.json`` and
``duplicate_conversations.json`` exports into ``module-index/``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from nwn_wiki import state
from nwn_wiki.gff import fld, list_items
from nwn_wiki.htmlgen.escape import nwn_text
from nwn_wiki.itemprops import _store_inv_key, itemprop_oneliner
from nwn_wiki.items import item_gp_value
from nwn_wiki.lookups import baseitem_name


# ---------------------------------------------------------------------------
# Tag-conflict report
# ---------------------------------------------------------------------------

def generate_tag_conflict_report(
    db: "Db",
    module_index_dir: Path,
    module_title: str,
    wiki_out: Path,
    base_url: str,
) -> None:
    """Scan all item blueprints for shared TAG values with differing properties.

    Writes item_tag_conflicts.json to module_index_dir.  Items whose TAG is empty
    or identical to their resref are skipped.  Groups where every variant has
    exactly the same property set are also skipped — only genuine differences are
    reported.

    Each variant includes a wiki_url field:
      - Fully-qualified URL when base_url is set (e.g. https://…/items/foo.html)
      - Relative path from project_root when base_url is empty (e.g. docs/items/foo.html)
    """

    project_root = module_index_dir.parent

    # Build a URL or relative path for a single item resref.
    if base_url:
        _url_prefix = base_url.rstrip("/") + "/items/"
        def _item_url(resref: str) -> str:
            return _url_prefix + resref + ".html"
    else:
        try:
            rel_wiki = wiki_out.relative_to(project_root)
        except ValueError:
            rel_wiki = wiki_out  # wiki is outside project root; use absolute path
        _rel_prefix = str(rel_wiki).rstrip("/") + "/items/"
        def _item_url(resref: str) -> str:
            return _rel_prefix + resref + ".html"

    # Group resrefs by their in-game TAG value (case-insensitive).
    # Items with no explicit Tag field use their resref as the implicit tag —
    # this catches conflicts where a plain InventoryRes reference (no inline Tag)
    # shares a tag with a custom inline item that explicitly sets the same tag.
    tag_groups: dict[str, list[str]] = defaultdict(list)
    for resref, item in db.items.items():
        tag = (fld(item, "Tag") or resref).strip()
        tag_groups[tag.lower()].append(resref)

    def _prop_sig(item: dict) -> frozenset[str]:
        return frozenset(itemprop_oneliner(p) for p in list_items(item.get("PropertiesList")))

    def _item_sources(resref: str) -> list[str]:
        sources: list[str] = []
        for s in db.item_sold_at.get(resref, []):
            area = db.area_name(s["area_rr"]) if "area_rr" in s else ""
            label = nwn_text(s["name"])
            sources.append("Sold at: " + label + (f" ({area})" if area else ""))
        for c in db.item_in_container.get(resref, []):
            area = db.area_name(c["area_rr"]) if "area_rr" in c else ""
            pname = nwn_text(c.get("pname", ""))
            sources.append("In container: " + pname + (f" ({area})" if area else ""))
        for c in db.item_carried_by.get(resref, []):
            area = db.area_name(c["area_rr"]) if "area_rr" in c else ""
            cname = nwn_text(c.get("cname", ""))
            sources.append("Carried by: " + cname + (f" ({area})" if area else ""))
        for s in db.item_from_script.get(resref, []):
            sources.append("Script: " + (s.get("label") or s.get("script", "")))
        return sources

    conflicts: list[dict] = []
    for _tag_lower, resrefs in sorted(tag_groups.items()):
        if len(resrefs) < 2:
            continue
        sigs = {rr: _prop_sig(db.items[rr]) for rr in resrefs}
        unique_sigs = set(frozenset(s) for s in sigs.values())
        if len(unique_sigs) == 1:
            continue  # all variants identical — not a conflict

        # Canonical tag: prefer an explicit Tag field; fall back to the group key.
        canonical_tag = next(
            (
                (fld(db.items[rr], "Tag") or "").strip()
                for rr in sorted(resrefs)
                if (fld(db.items[rr], "Tag") or "").strip()
            ),
            _tag_lower,  # group key (the implicit shared tag)
        )

        variants: list[dict] = []
        for rr in sorted(resrefs):
            item = db.items[rr]
            name = nwn_text(db.item_name(rr))
            bi = fld(item, "BaseItem")
            base_name = baseitem_name(bi) if bi is not None else ""
            cost = item_gp_value(item)
            props = sorted(sigs[rr])
            variants.append({
                "resref": rr,
                "name": name,
                "base_item": base_name,
                "cost_gp": cost,
                "wiki_url": _item_url(rr),
                "properties": props,
                "found_at": _item_sources(rr),
            })

        conflicts.append({
            "shared_tag": canonical_tag,
            "item_count": len(resrefs),
            "variants": variants,
            "recommendation": (
                "Give each variant a unique tag (and consider a matching unique resref) "
                "so scripts, players, and the wiki can unambiguously identify each item."
            ),
        })

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "module": module_title,
        "conflict_count": len(conflicts),
        "summary": (
            f"{len(conflicts)} item tag conflict(s) found across "
            f"{sum(c['item_count'] for c in conflicts)} item variants."
            if conflicts else "No item tag conflicts found."
        ),
        "conflicts": conflicts,
    }

    out_path = module_index_dir / "item_tag_conflicts.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if conflicts:
        state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: item_tag_conflicts.json ({len(conflicts)} conflict(s)) — {out_path}"))
    else:
        state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: item_tag_conflicts.json (none) — {out_path}"))


def generate_store_tag_conflict_report(
    db: "Db",
    module_index_dir: Path,
    module_title: str,
    wiki_out: Path,
    base_url: str,
) -> None:
    """Scan all store blueprints for shared Tag values with differing inventories.

    Writes store_tag_conflicts.json to module_index_dir.  Stores whose Tag
    is empty or identical to their resref are still included — any Tag clash
    on a UTM blueprint makes OpenStore("tag") calls ambiguous.  Groups where
    every variant has the exact same inventory and pricing are also reported
    (they are pure file-level duplicates).
    """
    project_root = module_index_dir.parent
    if base_url:
        _url_prefix = base_url.rstrip("/") + "/stores/"
        def _store_url(rr: str) -> str:
            return _url_prefix + rr + ".html"
    else:
        try:
            rel_wiki = wiki_out.relative_to(project_root)
        except ValueError:
            rel_wiki = wiki_out
        _rel_prefix = str(rel_wiki).rstrip("/") + "/stores/"
        def _store_url(rr: str) -> str:
            return _rel_prefix + rr + ".html"

    def _store_areas(rr: str) -> list[str]:
        areas: list[str] = []
        for area_rr, inst_list in db.area_stores.items():
            for inst in inst_list:
                inst_rr = (fld(inst, "ResRef", "") or fld(inst, "TemplateResRef", "") or "").lower()
                if inst_rr == rr:
                    areas.append(db.area_name(area_rr))
        return sorted(set(areas))

    conflicts: list[dict] = []
    for tag_lower, resrefs in sorted(db.store_tag_groups.items()):
        if len(resrefs) < 2:
            continue
        canonical_tag = next(
            ((fld(db.stores[rr], "Tag") or "").strip() for rr in sorted(resrefs)
             if (fld(db.stores[rr], "Tag") or "").strip()),
            tag_lower,
        )
        sigs = {rr: _store_inv_key(db.stores[rr]) for rr in resrefs}
        unique_sigs = set(sigs.values())
        is_identical = len(unique_sigs) == 1
        variants: list[dict] = []
        for rr in sorted(resrefs):
            store = db.stores[rr]
            name = nwn_text(db.store_name(rr))
            item_count = sum(
                len(list_items(p.get("ItemList")))
                for p in list_items(store.get("StoreList"))
            )
            variants.append({
                "resref": rr,
                "name": name,
                "wiki_url": _store_url(rr),
                "item_count": item_count,
                "markup_pct": fld(store, "MarkUp", 0),
                "markdown_pct": fld(store, "MarkDown", 0),
                "store_gold": fld(store, "StoreGold", -1),
                "found_in_areas": _store_areas(rr),
            })
        conflicts.append({
            "shared_tag": canonical_tag,
            "store_count": len(resrefs),
            "inventories_identical": is_identical,
            "variants": variants,
            "recommendation": (
                "These stores share a Tag, making OpenStore(\"" + canonical_tag + "\") "
                "ambiguous — the engine opens whichever it finds first. "
                + ("The inventories are identical; consider merging into one blueprint. "
                   if is_identical else
                   "Give each store a unique Tag so scripts can target the correct one. ")
            ),
        })

    out_path = module_index_dir / "store_tag_conflicts.json"
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "module": module_title,
        "conflict_count": len(conflicts),
        "summary": (
            f"{len(conflicts)} store tag conflict(s) found across "
            f"{sum(c['store_count'] for c in conflicts)} store variants."
            if conflicts else "No store tag conflicts found."
        ),
        "conflicts": conflicts,
    }, indent=2, ensure_ascii=False) + "\n")
    if conflicts:
        state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: store_tag_conflicts.json ({len(conflicts)} conflict(s)) — {out_path}"))
    else:
        state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: store_tag_conflicts.json (none) — {out_path}"))


def generate_conversation_conflict_report(
    db: "Db",
    module_index_dir: Path,
    module_title: str,
    wiki_out: Path,
    base_url: str,
) -> None:
    """Detect dialog files with identical conversation trees (content duplicates).

    Walks every DLG file's entry/reply tree and computes a content fingerprint
    via _conversation_key().  Files with the same fingerprint have identical
    content — one is redundant and both blueprint Conversation fields should
    point to a single canonical resref.

    Writes duplicate_conversations.json to module_index_dir.
    """
    project_root = module_index_dir.parent
    if base_url:
        _url_prefix = base_url.rstrip("/") + "/conversations/"
        def _conv_url(rr: str) -> str:
            return _url_prefix + rr + ".html"
    else:
        try:
            rel_wiki = wiki_out.relative_to(project_root)
        except ValueError:
            rel_wiki = wiki_out
        _rel_prefix = str(rel_wiki).rstrip("/") + "/conversations/"
        def _conv_url(rr: str) -> str:
            return _rel_prefix + rr + ".html"

    def _caller_summary(resref: str) -> list[str]:
        summaries: list[str] = []
        for caller in db.dialog_callers.get(resref, []):
            kind = caller.get("kind", "")
            if kind in ("creature", "placeable", "door"):
                summaries.append(f"{kind}: {caller.get('resref', '')}")
            elif kind == "module-event":
                summaries.append(f"module event: {caller.get('event', '')}")
            else:
                summaries.append(kind)
        return summaries

    duplicates: list[dict] = []
    for key, resrefs in db._dialog_key_registry.items():
        # Skip synthesized z-dialog pseudo-entries (no real DLG file)
        real = [rr for rr in resrefs if not db.dialogs.get(rr, {}).get("__zdlg_handler__")]
        if len(real) < 2:
            continue
        entries_count = len(list_items(db.dialogs[real[0]].get("EntryList")))
        replies_count = len(list_items(db.dialogs[real[0]].get("ReplyList")))
        variants: list[dict] = []
        for rr in sorted(real):
            callers = _caller_summary(rr)
            variants.append({
                "resref": rr,
                "wiki_url": _conv_url(rr),
                "caller_count": len(callers),
                "callers": callers,
            })
        duplicates.append({
            "entry_count": entries_count,
            "reply_count": replies_count,
            "file_count": len(real),
            "files": variants,
            "recommendation": (
                "These dialog files have identical conversation trees. "
                "Keep one canonical resref and update all blueprint Conversation "
                "fields to point to it; then remove the redundant .dlg file(s)."
            ),
        })
    duplicates.sort(key=lambda d: sorted(v["resref"] for v in d["files"])[0])

    out_path = module_index_dir / "duplicate_conversations.json"
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "module": module_title,
        "duplicate_group_count": len(duplicates),
        "summary": (
            f"{len(duplicates)} group(s) of dialog files with identical conversation trees."
            if duplicates else "No duplicate conversation trees found."
        ),
        "duplicates": duplicates,
    }, indent=2, ensure_ascii=False) + "\n")
    if duplicates:
        state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: duplicate_conversations.json ({len(duplicates)} group(s)) — {out_path}"))
    else:
        state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: duplicate_conversations.json (none) — {out_path}"))

