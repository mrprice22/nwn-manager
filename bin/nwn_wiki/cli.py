#!/usr/bin/env python3
"""nwn-wiki — generate a static HTML wiki from an unpacked NWN1 module.

Usage:
  nwn-wiki --src <unpacked-dir> --out <wiki-dir> [--module-name NAME] [--seed N]

Reads the unpacked GFF-as-JSON tree, builds derived indexes (waypoint→area
map, transitions, per-area NPCs/loot/encounters/stores), and writes a static
multi-page wiki including a force-directed SVG map of all areas.

This module is the entry point only: the argument parser, the concrete ``Db``
(assembled from the mixins in :mod:`nwn_wiki.db`) and the build pipeline that
drives the loaders, reports and renderers.  Everything it calls lives in the
sibling modules of the :mod:`nwn_wiki` package.

Pure stdlib. No third-party deps.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nwn_wiki.bestiary import (
    backfill_server_first_player_names,
    load_bestiary_stats,
    seed_bestiary_catalogue,
)
from nwn_wiki.db.core import DbCore
from nwn_wiki.db.derived import DbDerivedMixin, _quest_hidden
from nwn_wiki.db.dialogs import DbDialogsMixin
from nwn_wiki.db.index import DbIndexMixin
from nwn_wiki.db.names import DbNamesMixin
from nwn_wiki.db.scripts import DbScriptsMixin
from nwn_wiki.gff import (
    STOCK_ITEM_NAMES,
    fld,
    loc,
    read_tlk,
)
from nwn_wiki.htmlgen.chrome import (
    SiteChrome,
    load_wiki_theme,
    scan_creature_pics,
)
from nwn_wiki.layout import layout_areas
from nwn_wiki.lookups import (
    APPEARANCE,
    ARMOR_ASF,
    BASEITEMS,
    CLASSES,
    FEATS,
    IPROP_DEFS,
    IPRP_FEATS,
    PARTS_CHEST_AC,
    RACES,
    SKILLS,
    SPELL_INFO,
    SPELLS,
    WEAPONS,
    load_json_overlay,
)
from nwn_wiki.paths import ASSETS_DIR, DATA_DIR, SCRIPT_DIR
from nwn_wiki.render.activity import (
    _load_activity_cache,   # re-export: poked as wiki._load_activity_cache
    _save_activity_cache,   # re-export: poked as wiki._save_activity_cache
    parse_nwserver_logs,
    render_activity_page,   # re-export: poked as wiki.render_activity_page
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
    render_conversation_page,
    render_conversations_index,
)
from nwn_wiki.render.achievements import (achievement_counts,
                                          compute_achievements,
                                          render_achievements)
from nwn_wiki.render.characters import (
    render_character_index,
    render_character_pages,
    render_online_page,
)
from nwn_wiki.render.players import render_player_pages, render_players_index
from nwn_wiki.render.creature_page import (
    render_creature_page,
    render_creatures_search,
)
from nwn_wiki.render.leaderboards import render_leaderboards
from nwn_wiki.render.creatures import (
    load_boss_registry,
    load_boss_registry_from_src,  # re-export: poked as wiki.load_boss_registry_from_src
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
    has_inaccessible_items,
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
    render_store_instance_page,
    render_store_page,
    render_stores_index,
)
from nwn_wiki.reports.conflicts import (
    generate_conversation_conflict_report,
    generate_store_tag_conflict_report,
    generate_tag_conflict_report,
)
from nwn_wiki.reports.module_index import generate_module_index
from nwn_wiki.twoda import detect_cep_haks, load_2da_overrides
from nwn_wiki.util import (
    _tz_label_from_env,   # re-export: poked as wiki._tz_label_from_env
    _write_json,
)

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
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the CLI parser and parse ``argv`` (default: ``sys.argv[1:]``)."""
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
    ap.add_argument("--vault-dir", default=None, dest="vault_dir", metavar="DIR",
                    help="Server vault directory (servervault/<CDKEY>/*.bic). When "
                         "given, the build renders the Players section: the "
                         "character index, a page per character and the "
                         "leaderboards. Omit for modules with no server behind "
                         "them -- the whole section, including its nav entry, is "
                         "then absent rather than empty.")
    ap.add_argument("--vault-cache", default=None, dest="vault_cache", metavar="PATH",
                    help="JSON cache of parsed .bic files, keyed by path+mtime+size. "
                         "Defaults to vault-characters.json beside --activity-cache. "
                         "Parsing a full vault costs ~18s cold and ~0.3s warm, so "
                         "this is what keeps the on-empty refresh cheap.")
    ap.add_argument("--players-snapshot", default=None, dest="players_snapshot",
                    metavar="PATH",
                    help="Frozen character records, for archived seasons. After a "
                         "build with a live vault this file is (re)written; when "
                         "the vault is gone it is read INSTEAD, so a finished "
                         "season's pages stay rebuildable after its NWN_HOME_DIR "
                         "is cleaned. Commit it: CD keys are stripped on write.")
    ap.add_argument("--online-api", default="", dest="online_api", metavar="URL",
                    help="URL the Who's Online page polls for the live roster "
                         "(e.g. /api/online). Set only on the realm whose worker "
                         "serves that route; without it the page and its nav "
                         "entry are not rendered at all, which is how archived "
                         "seasons and the forks stay fully static.")
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
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------
# Build stages
#
# Each stage below is one step of the pipeline main() drives, in the order
# main() calls them.  That order is load-bearing: loaders write module globals
# in nwn_wiki.state that later renderers read, and three of the stages carry
# the original comments explaining exactly which ordering constraint they are
# satisfying (_apply_2da_overrides, _parse_activity_logs, _freeze_site_chrome).
# Values computed by one stage are passed explicitly to the next rather than
# recomputed.
# ---------------------------------------------------------------------------

