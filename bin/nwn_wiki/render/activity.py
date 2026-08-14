"""NWN server-log parsing and the player-activity page.

Collects and caches the server log files (``nwserverLog*.txt`` / ``anvil.log``),
turns them into player sessions that survive log rotation, and renders
``activity.html`` from them -- including the inline SVG bar charts, which are
built here with plain stdlib string formatting rather than any charting library.

Only active when the build is given ``--log-dir``; ``bin/nwn-wiki-activity``
drives the same functions to refresh the page without a full wiki rebuild.
"""

from __future__ import annotations

import html
import json
import math
import re
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.htmlgen.escape import E


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
    write_page(out, PageCtx("activity.html"), "Player Activity", body,
               page_updated_at=now_str)
    print(f"[nwn-wiki] rendered activity page ({n_sessions} sessions, {n_players} players)")
