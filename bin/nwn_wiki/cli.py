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
import hashlib
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
    weapon_feat_id,
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
    list_items,
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
    nwn_html,
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
    _prop_value_num,
    _store_inv_key,
    _table_lookup,
    _yn,
    itemprop_oneliner,
)
from nwn_wiki.items import (
    CRIT_IMMUNITY_LABEL,
    PLAYER_SLOT_MASK,
    PLAYER_SLOTS,
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
    _item_accessible,
    _item_category,
    _item_category_label,
    baseitem_slots,
    extract_item_defense,
    extract_item_offense,
    is_ranged_weapon,
    item_ac_bonus,
    item_attack_bonus,
    item_damage_bonus,
    item_equip_slots,
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
    BASEITEM_COLUMNS_SEEN,
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
    extract_creature_defenses,
    extract_creature_offense,
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
from nwn_wiki.render.manual import (
    _RE_MANUAL_MENU,
    _manual_doc_body,
    _manual_menu,
    _manual_menu_order,
    _manual_sort_order,
)
from nwn_wiki.render.map import (
    _MAP_HINT_HTML,
    render_map_page,
    render_map_svg,
)
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
from nwn_wiki.sim.combat import (
    attack_profile,
    avg_roll,
    defense_profile,
    simulate,
)
from nwn_wiki.sim.gear import (
    _prune_pool,
    best_in_slot_kit,
    build_gear_pool,
    minimum_viable_kit,
)
from nwn_wiki.sim.pc import (
    _FIRST_EPIC_LEVEL,
    _epic_toughness_tiers,
    _great_ability_tiers,
    _kit_pieces,
)
from nwn_wiki.twoda import detect_cep_haks, load_2da_overrides
from nwn_wiki.util import _try_int
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
# Renderers
# ---------------------------------------------------------------------------

def render_index(db: Db, out: Path, module_title: str,
                 positions: dict[str, tuple[float, float]],
                 sizes: dict[str, tuple[float, float]],
                 base_url: str = "", project_root: Path | None = None) -> None:
    # Author-supplied landing page replaces the generated overview/map index.
    # .html takes precedence over .md. Body fragment only (same handling as
    # docs.manual pages) — page() adds the nav header/footer. The map still
    # lives on its own dedicated /map page (see render_map_page).
    if project_root is not None:
        override = next((project_root / f"index.{ext}" for ext in ("html", "md")
                         if (project_root / f"index.{ext}").is_file()), None)
        if override is not None:
            _, body_html = _manual_doc_body(override)
            write(out / "index.html", page(module_title, body_html, root_rel="."))
            print(f"[nwn-wiki] index: using override {override}")
            return

    # Module overview block
    ifo = db.ifo or {}
    start_area = fld(ifo, "Mod_Entry_Area", "")
    haks = list_items(ifo.get("Mod_HakList"))
    hak_names = [fld(h, "Mod_Hak", "") for h in haks]
    tlk = fld(ifo, "Mod_CustomTlk", "")
    xp = fld(ifo, "Mod_XPScale")
    desc = loc(ifo.get("Mod_Description")) if ifo else ""

    overview = [
        f'<h1>{nwn_html(module_title)}</h1>',
        '<dl class="meta">',
        f'<dt>Areas</dt><dd>{len(db.areas)}</dd>',
        f'<dt>Creatures</dt><dd>{len(db.creatures)}</dd>',
        f'<dt>Items</dt><dd>{len(db.items)}</dd>',
        f'<dt>Stores</dt><dd>{len(db.stores)}</dd>',
        f'<dt>Dialogues</dt><dd>{len(db.dialogs)}</dd>',
        f'<dt>Scripts</dt><dd>{len(db.scripts)}</dd>',
    ]
    if start_area:
        overview.append(f'<dt>Entry area</dt><dd>{link(f"areas/{start_area}.html", db.area_name(start_area))}</dd>')
    if tlk:
        overview.append(f'<dt>Custom TLK</dt><dd>{E(tlk)}</dd>')
    if xp is not None:
        overview.append(f'<dt>XP scale</dt><dd>{E(xp)}%</dd>')
    if hak_names:
        overview.append(f'<dt>HAKs</dt><dd>{E(", ".join(hak_names))}</dd>')
    overview.append('</dl>')
    if desc:
        overview.append(f'<p class="desc">{nwn_html(desc)}</p>')

    # Global-triggered conversations (rest menu, item activators, …) get
    # called out above the map: a player can fire them from anywhere, and
    # they often hide teleport destinations the map otherwise can't show.
    global_dlgs = sorted(
        db.global_convo_pseudo.values(),
        key=lambda info: info["conv_resref"],
    )
    if global_dlgs:
        overview.append("<h2>Global-trigger conversations</h2>")
        overview.append('<p class="muted">Reachable from anywhere via a '
                        'module-level event (rest, level-up, etc.) or a '
                        'tag-based item activator. Each contains at least '
                        'one teleport.</p>')
        rows = []
        for info in global_dlgs:
            rr = info["conv_resref"]
            callers = db.dialog_callers.get(rr, [])
            kinds = sorted({(c["kind"], c.get("event") or c.get("script", ""))
                            for c in callers
                            if c["kind"] in ("module-event", "item-script")})
            via = ", ".join(
                f"<code>{E(ev)}</code>" if k == "module-event"
                else f"item <code>{E(ev)}</code>"
                for k, ev in kinds
            )
            dests = ", ".join(
                link(f"areas/{a}.html", db.area_name(a))
                for a in info["dests"] if a in db.areas and a not in db.hidden_areas)
            rows.append(
                f"<tr><td>{link(f'conversations/{rr}.html', db.dialog_label(rr))}</td>"
                f"<td><code>{E(rr)}</code></td>"
                f"<td>{via}</td>"
                f"<td>{dests}</td></tr>"
            )
        overview.append(
            '<table class="data"><thead><tr>'
            "<th>Conversation</th><th>ResRef</th><th>Triggered via</th>"
            "<th>Teleports to</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    overview.append('<h2>Area map</h2>')
    overview.append(_MAP_HINT_HTML)
    overview.append(render_map_svg(db, positions, sizes, base_url=base_url))

    body = "\n".join(overview)
    write(out / "index.html", page(module_title, body, root_rel="."))


# =============================================================================
# NWN server log parser
# =============================================================================

_LOG_JOIN_RE = re.compile(
    r'^\[(\w{3} \w{3} [ \d]\d \d{2}:\d{2}:\d{2})\] (.+?) \((\w+)\) Joined as (Player|Game Master) \d+'
)
_LOG_LEAVE_RE = re.compile(
    r'^\[(\w{3} \w{3} [ \d]\d \d{2}:\d{2}:\d{2})\] (.+?) Left as a (Player|Game Master)'
)
_LOG_HEADER_RE = re.compile(
    r'^Messages for: \w{3} \w{3} [ \d]\d \d{2}:\d{2}:\d{2} (\d{4})'
)
# Anvil ServerLogRedirectorService format (NWNX Anvil):
# I [2026/06/03 09:15:31.274] [Anvil.Services.ServerLogRedirectorService] Alek Cain (CDKEY) Joined as Player 1
_ANVIL_JOIN_RE = re.compile(
    r'^I \[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\.\d+\] \[Anvil\.Services\.ServerLogRedirectorService\] (.+?) \((\w+)\) Joined as (Player|Game Master) \d+'
)
_ANVIL_LEAVE_RE = re.compile(
    r'^I \[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\.\d+\] \[Anvil\.Services\.ServerLogRedirectorService\] (.+?) Left as a (Player|Game Master)'
)
# Written each time the server finishes loading a module (i.e. every restart).
# Any sessions still open before this line are stale — the server crashed/rebooted
# without logging leaves.  This line is emitted by the *main* nwserver process, so it
# normally lands in nwserverLog*.txt, NOT anvil.log; we therefore look for it in every
# log file (regardless of format) and use it to invalidate stale open sessions across
# files (e.g. a crashed session dangling in anvil.log, cleared by a restart logged in
# nwserverLog.txt).  Matched with search() so an optional timestamp prefix is tolerated.
_RESTART_RE = re.compile(r'Server: Module loaded\b')
# Timestamp prefixes for the two log formats, used to date a restart marker (and any
# other line) so restarts can be ordered against session join times across files.
_ANVIL_TS_RE = re.compile(r'^I \[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\.\d+\]')
_NWSERVER_TS_RE = re.compile(r'^\[(\w{3} \w{3} [ \d]\d \d{2}:\d{2}:\d{2})\]')


def _log_subdir_sort_key(p: Path) -> list:
    """Natural sort key so logs.9 < logs.10 (reversed for oldest-first processing)."""
    parts = re.split(r'(\d+)', p.name)
    return [int(x) if x.isdigit() else x for x in parts]


_LOG_FILE_GLOBS = ("nwserverLog*.txt", "anvil.log")


def _collect_log_files(log_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for d in log_dirs:
        if not d.is_dir():
            continue
        for glob in _LOG_FILE_GLOBS:
            for f in sorted(d.glob(glob)):
                if f not in seen:
                    files.append(f)
                    seen.add(f)
        # Sort subdirs in REVERSE numeric order so the oldest rotation (highest
        # number, e.g. logs.12) is processed before newer ones (logs.0).
        subdirs = sorted(
            (p for p in d.iterdir() if p.is_dir()),
            key=_log_subdir_sort_key,
            reverse=True,
        )
        for sub in subdirs:
            for glob in _LOG_FILE_GLOBS:
                for f in sorted(sub.glob(glob)):
                    if f not in seen:
                        files.append(f)
                        seen.add(f)
    return files


def _log_file_fingerprint(path: Path) -> dict | None:
    try:
        st = path.stat()
        return {"mtime": round(st.st_mtime, 3), "size": st.st_size}
    except OSError:
        return None


_ACTIVITY_CACHE_VERSION = 2  # bump to invalidate stale caches (v2: sessions store cdkey)


def _migrate_activity_cache(data: dict) -> dict:
    """Bring an older-version cache up to the current schema in place.

    Session history is irreplaceable (it preserves hours after the source logs
    rotate away), so a version mismatch must NEVER discard sessions — every
    schema bump so far has been purely additive. Default any newly-added fields
    on existing sessions and carry fingerprints / restart marker forward.
    """
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        sessions = []
    for s in sessions:
        if isinstance(s, dict):
            s.setdefault("cdkey", None)      # added in v2
            s.setdefault("role", "Player")
    data["sessions"] = sessions
    if not isinstance(data.get("file_fingerprints"), dict):
        data["file_fingerprints"] = {}
    data["version"] = _ACTIVITY_CACHE_VERSION
    return data


def _load_activity_cache(cache_path: Path) -> dict:
    if cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                if data.get("version") == _ACTIVITY_CACHE_VERSION:
                    return data
                # Older (or future-but-compatible) version: migrate, don't wipe.
                return _migrate_activity_cache(data)
        except Exception:
            pass
    return {"version": _ACTIVITY_CACHE_VERSION, "sessions": [], "file_fingerprints": {}}


def _save_activity_cache(cache_path: Path, data: dict) -> None:
    try:
        # Keep one recoverable generation in case a write (or a future schema
        # change) ever loses data; the session history cannot be rebuilt once
        # the source logs have rotated away.
        if cache_path.is_file():
            try:
                shutil.copy2(cache_path, cache_path.with_suffix(cache_path.suffix + ".bak"))
            except OSError:
                pass
        # Atomic replace so an interrupted write can't truncate the cache.
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as e:
        print(f"[nwn-wiki] warn: could not save activity cache: {e}", file=sys.stderr)


def _parse_one_log_file(log_path: Path) -> tuple[list[dict], bool, "datetime | None"]:
    """Parse one NWN server log file (nwserverLog*.txt or Anvil anvil.log).

    Returns (sessions, has_open_sessions, restart_ts). Each session has player,
    role, join (datetime), leave (datetime|None), duration_min (float|None).
    restart_ts is the timestamp of the latest server restart seen in this file
    (or None), used to invalidate stale open sessions across files.
    """
    is_anvil = log_path.name == "anvil.log"
    join_re = _ANVIL_JOIN_RE if is_anvil else _LOG_JOIN_RE
    leave_re = _ANVIL_LEAVE_RE if is_anvil else _LOG_LEAVE_RE
    year = datetime.now().year
    sessions: list[dict] = []
    open_sessions: dict[str, dict] = {}
    last_ts: datetime | None = None     # most recent timestamp seen on any line
    restart_ts: datetime | None = None  # timestamp of the latest restart marker

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False, None

    for line in text.splitlines():
        # Track the most recent timestamp so a restart marker can be dated even
        # when its own line carries no timestamp (e.g. bare "Server: Module loaded").
        am = _ANVIL_TS_RE.match(line)
        if am:
            try:
                last_ts = datetime.strptime(am.group(1), "%Y/%m/%d %H:%M:%S")
            except ValueError:
                pass
        else:
            nm = _NWSERVER_TS_RE.match(line)
            if nm:
                try:
                    norm = re.sub(r' +', ' ', nm.group(1).strip())
                    last_ts = datetime.strptime(f"{norm} {year}", "%a %b %d %H:%M:%S %Y")
                except ValueError:
                    pass

        if _RESTART_RE.search(line):
            # Server restarted; sessions open before this point never logged a
            # leave (crash/reboot), so discard them rather than reporting online.
            open_sessions.clear()
            if last_ts is not None:
                restart_ts = last_ts
            continue
        if not is_anvil:
            m = _LOG_HEADER_RE.match(line)
            if m:
                year = int(m.group(1))
                continue
        m = join_re.match(line)
        if m:
            ts_str, player, cdkey, role = m.group(1), m.group(2), m.group(3), m.group(4)
            try:
                if is_anvil:
                    ts = datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S")
                else:
                    norm = re.sub(r' +', ' ', ts_str.strip())
                    ts = datetime.strptime(f"{norm} {year}", "%a %b %d %H:%M:%S %Y")
            except ValueError:
                continue
            if player in open_sessions:
                prev = open_sessions.pop(player)
                dur = (ts - prev["join"]).total_seconds() / 60
                sessions.append({**prev, "leave": ts, "duration_min": max(0.0, dur)})
            open_sessions[player] = {"player": player, "cdkey": cdkey, "role": role, "join": ts}
            continue
        m = leave_re.match(line)
        if m:
            ts_str, player = m.group(1), m.group(2)
            try:
                if is_anvil:
                    ts = datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S")
                else:
                    norm = re.sub(r' +', ' ', ts_str.strip())
                    ts = datetime.strptime(f"{norm} {year}", "%a %b %d %H:%M:%S %Y")
            except ValueError:
                continue
            if player in open_sessions:
                prev = open_sessions.pop(player)
                dur = (ts - prev["join"]).total_seconds() / 60
                sessions.append({**prev, "leave": ts, "duration_min": max(0.0, dur)})

    has_open = bool(open_sessions)
    for player, data in open_sessions.items():
        sessions.append({**data, "leave": None, "duration_min": None})

    return sessions, has_open, restart_ts


def parse_nwserver_logs(
    log_dirs: list[Path],
    cache_path: Path | None = None,
    online_floor: "datetime | None" = None,
) -> dict:
    """Parse NWN server log files; return {"sessions": [...], "file_count": N}.

    Each session dict has: player (str), role ("Player"|"Game Master"),
    join (datetime), leave (datetime|None), duration_min (float|None).

    If cache_path is given, closed sessions are persisted to a JSON file so
    that hours never decrease even when old log rotations are deleted.

    If online_floor is given, any session still open whose join precedes it is
    dropped from the "currently online" set — it is a leftover from a previous
    server run that was killed without logging a leave. Callers that restart the
    server alongside the monitor (nwn-manager serve) pass their own start time.
    """
    log_files = _collect_log_files(log_dirs)

    cache = _load_activity_cache(cache_path) if cache_path else {
        "version": _ACTIVITY_CACHE_VERSION, "sessions": [], "file_fingerprints": {},
    }
    cached_sessions: list[dict] = cache.setdefault("sessions", [])
    cached_fps: dict = cache.setdefault("file_fingerprints", {})

    # Index of sessions already stored: (player, join_isoformat) → True
    seen_keys: set[tuple] = {
        (s["player"], s["join"])
        for s in cached_sessions
        if s.get("join") and s.get("duration_min") is not None
    }

    cache_updated = False
    # Track open sessions per file so we can discard stale ones after the loop.
    # Key: str(log_path), Value: (file_mtime_float, [session, ...])
    _open_by_file: dict[str, tuple[float, list[dict]]] = {}
    # Latest server-restart timestamp across all files. A restart invalidates any
    # session left open (un-left) before it, even when the restart is logged in a
    # different file (nwserverLog.txt) than the dangling session (anvil.log).
    # Persisted in the cache so it survives once the restart's log file stops
    # changing and gets fingerprint-skipped on later polls.
    latest_restart_ts: datetime | None = None
    _cached_restart = cache.get("latest_restart_ts")
    if _cached_restart:
        try:
            latest_restart_ts = datetime.fromisoformat(_cached_restart)
        except (ValueError, TypeError):
            latest_restart_ts = None

    for log_path in log_files:
        path_key = str(log_path)
        fp = _log_file_fingerprint(log_path)

        # Skip files whose content hasn't changed since last parse
        if fp is not None and cached_fps.get(path_key) == fp:
            continue

        file_sessions, has_open, restart_ts = _parse_one_log_file(log_path)
        if restart_ts is not None and (
            latest_restart_ts is None or restart_ts > latest_restart_ts
        ):
            latest_restart_ts = restart_ts

        # Persist all newly closed sessions
        file_opens: list[dict] = []
        for s in file_sessions:
            if s.get("duration_min") is None:
                file_opens.append(s)
                continue
            join_key = (s["player"], s["join"].isoformat())
            if join_key in seen_keys:
                continue
            seen_keys.add(join_key)
            cached_sessions.append({
                "player": s["player"],
                "cdkey": s.get("cdkey"),
                "role": s.get("role", "Player"),
                "join": s["join"].isoformat(),
                "leave": s["leave"].isoformat() if s.get("leave") else None,
                "duration_min": s["duration_min"],
            })
            cache_updated = True

        if file_opens:
            file_mtime = fp["mtime"] if fp else 0.0
            _open_by_file[path_key] = (file_mtime, file_opens)

        # Only fingerprint files that are fully closed (no players still online)
        if not has_open and fp is not None:
            cached_fps[path_key] = fp
            cache_updated = True

    if latest_restart_ts is not None:
        restart_iso = latest_restart_ts.isoformat()
        if cache.get("latest_restart_ts") != restart_iso:
            cache["latest_restart_ts"] = restart_iso
            cache_updated = True

    if cache_path is not None and cache_updated:
        _save_activity_cache(cache_path, cache)

    # Only include open sessions from the most recently modified log file.
    # If a newer file exists (e.g. server restarted after a crash), sessions left
    # open in an older file are stale — those players are no longer connected.
    max_log_mtime = 0.0
    for lp in log_files:
        try:
            mt = lp.stat().st_mtime
            if mt > max_log_mtime:
                max_log_mtime = mt
        except OSError:
            pass

    open_sessions_out: list[dict] = []
    for file_mtime, file_opens in _open_by_file.values():
        # Allow up to 60 s of clock skew / filesystem resolution
        if max_log_mtime - file_mtime <= 60:
            for s in file_opens:
                # Drop sessions that began before the last server restart: the
                # player was disconnected by the reboot and never logged a leave.
                if latest_restart_ts is not None and s["join"] < latest_restart_ts:
                    continue
                # Drop sessions predating the caller's online floor (e.g. the
                # monitor's own start time): leftovers from a previous run whose
                # new "Module loaded" marker may not have been logged yet.
                if online_floor is not None and s["join"] < online_floor:
                    continue
                open_sessions_out.append(s)

    # Convert cached sessions back to datetime objects for rendering
    sessions_out: list[dict] = []
    for s in cached_sessions:
        try:
            join_dt = datetime.fromisoformat(s["join"])
            leave_dt = datetime.fromisoformat(s["leave"]) if s.get("leave") else None
        except (ValueError, KeyError):
            continue
        sessions_out.append({
            "player": s["player"],
            "cdkey": s.get("cdkey"),
            "role": s.get("role", "Player"),
            "join": join_dt,
            "leave": leave_dt,
            "duration_min": s.get("duration_min"),
            # Preserve provenance so the renderer can exclude recovered sessions
            # (fabricated join times) from the timing-based charts.
            "synthetic": s.get("synthetic", False),
        })

    sessions_out.extend(open_sessions_out)
    return {"sessions": sessions_out, "file_count": len(log_files)}


# =============================================================================
# SVG chart helpers (pure stdlib — no third-party deps)
# =============================================================================

def _nice_upper(val: float) -> float:
    """Round val up to a visually nice axis maximum."""
    if val <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(val))
    norm = val / exp
    for nice in (1, 2, 2.5, 5, 10):
        if nice >= norm:
            return nice * exp
    return float(val)


def _fmt_num(val: float) -> str:
    if val == int(val):
        return str(int(val))
    return f"{val:.1f}" if val < 10 else str(int(round(val)))


def _se(s: Any) -> str:
    """html.escape shorthand for SVG text content."""
    return html.escape(str(s))


def svg_vbar_chart(
    labels: list[str], values: list[float], title: str,
    ylabel: str = "", bar_color: str = "#6b3a1c",
    width: int = 700, height: int = 270,
    rotate_labels: bool = False,
) -> str:
    """Vertical bar chart returned as an inline SVG string."""
    mt, mb = 32, (72 if rotate_labels else 50)
    ml, mr = 55, 20
    pw, ph = width - ml - mr, height - mt - mb
    n = len(labels)
    if n == 0:
        return f'<svg width="{width}" height="{height}"><text x="10" y="20">No data</text></svg>'
    y_max = _nice_upper(max(values) if values else 1)
    bar_w = max(2.0, pw / n * 0.65)
    bar_gap = pw / n
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="font-family:Georgia,serif;background:#fff;'
        f'border:1px solid #d6d2c4;border-radius:4px;display:block;">'
    ]
    out.append(
        f'<text x="{width/2:.1f}" y="22" text-anchor="middle" '
        f'font-size="13" fill="#6b3a1c">{_se(title)}</text>'
    )
    if ylabel:
        cy = mt + ph / 2
        out.append(
            f'<text x="13" y="{cy:.1f}" text-anchor="middle" font-size="11" fill="#6b6b6b" '
            f'transform="rotate(-90 13 {cy:.1f})">{_se(ylabel)}</text>'
        )
    for i in range(6):
        tv = y_max * i / 5
        y = mt + ph - (tv / y_max) * ph
        out.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+pw}" y2="{y:.1f}" '
            f'stroke="#d6d2c4" stroke-width="1" stroke-dasharray="3,3"/>'
        )
        out.append(
            f'<text x="{ml-6}" y="{y+4:.1f}" text-anchor="end" '
            f'font-size="10" fill="#6b6b6b">{_fmt_num(tv)}</text>'
        )
    out.append(
        f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ph}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )
    out.append(
        f'<line x1="{ml}" y1="{mt+ph}" x2="{ml+pw}" y2="{mt+ph}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )
    for i, (lbl, val) in enumerate(zip(labels, values)):
        bx = ml + i * bar_gap + (bar_gap - bar_w) / 2
        bh = (val / y_max) * ph
        by = mt + ph - bh
        out.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
            f'fill="{bar_color}" opacity="0.82"/>'
        )
        if val > 0:
            out.append(
                f'<text x="{bx+bar_w/2:.1f}" y="{by-3:.1f}" text-anchor="middle" '
                f'font-size="10" fill="{bar_color}">{_fmt_num(val)}</text>'
            )
        cx = bx + bar_w / 2
        if rotate_labels:
            out.append(
                f'<text x="{cx:.1f}" y="{mt+ph+10}" text-anchor="end" font-size="10" '
                f'fill="#6b6b6b" transform="rotate(-45 {cx:.1f} {mt+ph+10})">'
                f'{_se(lbl)}</text>'
            )
        else:
            out.append(
                f'<text x="{cx:.1f}" y="{mt+ph+16}" text-anchor="middle" '
                f'font-size="11" fill="#6b6b6b">{_se(lbl)}</text>'
            )
    out.append('</svg>')
    return "\n".join(out)