def _load_tlks(args: argparse.Namespace) -> None:
    """Populate ``state.BASE_TLK`` / ``state.CUSTOM_TLK`` from the CLI paths.

    Must run before anything resolves a StrRef (i.e. before the Db loads).
    """
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


def _audit_wiki_data() -> bool:
    """wiki_data pre-flight.  Returns False (after printing) if a file is missing."""
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
        ("armor.json",            lambda: len(ARMOR_ASF),                             "bin/wiki_data/_build_stock.py"),
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
        return False
    return True


def _load_db(src: Path, args: argparse.Namespace) -> Db:
    """Construct the Db, apply the CLI dials, and run the three load passes."""
    print(f"[nwn-wiki] reading {src}")
    db = Db(
        src,
        max_character_level=args.max_character_level,
        max_ability_bonus=args.max_ability_bonus,
        max_player_level=args.max_player_level or args.max_character_level or 40,
    )
    db.exclude_option_texts = list(args.exclude_conv_option or [])
    if db.exclude_option_texts:
        print(f"[nwn-wiki] excluding map edges from dialog subtrees under: "
              + ", ".join(repr(t) for t in db.exclude_option_texts))
    print(f"[nwn-wiki] combat dials: max-character-level="
          f"{db.max_character_level or 'uncapped'}, "
          f"max-ability-bonus=+{db.max_ability_bonus}, "
          f"max-player-level={db.max_player_level}")
    db.load()
    db.index()
    db.index_dialogs()
    return db


def _compute_area_paths(db: Db, args: argparse.Namespace,
                        graph: dict) -> tuple[dict[str, list | None], str]:
    """Shortest paths back to ``--path-from``.  Empty when the flag is absent."""
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
    return area_paths, path_from_name


def _apply_2da_overrides(db: Db, src: Path, args: argparse.Namespace) -> None:
    """Overlay CEP / --2da-dir labels onto the bundled stock lookups.

    ORDER IS LOAD-BEARING (see the comment below): stock, then CEP, then
    --2da-dir, and all of it after ``db.load()`` but before any rendering.
    """
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


