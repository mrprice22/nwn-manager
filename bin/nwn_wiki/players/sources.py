"""Read-only loaders for every persistent store the awards draw on.

Nothing in this module writes. Databases are opened through a ``file:...?mode=ro``
URI so a bug here can never touch live server data — these files are the season's
only record and several of them (``meritdb``, ``admindb``) are *shared with the
running seasons* via symlink into ``~/.local/share/nwn-shared/``.

The NWNX key/value tables (``db``) all share one shape::

    db(varname, playerid, vartype, payload, compressed)

``payload`` for an int (``vartype 73``) is the decimal number as text, so the
key/value reads below are plain ``int(...)`` — no GFF decoding. Bank *boxes*
(``bank_box_N``) are serialized objects and are deliberately not read.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def open_ro(path: Path) -> sqlite3.Connection | None:
    """Open a SQLite file read-only, or return None (with a warning) if absent."""
    if not Path(path).exists():
        print(f"[warn] missing database: {path}", file=sys.stderr)
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        # Player-chosen character names are not reliably UTF-8 -- the engine writes
        # whatever the client sent, so a name like "Slayer of Th\xe9oden" arrives as
        # Latin-1 and would abort the whole query. Replace rather than raise: a
        # mangled character in a name must not cost us the entire kill ledger.
        conn.text_factory = lambda b: b.decode("utf-8", "replace")
        return conn
    except sqlite3.Error as exc:
        print(f"[warn] cannot open {path}: {exc}", file=sys.stderr)
        return None


def _rows(conn: sqlite3.Connection | None, sql: str, args=()) -> list[tuple]:
    if conn is None:
        return []
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error as exc:
        print(f"[warn] query failed ({exc}): {sql.strip()[:60]}", file=sys.stderr)
        return []


def kv_ints(conn: sqlite3.Connection | None, like: str) -> list[tuple[str, str, int]]:
    """Every int-valued row of a `db` table whose varname matches a LIKE pattern.

    Returns (varname, playerid, value). Non-numeric payloads are skipped rather
    than raising — a serialized object under an unexpected key must not abort a run.
    """
    out = []
    for varname, playerid, payload in _rows(
        conn, "select varname, playerid, cast(payload as text) from db where varname like ?", (like,)
    ):
        try:
            out.append((varname, playerid or "", int(str(payload).strip())))
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- #
# bestiarydb — the kill ledger
# --------------------------------------------------------------------------- #

def load_kills(conn) -> list[dict]:
    return [
        {"uuid": u, "cdkey": c or "", "char_name": n or "", "resref": (r or "").lower(),
         "solo": s or 0, "party": p or 0, "last": la or ""}
        for u, c, n, r, s, p, la in _rows(
            conn, "select uuid, cdkey, char_name, resref, solo_kills, party_kills, last_kill from kills"
        )
    ]


def load_server_firsts(conn) -> list[dict]:
    return [
        {"resref": (r or "").lower(), "cr": cr or 0, "player": pn or "",
         "cdkey": ck or "", "char_name": cn or "", "at": at or ""}
        for r, cr, pn, ck, cn, at in _rows(
            conn,
            "select resref, cr, first_player_name, first_cdkey, first_name, first_at from server_first",
        )
    ]


def load_catalogue(conn) -> dict[str, dict]:
    return {
        (r or "").lower(): {"name": n or r, "cr": cr or 0}
        for r, n, cr in _rows(conn, "select resref, name, cr from catalogue")
    }


def load_kill_aliases(conn) -> dict[str, str]:
    """resref -> canonical resref, so re-skinned duplicates collapse into one species."""
    return {
        (r or "").lower(): (c or "").lower()
        for r, c in _rows(conn, "select resref, canonical from resref_alias")
    }


# --------------------------------------------------------------------------- #
# respawndb — the boss registry
# --------------------------------------------------------------------------- #

def load_bosses(conn) -> dict[str, dict]:
    return {
        (r or "").lower(): {"name": n or r, "area": a or "", "cr": cr or 0}
        for r, n, a, cr in _rows(
            conn, "select resref, name, area_name, cr from boss_registry"
        )
    }


# --------------------------------------------------------------------------- #
# meritdb / admindb — shared across seasons, so always date-filter
# --------------------------------------------------------------------------- #

def load_merit_ledger(conn) -> list[dict]:
    return [
        {"cdkey": c or "", "player": p or "", "delta": d or 0,
         "reason": r or "", "at": (at or "")[:10]}
        for c, p, d, r, at in _rows(
            conn, "select cdkey, player_name, delta, reason, created_at from merit_ledger"
        )
    ]


def load_redemptions(conn) -> list[dict]:
    return [
        {"cdkey": c or "", "player": p or "", "label": lb or "", "cost": co or 0,
         "status": st or "", "at": (at or "")[:10]}
        for c, p, lb, co, st, at in _rows(
            conn,
            "select cdkey, player_name, reward_label, cost, status, requested_at from redemptions",
        )
    ]


def load_houses(conn) -> list[dict]:
    return [
        {"cdkey": c or "", "player": p or "", "area_tag": a or "", "at": (at or "")[:10]}
        for c, p, a, at in _rows(
            conn, "select cdkey, player_name, area_tag, added_at from houses"
        )
    ]


# --------------------------------------------------------------------------- #
# activity-sessions.json — the playtime cache written by the wiki build
# --------------------------------------------------------------------------- #

def load_sessions(path: Path) -> list[dict]:
    """Closed play sessions: {player, cdkey, role, join, leave, duration_min}.

    This file is irreplaceable — it preserves hours after the source server logs
    rotate away — so it is only ever read here, never rewritten.
    """
    if not Path(path).exists():
        print(f"[warn] missing activity cache: {path}", file=sys.stderr)
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] bad activity cache: {exc}", file=sys.stderr)
        return []
    return [s for s in data.get("sessions", []) if s.get("role") != "Game Master"]


# --------------------------------------------------------------------------- #
# module-index/*.json — resolved names, written by the wiki build
# --------------------------------------------------------------------------- #

def load_creature_index(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"[warn] no creature index at {path}", file=sys.stderr)
        return {}
    out = {}
    for c in data.get("creatures", []):
        for key in (c.get("canonical_resref"), c.get("blueprint_resref")):
            if key:
                out.setdefault(key.lower(), c)
    return out


def load_admin_cdkeys(conn) -> set[str]:
    """CD keys in admindb's ``admins`` table -- server admins and DMs.

    Their characters are excluded from every published player page. Admins have
    access to DM-only gear and free teleports, so their kills, wealth and
    playtime are not comparable to a player's and must not sit in a ranking.

    Returns an empty set when the table or the DB is missing, which is the right
    default: a module with no admin registry has no admins to exclude.
    """
    return {r[0] for r in _rows(conn, "SELECT cdkey FROM admins") if r[0]}


def load_combat_dummy_runs(conn) -> dict[str, list[dict]]:
    """Character UUID -> its combat-dummy trials, newest first.

    The dummy records one row per 10-round trial (see the combat-dummy roadmap
    item), so a character has as many rows as times they hit it. The whole
    history is returned rather than a single figure: the character page shows
    the best run, the average of the recent ones, and when each happened, and
    those answer different questions -- "what can this build do" versus "what
    is it doing lately".

    Empty when the module has no combat dummy.
    """
    runs: dict[str, list[dict]] = {}
    for uuid, dpr, rounds, at in _rows(
            conn, "SELECT uuid, dpr, rounds, at FROM sessions"):
        if not uuid:
            continue
        runs.setdefault(uuid, []).append(
            {"dpr": dpr or 0.0, "rounds": rounds or 0, "at": at or ""})
    # Newest first. `at` is "YYYY-MM-DD HH:MM:SS" from SQLite's datetime('now'),
    # which sorts correctly as a string.
    for rows in runs.values():
        rows.sort(key=lambda r: r["at"], reverse=True)
    return runs


def load_playtime(conn) -> tuple[dict[str, dict], str]:
    """Per-CHARACTER play time, and the date tracking began.

    Returns ({uuid: {"minutes", "sessions"}}, tracking_started).

    Only completed sessions count: a row with NULL minutes was abandoned (the
    server went down without firing Mod_OnClientLeav) and its length is
    genuinely unknown, so ptm_db.nss records it as unknown rather than guessing.
    Summing it as zero would be just as wrong as summing it as "until now".

    ``tracking_started`` matters as much as the figures. The module stamps it on
    the first load after the table ships, and every season predates it -- a
    character with 3 hours here may have been played for months. Any page
    showing these numbers must show this date too, or it is quietly lying.
    """
    totals: dict[str, dict] = {}
    for row in _rows(conn,
                     "SELECT uuid, SUM(minutes) AS m, COUNT(*) AS n "
                     "FROM sessions WHERE minutes IS NOT NULL GROUP BY uuid"):
        uuid, mins, n = row[0], row[1] or 0.0, row[2] or 0
        if uuid:
            totals[uuid] = {"minutes": mins, "sessions": n}

    started = ""
    rows = _rows(conn, "SELECT value FROM meta WHERE key = 'tracking_started'")
    if rows:
        started = rows[0][0] or ""
    return totals, started

