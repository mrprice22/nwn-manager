#!/usr/bin/env python3
"""nwn-wiki — generate a static HTML wiki from an unpacked NWN1 module.

Usage:
  nwn-wiki --src <unpacked-dir> --out <wiki-dir> [--module-name NAME] [--seed N]

Reads the unpacked GFF-as-JSON tree, builds derived indexes (waypoint→area
map, transitions, per-area NPCs/loot/encounters/stores), and writes a static
multi-page wiki including a force-directed SVG map of all areas.

Pure stdlib. No third-party deps.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from nwn_wiki.bestiary import (
    _utc_to_local,
    backfill_server_first_player_names,
    load_bestiary_stats,
    seed_bestiary_catalogue,
)
from nwn_wiki.combat import (
    EPIC_TOUGHNESS_BASE,
    FINESSE_BASEITEMS,
    WEAPON_FINESSE_FEAT,
    _class_bab,
    ability_mod,
    attack_schedule,
    creature_bab,
    creature_class_bab,
    crit_feat_effects,
    epic_toughness_hp,
    feat_attack_bonus,
)
from nwn_wiki.db.core import DbCore
from nwn_wiki.db.derived import DbDerivedMixin, _quest_hidden
from nwn_wiki.db.dialogs import DbDialogsMixin
from nwn_wiki.db.index import DbIndexMixin, _store_instance_slug
from nwn_wiki.db.names import DbNamesMixin
from nwn_wiki.db.scripts import DbScriptsMixin, _strip_nss_comments
from nwn_wiki.gff import (
    STOCK_CREATURE_NAMES,
    STOCK_ITEM_NAMES,
    fld,
    gff,
    loc,
    read_tlk,
)
from nwn_wiki.htmlgen.chrome import (
    _activity_dropdown,
    _brand_html,
    _creature_cr_value,
    _custom_manual_dropdowns,
    _docs_dropdown,
    _img_pixel_size,
    _manual_menu_rows,
    _quests_nav,
    load_wiki_theme,
    page,
    scan_creature_pics,
    write,
)
from nwn_wiki.htmlgen.escape import (
    E,
    colorize_damage_words,
    nwn_first_color,
    nwn_text,
)
from nwn_wiki.htmlgen.links import (
    _conv_link,
    _faction_cell,
    _faction_dd,
    _race_link,
    _script_link,
    link,
    tileset_label,
)
from nwn_wiki.itemprops import (
    _fmt_hp,
    _item_prop_key,
    _table_lookup,
    _yn,
)
from nwn_wiki.items import (
    SHIELD_BASEITEMS,
    SLOT_CHEST,
    SLOT_CWEAP_B,
    SLOT_CWEAP_L,
    SLOT_CWEAP_R,
    SLOT_LEFT,
    SLOT_NAMES,
    SLOT_RIGHT,
    _ARMOR_BASEITEMS,
    _CEP_WEAPON_BASEITEMS,
    _CREATURE_ITEM_BASEITEMS,
    _CREATURE_WEAPON_BASEITEMS,
    _CWEAP_SLOTS,
    _SCROLL_BASEITEMS,
    _TOC_GROUPS,
    _item_category,
    _item_category_label,
    baseitem_slots,
    extract_item_defense,
    extract_item_offense,
    is_ranged_weapon,
    item_ac_bonus,
    item_attack_bonus,
    item_damage_bonus,
    item_gp_value,
    slot_label,
    weapon_crit_string,
    weapon_damage_props,
    weapon_damage_string,
    weapon_enhancement,
)
from nwn_wiki.layout import layout_areas
from nwn_wiki.lookups import (
    APPEARANCE,
    BASEITEMS,
    CLASS_BAB,
    CLASSES,
    FEATS,
    IPROP_DEFS,
    IPRP_FEATS,
    PARTS_CHEST_AC,
    RACE_ABILITY_ADJ,
    RACES,
    SKILLS,
    SPELL_INFO,
    SPELLS,
    STOCK_BASEITEMS,
    STOCK_FEAT_NAMES,
    WEAPONS,
    _CAST_SPELL_PROP_ID,
    _DAMAGE_TYPE_LABELS,
    _scroll_cast_spell_info,
    _torso_base_ac,
    appearance_name,
    baseitem_label,
    baseitem_name,
    class_name,
    creature_race_immunities,
    feat_name,
    load_json_overlay,
    placeable_name,
    race_name,
    skill_name,
    spell_name,
    tileset_name,
)
from nwn_wiki.paths import ASSETS_DIR, DATA_DIR, SCRIPT_DIR
from nwn_wiki.render.activity import (
    _load_activity_cache,
    _save_activity_cache,
    parse_nwserver_logs,
    render_activity_page,
)
from nwn_wiki.render.areas import (
    _OMIT,
    bfs_shortest_path,
    build_area_graph,
    render_area_page,
    render_areas_index,
    render_container_page,
)
from nwn_wiki.render.conversations import (
    _caller_html,
    render_conversation_page,
    render_conversations_index,
)
from nwn_wiki.render.creature_page import (
    _creature_detail_sections,
    _retaliation_sentence,
    render_creature_page,
    render_creatures_search,
)
from nwn_wiki.render.creatures import (
    _pic_figures,
    creature_max_hp,
    load_boss_registry,
    load_boss_registry_from_src,
    render_bosses_index,
    render_creature_pictures,
    render_creatures_by_area,
    render_creatures_by_cr,
    render_creatures_by_race,
    render_creatures_index,
)
from nwn_wiki.render.factions import render_factions
from nwn_wiki.render.index import render_index
from nwn_wiki.render.itemprops_pages import (
    render_items_by_property,
    render_items_search,
)
from nwn_wiki.render.items import (
    _items_col_flags,
    _items_row,
    _items_table_head,
    render_item_page,
    render_items_index,
)
from nwn_wiki.render.manual import render_manual_pages
from nwn_wiki.render.map import render_map_page
from nwn_wiki.render.quests import (
    _quest_categories,
    _quest_slugs,
    render_quest_page,
    render_quests_index,
)
from nwn_wiki.render.scripts import (
    render_script_page,
    render_scripts_index,
)
from nwn_wiki.render.stores import (
    _buy_limit_str,
    _creature_store_section,
    _store_buy_summary,
    _store_item_gp_stats,
    _store_opener_html,
    render_store_instance_page,
    render_store_page,
    render_stores_index,
)
from nwn_wiki.reports.conflicts import (
    generate_conversation_conflict_report,
    generate_store_tag_conflict_report,
    generate_tag_conflict_report,
)
from nwn_wiki.reports.counter_gear import (
    _module_index_url_helpers,
    check_counter_gear_freshness,
    generate_counter_gear_index,
)
from nwn_wiki.sim.combat import (
    avg_roll,
    simulate,
)
from nwn_wiki.sim.pc import (
    _FIRST_EPIC_LEVEL,
    _epic_toughness_tiers,
    _great_ability_tiers,
    _kit_pieces,
)
from nwn_wiki.twoda import detect_cep_haks, load_2da_overrides
from nwn_wiki.util import _try_int, _tz_label_from_env, _write_json
from nwn_wiki.warn import _warn_once

from nwn_wiki import state

_C_INFO  = "\033[36m"    # cyan — informational
_C_WARN  = "\033[33m"    # yellow — warnings
_C_ISSUE = "\033[1;31m"  # bold red — major issues
_C_RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Db(DbNamesMixin, DbDerivedMixin, DbDialogsMixin, DbScriptsMixin, DbIndexMixin,
         DbCore):
    # ---- Dialog & script cross-referencing ----

    # Module-event field → human label, used when a module-bound event
    # script is the bridge between a player action (rest, level-up …) and
    # an ActionStartConversation call.
    MODULE_EVENT_FIELDS: dict[str, str] = {
        "Mod_OnAcquirItem": "OnAcquireItem",
        "Mod_OnActvtItem": "OnActivateItem",
        "Mod_OnClientEntr": "OnClientEnter",
        "Mod_OnClientLeav": "OnClientLeave",
        "Mod_OnCutsnAbort": "OnCutsceneAbort",
        "Mod_OnHeartbeat": "OnHeartbeat",
        "Mod_OnModLoad": "OnModuleLoad",
        "Mod_OnModStart": "OnModuleStart",
        "Mod_OnPlrDeath": "OnPlayerDeath",
        "Mod_OnPlrDying": "OnPlayerDying",
        "Mod_OnPlrEqItm": "OnPlayerEquipItem",
        "Mod_OnPlrLvlUp": "OnPlayerLevelUp",
        "Mod_OnPlrRest": "OnPlayerRest",
        "Mod_OnPlrUnEqItm": "OnPlayerUnequipItem",
        "Mod_OnSpawnBtnDn": "OnSpawnButtonDown",
        "Mod_OnUnAqreItem": "OnUnacquireItem",
        "Mod_OnUsrDefined": "OnUserDefined",
    }

    # Per-blueprint event slots that we walk for ActionStartConversation
    # calls. Names mirror the GFF field names on the blueprint.
    CREATURE_EVENT_FIELDS: dict[str, str] = {
        "ScriptDialogue": "OnConversation",
        "ScriptSpawn": "OnSpawn",
        "ScriptHeartbeat": "OnHeartbeat",
        "ScriptOnNotice": "OnPerception",
        "ScriptUserDefine": "OnUserDefined",
    }
    PLACEABLE_EVENT_FIELDS: dict[str, str] = {
        "OnUsed": "OnUsed",
        "OnDialog": "OnConversation",
        "OnHeartbeat": "OnHeartbeat",
        "OnUserDefined": "OnUserDefined",
        "OnOpen": "OnOpen",
        "OnClick": "OnClick",
    }
    DOOR_EVENT_FIELDS: dict[str, str] = {
        "OnUsed": "OnUsed",
        "OnDialog": "OnConversation",
        "OnOpen": "OnOpen",
        "OnClick": "OnClick",
    }
    TRIGGER_EVENT_FIELDS: dict[str, str] = {
        "OnEnter": "OnEnter",
        "OnExit": "OnExit",
        "OnHeartbeat": "OnHeartbeat",
        "OnUserDefined": "OnUserDefined",
        "ScriptOnEnter": "OnEnter",
        "ScriptOnExit": "OnExit",
        "ScriptHeartbeat": "OnHeartbeat",
        "ScriptUserDefine": "OnUserDefined",
    }
    AREA_EVENT_FIELDS: dict[str, str] = {
        "OnEnter": "OnEnter",
        "OnExit": "OnExit",
        "OnHeartbeat": "OnHeartbeat",
        "OnUserDefined": "OnUserDefined",
    }



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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True, help="path to unpacked/ tree")
    ap.add_argument("--out", required=True, help="path to output wiki/ dir")
    ap.add_argument("--module-name", default=None,
                    help="override module title (default: from module.ifo)")
    ap.add_argument("--seed", type=int, default=1, help="layout RNG seed")
    ap.add_argument("--2da-dir", dest="twoda_dir", default=None,
                    help="directory of extracted 2DA files (e.g. baseitems.2da, "
                         "iprp_feats.2da) to override the bundled stock lookups. "
                         "Use this when the module relies on CEP / custom HAKs.")
    ap.add_argument("--dialog-tlk", default=None,
                    help="path to base-game dialog.tlk for resolving "
                         "StrRef references (id < 16777216).")
    ap.add_argument("--nwn-install", default=None,
                    help="(ignored — stock item names are now pre-cached in "
                         "wiki_data/stock_item_names.json; see bin/refresh-nwn-stock-items)")
    ap.add_argument("--custom-tlk", default=None,
                    help="path to the module's custom TLK (named in module.ifo "
                         "Mod_CustomTlk) for resolving StrRefs >= 16777216.")
    ap.add_argument("--base-url", default="",
                    help="public URL the wiki is served from (e.g. "
                         "https://user.github.io/project/). When set, the area "
                         "map SVG uses absolute URLs for node links so a "
                         "downloaded standalone SVG keeps clickable links.")
    ap.add_argument("--exclude-conv-option", action="append", default=[],
                    metavar="TEXT",
                    help="Player-option label (exact match, after stripping "
                         "NWN colour tokens and surrounding whitespace) whose "
                         "dialog subtree should NOT contribute teleport edges "
                         "to the area map. Repeat the flag for multiple "
                         "labels (e.g. --exclude-conv-option '[Admin Options]' "
                         "--exclude-conv-option '[DM Options]'). Use this to "
                         "hide admin/DM teleport menus from cluttering the map "
                         "— the teleports still appear on the conversation "
                         "page, only the map edges are suppressed.")
    ap.add_argument("--log-dir", action="append", default=[], dest="log_dirs",
                    metavar="DIR",
                    help="Directory containing NWN server logs (nwserverLog*.txt) "
                         "or a parent directory whose subdirectories contain them "
                         "(e.g. ~/.local/state/nwnxee-mygame/ whose logs.0/, "
                         "logs.1/, … hold nwserverLog1.txt). Repeat for multiple "
                         "log roots. When provided, an Activity page with "
                         "player-activity charts is generated and added to the wiki.")
    ap.add_argument("--db-dir", default=None, dest="db_dir", metavar="DIR",
                    help="Directory holding the live NWN:EE campaign SQLite "
                         "databases (where bestiary.sqlite3 lives). Used to seed "
                         "the bestiary creature catalogue and read kill stats. "
                         "On containerized servers this is the host path bound to "
                         "the server's database/ dir, which usually differs from "
                         "--log-dir. If omitted, falls back to <log-dir>/database "
                         "when that is a real directory.")
    ap.add_argument("--activity-cache", default=None, dest="activity_cache",
                    metavar="PATH",
                    help="JSON file for persisting player-session data across log "
                         "rotations. Defaults to activity-sessions.json inside the "
                         "first --log-dir. Keeps historical hours from being lost "
                         "when old log rotations are deleted by the server.")
    ap.add_argument("--path-from", default=None, metavar="RESREF",
                    help="Area resref to compute shortest paths FROM. When given, "
                         "every area page (except the source itself) shows the "
                         "shortest path back to this area via door/trigger/conversation "
                         "transitions. Omit to suppress the section entirely.")
    ap.add_argument("--cr-bucket-size", type=int, default=10, metavar="N",
                    dest="cr_bucket_size",
                    help="Width of each Challenge Rating range bucket on the "
                         "Creatures → By Challenge Rating page (default: 10, "
                         "producing CR 0–9, CR 10–19, …).")
    ap.add_argument("--max-character-level", type=int, default=40, metavar="N",
                    dest="max_character_level",
                    help="Server level cap used when deriving a creature's base "
                         "attack bonus: its total class levels are clamped to N "
                         "(in ClassList order) so an over-HD boss doesn't get "
                         "unbounded BAB. 0 = no cap. Default: 40 (NWN default).")
    ap.add_argument("--max-ability-bonus", type=int, default=12, metavar="N",
                    dest="max_ability_bonus",
                    help="Cap on the ability-score bonus a single item may grant, "
                         "applied when folding equipped-item ability bonuses into "
                         "a creature's effective scores. Default: 12 (NWN default; "
                         "some modules raise it, e.g. 24).")
    ap.add_argument("--max-player-level", type=int, default=0, metavar="N",
                    dest="max_player_level",
                    help="Level cap a player character can actually reach, used "
                         "for the counter-gear report's reference PC. Separate "
                         "from --max-character-level (which clamps creature BAB): "
                         "a server running NWNX MaxLevel can let players past 40 "
                         "while creature stats still want the engine cap. "
                         "0 (default) = follow --max-character-level.")
    ap.add_argument("--devcrit-bonus-dice", type=int, default=0, metavar="N",
                    dest="devcrit_bonus_dice",
                    help="Bonus damage dice a critical hit deals when the module "
                         "has replaced the engine's save-or-die Devastating "
                         "Critical with flat damage. Whether the engine's version "
                         "is still active is DETECTED from baseitems.2da's "
                         "EpicWeaponDevastatingCriticalFeat column, not "
                         "configured; this only supplies the replacement, whose "
                         "die size follows WeaponSize (1-2 d6, 3 d8, 4+ d10). "
                         "Default: 0 (stock behaviour).")
    ap.add_argument("--counter-gear", action="store_true", dest="counter_gear",
                    help="Run the counter-gear combat simulation and rewrite "
                         "module-index/counter_gear.{json,md}. Off by default "
                         "because it simulates every creature against every "
                         "attainable item; normal runs instead compare an input "
                         "fingerprint and warn when the existing report is stale.")
    args = ap.parse_args()

    state._GENERATED_AT = datetime.now().strftime("%b %-d, %Y %H:%M")

    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    if not src.is_dir():
        print(f"error: --src not a directory: {src}", file=sys.stderr)
        return 1

    if args.dialog_tlk:
        p = Path(args.dialog_tlk).resolve()
        if p.is_file():
            state.BASE_TLK.update(read_tlk(p))
            print(f"[nwn-wiki] loaded base TLK ({len(state.BASE_TLK)} entries) from {p}")
        else:
            print(f"warn: --dialog-tlk {p} not found; StrRefs will show as [TLK#N]",
                  file=sys.stderr)
    if args.custom_tlk:
        p = Path(args.custom_tlk).resolve()
        if p.is_file():
            state.CUSTOM_TLK.update(read_tlk(p))
            print(f"[nwn-wiki] loaded custom TLK ({len(state.CUSTOM_TLK)} entries) from {p}")
        else:
            print(f"warn: --custom-tlk {p} not found; custom StrRefs will show as [TLK#N]",
                  file=sys.stderr)

    if args.nwn_install:
        print("[nwn-wiki] --nwn-install is ignored; stock item names are pre-cached in "
              "wiki_data/stock_item_names.json (run bin/refresh-nwn-stock-items to rebuild)",
              file=sys.stderr)

    # --- wiki_data pre-flight: confirm all data files are present ---
    _DATA_AUDIT: list[tuple[str, Any, str]] = [
        ("baseitems.json",        lambda: len(BASEITEMS),                              "bin/wiki_data/_build_stock.py"),
        ("classes.json",          lambda: len(CLASSES),                               "bin/wiki_data/_build_stock.py"),
        ("racialtypes.json",      lambda: len(RACES),                                 "bin/wiki_data/_build_stock.py"),
        ("appearance.json",       lambda: len(APPEARANCE),                            "bin/wiki_data/_build_stock.py"),
        ("feat.json",             lambda: len(FEATS),                                 "bin/wiki_data/_build_stock.py"),
        ("iprp_feats.json",       lambda: len(IPRP_FEATS),                            "bin/wiki_data/_build_stock.py"),
        ("skills.json",           lambda: len(SKILLS),                                "bin/wiki_data/_build_stock.py"),
        ("spells.json",           lambda: len(SPELLS),                                "bin/wiki_data/_build_stock.py"),
        ("weapons.json",          lambda: len(WEAPONS),                               "bin/wiki_data/_build_stock.py"),
        ("itemprops.json",        lambda: len(IPROP_DEFS),                            "committed — restore from git"),
        ("parts_chest.2da",       lambda: len(PARTS_CHEST_AC),                        "committed — restore from git"),
        ("stock_item_names.json", lambda: len(STOCK_ITEM_NAMES),                      "bin/refresh-nwn-stock-items"),
        ("spell_info.json",       lambda: sum(len(v) for v in SPELL_INFO.values()),   "bin/refresh-nwn-spell-info"),
    ]
    _missing_data: list[tuple[str, str]] = []
    for _fname, _count_fn, _refresh in _DATA_AUDIT:
        _p = DATA_DIR / _fname
        if _p.exists():
            print(f"[nwn-wiki] data: {_fname:<25} ok ({_count_fn()})")
        else:
            print(f"[nwn-wiki] data: {_fname:<25} MISSING", file=sys.stderr)
            _missing_data.append((_fname, _refresh))
    if _missing_data:
        _by_cmd: dict[str, list[str]] = {}
        for _fname, _refresh in _missing_data:
            _by_cmd.setdefault(_refresh, []).append(_fname)
        print("error: required wiki_data files are missing:", file=sys.stderr)
        for _cmd, _files in _by_cmd.items():
            print(f"  {_cmd}  ({', '.join(_files)})", file=sys.stderr)
        return 1

    print(f"[nwn-wiki] reading {src}")
    db = Db(src)
    db.exclude_option_texts = list(args.exclude_conv_option or [])
    if db.exclude_option_texts:
        print(f"[nwn-wiki] excluding map edges from dialog subtrees under: "
              + ", ".join(repr(t) for t in db.exclude_option_texts))
    db.max_character_level = args.max_character_level
    db.max_ability_bonus = args.max_ability_bonus
    db.max_player_level = args.max_player_level or args.max_character_level or 40
    print(f"[nwn-wiki] combat dials: max-character-level="
          f"{db.max_character_level or 'uncapped'}, "
          f"max-ability-bonus=+{db.max_ability_bonus}, "
          f"max-player-level={db.max_player_level}")
    db.load()
    db.index()
    db.index_dialogs()

    # Area graph is always built (used for module-index and, conditionally, shortest paths).
    graph = build_area_graph(db)

    # Shortest-path data keyed by area resref (only populated when --path-from given).
    area_paths: dict[str, list | None] = {}
    path_from_name: str = ""
    if args.path_from:
        src_rr = args.path_from
        if src_rr not in db.areas:
            print(f"warn: --path-from {src_rr!r} not found in module areas; "
                  "skipping shortest-path sections", file=sys.stderr)
        else:
            path_from_name = db.area_name(src_rr)
            print(f"[nwn-wiki] computing shortest paths from {src_rr!r} ({path_from_name})")
            for rr in db.areas:
                if rr == src_rr:
                    continue
                area_paths[rr] = bfs_shortest_path(graph, src_rr, rr)

    # Apply 2DA-derived label overrides on top of the bundled stock JSON.
    # The lookup dicts (BASEITEMS, RACES, …) are module-level and only
    # consulted during rendering, so it's safe to mutate them after db.load().
    # Order matters — later writers win:
    #   1. stock         (baked into wiki_data/*.json on import)
    #   2. CEP overlay   (auto-loaded when the IFO references any cep* hak)
    #   3. --2da-dir     (or auto-picked <src>/../hak_2da, <src>/2da)
    cep_haks = detect_cep_haks(db.ifo)
    if cep_haks:
        cep_overlay = DATA_DIR / "cep"
        if cep_overlay.is_dir():
            print(f"[nwn-wiki] applying CEP overlay (haks: {', '.join(cep_haks)})")
            load_json_overlay(cep_overlay, label="cep")
        else:
            print(f"warn: CEP haks present but {cep_overlay} not bundled; "
                  f"run wiki_data/cep/_build.py to regenerate",
                  file=sys.stderr)

    twoda_dir: Path | None = None
    if args.twoda_dir:
        twoda_dir = Path(args.twoda_dir).resolve()
    else:
        for candidate in (src.parent / "hak_2da", src / "2da"):
            if candidate.is_dir():
                twoda_dir = candidate
                break
    db.twoda_dir = twoda_dir
    db.run_counter_gear = bool(args.counter_gear)
    db.devcrit_bonus_dice = max(0, args.devcrit_bonus_dice)
    if twoda_dir is not None:
        if twoda_dir.is_dir():
            print(f"[nwn-wiki] applying 2DA overrides from {twoda_dir}")
            load_2da_overrides(twoda_dir)
        else:
            print(f"warn: --2da-dir {twoda_dir} is not a directory; ignoring",
                  file=sys.stderr)

    # Wipe stale generated content so deleted resources don't linger in the
    # wiki. We only nuke the contents — keeping the directory itself preserves
    # any user-created tooling (CNAME, custom workflow files) co-located in
    # docs/. CNAME is the one well-known file we explicitly preserve.
    if out.is_dir():
        cname = out / "CNAME"
        keep_cname = cname.read_bytes() if cname.is_file() else None
        for child in out.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        if keep_cname is not None:
            cname.write_bytes(keep_cname)

    out.mkdir(parents=True, exist_ok=True)
    # Disable Jekyll on GitHub Pages so files starting with _ etc. are served verbatim.
    (out / ".nojekyll").touch()
    # Copy bundled stylesheet (idempotent)
    if ASSETS_DIR.is_dir():
        (out / "assets").mkdir(parents=True, exist_ok=True)
        for f in ASSETS_DIR.iterdir():
            if f.is_file():
                shutil.copy2(f, out / "assets" / f.name)
    (out / "assets" / "meta.json").write_text(
        json.dumps({"generated_at": state._GENERATED_AT}, ensure_ascii=False)
    )

    # Load per-module theme from wiki-theme/ next to the unpacked/ directory.
    load_wiki_theme(src.parent, out)

    # Index creature artwork from creature-pics/ and copy the files into the
    # output tree (creatures/pics/). Done after db is built so names resolve.
    scan_creature_pics(src.parent, db)
    if state._CREATURE_PIC_GROUPS:
        pics_src = src.parent / "creature-pics"
        pics_out = out / "creatures" / "pics"
        pics_out.mkdir(parents=True, exist_ok=True)
        wanted = {fn for grp in state._CREATURE_PIC_GROUPS for fn in grp["images"]}
        for f in pics_src.iterdir():
            if f.is_file() and f.name in wanted:
                shutil.copy2(f, pics_out / f.name)

    # Module title
    title = args.module_name
    if not title and db.ifo:
        title = loc(db.ifo.get("Mod_Name")) or "NWN Module"
    title = title or "NWN Module"

    module_index_dir = src.parent / "module-index"
    # Wipe stale content so removed resources don't linger across refreshes.
    # The counter-gear report is exempt: it is rebuilt only on demand
    # (--counter-gear), so deleting it here would destroy the previous run's
    # analysis and silently force the slow rebuild on every refresh.
    _KEEP = {"counter_gear.json", "counter_gear.md"}
    if module_index_dir.is_dir():
        for _child in module_index_dir.iterdir():
            if _child.name in _KEEP:
                continue
            if _child.is_dir():
                shutil.rmtree(_child)
            else:
                _child.unlink()
    module_index_dir.mkdir(parents=True, exist_ok=True)

    # Tag-conflict burndown list + all module-index JSON exports
    generate_tag_conflict_report(db, module_index_dir, title, out, args.base_url or "")
    generate_store_tag_conflict_report(db, module_index_dir, title, out, args.base_url or "")
    generate_conversation_conflict_report(db, module_index_dir, title, out, args.base_url or "")
    generate_module_index(
        db, module_index_dir, title,
        graph, area_paths if args.path_from else None,
        args.path_from or "", path_from_name,
        out, args.base_url or "",
    )

    # Layout
    print("[nwn-wiki] computing area layout")
    t0 = time.time()
    visible_areas = {r for r in db.areas if r not in db.hidden_areas}
    edges = []
    for tr in db.transitions:
        a, b = tr["src_area"], tr["dst_area"]
        if a in visible_areas and b in visible_areas:
            edges.append((a, b))
        # Include alt destinations for dup-tag transitions so they appear on the map.
        if tr.get("is_dup_tag"):
            for alt in tr.get("dst_area_alts", []):
                if a in visible_areas and alt in visible_areas:
                    edges.append((a, alt))
    for tr in db.script_transitions:
        a, b = tr["src_area"], tr["dst_area"]
        if a in visible_areas and b and b in visible_areas:
            edges.append((a, b))
    # Conversation-teleport edges and the pseudo-nodes that sit at the
    # source of "global" trigger conversations (rest menu, item activators).
    pseudo_nodes = sorted(db.global_convo_pseudo.keys())
    for tr in db.conv_transitions:
        s = tr["src"]
        d = tr["dst_area"]
        # Either both endpoints are visible areas, or src is a pseudo-node we'll
        # add to node_ids below.
        if d not in visible_areas:
            continue
        if s in visible_areas or s in db.global_convo_pseudo:
            edges.append((s, d))
    positions, sizes = layout_areas(
        sorted(visible_areas) + pseudo_nodes,
        edges,
        db=db,
        seed=args.seed,
    )
    print(f"  layout in {time.time() - t0:.1f}s")

    # Parse server logs and set the activity-page flag BEFORE any page() calls
    # so the Activity nav link appears on every rendered page.
    activity: dict | None = None
    if args.log_dirs:
        log_dir_paths = [Path(d).resolve() for d in args.log_dirs]
        if args.activity_cache:
            cache_path = Path(args.activity_cache).resolve()
        else:
            cache_path = log_dir_paths[0] / "activity-sessions.json"
        activity = parse_nwserver_logs(log_dir_paths, cache_path=cache_path)
        player_sessions = [
            s for s in activity["sessions"]
            if s.get("join") is not None and s.get("role") == "Player"
        ]
        if player_sessions:
            state._HAS_ACTIVITY_PAGE = True
            print(f"[nwn-wiki] found {len(player_sessions)} player sessions "
                  f"across {activity['file_count']} log file(s) "
                  f"(cache: {cache_path})")
        else:
            print("[nwn-wiki] log-dir specified but no player sessions found; "
                  "skipping activity page", file=sys.stderr)

    # Bestiary kill stats: only when this module ships the bestiary system
    # (detected by the book item). Locate the live campaign-DB dir from --db-dir,
    # falling back to <log-dir>/database when that is a real directory (it is a
    # dangling container symlink on this server, hence the explicit --db-dir).
    if "bestiarybook" in db.items:
        _bestiary_db_dir: Path | None = None
        if args.db_dir:
            _bestiary_db_dir = Path(args.db_dir).expanduser()
        elif args.log_dirs:
            _cand = Path(args.log_dirs[0]).expanduser() / "database"
            if _cand.is_dir():
                _bestiary_db_dir = _cand
        if _bestiary_db_dir is not None:
            seed_bestiary_catalogue(db, _bestiary_db_dir)
            # Migrate/back-fill server-first player names from log cdkeys BEFORE
            # reading stats, so the Player column is populated in this same build.
            if activity is not None:
                backfill_server_first_player_names(
                    _bestiary_db_dir, activity["sessions"])
            load_bestiary_stats(
                _bestiary_db_dir,
                activity["sessions"] if activity is not None else None)
        else:
            print("[nwn-wiki] bestiary: no reachable database dir "
                  "(set --db-dir); skipping kill stats", file=sys.stderr)

    # Boss respawn tracker registry — must load before ANY page renders (manual
    # pages here, the activity subprocess below, and the main pages) so the
    # conditional "Bosses" nav link appears consistently on every page.
    load_boss_registry(db)

    render_manual_pages(src.parent, out)

    if state._HAS_ACTIVITY_PAGE:
        _act_cmd = [sys.executable, str(SCRIPT_DIR / "nwn-wiki-activity"),
                    "--src", str(src), "--out", str(out)]
        for _d in args.log_dirs:
            _act_cmd += ["--log-dir", str(_d)]
        if args.activity_cache:
            _act_cmd += ["--activity-cache", str(args.activity_cache)]
        if "bestiarybook" in db.items and args.db_dir:
            _act_cmd += ["--db-dir", str(Path(args.db_dir).expanduser())]
        subprocess.run(_act_cmd, check=True)

    print("[nwn-wiki] rendering pages")
    t0 = time.time()
    render_index(db, out, title, positions, sizes, base_url=args.base_url,
                 project_root=src.parent)
    render_map_page(db, out, positions, sizes, base_url=args.base_url)
    render_areas_index(db, out,
                       area_paths=area_paths if args.path_from else None,
                       path_from_resref=args.path_from or "",
                       path_from_name=path_from_name)
    for resref in db.areas:
        if resref in db.hidden_areas:
            continue
        render_area_page(db, resref, out,
                         path_from_name=path_from_name,
                         path_steps=area_paths.get(resref, _OMIT))
    for resref, conts in db.area_containers.items():
        if resref in db.hidden_areas:
            continue
        for c in conts:
            render_container_page(db, resref, c, out)
    render_creatures_index(db, out)
    render_bosses_index(db, out)
    render_creature_pictures(db, out)
    render_creatures_by_area(db, out)
    render_creatures_by_cr(db, out, cr_bucket_size=args.cr_bucket_size)
    render_creatures_by_race(db, out)
    render_creatures_search(db, out)
    state._current_context = ""
    for can_rr in db.canonical_creatures:
        state._current_context = f"creature:{can_rr} ({db.canonical_creature_name(can_rr)})"
        render_creature_page(db, can_rr, out)
    state._current_context = ""
    render_items_index(db, out)
    for resref in db.items:
        state._current_context = f"item:{resref} ({db.item_name(resref)})"
        render_item_page(db, resref, out)
    state._current_context = ""
    render_items_by_property(db, out)
    state._current_context = ""
    render_items_search(db, out)
    state._current_context = ""
    render_stores_index(db, out)
    for area_rr, inst_list in db.area_stores.items():
        if area_rr in db.hidden_areas:
            continue
        for inst in inst_list:
            render_store_instance_page(db, area_rr, inst, out)
    for resref in db.stores:
        render_store_page(db, resref, out)
    render_conversations_index(db, out)
    for resref in db.dialogs:
        render_conversation_page(db, resref, out)
    render_scripts_index(db, out)
    for resref in db.script_paths:
        render_script_page(db, resref, out)
    render_factions(db, out)
    qcats = _quest_categories(db)
    qslugs = _quest_slugs(qcats)
    render_quests_index(db, out)
    for qcat, qslug in zip(qcats, qslugs):
        if _quest_hidden(fld(qcat, "Comment", "")):
            continue  # retired/inactive quest — no detail page
        render_quest_page(db, qcat, qslug, out)
    print(f"  rendered in {time.time() - t0:.1f}s")

    # Write lookup warnings to module-index so they're visible outside the build log.
    warnings_path = module_index_dir / "lookup_warnings.json"
    sorted_warnings = sorted(state._warned.keys())
    warnings_out = [
        {"message": msg, "referenced_by": sorted(state._warned[msg])}
        for msg in sorted_warnings
    ]
    _write_json(warnings_path, {
        "_description": "Lookup failures encountered during wiki generation. "
                        "Integers or [TLK#N] placeholders may appear on wiki pages "
                        "for each entry below. Re-run with --dialog-tlk / --custom-tlk "
                        "and/or --2da-dir to resolve.",
        "count": len(warnings_out),
        "warnings": warnings_out,
    })

    # Boss respawn tracker registry (parsed from brd_db.nss BRD_SeedBoss rows —
    # the same list the game seeds into respawndb, and the creatures/bosses.html
    # page renders). Written for LLM-assist consumers.
    if state._BOSS_REGISTRY:
        _write_json(module_index_dir / "bosses.json", {
            "_description": "Bosses tracked by the in-game 'Roll of the Fallen' "
                            "respawn board, parsed from the BRD_SeedBoss rows in "
                            "unpacked/brd_db.nss (single source of truth shared "
                            "with the game). See creatures/bosses.html.",
            "count": len(state._BOSS_REGISTRY),
            "aliases": state._BOSS_ALIASES,
            "bosses": [
                {
                    "resref": b["resref"],
                    "name": b["name"],
                    "tag": b["tag"],
                    "area_resref": b["area"],
                    "area_name": b["area_name"],
                    "cr": b["cr"],
                    "has_creature_page": b["resref"] in db.canonical_creatures,
                    "kills": state._BESTIARY_KILLS.get(b["resref"]),
                }
                for b in sorted(state._BOSS_REGISTRY, key=lambda b: -b["cr"])
            ],
        })
    if warnings_out:
        state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: lookup_warnings.json ({len(warnings_out)} lookup failure(s)) — {warnings_path}"))
    else:
        state._module_index_summary.append(("warn", f"[nwn-wiki] module-index: lookup_warnings.json (no lookup failures)"))

    if state._module_index_summary:
        print()
        print("[nwn-wiki] module-index summary:")
        for label, color, sev in [
            ("Informational", _C_INFO,  "info"),
            ("Warnings",      _C_WARN,  "warn"),
            ("Issues",        _C_ISSUE, "issue"),
        ]:
            msgs = [m for s, m in state._module_index_summary if s == sev]
            if msgs:
                print(f"  {color}{label}:{_C_RESET}")
                for m in msgs:
                    print(f"    {color}{m}{_C_RESET}")
    
    
    print(f"[nwn-wiki] done — {out}/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