def _prepare_out_dir(out: Path) -> None:
    """Wipe and re-seed the output tree (.nojekyll, assets/, assets/meta.json)."""
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


def _load_theme_and_pics(db: Db, src: Path, out: Path) -> None:
    """Load the per-module theme and index/copy creature artwork.

    Both write nwn_wiki.state globals the page shell reads, so this must run
    before any page renders.
    """
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


def _module_title(db: Db, args: argparse.Namespace) -> str:
    # Module title
    title = args.module_name
    if not title and db.ifo:
        title = loc(db.ifo.get("Mod_Name")) or "NWN Module"
    title = title or "NWN Module"
    return title


def _prepare_module_index_dir(src: Path) -> Path:
    """Wipe (all but the counter-gear report) and return module-index/."""
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
    return module_index_dir


def _generate_reports(db: Db, module_index_dir: Path, title: str, graph: dict,
                      area_paths: dict[str, list | None], path_from_name: str,
                      out: Path, args: argparse.Namespace) -> None:
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


def _compute_layout(db: Db, args: argparse.Namespace) -> tuple[dict, dict]:
    """Force-directed placement of the visible areas.  Returns (positions, sizes)."""
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
    return positions, sizes


def _parse_activity_logs(args: argparse.Namespace) -> dict | None:
    """Parse --log-dir server logs and raise ``state._HAS_ACTIVITY_PAGE``.

    ORDER IS LOAD-BEARING (see the comment below): before any page() call.
    """
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
    return activity


def _load_bestiary(db: Db, args: argparse.Namespace, activity: dict | None) -> None:
    """Seed the bestiary catalogue and read kill stats from the live campaign DB."""
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


