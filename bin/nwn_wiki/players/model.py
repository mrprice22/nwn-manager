"""Join the vault, the kill ledger and the session history into character records.

The renderers stay dumb: every page reads the records this module produces, the
same way the creature pages read ``state._BESTIARY_TOP``. Everything here is
read-only.

Three sources have to be reconciled, and none of them agrees with the others
about what identifies a character:

* the **vault** knows a character by ``uuid`` and its owning CD-key directory;
* ``bestiarydb.kills`` knows it by ``uuid`` *and* a ``char_name`` snapshot taken
  at kill time (so a renamed character has rows under both names);
* the **session log** knows only the *account* — never the character.

So kills join on ``uuid``, and the account display name comes from the roster
(:mod:`nwn_wiki.players.identity`). Where the roster cannot name an account we
render nothing rather than falling back to the CD key: a CD key is account
credentials and must never reach a page (see ``state.py`` on uuid).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime

from nwn_wiki.lookups import class_name, race_name

# Alignment thresholds match NWN's own bands (0-30 / 31-69 / 70-100).
_LOW, _HIGH = 30, 69


def _slug(name: str, uuid: str) -> str:
    """URL-safe page name. Distinct characters can share a display name, so the
    slug is suffixed with a short uuid fragment whenever the name alone collides;
    callers resolve that via :func:`assign_slugs`, not here."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or f"character-{uuid[:8]}"


def assign_slugs(records: list[dict]) -> None:
    """Give every record a unique ``slug``, in place.

    Two characters legitimately share a name (the dev vault has three "Fireberry"
    across two accounts), and a filename collision would silently overwrite a
    page, so colliding slugs take a uuid suffix. Sorted first so the suffix a
    character gets is stable between builds rather than filesystem-order.
    """
    by_slug: dict[str, list[dict]] = defaultdict(list)
    for rec in sorted(records, key=lambda r: (r["name"].lower(), r["uuid"], r["_cdkey"])):
        by_slug[_slug(rec["name"], rec["uuid"])].append(rec)

    taken: set[str] = set()
    for slug, group in sorted(by_slug.items()):
        for rec in group:
            cand = slug
            if len(group) > 1:
                # A uuid suffix separates same-named characters -- but the vault
                # can hold the SAME character under two accounts (a .bic copied
                # between CD-key directories: the dev vault is synced from prod,
                # and the admin keeps test copies), so uuid alone is not unique
                # either. Fall back to a counter so a page can never be silently
                # overwritten by another character's build.
                cand = f"{slug}-{rec['uuid'][:8]}" if rec["uuid"] else slug
                if cand in taken:
                    n = 2
                    while f"{cand}-{n}" in taken:
                        n += 1
                    cand = f"{cand}-{n}"
            rec["slug"] = cand
            taken.add(cand)


def _alignment(good_evil: int, law_chaos: int) -> str:
    ge = "Good" if good_evil > _HIGH else "Evil" if good_evil <= _LOW else "Neutral"
    lc = "Lawful" if law_chaos > _HIGH else "Chaotic" if law_chaos <= _LOW else "Neutral"
    if ge == "Neutral" and lc == "Neutral":
        return "True Neutral"
    return f"{lc} {ge}"


def _class_line(classes: list) -> str:
    """"Sorcerer 37 / Bard 1" — highest level first, which is how players say it."""
    parts = [(class_name(cid), lvl) for cid, lvl in classes]
    parts.sort(key=lambda p: -p[1])
    return " / ".join(f"{n} {l}" for n, l in parts)


def _parse_ts(v):
    """Session/kill timestamps arrive as ISO strings or datetimes depending on
    whether they came from the JSON cache or a live parse."""
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(v)[:19], fmt)
        except ValueError:
            continue
    return None


