"""Bestiary kill stats: the live NWNX:EE campaign DB reads and seeds.

Seeds the creature catalogue the in-game bestiary book reads, loads kill
counts / top-killer rows / server-first records back out of that DB, and
back-fills player names onto server-first rows from parsed log sessions.
Only active when the build is given ``--db-dir``.

Mutable build state (the bestiary globals) lives in :mod:`nwn_wiki.state` and
is always reached through the module object -- ``state.X`` -- so writes by the
loaders here are visible to the renderers.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from nwn_wiki.gff import fld
from nwn_wiki.htmlgen.escape import nwn_text
from nwn_wiki.util import _try_float

from nwn_wiki import state


def seed_bestiary_catalogue(db: "Db", db_dir: Path) -> None:
    """Write the creature catalogue (resref, name, CR) into the bestiary DB
    (<db_dir>/bestiarydb.sqlite3 — must match BST_DB in bst_db.nss, the file the
    in-game scripts use) so the in-game book can list creatures killed or not.

    Seeds only PLAYER-ACCESSIBLE creatures — the same "present" set the creatures
    index uses (has >=1 placement or encounter, i.e. canonical_locations count
    > 0), including stock NWN/CEP creatures registered from encounter pools.
    Blueprint-only creatures (e.g. DM avatars) are excluded. Rows are keyed by the
    runtime resref (the base blueprint, what GetResRef returns in-game), never the
    synthetic __v2/__orphan ids, so kills JOIN. The table is rebuilt each run so
    creatures that become inaccessible are pruned. Tolerant of the server holding
    the DB."""
    import sqlite3
    # The dir is normally the live server's database/ (already present). Create
    # it only when genuinely absent — mkdir(exist_ok) still raises on a symlink
    # whose target doesn't resolve on this host (e.g. a container mount).
    if not db_dir.exists():
        try:
            db_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"[nwn-wiki] bestiary: cannot create {db_dir}: {e}", file=sys.stderr)
            return
    db_file = db_dir / "bestiarydb.sqlite3"
    try:
        con = sqlite3.connect(str(db_file), timeout=5.0)
    except sqlite3.Error as e:
        print(f"[nwn-wiki] bestiary: cannot open {db_file}: {e}", file=sys.stderr)
        return
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("CREATE TABLE IF NOT EXISTS catalogue "
                    "(resref TEXT PRIMARY KEY, name TEXT, cr REAL)")
        # Accessible = the base blueprint of any canonical that has a location
        # (placement or encounter). A base counts if it OR any of its variants
        # is placed/encountered.
        accessible_bases = set()
        for c_rr in db.canonical_creatures:
            if sum(l["count"] for l in db.canonical_locations.get(c_rr, [])) > 0:
                accessible_bases.add(db.canonical_bp_of.get(c_rr, c_rr))
        rows = []
        for can_rr, entry in db.canonical_creatures.items():
            if db.canonical_bp_of.get(can_rr) != can_rr:
                continue  # variant; never appears in-game by ResRef
            if can_rr.startswith("__orphan_"):
                continue  # synthetic id with no runtime resref — can't be killed by it
            if can_rr not in accessible_bases:
                continue  # blueprint-only / un-encounterable
            c = entry["c"]
            bp_rr = entry["bp_rr"]
            bp = db.creatures.get(bp_rr) if bp_rr != can_rr else None
            cr_raw = fld(c, "ChallengeRating")
            if cr_raw is None and bp is not None:
                cr_raw = fld(bp, "ChallengeRating")
            cr = _try_float(cr_raw or 0, 0.0)
            rows.append((can_rr, nwn_text(db.canonical_creature_name(can_rr)), cr))
        # Rebuild so newly-inaccessible creatures are pruned (catalogue is derived
        # data; kills / server_first are separate tables and untouched).
        con.execute("DELETE FROM catalogue")
        con.executemany(
            "INSERT INTO catalogue(resref,name,cr) VALUES(?,?,?)", rows)
        con.commit()
        print(f"[nwn-wiki] bestiary: seeded {len(rows)} accessible catalogue rows "
              f"into {db_file}")
    except sqlite3.Error as e:
        print(f"[nwn-wiki] bestiary: catalogue seed failed: {e}", file=sys.stderr)
    finally:
        con.close()


def load_bestiary_stats(db_dir: Path, sessions: list | None = None) -> None:
    """Populate the bestiary globals from the live DB (read-only). Missing DB or
    missing tables (no kills yet) leave the stats empty but mark the system active
    so the Server-First page still renders. `sessions` (parsed log sessions, if
    available) supplies a cdkey->player-name mapping for the Top Killers table's
    Player column; kills only store cdkey, not the account/player name."""
    import sqlite3
    cdkey_to_name: dict = {}
    for s in (sessions or []):
        if s.get("cdkey") and s.get("player"):
            cdkey_to_name[s["cdkey"]] = s["player"]
    state._BESTIARY_KILLS = {}
    state._BESTIARY_TOP = {}
    state._SERVER_FIRSTS = []
    db_file = db_dir / "bestiarydb.sqlite3"
    if not db_file.is_file():
        return
    state._BESTIARY_ACTIVE = True
    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as e:
        print(f"[nwn-wiki] bestiary: cannot read {db_file}: {e}", file=sys.stderr)
        return
    try:
        for resref, total, solo, party in con.execute(
            "SELECT resref, SUM(solo_kills+party_kills), SUM(solo_kills), "
            "SUM(party_kills) FROM kills GROUP BY resref"
        ).fetchall():
            state._BESTIARY_KILLS[resref] = {"total": int(total or 0),
                                       "solo": int(solo or 0),
                                       "party": int(party or 0)}
    except sqlite3.Error:
        pass  # kills table not created yet (no kills recorded)
    # Per-character rows for the "Top Killers" tables on creature pages.
    # Loaded once here (never per-page SQL). Fold variant resrefs through the
    # DB's resref_alias table so a character's kills of a variant blueprint
    # count toward the canonical creature (kills are normally written already
    # canonical by Bst_Canonical, so this is a safety net).
    try:
        try:
            alias = dict(con.execute(
                "SELECT resref, canonical FROM resref_alias").fetchall())
        except sqlite3.Error:
            alias = {}
        per_char: dict[tuple[str, str], dict] = {}
        for resref, uuid, cdkey, name, solo, party, last in con.execute(
            "SELECT resref, uuid, cdkey, char_name, solo_kills, party_kills, "
            "last_kill FROM kills"
        ).fetchall():
            rr = alias.get(resref, resref)
            row = per_char.setdefault((rr, uuid), {
                "uuid": uuid, "name": name or "", "player": "", "solo": 0,
                "party": 0, "last": ""})
            row["solo"] += int(solo or 0)
            row["party"] += int(party or 0)
            if (last or "") >= row["last"]:
                row["last"] = last or ""
                if name:  # most recent char_name wins (renames)
                    row["name"] = name
                if cdkey and cdkey_to_name.get(cdkey):
                    row["player"] = cdkey_to_name[cdkey]
        for (rr, _uuid), row in per_char.items():
            row["total"] = row["solo"] + row["party"]
            state._BESTIARY_TOP.setdefault(rr, []).append(row)
        for rows_ in state._BESTIARY_TOP.values():
            rows_.sort(key=lambda r: (-r["total"], r["name"].lower()))
    except sqlite3.Error:
        pass  # kills table not created yet
    try:
        try:
            rows = con.execute(
                "SELECT s.resref, s.cr, s.first_name, s.first_at, "
                "COALESCE(c.name, s.resref), s.first_player_name FROM server_first s "
                "LEFT JOIN catalogue c ON c.resref = s.resref "
                "ORDER BY s.cr DESC, s.first_at ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            # first_player_name column absent (pre-migration DB); fall back without it
            rows = [
                (rr, cr, nm, at, cn, "")
                for rr, cr, nm, at, cn in con.execute(
                    "SELECT s.resref, s.cr, s.first_name, s.first_at, "
                    "COALESCE(c.name, s.resref) FROM server_first s "
                    "LEFT JOIN catalogue c ON c.resref = s.resref "
                    "ORDER BY s.cr DESC, s.first_at ASC"
                ).fetchall()
            ]
        for resref, cr, name, at, cname, player_name in rows:
            state._SERVER_FIRSTS.append({"resref": resref, "cr": cr or 0,
                                   "name": name or "", "cname": cname or resref,
                                   "at": at or "", "player_name": player_name or ""})
    except sqlite3.Error:
        pass  # server_first table not created yet
    con.close()
    print(f"[nwn-wiki] bestiary: {len(state._BESTIARY_KILLS)} creatures with kills, "
          f"{len(state._SERVER_FIRSTS)} server-first record(s)")


def backfill_server_first_player_names(db_dir: Path, sessions: list) -> None:
    """Update server_first rows where first_player_name is NULL using a
    cdkey→player_name mapping derived from parsed log sessions (most recently
    seen name for each CD key wins)."""
    import sqlite3
    cdkey_to_name: dict = {}
    for s in sessions:
        if s.get("cdkey") and s.get("player"):
            cdkey_to_name[s["cdkey"]] = s["player"]
    db_file = db_dir / "bestiarydb.sqlite3"
    if not db_file.is_file():
        return
    try:
        con = sqlite3.connect(str(db_file), timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            con.execute("ALTER TABLE server_first ADD COLUMN first_player_name TEXT")
            con.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        if cdkey_to_name:
            con.executemany(
                "UPDATE server_first SET first_player_name = ? "
                "WHERE first_cdkey = ? AND first_player_name IS NULL",
                [(name, cdkey) for cdkey, name in cdkey_to_name.items()]
            )
            updated = con.total_changes
            con.commit()
            if updated:
                print(f"[nwn-wiki] bestiary: back-filled player name for {updated} server-first record(s)")
        con.close()
    except sqlite3.Error as e:
        print(f"[nwn-wiki] bestiary: back-fill player names failed: {e}", file=sys.stderr)


def _utc_to_local(ts_str: str) -> str:
    """Convert a 'YYYY-MM-DD HH:MM:SS' UTC string to server local time using TZ env."""
    if not ts_str:
        return ts_str
    tz_name = os.environ.get("TZ", "")
    if not tz_name:
        return ts_str
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone
        dt_utc = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
        return dt_local.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts_str