def _load_players(args: argparse.Namespace, activity: dict | None) -> None:
    """Parse the server vault and build the character records.

    No vault means no Players section: modules with no server behind them (the
    2009 and lordoftherings forks) pass no --vault-dir, and every page and nav
    entry below is gated on state._CHARACTERS being non-empty.

    Kills and the creature catalogue come from the same campaign DB the bestiary
    already reads, and sessions from the same cache the activity page maintains,
    so the Players pages can never disagree with Top Killers or Player Activity.
    """
    # Independent of the vault: a realm can serve a live roster without having
    # its character pages built, and vice versa.
    state._ONLINE_API = args.online_api or ""

    from nwn_wiki.players import bicreader, identity, model, sources

    snapshot = (Path(args.players_snapshot).expanduser()
                if args.players_snapshot else None)

    if not args.vault_dir:
        # No live vault. An archived season still has pages to render if a
        # previous build froze its records; anything else genuinely has no
        # Players section.
        if snapshot and snapshot.is_file():
            state._CHARACTERS = model.load_snapshot(snapshot)
            print(f"[nwn-wiki] players: {len(state._CHARACTERS)} character(s) "
                  f"from the frozen snapshot {snapshot}")
        return

    vault = Path(args.vault_dir).expanduser()
    if not vault.is_dir():
        if snapshot and snapshot.is_file():
            state._CHARACTERS = model.load_snapshot(snapshot)
            print(f"[nwn-wiki] players: --vault-dir {vault} is gone; rendering "
                  f"{len(state._CHARACTERS)} character(s) from {snapshot}",
                  file=sys.stderr)
        else:
            print(f"[nwn-wiki] warn: --vault-dir {vault} is not a directory; "
                  "skipping the Players section", file=sys.stderr)
        return

    # Must resolve to the SAME file nwn-wiki-activity picks, or the full build
    # and the on-empty refresh each pay the ~18s cold parse instead of sharing
    # one warm cache. Both derive it from the activity cache, whose own default
    # is <first log-dir>/activity-sessions.json.
    if args.vault_cache:
        cache_path = Path(args.vault_cache).expanduser()
    elif args.activity_cache:
        cache_path = Path(args.activity_cache).expanduser().with_name(
            "vault-characters.json")
    elif args.log_dirs:
        cache_path = Path(args.log_dirs[0]).expanduser() / "vault-characters.json"
    else:
        cache_path = vault.parent / "vault-characters.json"

    chars = bicreader.load_vault(vault, cache_path=cache_path)
    if not chars:
        print(f"[nwn-wiki] warn: no characters parsed from {vault}", file=sys.stderr)
        return

    kills: list[dict] = []
    catalogue: dict = {}
    admin_cdkeys: set[str] = set()
    dummy_best: dict = {}
    if args.db_dir:
        db_dir = Path(args.db_dir).expanduser()
        bes = sources.open_ro(db_dir / "bestiarydb.sqlite3")
        if bes is not None:
            kills = sources.load_kills(bes)
            catalogue = sources.load_catalogue(bes)
        # Admin/DM accounts are never published. NOTE: admindb is symlinked to
        # ~/.local/share/nwn-shared and is therefore SHARED across seasons, so
        # this is always the current admin list, not the one in force when an
        # archived season was played. Freeze an archived season with
        # --players-snapshot to pin its pages against later admin changes.
        adm = sources.open_ro(db_dir / "admindb.sqlite3")
        if adm is not None:
            admin_cdkeys = sources.load_admin_cdkeys(adm)
        dummy = sources.open_ro(db_dir / "combatdummydb.sqlite3")
        if dummy is not None:
            dummy_best = sources.load_combat_dummy_best(dummy)

    sessions = (activity or {}).get("sessions") or []

    # Curated identity tables are per-project; none are wired up yet, so the
    # roster merges nothing and resolves no roadmap names -- the documented
    # "report, never guess" default.
    identity.configure()
    roster = identity.build_roster(sessions, kills, chars)
    if roster.unmatched:
        print(f"[nwn-wiki] players: {len(roster.unmatched)} playerid(s) could not "
              f"be matched to an account", file=sys.stderr)

    state._CHARACTERS = model.build_records(chars, kills, sessions, roster,
                                            catalogue,
                                            exclude_cdkeys=admin_cdkeys,
                                            dummy_best=dummy_best)
    state._PLAYERS = model.build_players(state._CHARACTERS)
    state._PLAYER_SLUGS = {p["name"]: p["slug"] for p in state._PLAYERS}
    state._ACHIEVEMENTS = compute_achievements()
    state._PLAYER_ACHIEVEMENTS = achievement_counts(state._ACHIEVEMENTS)

    named = sum(1 for r in state._CHARACTERS if r["player"])
    dropped = len(chars) - len(state._CHARACTERS)
    admin_note = (f", {dropped} admin-owned character(s) excluded"
                  if dropped else "")
    print(f"[nwn-wiki] players: {len(state._CHARACTERS)} character(s), "
          f"{named} with a resolved player name{admin_note}, "
          f"{len(state._PLAYERS)} player page(s) "
          f"(cache: {cache_path})")

    if snapshot:
        model.save_snapshot(state._CHARACTERS, snapshot)
        print(f"[nwn-wiki] players: froze records to {snapshot}")


def _freeze_site_chrome(db: Db, src: Path, out: Path) -> None:
    """Load the boss registry, render the manual, then freeze ``state.CHROME``.

    ORDER IS LOAD-BEARING (see the two comments below): the boss registry must
    be loaded before ANY page renders, and the chrome can only be frozen once
    every loader the page shell depends on has run.
    """
    # Boss respawn tracker registry — must load before ANY page renders (manual
    # pages here, the activity subprocess below, and the main pages) so the
    # conditional "Bosses" nav link appears consistently on every page.
    load_boss_registry(db)

    render_manual_pages(src.parent, out)

    # Every loader that the page shell depends on has now run (theme, creature
    # pics, activity flag, server firsts, boss registry, manual menus), so
    # freeze those facts into one SiteChrome. From here on page() reads it
    # instead of the individual globals, and .chrome.json hands the identical
    # facts to the nwn-wiki-activity subprocess below — which is why that
    # subprocess can no longer emit a nav bar that disagrees with this build.
    state.CHROME = SiteChrome.from_state(
        has_inaccessible=has_inaccessible_items(db),
    )
    state.CHROME.save(out)