def build_records(chars: list[dict], kills: list[dict], sessions: list[dict],
                  roster, catalogue: dict | None = None,
                  exclude_cdkeys: "set[str] | None" = None,
                  dummy_best: dict | None = None) -> list[dict]:
    """One record per character in the vault, enriched with kills and play data.

    ``catalogue`` maps resref -> creature row (from ``sources.load_catalogue``)
    and is used only to name a character's most-killed creature; without it the
    records still build, just with resrefs instead of names.

    ``exclude_cdkeys`` drops every character owned by those accounts before any
    record is built -- the admin/DM accounts from ``sources.load_admin_cdkeys``.
    They are excluded from publishing entirely, not merely from the boards:
    admins can hand themselves DM-only gear and teleport at will, so their
    kills, wealth and playtime are not comparable to a player's and would
    distort any ranking they appeared in.

    Excluding them also removes the only source of duplicate characters. The dev
    realm's vault holds admin copies of players' .bic files (pulled by
    sync-vault-from-prod for debugging), which share a UUID with the player's
    own character; with the admin accounts gone, no UUID appears twice.
    """
    catalogue = catalogue or {}
    dummy_best = dummy_best or {}
    if exclude_cdkeys:
        chars = [c for c in chars if c["cdkey"] not in exclude_cdkeys]

    # -- kills, keyed by character uuid ------------------------------------- #
    by_uuid: dict[str, list[dict]] = defaultdict(list)
    for k in kills:
        if k.get("uuid"):
            by_uuid[k["uuid"]].append(k)

    # -- per-account play history ------------------------------------------- #
    acct_last: dict[str, datetime] = {}
    acct_minutes: dict[str, float] = defaultdict(float)
    for s in sessions:
        if s.get("role") != "Player":
            continue
        ck = roster.key_for_session(s)
        if not ck:
            continue
        acct_minutes[ck] += s.get("duration_min") or 0.0
        ts = _parse_ts(s.get("leave") or s.get("join"))
        if ts and (ck not in acct_last or ts > acct_last[ck]):
            acct_last[ck] = ts

    records: list[dict] = []
    for c in chars:
        ck = c["cdkey"]
        rows = by_uuid.get(c.get("uuid") or "", [])
        solo = sum(r["solo"] for r in rows)
        party = sum(r["party"] for r in rows)
        last_kill = max((_parse_ts(r.get("last")) for r in rows if r.get("last")),
                        default=None)

        top = max(rows, key=lambda r: r["solo"] + r["party"], default=None)
        top_kill = None
        if top:
            entry = catalogue.get(top["resref"]) or {}
            top_kill = {
                "name": entry.get("name") or top["resref"],
                "count": top["solo"] + top["party"],
            }

        # The roster names the ACCOUNT; it returns the CD key when it has never
        # seen that account log in. That is not a display name -- blank it.
        account = roster.account(ck)
        player = account if account and account != ck else ""

        records.append({
            "uuid": c.get("uuid") or "",
            "name": c["name"],
            "player": player,
            "level": c["level"],
            "classes": c["classes"],
            "class_line": _class_line(c["classes"]),
            "race": race_name(c["race"]) if c["race"] >= 0 else "",
            "subrace": c.get("subrace") or "",
            "deity": c.get("deity") or "",
            "alignment": _alignment(c["good_evil"], c["law_chaos"]),
            "abilities": c["abilities"],
            "max_hp": c["max_hp"],
            "gold": c["gold"],
            "xp": c["xp"],
            "feats": c["feats"],
            "skills": c["skills"],
            "equipped": c["equipped"],
            "items": c["items"],
            "familiar_type": c.get("familiar_type"),
            "companion_type": c.get("companion_type"),
            # derived
            "kills_solo": solo,
            "kills_party": party,
            "kills_total": solo + party,
            "unique_creatures": len({r["resref"] for r in rows}),
            # Internal: the actual resrefs, so a player's bestiary progress can
            # be a real union across their characters rather than a max.
            "_resrefs": sorted({r["resref"] for r in rows}),
            # Best combat-dummy run, when this character ever hit the dummy.
            "best_dpr": (dummy_best.get(c.get("uuid") or "") or {}).get("dpr"),
            # Character-sheet totals, for the player averages on the Hall of
            # Fame. Ability scores here are the stored (pre-racial, pre-gear)
            # values -- what the sheet calls the character's own scores.
            "ability_total": sum(c["abilities"].values()),
            "skill_total": sum(c["skills"]),
            "top_kill": top_kill,
            "last_kill": last_kill,
            "last_seen": acct_last.get(ck),
            "play_hours": round(acct_minutes.get(ck, 0.0) / 60.0, 1),
            # internal only -- never rendered (see module docstring)
            "_cdkey": ck,
        })

    assign_slugs(records)
    records.sort(key=lambda r: (-r["level"], r["name"].lower()))
    return records