def svg_hbar_chart(
    labels: list[str], values: list[float], title: str,
    xlabel: str = "", bar_color: str = "#6b3a1c",
) -> str:
    """Horizontal bar chart returned as an inline SVG string."""
    bar_h, bar_gap = 22, 30
    n = len(labels)
    width = 700
    ml, mr, mt, mb = 130, 65, 34, 38
    height = mt + n * bar_gap + mb
    pw = width - ml - mr
    x_max = _nice_upper(max(values) if values else 1)
    y_bot = mt + n * bar_gap
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="font-family:Georgia,serif;background:#fff;'
        f'border:1px solid #d6d2c4;border-radius:4px;display:block;">'
    ]
    out.append(
        f'<text x="{width/2:.1f}" y="24" text-anchor="middle" '
        f'font-size="13" fill="#6b3a1c">{_se(title)}</text>'
    )
    for i in range(6):
        tv = x_max * i / 5
        x = ml + (tv / x_max) * pw
        out.append(
            f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{y_bot}" '
            f'stroke="#d6d2c4" stroke-width="1" stroke-dasharray="3,3"/>'
        )
        out.append(
            f'<text x="{x:.1f}" y="{y_bot+14}" text-anchor="middle" '
            f'font-size="10" fill="#6b6b6b">{_fmt_num(tv)}</text>'
        )
    if xlabel:
        out.append(
            f'<text x="{ml+pw/2:.1f}" y="{y_bot+30}" text-anchor="middle" '
            f'font-size="11" fill="#6b6b6b">{_se(xlabel)}</text>'
        )
    out.append(
        f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{y_bot}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )
    out.append(
        f'<line x1="{ml}" y1="{y_bot}" x2="{ml+pw}" y2="{y_bot}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )
    for i, (lbl, val) in enumerate(zip(labels, values)):
        by = mt + i * bar_gap + (bar_gap - bar_h) / 2
        bw = (val / x_max) * pw
        out.append(
            f'<rect x="{ml}" y="{by:.1f}" width="{bw:.1f}" height="{bar_h}" '
            f'fill="{bar_color}" opacity="0.82"/>'
        )
        if val > 0:
            out.append(
                f'<text x="{ml+bw+5:.1f}" y="{by+bar_h/2+4:.1f}" '
                f'font-size="10" fill="{bar_color}">{_fmt_num(val)}</text>'
            )
        out.append(
            f'<text x="{ml-8}" y="{by+bar_h/2+4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#1f1f1f">{_se(lbl)}</text>'
        )
    out.append('</svg>')
    return "\n".join(out)


# =============================================================================
# Activity page renderer
# =============================================================================

def _tz_label_from_env() -> str:
    """Derive a GMT±N label from the TZ environment variable.

    Uses the zoneinfo module (Python 3.9+) so DST is handled correctly
    (e.g. America/Chicago → 'GMT-5' in winter, 'GMT-4' in summer).
    Falls back to 'GMT+0' when TZ is unset or the name is unrecognised.
    """
    tz_name = os.environ.get("TZ", "")
    if not tz_name:
        return "GMT+0"
    try:
        from zoneinfo import ZoneInfo
        zi = ZoneInfo(tz_name)
        offset = datetime.now(zi).utcoffset()
        total_sec = int(offset.total_seconds())
        h, m = divmod(abs(total_sec) // 60, 60)
        sign = "+" if total_sec >= 0 else "-"
        return f"GMT{sign}{h}" if m == 0 else f"GMT{sign}{h}:{m:02d}"
    except Exception:
        return tz_name


def render_activity_page(activity: dict, out: Path, tz_label: str = "GMT+0") -> None:
    """Write activity.html with player-activity charts derived from server logs."""
    sessions = activity.get("sessions", [])
    file_count = activity.get("file_count", 0)

    ps = [s for s in sessions if s.get("join") is not None and s.get("role") == "Player"]
    if not ps:
        return

    sess_by_player: Counter = Counter(s["player"] for s in ps)
    time_by_player: dict[str, float] = {}
    for s in ps:
        if s.get("duration_min") is not None:
            time_by_player[s["player"]] = (
                time_by_player.get(s["player"], 0.0) + s["duration_min"]
            )

    all_dates = sorted({s["join"].date() for s in ps})
    if all_dates:
        min_date, max_date = all_dates[0], all_dates[-1]
        date_range = [
            min_date + timedelta(days=i)
            for i in range((max_date - min_date).days + 1)
        ]
    else:
        date_range = []
    date_hours: dict = {}
    hour_hours: dict[int, float] = {}
    dow_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_hours: dict[str, float] = {}
    for s in ps:
        if s.get("duration_min") is not None:
            d = s["join"].date()
            day = s["join"].strftime("%a")
            hrs = s["duration_min"] / 60.0
            date_hours[d] = date_hours.get(d, 0.0) + hrs
            dow_hours[day] = dow_hours.get(day, 0.0) + hrs
            # Distribute play-hours across every clock hour the session spans,
            # correctly handling sessions that cross midnight. Synthetic sessions
            # (recovered from old chart snapshots — see bin/recover-activity-gap)
            # carry a faithful daily total but a fabricated join time, so they are
            # excluded here to keep the hour-of-day chart honest.
            if not s.get("synthetic"):
                cur = s["join"]
                end = cur + timedelta(hours=hrs)
                while cur < end:
                    next_hour = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                    seg_end = min(next_hour, end)
                    hour_hours[cur.hour] = hour_hours.get(cur.hour, 0.0) + (seg_end - cur).total_seconds() / 3600.0
                    cur = seg_end

    # Sweep-line: peak concurrent players per day and overall.
    # Leaves are sorted before joins at identical timestamps so a player
    # departing and another arriving at the same instant don't inflate the peak.
    _now = datetime.now()
    conc_events: list[tuple] = []
    for s in ps:
        if s.get("join") is None or s.get("synthetic"):
            # Synthetic recovery sessions share a single fabricated join time, so
            # they would inflate the peak; the gap window is dropped from this chart.
            continue
        conc_events.append((s["join"], 1))
        conc_events.append((s.get("leave") or _now, -1))
    conc_events.sort(key=lambda e: (e[0], e[1]))
    daily_peak_conc: dict = {}
    peak_conc = 0
    peak_conc_date = None
    _cur = 0
    for _t, _delta in conc_events:
        _cur += _delta
        _d = _t.date()
        if _cur > daily_peak_conc.get(_d, 0):
            daily_peak_conc[_d] = _cur
        if _cur > peak_conc:
            peak_conc = _cur
            peak_conc_date = _d

    first_seen: dict[str, object] = {}
    for s in ps:
        p, j = s["player"], s["join"]
        if p not in first_seen or j < first_seen[p]:
            first_seen[p] = j
    newest_players = sorted(first_seen.items(), key=lambda x: x[1], reverse=True)[:3]

    n_players = len(sess_by_player)
    n_sessions = len(ps)
    total_hours = sum(time_by_player.values()) / 60
    top_player = max(time_by_player, key=time_by_player.get) if time_by_player else "—"
    top_day = max(date_hours, key=date_hours.get) if date_hours else None
    top_day_str = top_day.strftime("%b %d, %Y") if top_day else "—"
    top_day_hours = date_hours[top_day] if top_day else 0.0
    peak_conc_str = (
        f"{peak_conc} player{'s' if peak_conc != 1 else ''}"
        f" ({peak_conc_date.strftime('%b %d, %Y')})"
        if peak_conc_date else "—"
    )

    top_players = sorted(time_by_player, key=time_by_player.get, reverse=True)[:15]
    chart_time = svg_hbar_chart(
        list(reversed(top_players)),
        [round(time_by_player.get(p, 0.0) / 60, 2) for p in reversed(top_players)],
        "Play-hours per player",
        xlabel="hours",
        bar_color="#5a2b78",
    )
    _daily_cutoff = date(2026, 5, 17)
    _conc_cutoff = date(2026, 6, 1)

    # Daily charts show at most the most recent DAILY_WINDOW days. Once the data
    # runs longer than that, the days that fall off the daily chart aren't lost:
    # a weekly roll-up of the *whole* range is rendered underneath it.
    DAILY_WINDOW = 35

    def _weekly(days: list, value_of, combine) -> tuple[list[str], list]:
        """Roll a per-day series up into Mon-anchored weeks.

        `value_of(day)` yields that day's value; `combine(list)` reduces a week's
        worth of values (sum for hours, max for a peak). Returns (labels, values).
        """
        buckets: dict = {}
        for d in days:
            wk = d - timedelta(days=d.weekday())
            buckets.setdefault(wk, []).append(value_of(d))
        weeks = sorted(buckets)
        return (
            [w.strftime("%b %-d") for w in weeks],
            [combine(buckets[w]) for w in weeks],
        )

    date_range_daily_all = [d for d in date_range if d > _daily_cutoff]
    date_range_daily = date_range_daily_all[-DAILY_WINDOW:]
    daily_labels = [d.strftime("%b %-d") for d in date_range_daily]
    date_hour_values = [round(date_hours.get(d, 0.0), 2) for d in date_range_daily]
    chart_daily_hours = svg_vbar_chart(
        daily_labels, date_hour_values,
        "Daily play-hours",
        ylabel="hours",
        width=max(700, len(date_range_daily) * 20 + 80),
        height=270,
        rotate_labels=True,
        bar_color="#5a2b78",
    )
    chart_weekly_hours = ""
    if len(date_range_daily_all) > DAILY_WINDOW:
        wk_labels, wk_values = _weekly(
            date_range_daily_all,
            lambda d: date_hours.get(d, 0.0),
            lambda vs: round(sum(vs), 2),
        )
        chart_weekly_hours = svg_vbar_chart(
            wk_labels, wk_values,
            "Weekly play-hours (week beginning)",
            ylabel="hours",
            width=max(700, len(wk_labels) * 20 + 80),
            height=270,
            rotate_labels=True,
            bar_color="#5a2b78",
        )

    date_range_conc_all = [d for d in date_range if d > _conc_cutoff]
    date_range_conc = date_range_conc_all[-DAILY_WINDOW:]
    conc_labels = [d.strftime("%b %-d") for d in date_range_conc]
    conc_values = [daily_peak_conc.get(d, 0) for d in date_range_conc]
    chart_concurrent = svg_vbar_chart(
        conc_labels, conc_values,
        "Peak concurrent players per day",
        ylabel="players",
        width=max(700, len(date_range_conc) * 20 + 80),
        height=270,
        rotate_labels=True,
    )
    chart_weekly_conc = ""
    if len(date_range_conc_all) > DAILY_WINDOW:
        wk_labels, wk_values = _weekly(
            date_range_conc_all,
            lambda d: daily_peak_conc.get(d, 0),
            max,
        )
        chart_weekly_conc = svg_vbar_chart(
            wk_labels, wk_values,
            "Peak concurrent players per week (week beginning)",
            ylabel="players",
            width=max(700, len(wk_labels) * 20 + 80),
            height=270,
            rotate_labels=True,
        )
    chart_hour = svg_vbar_chart(
        [str(h) for h in range(24)],
        [round(hour_hours.get(h, 0.0), 2) for h in range(24)],
        f"Play-hours by hour of day ({tz_label})",
        ylabel="hours",
        width=700, height=260,
    )
    chart_dow = svg_vbar_chart(
        dow_order,
        [round(dow_hours.get(d, 0.0), 2) for d in dow_order],
        "Play-hours by day of week",
        ylabel="hours",
        width=500, height=240,
        bar_color="#5a2b78",
    )

    range_str = (
        f"{all_dates[0].strftime('%b %d, %Y')} – {all_dates[-1].strftime('%b %d, %Y')}"
        if all_dates else ""
    )
    body = (
        "<h1>Player Activity</h1>\n"
        f'<p class="muted">Parsed from {file_count} server log file'
        f'{"s" if file_count != 1 else ""}'
        f'{f" &mdash; {E(range_str)}" if range_str else ""}</p>\n'
        + (
            "<h2>Welcome, new adventurers!</h2>\n"
            "<p>Our most recently seen players:</p>\n"
            "<ul>\n"
            + "".join(
                f"  <li><strong>{E(p)}</strong> &mdash; first joined"
                f" {j.strftime('%b %d, %Y')}</li>\n"
                for p, j in newest_players
            )
            + "</ul>\n"
            if newest_players else ""
        )
        + "<h2>Summary</h2>\n"
        '<dl class="meta">\n'
        f"  <dt>Unique players</dt><dd>{n_players}</dd>\n"
        f"  <dt>Total sessions</dt><dd>{n_sessions}</dd>\n"
        f"  <dt>Combined play-hours</dt><dd>{total_hours:.1f} h</dd>\n"
        f"  <dt>Most active player</dt><dd>{E(top_player)}</dd>\n"
        f"  <dt>Busiest day</dt><dd>{E(top_day_str)}"
        f" ({top_day_hours:.1f} h)</dd>\n"
        f"  <dt>Peak concurrent players</dt><dd>{E(peak_conc_str)}</dd>\n"
        "</dl>\n"
        "<h2>Play-hours per player</h2>\n"
        f'<div style="overflow-x:auto;">{chart_time}</div>\n'
        "<h2>Play-hours per period</h2>\n"
        f'<div style="overflow-x:auto;">{chart_daily_hours}</div>\n'
        + (
            f'<p style="overflow-x:auto;">{chart_weekly_hours}</p>\n'
            if chart_weekly_hours else ""
        )
        + "<h2>Concurrent players</h2>\n"
        f'<div style="overflow-x:auto;">{chart_concurrent}</div>\n'
        + (
            f'<p style="overflow-x:auto;">{chart_weekly_conc}</p>\n'
            if chart_weekly_conc else ""
        )
        +
        "<h2>Active time of day</h2>\n"
        f'<div style="overflow-x:auto;">{chart_hour}</div>\n'
        "<h2>Day of week</h2>\n"
        f'<div style="overflow-x:auto;">{chart_dow}</div>\n'
    )
    now_str = datetime.now().strftime("%b %-d, %Y %H:%M")
    write(out / "activity.html", page("Player Activity", body, page_updated_at=now_str))
    print(f"[nwn-wiki] rendered activity page ({n_sessions} sessions, {n_players} players)")


# ---------------------------------------------------------------------------
# Bestiary kill stats (read from / seeded into the live NWNX:EE campaign DB)
# ---------------------------------------------------------------------------


def _render_server_first_body() -> str:
    """Inner HTML for the generated Server-First leaderboard manual page."""
    tz_label = _tz_label_from_env()
    parts = [
        "<h1>Server First Kills</h1>",
        f"<p>The first adventurer (or party) to slay each fearsome creature of "
        f"Challenge Rating {state.BST_SF_CR} or higher, recorded server-wide.</p>",
        "<p class=\"note\"><strong>How the credited player is chosen:</strong> "
        "the server-first record goes to the player who landed the "
        "<em>killing blow</em>. When a creature is slain by a party, only that "
        "finisher is named here — every contributing party member still gets "
        "the kill counted on the creature's own page (under Party). The "
        "<strong>Player</strong> column is the player’s account name and the "
        "<strong>Character</strong> column the character they were playing at the "
        "time.</p>",
    ]
    if not state._SERVER_FIRSTS:
        parts.append("<p><em>No server-first kills have been recorded yet — "
                     "the legends are still unwritten.</em></p>")
        return "\n".join(parts)
    rows = []
    for sf in state._SERVER_FIRSTS:
        rr = sf["resref"]
        cname = link(f"../creatures/{rr}.html", sf["cname"])
        cr = int(round(sf["cr"]))
        player_display = sf["player_name"] or sf["name"]
        rows.append(
            f"<tr><td>{cname}</td><td>{cr}</td>"
            f"<td>{E(player_display)}</td><td>{E(sf['name'])}</td>"
            f"<td>{E(_utc_to_local(sf['at']))}</td></tr>"
        )
    parts.append(
        '<table class="data"><thead><tr>'
        f"<th>Creature</th><th>CR</th><th>Player</th><th>Character</th><th>When ({tz_label})</th>"
        "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
    )
    return "\n".join(parts)


def render_manual_pages(project_root: Path, out: Path) -> None:
    """Scan <project_root>/docs.manual/ for .md/.html files and subdirs, render each."""
    state._MANUAL_MENUS = {}
    state._MANUAL_MENU_ORDER = {}
    manual_dir = project_root / "docs.manual"
    if not manual_dir.is_dir():
        return

    # Pass 1: collect all page metadata and content so state._MANUAL_MENUS is complete
    # before any page HTML is written (the dropdowns on every page must list all docs).
    # (out_path, title, body, root_rel, page_updated_at)
    pages_to_write: list[tuple[Path, str, str, str, str]] = []

    def note_menu_order(menu_name: str, menu_order: int | None) -> None:
        if menu_order is not None and menu_name not in state._MANUAL_MENU_ORDER:
            state._MANUAL_MENU_ORDER[menu_name] = menu_order

    top_files = sorted(
        p for p in manual_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".md", ".html")
    )
    for doc_path in top_files:
        raw_text = doc_path.read_text(encoding="utf-8")
        menu_name = _manual_menu(raw_text)
        order = _manual_sort_order(raw_text)
        note_menu_order(menu_name, _manual_menu_order(raw_text))
        title, body = _manual_doc_body(doc_path, text=raw_text)
        stem = doc_path.stem
        state._MANUAL_MENUS.setdefault(menu_name, []).append(
            {"kind": "file", "title": title, "stem": stem, "_order": order})
        pages_to_write.append((out / "manual" / f"{stem}.html", title, body, "..", ""))

    # Generated (data-driven) page: Server-First kill leaderboard. Surfaced via the
    # Activity nav dropdown (see _activity_dropdown), not Documents. Its content is
    # (re)generated only when the bestiary DB was loaded this run; otherwise, if a
    # prior full build already produced the page, keep it in the nav without
    # rewriting it — this keeps the nav consistent when nwn-wiki-activity re-renders
    # manual pages without DB access.
    sf_path = out / "manual" / "ServerFirsts.html"
    if state._BESTIARY_ACTIVE:
        sf_now = datetime.now().strftime("%b %-d, %Y %H:%M")
        state._HAS_SERVER_FIRSTS = True
        pages_to_write.append((sf_path, "Server Firsts",
                               _render_server_first_body(), "..", sf_now))
    elif sf_path.exists():
        state._HAS_SERVER_FIRSTS = True

    for sub_dir in sorted(d for d in manual_dir.iterdir() if d.is_dir()):
        doc_files = sorted(
            p for p in sub_dir.iterdir()
            if p.is_file() and p.suffix.lower() in (".md", ".html")
        )
        if not doc_files:
            continue
        dirname = sub_dir.name
        folder_title = dirname.replace("-", " ").replace("_", " ")
        items: list[dict] = []
        # Folder-level @menu/@order/@menu-order are taken from the first file
        # (in sorted order) that declares them — first found wins, mirroring
        # the quest @group-order rule.
        folder_menu: str | None = None
        folder_order: int | None = None
        for doc_path in doc_files:
            raw_text = doc_path.read_text(encoding="utf-8")
            if folder_menu is None and _RE_MANUAL_MENU.search(raw_text):
                folder_menu = _manual_menu(raw_text)
            if folder_order is None:
                folder_order = _manual_sort_order(raw_text)
            note_menu_order(_manual_menu(raw_text), _manual_menu_order(raw_text))
            title, body = _manual_doc_body(doc_path, text=raw_text)
            stem = doc_path.stem
            items.append({"title": title, "stem": stem})
            pages_to_write.append((
                out / "manual" / dirname / f"{stem}.html", title, body, "../..", "",
            ))
        state._MANUAL_MENUS.setdefault(folder_menu or "Documents", []).append(
            {"kind": "dir", "title": folder_title, "dirname": dirname,
             "items": items, "_order": folder_order})

    # Sort each menu's entries by @order (list.sort is stable, so entries
    # without @order keep their original alphabetical/insertion order,
    # trailing after any explicitly-ordered ones).
    for entries in state._MANUAL_MENUS.values():
        entries.sort(key=lambda e: e["_order"] if e["_order"] is not None else 10**9)

    # Pass 2: write all pages now that state._MANUAL_MENUS is fully populated.
    for out_path, title, body, root_rel, page_ts in pages_to_write:
        write(out_path, page(title, body, root_rel=root_rel, page_updated_at=page_ts))

    total = len(pages_to_write)
    if total:
        print(f"[nwn-wiki] rendered {total} manual page(s) from {manual_dir}")


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


# ---------------------------------------------------------------------------
# Module-index: LLM-friendly JSON exports
# ---------------------------------------------------------------------------

def _module_index_url_helpers(
    module_index_dir: Path,
    wiki_out: Path,
    base_url: str,
) -> "tuple[Any, Any, Any, Any]":
    """Return (creature_url, item_url, area_url, store_url) callables."""
    project_root = module_index_dir.parent
    if base_url:
        _b = base_url.rstrip("/")
        def _cu(rr: str) -> str: return f"{_b}/creatures/{rr}.html"
        def _iu(rr: str) -> str: return f"{_b}/items/{rr}.html"
        def _au(rr: str) -> str: return f"{_b}/areas/{rr}.html"
        def _su(rr: str) -> str: return f"{_b}/stores/{rr}.html"
    else:
        try:
            rel_wiki = wiki_out.relative_to(project_root)
        except ValueError:
            rel_wiki = wiki_out
        _wp = str(rel_wiki).rstrip("/")
        def _cu(rr: str) -> str: return f"{_wp}/creatures/{rr}.html"
        def _iu(rr: str) -> str: return f"{_wp}/items/{rr}.html"
        def _au(rr: str) -> str: return f"{_wp}/areas/{rr}.html"
        def _su(rr: str) -> str: return f"{_wp}/stores/{rr}.html"
    return _cu, _iu, _au, _su



# Challenge-Rating buckets for the progression ladder. [lo, hi) half-open; the
# last bucket's hi is open-ended. The bands past the player level cap exist to
# separate "hard" from "not intended to be soloed at cap" — every creature in
# them faces the same level-capped reference PC, which is the point.
_CR_TIERS: list[tuple[float, float, str]] = [
    (0, 5, "CR 0–5"),
    (5, 10, "CR 5–10"),
    (10, 20, "CR 10–20"),
    (20, 40, "CR 20–40"),
    (40, 80, "CR 40–80"),
    (80, 200, "CR 80–200"),
    (200, float("inf"), "CR 200+"),
]

# Bumped by hand whenever the simulation, the reference PC or the kit solver
# changes in a way that would move the numbers. Part of the staleness
# fingerprint, so an algorithm change makes existing reports report themselves
# as out of date without anyone having to remember to delete them.
_COUNTER_GEAR_ALGO_VERSION = 4

# Past this many rounds a "win" is really a war of attrition (100 rounds is ten
# minutes of unbroken melee), so the report labels it instead of just ticking it.
_ATTRITION_ROUNDS = 100


def _parse_cr(cr_raw: object) -> float:
    """Numeric Challenge Rating, or 0.0 when unparseable."""
    try:
        return float(cr_raw)
    except (TypeError, ValueError):
        return 0.0


def _fmt_gp(cost: int) -> str:
    """Compact gold display: 1234 → '1.2k gp', 1500000 → '1.5M gp'."""
    if cost >= 1_000_000_000:
        return f"{cost / 1_000_000_000:.1f}B gp"
    if cost >= 1_000_000:
        return f"{cost / 1_000_000:.1f}M gp"
    if cost >= 1_000:
        return f"{cost / 1_000:.1f}k gp"
    return f"{cost} gp"


def devcrit_mode(db: "Db") -> dict:
    """Which Devastating Critical rule is in force, detected from the 2DAs.

    The engine only rolls a devastating critical for a weapon whose baseitems.2da
    row names a feat in EpicWeaponDevastatingCriticalFeat. A module that blanks
    that column across the board (as nwn_homers_lotr does, via
    bin/gen-devcrit-map.py) has disabled the save-or-die at source, for players
    and NPCs alike — so no configuration is needed to notice, and any module that
    left it alone keeps stock behaviour automatically.
    """
    col = "EpicWeaponDevastatingCriticalFeat"
    armed = sum(1 for bi in WEAPONS if weapon_feat_id(bi, col))
    if armed:
        return {"mode": "stock", "weapons_armed": armed, "bonus_dice": 0,
                "label": f"save-or-die (stock; {armed} base items armed)"}
    if col not in BASEITEM_COLUMNS_SEEN:
        # No baseitems.2da carried the column, so we never saw the evidence
        # either way. Assume the engine default rather than silently reporting
        # a mechanic as disabled — run with --2da-dir to detect it properly.
        return {"mode": "unknown", "weapons_armed": 0, "bonus_dice": 0,
                "label": ("assumed stock save-or-die — no baseitems.2da was "
                          "available to check; pass --2da-dir to detect it")}
    if db.devcrit_bonus_dice > 0:
        return {"mode": "bonus-dice", "weapons_armed": 0,
                "bonus_dice": db.devcrit_bonus_dice,
                "label": ("disabled at source; replaced by "
                          f"+{db.devcrit_bonus_dice} damage dice on a critical")}
    return {"mode": "disabled", "weapons_armed": 0, "bonus_dice": 0,
            "label": "disabled at source, with no replacement configured"}


def _pc_level_for_cr(cr: float, db: "Db") -> int:
    """Reference-PC level for a creature of this Challenge Rating: CR, clamped
    to the server's player cap. Everything above the cap gets a capped PC — so a
    boss the cap cannot beat is reported as unwinnable rather than quietly
    matched against a character who could never exist."""
    return max(1, min(int(round(cr)) or 1, db.max_player_level or 40))


# ---------------------------------------------------------------------------
# Staleness fingerprint.
#
# The simulation is far too slow to run on every wiki refresh, but a stale
# report is worse than no report. Both problems go away with a cheap hash of
# everything the analysis actually reads: if it matches, the report on disk is
# still correct; if it does not, a normal run says so and you re-run with
# --counter-gear. Module-agnostic — nothing here knows about any one module.
# ---------------------------------------------------------------------------

def _counter_gear_fingerprint(db: "Db") -> str:
    """Hash of every input the counter-gear analysis depends on."""
    h = hashlib.sha256()
    h.update(f"algo={_COUNTER_GEAR_ALGO_VERSION}\n".encode())
    h.update(f"dials={db.max_character_level},{db.max_ability_bonus},"
             f"{db.max_player_level},{db.devcrit_bonus_dice}\n".encode())

    # 2DA overrides change base item stats, slots and property tables.
    if db.twoda_dir and Path(db.twoda_dir).is_dir():
        for p in sorted(Path(db.twoda_dir).glob("*.2da")):
            st = p.stat()
            h.update(f"2da:{p.name}:{st.st_size}:{int(st.st_mtime)}\n".encode())

    # Items: what they are, what they cost, what they do, and whether a player
    # can get one (an item becoming reachable changes every kit).
    h.update(b"--items--\n")
    for rr in sorted(db.items):
        it = db.items[rr]
        h.update(f"{rr}|{fld(it, 'BaseItem', '')}|{item_gp_value(it)}|"
                 f"{int(_item_accessible(db, rr))}|".encode())
        for p in list_items(it.get("PropertiesList")):
            h.update(f"{fld(p, 'PropertyName', '')},{fld(p, 'Subtype', '')},"
                     f"{fld(p, 'CostValue', '')},{fld(p, 'Param1Value', '')};".encode())
        h.update(b"\n")

    # Creatures: the canonical set and the fields the sim reads off them.
    h.update(b"--creatures--\n")
    for rr in sorted(db.canonical_creatures):
        entry = db.canonical_creatures[rr]
        c = entry["c"]
        h.update(f"{rr}|{fld(c, 'ChallengeRating', '')}|{fld(c, 'MaxHitPoints', '')}|"
                 f"{fld(c, 'NaturalAC', '')}|{fld(c, 'ScriptDamaged', '')}|".encode())
        for key in ("Equip_ItemList", "ClassList", "FeatList", "SpecAbilityList",
                    "PropertiesList"):
            for e in list_items(c.get(key)):
                h.update(repr(sorted(
                    (k, v) for k, v in e.items() if isinstance(v, (int, str))
                )).encode())
        h.update(b"\n")
    return h.hexdigest()


def check_counter_gear_freshness(db: "Db", module_index_dir: Path) -> bool:
    """Compare the on-disk counter_gear.json against the current inputs.

    Returns True when the report is present and current. Called on every wiki
    run; pushes a warning onto the module-index summary when the report is
    missing or stale so the staleness surfaces without paying for the sim.
    """
    path = module_index_dir / "counter_gear.json"
    if not path.is_file():
        state._module_index_summary.append(("warn",
            "[nwn-wiki] module-index: counter_gear.json absent — "
            "run with --counter-gear to build it"))
        return False
    try:
        stored = json.loads(path.read_text(encoding="utf-8")).get("input_fingerprint")
    except (OSError, ValueError):
        stored = None
    if stored and stored == _counter_gear_fingerprint(db):
        state._module_index_summary.append(("info",
            "[nwn-wiki] module-index: counter_gear.json current (inputs unchanged)"))
        return True
    state._module_index_summary.append(("warn",
        "[nwn-wiki] module-index: counter_gear.json is STALE — items, creatures, "
        "2DAs or combat dials changed since it was built. Re-run with "
        "--counter-gear."))
    return False


# ---------------------------------------------------------------------------
# Report data + rendering
# ---------------------------------------------------------------------------

def _top_value_by_slot(db: "Db", _iu, limit: int = 5) -> list[dict]:
    """The most valuable player-attainable items in each equipment slot.

    A blunt but effective best-in-slot proxy: builders price power, so sorting
    a slot by gold piece value surfaces its strongest items without simulating
    anything. Attainability is the same rule the items index uses. Items that
    fit several slots appear under each of them.
    """
    buckets: dict[str, list[dict]] = {key: [] for key, _, _, _ in PLAYER_SLOTS}
    for rr in sorted(db.items):
        if not _item_accessible(db, rr):
            continue
        item = db.items[rr]
        slots = item_equip_slots(item) & PLAYER_SLOT_MASK
        if not slots:
            continue
        name = nwn_text(db.item_name(rr))
        if name.startswith("[TLK#") or name == rr:
            continue                              # broken/unnamed blueprint
        entry = {
            "resref": rr, "name": name,
            "gp_value": item_gp_value(item),
            "base_item": baseitem_name(_try_int(fld(item, "BaseItem", -1), -1)),
            "wiki_url": _iu(rr),
        }
        for key, _label, mask, _n in PLAYER_SLOTS:
            if slots & mask:
                buckets[key].append(entry)

    out: list[dict] = []
    for key, label, _mask, wearable in PLAYER_SLOTS:
        items = sorted(buckets[key],
                       key=lambda e: (-e["gp_value"], e["name"].lower()))[:limit]
        if items:
            out.append({"slot": key, "label": label,
                        "wearable": wearable, "items": items})
    return out


def _kit_entries(kit: dict, _iu) -> list[dict]:
    """Serialise a solved kit to slot-ordered JSON entries."""
    entries: list[dict] = []
    for key, label, _mask, _n in PLAYER_SLOTS:
        held = kit.get(key)
        if not held:
            continue
        for piece in (held if isinstance(held, list) else [held]):
            entries.append({
                "slot": key, "label": label,
                "resref": piece["resref"], "name": piece["name"],
                "gp_value": piece["cost"], "wiki_url": _iu(piece["resref"]),
            })
    return entries


def _fmt_rounds(sim: dict) -> str:
    """One-line verdict for a simulated fight."""
    kill = sim["rounds_to_kill"]
    die = sim["rounds_to_die"]
    kill_s = f"{kill} rds" if kill is not None else "never"
    if sim.get("outlasts_heal_cooldown"):
        # Survives longer than the heal cooldown, so the heal keeps it standing.
        die_s = f"outlasts heal ({die} rds/heal)" if die is not None else "never"
    else:
        die_s = f"dies in {die} rds" if die is not None else "never dies"
    verdict = "✅" if sim["wins"] else "❌"
    out = f"kills in {kill_s}, {die_s} {verdict}"
    # A win measured in thousands of rounds is a win on paper only — that is
    # hours of real time, and it usually means the kit only just out-paces the
    # creature's regeneration. Say so rather than let ✅ imply "go fight this".
    if kill is not None and kill > _ATTRITION_ROUNDS:
        out += f" ⏳ attrition ({kill * 6 / 60:.0f} min)"
    if sim["save_fail"] > 0:
        out += f" (save-fail {sim['save_fail'] * 100:.0f}%/rd)"
    return out


def _kit_lines(entries: list[dict]) -> list[str]:
    """Markdown table rows for a kit, or a single 'nothing needed' line."""
    if not entries:
        return ["  - _(nothing — the fight is won bare-handed)_"]
    return [f"  - {e['label']}: {e['name']} (`{e['resref']}`, {_fmt_gp(e['gp_value'])})"
            for e in entries]


def _counter_gear_markdown(data: dict) -> str:
    """Render the counter_gear.json payload as a human-readable report."""
    L: list[str] = []
    L.append(f"# Counter-gear analysis — {data['module']}")
    L.append("")
    L.append(f"_Generated {data['generated_at']}. Every creature is fought by a "
             f"synthetic reference PC — a single-class fighter at level "
             f"min(CR, {data['max_player_level']}), every level-up point and all "
             "ten tiers of Great Strength (Great Dexterity behind a finesse or "
             "ranged weapon) in its attacking stat, specced into whatever weapon "
             "it is holding, with a free full heal every 150s — using only items "
             "players can actually obtain. Ability bonuses stack across items up "
             f"to this module's +{data['max_ability_bonus']} cap. Rounds are "
             "expected values, not rolls. No spells, potions or summons are "
             "modelled, so **\"unwinnable\" means unwinnable on gear alone**, and "
             "save DCs are estimates (innate spell level isn't in the data)._")
    L.append("")
    L.append(f"_Critical hits: threat range and multiplier come from each "
             "weapon's own `baseitems.2da` row, and a critical needs a "
             "threatening roll that hits followed by a confirmation roll. The PC "
             "takes Improved Critical (doubling the threat range) and, where the "
             "weapon allows it and Strength reaches 25, Overwhelming and "
             "Devastating Critical; creatures get only the critical feats their "
             "blueprint actually carries. Devastating Critical in this module: "
             f"**{data['devastating_critical']['label']}**. Creatures immune to "
             "critical hits take none of it. Legendary feats are not simulated._")
    L.append("")
    creatures = data["creatures"]
    won = [c for c in creatures if c["sim"]["wins"]]
    L.append("## Summary")
    L.append("")
    L.append(f"- Creatures analysed: **{len(creatures)}**")
    L.append(f"- Beatable with obtainable gear: **{len(won)}**")
    L.append(f"- Unwinnable at the level cap: **{len(data['unbeatable'])}**")
    L.append(f"- Needs manual review: **{len(data['manual_review'])}**")
    L.append(f"- Damage types dealt by obtainable weapons: "
             f"{', '.join(data['damage_types']) or '(none found)'}")
    L.append("")

    # ---- best in slot by GP ------------------------------------------------
    L.append("## Best in slot (by GP value)")
    L.append("")
    L.append("_The most valuable items a player can actually get hold of, per "
             "equipment slot. Gold is a proxy for power, not a measure of it — "
             "use the per-creature kits below for what actually wins fights._")
    L.append("")
    for grp in data.get("top_value_by_slot", []):
        heading = grp["label"]
        if grp["wearable"] > 1:
            heading += f" (×{grp['wearable']} wearable)"
        L.append(f"### {heading}")
        L.append("")
        L.append("| # | Item | ResRef | GP Value |")
        L.append("|--:|------|--------|---------:|")
        for i, e in enumerate(grp["items"], 1):
            L.append(f"| {i} | {e['name']} | `{e['resref']}` | {e['gp_value']:,} |")
        L.append("")

    if data["unbeatable"]:
        L.append("## ⛔ Unwinnable at the level cap")
        L.append("")
        for u in data["unbeatable"]:
            L.append(f"- **{u['name']}** (`{u['canonical_resref']}`) — "
                     + "; ".join(u["reasons"]))
        L.append("")

    if data["manual_review"]:
        L.append("## ⚠️ Needs manual review")
        L.append("")
        for u in data["manual_review"]:
            L.append(f"- **{u['name']}** (`{u['canonical_resref']}`) — "
                     + "; ".join(u["notes"]))
        L.append("")

    # ---- progression ladder ------------------------------------------------
    L.append("## Progression ladder (by Challenge Rating)")
    L.append("")
    L.append("_Each tier's kit is the minimum kit of the band's most gear-hungry "
             "winnable fight — the band's worst equipment requirement, not an "
             "average. The hardest fight is listed separately: it is usually a "
             "different creature, one that needs skill rather than shopping._")
    L.append("")
    for t in data.get("progression_tiers", []):
        prof = t["defeat_profile"]
        lo, hi = prof["ac_target_range"]
        L.append(f"### {t['label']} — {t['creature_count']} creatures "
                 f"(reference PC level {t['pc_level']})")
        bits = []
        if prof["min_enhancement_to_bypass_dr"]:
            bits.append(f"weapon ≥ +{prof['min_enhancement_to_bypass_dr']} (DR)")
        bits.append(f"AC target {lo}–{hi}")
        if prof["resist_priority"]:
            bits.append("resist " + "/".join(prof["resist_priority"]))
        if prof["has_special_attacks"]:
            bits.append("save gear (special attacks)")
        L.append(f"- Profile: {' · '.join(bits)}")
        if t.get("hardest"):
            L.append(f"- Hardest fight: **{t['hardest']['name']}** "
                     f"(CR {t['hardest']['cr']}) in the module's best gear — "
                     f"{_fmt_rounds(t['hardest']['sim'])}")
        if t.get("benchmark"):
            L.append(f"- Most gear-hungry fight: **{t['benchmark']['name']}** "
                     f"(CR {t['benchmark']['cr']}) — {_fmt_rounds(t['benchmark']['sim'])}; "
                     "its minimum kit is the tier kit below")
        L.append(f"- Kit ({_fmt_gp(t['kit_cost'])} total):")
        L.extend(_kit_lines(t["recommended_kit"]))
        if t["unwinnable"]:
            L.append(f"- ⛔ Unwinnable in this band: "
                     + ", ".join(u["name"] for u in t["unwinnable"][:10])
                     + (f" (+{len(t['unwinnable']) - 10} more)"
                        if len(t["unwinnable"]) > 10 else ""))
        L.append("")

    L.append("## Per-creature counter-gear")
    L.append("")
    for c in creatures:
        sim = c["sim"]
        flag = ""
        if c["unbeatable"]:
            flag = " ⛔ UNWINNABLE"
        elif c["needs_manual_review"]:
            flag = " ⚠️ review"
        L.append(f"### {c['name']} (CR {c['cr']}){flag}")
        d = c["defenses"]
        dr = d.get("dr")
        dr_txt = (f"{dr.get('soak') or ''} {('bypass ' + dr['bypass']) if dr.get('bypass') else ''}".strip()
                  if dr else "—")
        L.append(f"- HP {c['hp'] if c['hp'] is not None else '?'} · AC {d['ac']} · "
                 f"Fort {d['fort']:+d}/Ref {d['ref']:+d}/Will {d['will']:+d}"
                 f" · SR {d['sr']} · DR {dr_txt or '—'}")
        sv = c["survival"]
        off_bits = [f"attacks +{sv['attack_bonus']}"]
        if sv.get("damage_types_dealt"):
            off_bits.append("deals " + "/".join(sv["damage_types_dealt"]))
        if sv.get("save_threats"):
            st = sv["save_threats"][0]
            off_bits.append(
                f"special DC ~{st['dc_est']} est. ({len(sv['save_threats'])} abilities)")
        L.append(f"- Offense: {' · '.join(off_bits)}")
        req = c["defeat_requirements"]
        if d.get("crit_immune"):
            L.append("- **Immune to critical hits** — no multiplier, no "
                     "Overwhelming or Devastating Critical damage")
        if req["immune_types"]:
            L.append(f"- Immune to: {', '.join(req['immune_types'])}")
        if req["mandatory_weapon_tags"]:
            L.append(f"- **Requires weapon tag:** {', '.join(req['mandatory_weapon_tags'])}")
        if req["min_enhancement_to_bypass_dr"]:
            L.append(f"- Needs +{req['min_enhancement_to_bypass_dr']} enhancement to bypass DR")
        for u in c["unbeatable_reasons"]:
            L.append(f"- ⛔ {u}")
        for n in c["review_notes"]:
            L.append(f"- ⚠️ {n}")
        L.append(f"- Simulated (L{c['pc_level']} PC, best gear): {_fmt_rounds(c['best_sim'])}")
        if c["minimum_kit"] is not None:
            L.append(f"- Minimum kit that wins ({_fmt_gp(c['minimum_kit_cost'])}) — "
                     f"{_fmt_rounds(sim)}:")
            L.extend(_kit_lines(c["minimum_kit"]))
        L.append("- Best obtainable kit:")
        L.extend(_kit_lines(c["best_kit"]))
        L.append("")
    return "\n".join(L)


def generate_counter_gear_index(
    db: "Db",
    module_index_dir: Path,
    module_title: str,
    now: str,
    _cu,
    _iu,
) -> None:
    """Simulate every canonical creature against a level-appropriate reference PC
    and report the gear needed to beat it.

    For each creature this derives its objective defences and offensive threat
    (via extract_creature_defenses / extract_creature_offense, shared with the
    creature pages so the combat maths never forks), then solves for two kits
    out of the player-attainable item pool: the strongest one available, and the
    cheapest one that still wins. Creatures are bucketed by Challenge Rating into
    a progression ladder, and the report opens with the highest-GP attainable
    item in every slot. Writes counter_gear.json + counter_gear.md.
    """
    t0 = time.time()
    pool = _prune_pool(build_gear_pool(db))
    print("[nwn-wiki] counter-gear: gear pool "
          + ", ".join(f"{k}={len(v)}" for k, v in pool.items() if v))

    # Damage types any obtainable weapon can deal — used to spot creatures that
    # nothing in the module can hurt.
    module_weapon_dtypes: set[str] = set()
    for pieces in pool.values():
        for p in pieces:
            if p["is_weapon"]:
                module_weapon_dtypes.update(p["off"]["damage_dtypes"])

    def _tag_has_obtainable(tag: str) -> bool:
        return any(_item_accessible(db, irr)
                   for irr in db.item_tag_groups.get(tag.lower(), []))

    creatures_out: list[dict] = []
    unbeatable_out: list[dict] = []
    review_out: list[dict] = []

    ordered = [r for r in sorted(
        db.canonical_creatures,
        key=lambda r: nwn_text(db.canonical_creature_name(r)).lower())
        if not r.startswith("__orphan_")]

    for n, can_rr in enumerate(ordered, 1):
        if n % 100 == 0:
            print(f"[nwn-wiki] counter-gear: {n}/{len(ordered)} creatures "
                  f"({time.time() - t0:.0f}s)")
        entry = db.canonical_creatures[can_rr]
        c = entry["c"]
        bp_rr = entry["bp_rr"]
        bp = db.creatures.get(bp_rr) if bp_rr != can_rr else None
        name = nwn_text(db.canonical_creature_name(can_rr))
        cr_raw = fld(c, "ChallengeRating") or (fld(bp, "ChallengeRating") if bp else "") or ""
        D = extract_creature_defenses(db, c, bp)
        O = extract_creature_offense(db, c, bp, D)

        immune_types = sorted(t for t, pct in D["immunities"].items() if pct >= 100)
        dr = D["dr"]
        min_enh = _prop_value_num(dr["bypass"]) if dr and dr.get("bypass") else 0
        mandatory_tags = sorted({t.lower() for t in D["hard_required_tags"]})

        # ---- the creature as a simulation opponent -------------------------
        # Its best weapon is the one that hits hardest; with none, the profile
        # list is empty and it can only be worn down by the PC.
        profiles = O["attack_profiles"]
        creature_att = (max(profiles, key=lambda p: p["schedule"][0] if p["schedule"] else -99)
                        if profiles else
                        attack_profile([], num_dice=0, die=0, flat=0, crit_threat=1,
                                       crit_mult=2, phys_types=[], elem={}, enhancement=0))
        crit_immune = CRIT_IMMUNITY_LABEL in D["misc_immunities"]
        creature_sim = {
            "attack": creature_att,
            "defense": defense_profile(
                ac=D["ac"], hp=D["hp"] or 1,
                dr_soak=_prop_value_num(dr["soak"]) if dr and dr.get("soak") else 0,
                dr_bypass=min_enh,
                resist=D["resistances"], immune=D["immunities"],
                regen=D["regen"], fort=D["fort"], ref=D["ref"], will=D["will"],
                crit_immune=crit_immune),
            "save_threats": O["save_threats"],
        }

        cr = _parse_cr(cr_raw)
        pc_level = _pc_level_for_cr(cr, db)
        best_kit, best_sim = best_in_slot_kit(pc_level, creature_sim, pool, db)
        min_kit, min_sim = minimum_viable_kit(pc_level, creature_sim, pool, db, best_kit)

        best_entries = _kit_entries(best_kit, _iu)
        min_entries = _kit_entries(min_kit, _iu) if best_sim["wins"] else None

        # ---- unwinnable / review detection ---------------------------------
        unbeatable_reasons: list[str] = []
        if mandatory_tags and not any(_tag_has_obtainable(t) for t in mandatory_tags):
            unbeatable_reasons.append(
                "requires a weapon tagged "
                + " or ".join(f"'{t}'" for t in mandatory_tags)
                + " but no obtainable item has that tag")
        if module_weapon_dtypes and not (module_weapon_dtypes - set(immune_types)):
            unbeatable_reasons.append(
                "immune to every damage type any obtainable weapon deals ("
                + ", ".join(sorted(module_weapon_dtypes)) + ")")
        if not best_sim["wins"]:
            if best_sim["no_damage"]:
                unbeatable_reasons.append(
                    f"takes no damage from a level-{pc_level} PC in the best "
                    "obtainable gear — its immunities, resistances and damage "
                    "reduction absorb every attack")
            elif best_sim["rounds_to_kill"] is None:
                unbeatable_reasons.append(
                    f"regenerates faster than a level-{pc_level} PC in the best "
                    "obtainable gear can deal damage")
            else:
                unbeatable_reasons.append(
                    f"kills a level-{pc_level} PC in the best obtainable gear "
                    f"({best_sim['rounds_to_die']} rounds) before it can be killed "
                    f"({best_sim['rounds_to_kill']} rounds)")
        if best_sim["save_fail"] > 0.5:
            review_notes_save = (
                f"special-attack save DC ~{O['save_threats'][0]['dc_est']} beats even "
                f"the best obtainable save gear ({best_sim['save_fail'] * 100:.0f}% "
                "failure per round) — survivable only if those abilities are not "
                "save-or-die, which the blueprint data cannot tell us")
        else:
            review_notes_save = ""

        review_notes: list[str] = []
        if review_notes_save:
            review_notes.append(review_notes_save)
        if D["mitigates_damage"]:
            review_notes.append(
                f"OnDamaged handler '{D['dmg_script']}' self-heals or grants itself "
                "damage immunity — the simulation cannot see that, so verify the "
                "result by hand")

        rec = {
            "canonical_resref": can_rr,
            "blueprint_resref": bp_rr,
            "name": name,
            "wiki_url": _cu(can_rr),
            "cr": str(cr_raw),
            "hp": D["hp"],
            "pc_level": pc_level,
            "defenses": {
                "ac": D["ac"], "fort": D["fort"], "ref": D["ref"], "will": D["will"],
                "sr": D["sr"], "dr": dr,
                "resistances": D["resistances"], "immunities": D["immunities"],
                "spell_immunities": D["spell_immunities"],
                "regen": D["regen"], "vampiric": D["vampiric"],
                "crit_immune": crit_immune,
            },
            "defeat_requirements": {
                "damageable_types": sorted(module_weapon_dtypes - set(immune_types)),
                "immune_types": immune_types,
                "min_enhancement_to_bypass_dr": min_enh,
                "mandatory_weapon_tags": mandatory_tags,
                "hard_gate": bool(mandatory_tags),
            },
            "survival": {
                "attack_bonus": O["attack_bonus"],
                "ac_target": O["ac_target"],
                "damage_types_dealt": O["damage_types_dealt"],
                "save_threats": O["save_threats"],
            },
            "sim": min_sim if min_entries is not None else best_sim,
            "best_sim": best_sim,
            "best_kit": best_entries,
            "best_kit_cost": sum(e["gp_value"] for e in best_entries),
            "minimum_kit": min_entries,
            "minimum_kit_cost": (sum(e["gp_value"] for e in min_entries)
                                 if min_entries else 0),
            "unbeatable": bool(unbeatable_reasons),
            "unbeatable_reasons": unbeatable_reasons,
            "needs_manual_review": bool(review_notes),
            "review_notes": review_notes,
        }
        creatures_out.append(rec)
        if unbeatable_reasons:
            unbeatable_out.append({"canonical_resref": can_rr, "name": name,
                                   "reasons": unbeatable_reasons})
        if review_notes:
            review_out.append({"canonical_resref": can_rr, "name": name,
                               "notes": review_notes})

    # ---- progression ladder ------------------------------------------------
    # A tier's kit has to beat the *hardest* creature in the band, not a typical
    # one — a kit that clears the average and dies to the boss is not a kit.
    progression_tiers: list[dict] = []
    by_resref = {r["canonical_resref"]: r for r in creatures_out}
    for lo, hi, label in _CR_TIERS:
        members = [r for r in creatures_out if lo <= _parse_cr(r["cr"]) < hi]
        if not members:
            continue
        winnable = [r for r in members if not r["unbeatable"]]
        ac_targets = [r["survival"]["ac_target"] for r in members]
        resist_counter: dict[str, int] = defaultdict(int)
        for r in members:
            for dt in r["survival"]["damage_types_dealt"]:
                resist_counter[dt] += 1
        top_resist = [dt for dt, _ in sorted(
            resist_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]

        # Two different questions, two different answers — conflating them is
        # what made the draft report useless.
        #
        # "Most demanding" is the fight in the band that needs the most gear, so
        # its minimum kit is the one to publish for the tier: buy that and you
        # have covered the band's worst equipment requirement.
        #
        # "Hardest" is the closest-run fight in the module's best gear. It is a
        # separate creature more often than not, and it cannot be found from the
        # minimum kits: those are shrunk until they only just win, so every
        # creature's margin there sits at ~1.0 by construction.
        benchmark = hardest = None
        kit_entries: list[dict] = []
        if winnable:
            demanding = max(winnable, key=lambda r: (r["minimum_kit_cost"],
                                                     r["canonical_resref"]))
            kit_entries = demanding["minimum_kit"] or []
            benchmark = {
                "canonical_resref": demanding["canonical_resref"],
                "name": demanding["name"], "cr": demanding["cr"],
                "sim": demanding["sim"],
            }
            tightest = min(winnable, key=lambda r: (r["best_sim"]["margin"],
                                                    -(r["best_sim"]["rounds_to_kill"] or 0)))
            hardest = {
                "canonical_resref": tightest["canonical_resref"],
                "name": tightest["name"], "cr": tightest["cr"],
                "sim": tightest["best_sim"],
            }
        progression_tiers.append({
            "label": label,
            "creature_count": len(members),
            "pc_level": _pc_level_for_cr(max(_parse_cr(r["cr"]) for r in members), db),
            "defeat_profile": {
                "min_enhancement_to_bypass_dr": max(
                    (r["defeat_requirements"]["min_enhancement_to_bypass_dr"]
                     for r in members), default=0),
                "ac_target_range": [min(ac_targets), max(ac_targets)],
                "resist_priority": top_resist,
                "has_special_attacks": any(r["survival"]["save_threats"] for r in members),
            },
            "benchmark": benchmark,
            "hardest": hardest,
            "recommended_kit": kit_entries,
            "kit_cost": sum(e["gp_value"] for e in kit_entries),
            "unwinnable": [{"canonical_resref": r["canonical_resref"], "name": r["name"]}
                           for r in members if r["unbeatable"]],
        })

    payload = {
        "generated_at": now,
        "module": module_title,
        "input_fingerprint": _counter_gear_fingerprint(db),
        "algo_version": _COUNTER_GEAR_ALGO_VERSION,
        "max_player_level": db.max_player_level,
        "max_ability_bonus": db.max_ability_bonus,
        "devastating_critical": devcrit_mode(db),
        "damage_types": sorted(module_weapon_dtypes),
        "top_value_by_slot": _top_value_by_slot(db, _iu),
        "creatures": creatures_out,
        "unbeatable": unbeatable_out,
        "manual_review": review_out,
        "progression_tiers": progression_tiers,
    }
    _write_json(module_index_dir / "counter_gear.json", payload)
    (module_index_dir / "counter_gear.md").write_text(
        _counter_gear_markdown(payload), encoding="utf-8")
    sev = "warn" if unbeatable_out else "info"
    state._module_index_summary.append((sev,
        f"[nwn-wiki] module-index: counter_gear.json "
        f"({len(creatures_out)} creatures, {len(unbeatable_out)} unwinnable, "
        f"{len(review_out)} review, {time.time() - t0:.0f}s)"))




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


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


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