def _run_activity_subprocess(db: Db, src: Path, out: Path,
                             args: argparse.Namespace) -> None:
    """Hand the frozen chrome to nwn-wiki-activity, which renders its own page."""
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


def _render_pages(db: Db, src: Path, out: Path, title: str,
                  positions: dict, sizes: dict,
                  area_paths: dict[str, list | None], path_from_name: str,
                  args: argparse.Namespace) -> None:
    """Render every wiki page.  Runs after every loader stage above."""
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
    state._ITEM_PAGES = set(db.items)
    # Shared with the nwn-wiki-activity subprocess, which has no module DB of
    # its own; without it a character's stock gear would lose its name on every
    # incremental refresh and regain it on the next full build.
    state._ITEM_NAMES = {rr: db.item_name(rr) for rr in db.items}
    state._MAX_ABILITY_BONUS = db.max_ability_bonus
    try:
        (out / ".players-data.json").write_text(json.dumps({
            "version": 1,
            "item_names": state._ITEM_NAMES,
            "max_ability_bonus": state._MAX_ABILITY_BONUS,
        }), encoding="utf-8")
    except OSError as exc:
        print(f"[nwn-wiki] warn: could not write .players-data.json: {exc}",
              file=sys.stderr)
    render_online_page(out)
    render_character_index(out)
    render_character_pages(out)
    render_leaderboards(out)
    render_players_index(out)
    render_player_pages(out)
    render_achievements(out)
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


def _write_lookup_warnings(module_index_dir: Path) -> tuple[Path, list[dict]]:
    """Dump the lookup failures accumulated in ``state._warned`` during rendering."""
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
    return warnings_path, warnings_out


def _write_boss_registry(db: Db, module_index_dir: Path) -> None:
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


def _print_module_index_summary(warnings_path: Path, warnings_out: list[dict]) -> None:
    """Fold the lookup-warning line into the summary and print it."""
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()

    state._GENERATED_AT = datetime.now().strftime("%b %-d, %Y %H:%M")

    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    if not src.is_dir():
        print(f"error: --src not a directory: {src}", file=sys.stderr)
        return 1

    _load_tlks(args)
    if not _audit_wiki_data():
        return 1

    db = _load_db(src, args)

    # Area graph is always built (used for module-index and, conditionally, shortest paths).
    graph = build_area_graph(db)
    area_paths, path_from_name = _compute_area_paths(db, args, graph)

    _apply_2da_overrides(db, src, args)
    _prepare_out_dir(out)
    _load_theme_and_pics(db, src, out)

    title = _module_title(db, args)
    module_index_dir = _prepare_module_index_dir(src)
    _generate_reports(db, module_index_dir, title, graph, area_paths,
                      path_from_name, out, args)

    positions, sizes = _compute_layout(db, args)

    activity = _parse_activity_logs(args)
    _load_bestiary(db, args, activity)
    _load_players(args, activity)
    _freeze_site_chrome(db, src, out)
    _run_activity_subprocess(db, src, out, args)

    _render_pages(db, src, out, title, positions, sizes,
                  area_paths, path_from_name, args)

    warnings_path, warnings_out = _write_lookup_warnings(module_index_dir)
    _write_boss_registry(db, module_index_dir)
    _print_module_index_summary(warnings_path, warnings_out)

    print(f"[nwn-wiki] done — {out}/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