# --------------------------------------------------------------------------- #
# Frozen snapshots, for archived seasons
# --------------------------------------------------------------------------- #

# Fields that exist only to join the three data sources and must never leave the
# process: a CD key is account credentials, and a snapshot is COMMITTED to the
# repo, so writing one unredacted would publish them to git forever.
_INTERNAL_FIELDS = ("_cdkey",)

# Record fields holding a datetime; JSON has no such type.
_DATE_FIELDS = ("last_seen", "last_kill")


def save_snapshot(records: list[dict], path: "Path") -> None:
    """Freeze the built records to JSON, minus the internal join keys.

    An archived season's vault and campaign DBs live outside the repo, under a
    per-season NWN_HOME_DIR that nothing backs up -- so once that directory is
    gone the season's character pages can never be rebuilt. Committing this file
    makes them reproducible forever, the same way module-index/
    activity-sessions.json preserves session history the logs have since rotated
    away.
    """
    out = []
    for rec in records:
        r = {k: v for k, v in rec.items() if k not in _INTERNAL_FIELDS}
        for f in _DATE_FIELDS:
            r[f] = r[f].isoformat() if r.get(f) else None
        out.append(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "characters": out}, indent=1),
                    encoding="utf-8")


def load_snapshot(path: "Path") -> list[dict]:
    """Records from a frozen snapshot, or [] when there is none to read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("characters") or []
    for r in records:
        for f in _DATE_FIELDS:
            r[f] = _parse_ts(r.get(f))
        # Restored records are render-only; nothing downstream may join on the
        # key that was deliberately stripped.
        r.setdefault("_cdkey", "")
    return records


# --------------------------------------------------------------------------- #
# Players (accounts)
# --------------------------------------------------------------------------- #

def build_players(records: list[dict]) -> list[dict]:
    """Group published characters by account, newest-active first.

    Only accounts the roster could actually name get a page: the account name is
    the page's whole identity, and a CD key is never a substitute for it (it is
    account credentials). Characters whose owner could not be resolved still get
    their own pages -- they just belong to no player page, which is the honest
    representation of "we do not know who played this".
    """
    by_name: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        if rec["player"]:
            by_name[rec["player"]].append(rec)

    players: list[dict] = []
    for name, chars in by_name.items():
        chars = sorted(chars, key=lambda r: (-r["level"], r["name"].lower()))
        last_seen = max((c["last_seen"] for c in chars if c["last_seen"]),
                        default=None)
        players.append({
            "name": name,
            "slug": _slug(name, ""),
            "characters": chars,
            "character_count": len(chars),
            # Playtime is per-ACCOUNT, not per-character: the session log knows
            # only who logged in, never which character they picked. Every one of
            # this account's records therefore carries the same figure, so take
            # it once rather than summing it per character.
            "play_hours": chars[0]["play_hours"] if chars else 0.0,
            "kills_total": sum(c["kills_total"] for c in chars),
            # Best single character's bestiary progress, next to the union
            # above: one shows the account's total coverage, the other how far
            # any one character got on its own.
            "best_bestiary": max((c["unique_creatures"] for c in chars),
                                 default=0),
            "best_dpr": max((c["best_dpr"] for c in chars
                             if c.get("best_dpr")), default=None),
            "avg_ability_total": (sum(c["ability_total"] for c in chars) / len(chars)
                                  if chars else 0.0),
            "avg_skill_total": (sum(c["skill_total"] for c in chars) / len(chars)
                                if chars else 0.0),
            "unique_creatures": len(set().union(
                *(c.get("_resrefs") or [] for c in chars))) if chars else 0,
            "top_level": max((c["level"] for c in chars), default=0),
            "last_seen": last_seen,
        })

    # Distinct account names can still collide once slugged (case, punctuation).
    by_slug: dict[str, list[dict]] = defaultdict(list)
    for p in sorted(players, key=lambda p: p["name"].lower()):
        by_slug[p["slug"]].append(p)
    for slug, group in by_slug.items():
        if len(group) > 1:
            for n, p in enumerate(group, 1):
                p["slug"] = f"{slug}-{n}"

    players.sort(key=lambda p: (p["last_seen"] is None,
                                -(p["last_seen"].timestamp() if p["last_seen"] else 0),
                                p["name"].lower()))
    return players

