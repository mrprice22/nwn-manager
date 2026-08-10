"""Module-index: the LLM-friendly JSON exports written into ``module-index/``.

:func:`generate_module_index` walks the loaded :class:`~nwn_wiki.cli.Db` and
writes the machine-readable snapshot of the module — areas, creatures, items,
stores, quests, scripts and the quality reports — that tooling and LLMs read
instead of scraping the HTML wiki.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from nwn_wiki import state
from nwn_wiki.gff import fld
from nwn_wiki.htmlgen.escape import nwn_text
from nwn_wiki.items import item_gp_value
from nwn_wiki.lookups import appearance_name, baseitem_name, race_name
from nwn_wiki.render.creature_page import _retaliation_sentence
from nwn_wiki.render.creatures import creature_max_hp
from nwn_wiki.reports.counter_gear import (
    _module_index_url_helpers,
    check_counter_gear_freshness,
    generate_counter_gear_index,
)
from nwn_wiki.util import _try_int, _write_json


# ---------------------------------------------------------------------------
# Module-index: LLM-friendly JSON exports
# ---------------------------------------------------------------------------


def generate_module_index(
    db: "Db",
    module_index_dir: Path,
    module_title: str,
    graph: "dict[str, list]",
    area_paths: "dict[str, list | None] | None",
    path_from_resref: str,
    path_from_name: str,
    wiki_out: Path,
    base_url: str,
) -> None:
    """Write LLM-friendly JSON indexes to module_index_dir.

    Files written:
      area_graph.json                    — directed area transition graph with names
      area_paths.json                    — BFS shortest paths (only when path_from_resref given)
      area_index.json                    — all areas with names, stats, and connections
      duplicate_destination_tags.json    — transitions whose LinkedTo tag matches multiple objects
      creature_index.json                — all canonical creatures with stats and locations
      creature_tag_conflicts.json        — creature blueprints that produced variant resrefs
      item_index.json                    — all items with names, costs, and sources
      cross_faction_creatures.json       — blueprints appearing in multiple factions
      faction_bp_instance_discrepancies.json — placed instances whose FactionID differs from their blueprint
      inaccessible_items.json            — items not reachable by players
      unspawned_creatures.json           — creature blueprints never placed or in an encounter
      instance_only_conversations.json   — creature instances with Conversation overrides not on the blueprint
    """
    module_index_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    _cu, _iu, _au, _su = _module_index_url_helpers(module_index_dir, wiki_out, base_url)

    # ------------------------------------------------------------------ helpers
    def _int_fid(raw: Any) -> "int | None":
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ area_graph.json
    area_graph_data: dict[str, Any] = {}
    for rr in sorted(db.areas):
        area_name = db.area_name(rr)
        edges = []
        for dst, kind, label, is_fallback, is_dup_tag in graph.get(rr, []):
            edge: dict[str, Any] = {
                "to": dst,
                "to_name": db.area_name(dst),
                "kind": kind,
                "label": label,
            }
            if is_fallback:
                edge["is_fallback"] = True
            if is_dup_tag:
                edge["is_dup_tag"] = True
            edges.append(edge)
        area_graph_data[rr] = {
            "name": area_name,
            "hidden": rr in db.hidden_areas,
            "wiki_url": _au(rr),
            "transitions": edges,
        }
    _write_json(module_index_dir / "area_graph.json", {
        "generated_at": now,
        "module": module_title,
        "area_count": len(area_graph_data),
        "areas": area_graph_data,
    })
    state._module_index_summary.append(("info", f"[nwn-wiki] module-index: area_graph.json ({len(area_graph_data)} areas)"))

    # ------------------------------------------------------------------ area_paths.json
    if path_from_resref and area_paths is not None:
        paths_out: dict[str, Any] = {}
        unreachable: list[str] = []
        for rr, steps in area_paths.items():
            if steps is None:
                unreachable.append(rr)
            else:
                paths_out[rr] = {
                    "dest_name": db.area_name(rr),
                    "steps": [
                        {
                            "from": frm,
                            "from_name": db.area_name(frm),
                            "to": to,
                            "to_name": db.area_name(to),
                            "kind": kind,
                            "label": label,
                            **({"is_fallback": True} if is_fb else {}),
                            **({"is_dup_tag": True} if is_dup else {}),
                        }
                        for frm, to, kind, label, is_fb, is_dup in steps
                    ],
                }
        _write_json(module_index_dir / "area_paths.json", {
            "generated_at": now,
            "module": module_title,
            "from_resref": path_from_resref,
            "from_name": path_from_name,
            "reachable_count": len(paths_out),
            "unreachable_count": len(unreachable),
            "paths": paths_out,
            "unreachable": sorted(unreachable),
        })
        state._module_index_summary.append(("info", f"[nwn-wiki] module-index: area_paths.json ({len(paths_out)} reachable, {len(unreachable)} unreachable)"))

    # -------------------------------------------------------- duplicate_destination_tags.json
    dup_tag_entries: list[dict] = []
    for tag in sorted(db.dup_dest_tags, key=str.lower):
        objects = db.dup_dest_tags[tag]
        matching = [
            {
                "kind": obj["kind"],
                "area": obj["area"],
                "area_name": db.area_name(obj["area"]),
                "wiki_url": _au(obj["area"]),
            }
            for obj in objects
            if obj["area"] in db.areas
        ]
        transitions_using = [
            {
                "src_area": tr["src_area"],
                "src_area_name": db.area_name(tr["src_area"]),
                "src_wiki_url": _au(tr["src_area"]),
                "kind": tr["kind"],
                "label": tr["label"],
                "primary_dst": tr["dst_area"] or "",
                "primary_dst_name": db.area_name(tr["dst_area"]) if tr["dst_area"] else "",
                "alt_dsts": [
                    {"area": a, "area_name": db.area_name(a)}
                    for a in tr.get("dst_area_alts", [])
                ],
            }
            for tr in db.transitions
            if tr.get("dst_tag") == tag
        ]
        dup_tag_entries.append({
            "tag": tag,
            "matching_object_count": len(matching),
            "matching_objects": matching,
            "transition_count": len(transitions_using),
            "transitions_using_tag": transitions_using,
        })
    _write_json(module_index_dir / "duplicate_destination_tags.json", {
        "_description": (
            "Transitions whose LinkedTo tag matches multiple objects (waypoints, doors, "
            "or triggers) across the module. The NWN engine resolves ambiguous tags to "
            "whichever matching object it finds first, so these routes may send the "
            "player to an unexpected area. Each entry lists all objects carrying the "
            "tag and every transition that references it."
        ),
        "generated_at": now,
        "module": module_title,
        "count": len(dup_tag_entries),
        "duplicate_tags": dup_tag_entries,
    })
    if dup_tag_entries:
        state._module_index_summary.append(("issue", f"[nwn-wiki] module-index: duplicate_destination_tags.json ({len(dup_tag_entries)} duplicate tag(s)) — {module_index_dir / 'duplicate_destination_tags.json'}"))
    else:
        state._module_index_summary.append(("issue", "[nwn-wiki] module-index: duplicate_destination_tags.json (none)"))

    # -------------------------------------------------------- area_tag_conflicts.json
    area_tag_conflicts: list[dict] = []
    for tag_lower, resrefs in sorted(db.area_tag_groups.items()):
        if len(resrefs) < 2:
            continue
        canonical_tag = next(
            ((fld(db.areas[rr], "Tag") or "").strip() for rr in sorted(resrefs)
             if (fld(db.areas[rr], "Tag") or "").strip()),
            tag_lower,
        )
        transition_counts = {
            rr: sum(1 for tr in db.transitions if tr.get("src_area") == rr or tr.get("dst_area") == rr)
            for rr in resrefs
        }
        area_tag_conflicts.append({
            "shared_tag": canonical_tag,
            "area_count": len(resrefs),
            "areas": [
                {
                    "resref": rr,
                    "name": db.area_name(rr),
                    "hidden": rr in db.hidden_areas,
                    "wiki_url": _au(rr),
                    "transition_count": transition_counts[rr],
                }
                for rr in sorted(resrefs, key=lambda r: db.area_name(r).lower())
            ],
            "recommendation": (
                f"Multiple areas share the Tag \"{canonical_tag}\". Scripts using "
                f"GetObjectByTag(\"{canonical_tag}\") will find whichever area the "
                "engine resolves first. Give each area a unique Tag."
            ),
        })
    _write_json(module_index_dir / "area_tag_conflicts.json", {
        "generated_at": now,
        "module": module_title,
        "conflict_count": len(area_tag_conflicts),
        "summary": (
            f"{len(area_tag_conflicts)} area tag conflict(s) found across "
            f"{sum(c['area_count'] for c in area_tag_conflicts)} areas."
            if area_tag_conflicts else "No area tag conflicts found."
        ),
        "conflicts": area_tag_conflicts,
    })
    if area_tag_conflicts:
        state._module_index_summary.append(("issue", f"[nwn-wiki] module-index: area_tag_conflicts.json ({len(area_tag_conflicts)} conflict(s)) — {module_index_dir / 'area_tag_conflicts.json'}"))
    else:
        state._module_index_summary.append(("issue", "[nwn-wiki] module-index: area_tag_conflicts.json (none)"))

    # ------------------------------------------------------------------ area_index.json
    area_index: list[dict] = []
    for rr in sorted(db.areas, key=lambda r: db.area_name(r).lower()):
        n_creatures = len(db.area_creature_instances.get(rr, []))
        n_encounters = len(db.area_encounters.get(rr, []))
        n_stores = len(db.area_stores.get(rr, []))
        n_containers = len(db.area_containers.get(rr, []))
        connections = sorted({
            dst for dst, _kind, _label, _fb, _dup in graph.get(rr, [])
        })
        area_index.append({
            "resref": rr,
            "name": db.area_name(rr),
            "hidden": rr in db.hidden_areas,
            "wiki_url": _au(rr),
            "creature_count": n_creatures,
            "encounter_count": n_encounters,
            "store_count": n_stores,
            "container_count": n_containers,
            "connections": connections,
        })
    _write_json(module_index_dir / "area_index.json", {
        "generated_at": now,
        "module": module_title,
        "area_count": len(area_index),
        "areas": area_index,
    })
    state._module_index_summary.append(("info", f"[nwn-wiki] module-index: area_index.json ({len(area_index)} areas)"))

    # ------------------------------------------------------------------ creature_index.json
    creature_index: list[dict] = []
    for can_rr in sorted(db.canonical_creatures,
                         key=lambda r: nwn_text(db.canonical_creature_name(r)).lower()):
        if can_rr.startswith("__orphan_"):
            continue
        entry = db.canonical_creatures[can_rr]
        c = entry["c"]
        bp_rr = entry["bp_rr"]
        bp = db.creatures.get(bp_rr, c)
        name = nwn_text(db.canonical_creature_name(can_rr))
        cr_raw = fld(c, "ChallengeRating") or fld(bp, "ChallengeRating") or ""
        hp = creature_max_hp(c, bp if bp is not c else None) or 0
        race_raw = fld(c, "Race") if fld(c, "Race") not in (None, "") else fld(bp, "Race")
        fid = _int_fid(fld(c, "FactionID") if fld(c, "FactionID") not in (None, "")
                       else fld(bp, "FactionID"))
        app_raw = fld(c, "Appearance_Type") if fld(c, "Appearance_Type") not in (None, "") \
            else fld(bp, "Appearance_Type")
        app_id = _try_int(app_raw, -1) if app_raw not in (None, "") else -1
        is_variant = bp_rr != can_rr
        locs = db.canonical_locations.get(can_rr, [])
        loc_list = [
            {
                "area_resref": l["area"],
                "area_name": db.area_name(l["area"]),
                "kind": l["kind"],
                "count": l["count"],
                **({"encounter_resref": l["enc_rr"]} if l.get("enc_rr") else {}),
            }
            for l in locs
        ]
        rec: dict[str, Any] = {
            "canonical_resref": can_rr,
            "blueprint_resref": bp_rr,
            "name": name,
            "cr": str(cr_raw),
            "hp": hp,
            "race_id": _try_int(race_raw, -1) if race_raw not in (None, "") else -1,
            "race": race_name(race_raw),
            "appearance_id": app_id,
            "appearance_name": appearance_name(app_id) if app_id >= 0 else "",
            "faction_id": fid,
            "faction_name": db.faction_name(fid) if fid is not None else "",
            "wiki_url": _cu(can_rr),
            "locations": loc_list,
        }
        if is_variant:
            rec["is_variant_of"] = bp_rr
        dmg_script = (fld(c, "ScriptDamaged") if fld(c, "ScriptDamaged") not in (None, "")
                      else fld(bp, "ScriptDamaged")) or ""
        dmg_script = dmg_script.strip()
        if db.is_custom_damage_script(dmg_script) and (
            dmg_script in db.script_mitigates_damage
            or db.script_damage_req_tags.get(dmg_script)
        ):
            rec["custom_damage_script"] = dmg_script
            reqs = []
            for tag in sorted(db.script_damage_req_tags.get(dmg_script, set())):
                for irr in db.item_tag_groups.get(tag.lower(), []):
                    reqs.append({"tag": tag, "resref": irr, "name": db.item_name(irr)})
                if not db.item_tag_groups.get(tag.lower()):
                    reqs.append({"tag": tag, "resref": None, "name": None})
            if reqs:
                rec["damage_requirements"] = reqs
        ret_info = db.script_retaliation.get(dmg_script) if dmg_script else None
        if db.is_custom_damage_script(dmg_script) and ret_info:
            rec["retaliation_script"] = dmg_script
            rec["retaliation_summary"] = _retaliation_sentence(ret_info)
        creature_index.append(rec)
    _write_json(module_index_dir / "creature_index.json", {
        "generated_at": now,
        "module": module_title,
        "count": len(creature_index),
        "creatures": creature_index,
    })
    state._module_index_summary.append(("info", f"[nwn-wiki] module-index: creature_index.json ({len(creature_index)} canonical creatures)"))

    # ------------------------------------------------------------------ counter_gear.json
    # Opt-in: the simulation runs every creature against the whole attainable
    # item pool, which is far too slow for a routine wiki refresh. A normal run
    # only fingerprints the inputs and warns when the report on disk no longer
    # matches them.
    if db.run_counter_gear or not (module_index_dir / "counter_gear.json").is_file():
        generate_counter_gear_index(db, module_index_dir, module_title, now, _cu, _iu)
    else:
        check_counter_gear_freshness(db, module_index_dir)

    # ------------------------------------------------------------------ appearance_faction_report.json
    # Group canonical creatures by (appearance_id, faction_id) so that LLMs can
    # quickly spot appearances that span multiple factions — a common cause of
    # NPC confusion (two guards that look identical but one is hostile).
    # appearance_id → faction_id → list of creature entries
    app_faction: dict[int, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    _NO_FACTION = -1  # sentinel for creatures with fid=None
    for crec in creature_index:
        aid = crec["appearance_id"]
        if aid < 0:
            continue
        fid_key = crec["faction_id"] if crec["faction_id"] is not None else _NO_FACTION
        app_faction[aid][fid_key].append({
            "canonical_resref": crec["canonical_resref"],
            "name": crec["name"],
            "race": crec["race"],
            "race_id": crec["race_id"],
            "cr": crec["cr"],
            "wiki_url": crec["wiki_url"],
        })

    app_report: list[dict] = []
    for aid in sorted(app_faction):
        factions_map = app_faction[aid]
        real_fids = [f for f in factions_map if f != _NO_FACTION]
        cross = len(real_fids) > 1
        races_in_app: set[int] = {
            crec["race_id"]
            for entries in factions_map.values()
            for crec in entries
            if crec["race_id"] >= 0
        }
        faction_entries: list[dict] = []
        for fid_key in sorted(factions_map):
            fname = db.faction_name(fid_key) if fid_key != _NO_FACTION else "(none)"
            faction_entries.append({
                "faction_id": fid_key if fid_key != _NO_FACTION else None,
                "faction_name": fname,
                "creature_count": len(factions_map[fid_key]),
                "creatures": sorted(factions_map[fid_key],
                                    key=lambda r: r["name"].lower()),
            })
        app_report.append({
            "appearance_id": aid,
            "appearance_name": appearance_name(aid),
            "race_ids": sorted(races_in_app),
            "races": sorted({race_name(r) for r in races_in_app}),
            "faction_count": len(real_fids),
            "cross_faction": cross,
            "total_creatures": sum(len(v) for v in factions_map.values()),
            "factions": faction_entries,
        })

    # Sort: cross-faction appearances first (most interesting), then by name.
    app_report.sort(key=lambda e: (not e["cross_faction"], e["appearance_name"].lower()))
    cross_count = sum(1 for e in app_report if e["cross_faction"])

    _write_json(module_index_dir / "appearance_faction_report.json", {
        "generated_at": now,
        "module": module_title,
        "appearance_count": len(app_report),
        "cross_faction_appearance_count": cross_count,
        "summary": (
            f"{cross_count} appearance(s) used by creatures in more than one faction."
            if cross_count else "No appearances span multiple factions."
        ),
        "appearances": app_report,
    })
    state._module_index_summary.append(("info", f"[nwn-wiki] module-index: appearance_faction_report.json ({len(app_report)} appearances, {cross_count} cross-faction)"))

    # ------------------------------------------------------------------ creature_tag_conflicts.json
    # Canonical entries whose resref was synthesised as bp__v2, bp__v3, …
    # indicate that the same blueprint was placed with differing overrides, giving
    # it a new synthetic canonical resref.  These are potential content issues:
    # two placed instances of the same .utc file that look different in-game.
    ct_conflicts: list[dict] = []
    for bp_rr in sorted(db.creatures,
                        key=lambda r: nwn_text(db.creature_name(r)).lower()):
        variants = [
            can_rr for can_rr, meta in db.canonical_creatures.items()
            if meta["bp_rr"] == bp_rr and can_rr != bp_rr
            and not can_rr.startswith("__orphan_")
        ]
        if not variants:
            continue
        all_can = [bp_rr] + sorted(variants)
        variant_rows: list[dict] = []
        for can_rr in all_can:
            locs = db.canonical_locations.get(can_rr, [])
            loc_summary = [
                {
                    "area_resref": l["area"],
                    "area_name": db.area_name(l["area"]),
                    "kind": l["kind"],
                    "count": l["count"],
                    **({"encounter_resref": l["enc_rr"]} if l.get("enc_rr") else {}),
                }
                for l in locs
            ]
            variant_rows.append({
                "canonical_resref": can_rr,
                "is_base": can_rr == bp_rr,
                "wiki_url": _cu(can_rr),
                "location_count": sum(l["count"] for l in locs),
                "locations": loc_summary,
            })
        ct_conflicts.append({
            "blueprint_resref": bp_rr,
            "name": nwn_text(db.creature_name(bp_rr)),
            "variant_count": len(variants),
            "variants": variant_rows,
            "recommendation": (
                "Instances of this blueprint diverge from each other or from the "
                "blueprint. Consider whether the differences are intentional; if so, "
                "promote each variant to its own blueprint with a unique resref."
            ),
        })
    _write_json(module_index_dir / "creature_tag_conflicts.json", {
        "generated_at": now,
        "module": module_title,
        "conflict_count": len(ct_conflicts),
        "summary": (
            f"{len(ct_conflicts)} blueprint(s) have placed instances that differ "
            f"from each other or from the source blueprint."
            if ct_conflicts else "No creature blueprint conflicts found."
        ),
        "conflicts": ct_conflicts,
    })
    state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: creature_tag_conflicts.json ({len(ct_conflicts)} blueprint(s) with variants)"))

    # ------------------------------------------------------------------ item_index.json
    def _item_sources(rr: str) -> list[str]:
        sources: list[str] = []
        for s in db.item_sold_at.get(rr, []):
            area = db.area_name(s["area_rr"]) if "area_rr" in s else ""
            label = nwn_text(s["name"])
            sources.append("Sold at: " + label + (f" ({area})" if area else ""))
        for c in db.item_in_container.get(rr, []):
            area = db.area_name(c["area_rr"]) if "area_rr" in c else ""
            pname = nwn_text(c.get("pname", ""))
            sources.append("In container: " + pname + (f" ({area})" if area else ""))
        for c in db.item_carried_by.get(rr, []):
            area = db.area_name(c["area_rr"]) if "area_rr" in c else ""
            cname = nwn_text(c.get("cname", ""))
            sources.append("Carried by: " + cname + (f" ({area})" if area else ""))
        for s in db.item_from_script.get(rr, []):
            sources.append("Script: " + (s.get("label") or s.get("script", "")))
        return sources

    item_index: list[dict] = []
    for rr in sorted(db.items, key=lambda r: nwn_text(db.item_name(r)).lower()):
        i = db.items[rr]
        name = nwn_text(db.item_name(rr))
        if name.startswith("[TLK#") or name == rr:
            continue  # broken/unnamed items; skip
        bi_raw = fld(i, "BaseItem", None)
        bi = -1 if bi_raw is None else _try_int(bi_raw, -1)
        cost = item_gp_value(i)
        carriers = db.item_carried_by.get(rr, [])
        accessible = (
            rr in db.item_sold_at
            or rr in db.item_in_container
            or any(e.get("dropable") or e.get("pickpocketable") for e in carriers)
            or rr in db.item_from_script
        )
        item_index.append({
            "resref": rr,
            "name": name,
            "base_item": baseitem_name(bi) if bi >= 0 else "",
            "cost_gp": cost,
            "accessible": accessible,
            "wiki_url": _iu(rr),
            "sources": _item_sources(rr),
        })
    _write_json(module_index_dir / "item_index.json", {
        "generated_at": now,
        "module": module_title,
        "count": len(item_index),
        "items": item_index,
    })
    state._module_index_summary.append(("info", f"[nwn-wiki] module-index: item_index.json ({len(item_index)} items)"))

    # ------------------------------------------------------------------ cross_faction_creatures.json
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
            if crr:
                crr_fids[crr].add(fid)
    for crr, spawns in db.creature_encounter_spawns.items():
        bp = db.creatures.get(crr, {})
        fid = _int_fid(fld(bp, "FactionID"))
        if fid is None:
            continue
        for s in spawns:
            area_rr = s["area"]
            if area_rr in db.hidden_areas:
                continue
            if crr:
                crr_fids[crr].add(fid)
    cross_faction = {crr: fids for crr, fids in crr_fids.items() if len(fids) > 1}
    cf_creatures: list[dict] = []
    for crr in sorted(cross_faction,
                      key=lambda r: (nwn_text(db.canonical_creature_name(
                          db.canonical_for_bp.get(r, r))) or r).lower()):
        can_crr = db.canonical_for_bp.get(crr, crr)
        fids_sorted = sorted(cross_faction[crr])
        cf_creatures.append({
            "blueprint_resref": crr,
            "canonical_resref": can_crr,
            "name": nwn_text(db.canonical_creature_name(can_crr)),
            "wiki_url": _cu(can_crr) if can_crr in db.canonical_creatures else "",
            "factions": [
                {"id": fid, "name": db.faction_name(fid)}
                for fid in fids_sorted
            ],
            "recommendation": (
                "Blueprint appears in multiple factions. Verify the faction override "
                "on each placed instance or encounter pool slot is intentional."
            ),
        })
    _write_json(module_index_dir / "cross_faction_creatures.json", {
        "generated_at": now,
        "module": module_title,
        "count": len(cf_creatures),
        "summary": (
            f"{len(cf_creatures)} blueprint(s) appear in more than one faction."
            if cf_creatures else "No cross-faction creatures found."
        ),
        "creatures": cf_creatures,
    })
    state._module_index_summary.append(("issue", f"[nwn-wiki] module-index: cross_faction_creatures.json ({len(cf_creatures)} blueprint(s))"))

    # ------------------------------------------------------------------ faction_bp_instance_discrepancies.json
    # Each entry: a placed GIT instance whose FactionID differs from its blueprint's FactionID.
    discrepancies: list[dict] = []
    for area_rr, insts in db.area_creature_instances.items():
        if area_rr in db.hidden_areas:
            continue
        for inst in insts:
            c = inst["c"]
            bp_rr = fld(c, "TemplateResRef", "") or ""
            if not bp_rr:
                continue
            bp = db.creatures.get(bp_rr)
            if bp is None:
                continue
            inst_fid = _int_fid(fld(c, "FactionID"))
            bp_fid = _int_fid(fld(bp, "FactionID"))
            if inst_fid is None or bp_fid is None or inst_fid == bp_fid:
                continue
            can_rr = db.canonical_for_inst.get((area_rr, inst["idx"]), bp_rr)
            discrepancies.append({
                "blueprint_resref": bp_rr,
                "canonical_resref": can_rr,
                "name": nwn_text(db.canonical_creature_name(can_rr)),
                "wiki_url": _cu(can_rr) if can_rr in db.canonical_creatures else "",
                "area_resref": area_rr,
                "area_name": nwn_text(db.area_name(area_rr)),
                "area_url": _au(area_rr),
                "instance_index": inst["idx"],
                "blueprint_faction": {"id": bp_fid, "name": db.faction_name(bp_fid)},
                "instance_faction": {"id": inst_fid, "name": db.faction_name(inst_fid)},
            })
    discrepancies.sort(key=lambda d: (
        (nwn_text(db.canonical_creature_name(d["canonical_resref"])) or d["blueprint_resref"]).lower(),
        d["area_resref"],
        d["instance_index"],
    ))
    _write_json(module_index_dir / "faction_bp_instance_discrepancies.json", {
        "generated_at": now,
        "module": module_title,
        "count": len(discrepancies),
        "summary": (
            f"{len(discrepancies)} placed instance(s) have a FactionID that differs from their blueprint."
            if discrepancies else "No blueprint/instance faction discrepancies found."
        ),
        "discrepancies": discrepancies,
    })
    state._module_index_summary.append(("issue", f"[nwn-wiki] module-index: faction_bp_instance_discrepancies.json ({len(discrepancies)} instance(s))"))

    # ------------------------------------------------------------------ inaccessible_items.json
    inac_items: list[dict] = []
    for rr in sorted(db.items, key=lambda r: nwn_text(db.item_name(r)).lower()):
        i = db.items[rr]
        name = nwn_text(db.item_name(rr))
        if name.startswith("[TLK#") or name == rr:
            continue
        carriers = db.item_carried_by.get(rr, [])
        accessible = (
            rr in db.item_sold_at
            or rr in db.item_in_container
            or any(e.get("dropable") or e.get("pickpocketable") for e in carriers)
            or rr in db.item_from_script
        )
        if accessible:
            continue
        bi_raw = fld(i, "BaseItem", None)
        bi = -1 if bi_raw is None else _try_int(bi_raw, -1)
        reason = "undroppable_carried" if carriers else "not_found_anywhere"
        carrier_list = []
        for ce in carriers:
            crr = ce.get("crr", "")
            can_crr = db.canonical_for_bp.get(crr, crr)
            carrier_list.append({
                "creature_resref": crr,
                "canonical_resref": can_crr,
                "creature_name": nwn_text(db.canonical_creature_name(can_crr)) if crr else "",
                "wiki_url": _cu(can_crr) if can_crr in db.canonical_creatures else "",
            })
        inac_items.append({
            "resref": rr,
            "name": name,
            "base_item": baseitem_name(bi) if bi >= 0 else "",
            "cost_gp": item_gp_value(i),
            "wiki_url": _iu(rr),
            "reason": reason,
            "carriers": carrier_list,
        })
    _write_json(module_index_dir / "inaccessible_items.json", {
        "generated_at": now,
        "module": module_title,
        "count": len(inac_items),
        "summary": (
            f"{len(inac_items)} item(s) that players cannot obtain "
            f"(carried but not droppable, or not found anywhere)."
            if inac_items else "No inaccessible items found."
        ),
        "items": inac_items,
    })
    state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: inaccessible_items.json ({len(inac_items)} item(s))"))

    # ------------------------------------------------------------------ unspawned_creatures.json
    placed_bps: set[str] = {
        fld(inst["c"], "TemplateResRef", "") or ""
        for insts in db.area_creature_instances.values()
        for inst in insts
    } - {""}
    encounter_bps: set[str] = set(db.creature_encounter_spawns.keys())
    script_bps: set[str] = {
        sp["bp_rr"]
        for spawns in db.area_script_spawns.values()
        for sp in spawns
    }
    used_bps = placed_bps | encounter_bps | script_bps
    unspawned: list[dict] = []
    for bp_rr in sorted(db.creatures,
                        key=lambda r: nwn_text(db.creature_name(r)).lower()):
        if bp_rr in used_bps:
            continue
        can_rr = db.canonical_for_bp.get(bp_rr, bp_rr)
        name = nwn_text(db.creature_name(bp_rr))
        bp = db.creatures[bp_rr]
        cr_raw = fld(bp, "ChallengeRating") or ""
        race_raw = fld(bp, "Race")
        unspawned.append({
            "resref": bp_rr,
            "canonical_resref": can_rr,
            "name": name,
            "cr": str(cr_raw),
            "race": race_name(race_raw),
            "wiki_url": _cu(can_rr) if can_rr in db.canonical_creatures else "",
        })
    _write_json(module_index_dir / "unspawned_creatures.json", {
        "generated_at": now,
        "module": module_title,
        "count": len(unspawned),
        "summary": (
            f"{len(unspawned)} creature blueprint(s) never placed in any area, "
            f"referenced in any encounter pool, or spawned by script."
            if unspawned else
            "All creature blueprints are placed, in an encounter pool, or spawned by script."
        ),
        "creatures": unspawned,
    })
    state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: unspawned_creatures.json ({len(unspawned)} blueprint(s))"))

    # -------------------------------------------------------- instance_only_conversations.json
    # Creature instances whose Conversation field is set in the GIT placement but
    # either missing or empty on the blueprint.  The wiki reads Conversation from
    # the blueprint canonical, so these NPCs appear to have no conversation on
    # their creature detail page while showing one on the area page.
    # Fix: move the conversation value to the blueprint (.utc.json).
    inst_conv_mismatches: list[dict] = []
    seen_inst_conv: set[tuple] = set()  # (area_rr, idx) dedup
    for area_rr, insts in db.area_creature_instances.items():
        if area_rr in db.hidden_areas:
            continue
        for idx, inst in enumerate(insts):
            key_inst = (area_rr, idx)
            if key_inst in seen_inst_conv:
                continue
            seen_inst_conv.add(key_inst)
            c_git = inst["c"]
            inst_conv = (fld(c_git, "Conversation", "") or "").strip()
            if not inst_conv:
                continue
            bp_rr = (fld(c_git, "TemplateResRef", "") or "").lower()
            bp = db.creatures.get(bp_rr)
            bp_conv = (fld(bp, "Conversation", "") or "").strip() if bp else ""
            if inst_conv == bp_conv:
                continue  # Blueprint already carries the same value — no mismatch
            can_rr = db.canonical_for_inst.get((area_rr, idx), bp_rr)
            npc_name = nwn_text(
                db.canonical_creature_name(can_rr) if can_rr in db.canonical_creatures
                else (db.creature_name(bp_rr) if bp_rr else "")
            )
            inst_conv_mismatches.append({
                "blueprint_resref": bp_rr,
                "canonical_resref": can_rr,
                "npc_name": npc_name,
                "area_resref": area_rr,
                "area_name": db.area_name(area_rr),
                "instance_conversation": inst_conv,
                "blueprint_conversation": bp_conv,
                "wiki_url": _cu(can_rr) if can_rr in db.canonical_creatures else "",
                "area_url": _au(area_rr) if area_rr in db.areas else "",
            })
    inst_conv_mismatches.sort(key=lambda e: (e["area_name"].lower(), e["npc_name"].lower()))
    _write_json(module_index_dir / "instance_only_conversations.json", {
        "generated_at": now,
        "module": module_title,
        "count": len(inst_conv_mismatches),
        "summary": (
            f"{len(inst_conv_mismatches)} creature instance(s) whose Conversation field is set "
            f"on the GIT placement but not (or differently) on the blueprint. "
            f"Fix: copy the value to the blueprint .utc.json and clear the instance override."
            if inst_conv_mismatches else
            "All creature conversations are consistently set on blueprints."
        ),
        "instances": inst_conv_mismatches,
    })
    state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: instance_only_conversations.json ({len(inst_conv_mismatches)} instance(s))"))
