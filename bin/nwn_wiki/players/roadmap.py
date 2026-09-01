"""Roadmap credit: who reported, requested and tested what.

The ideas themselves live in the *project* repo, in ``roadmap.yaml``, and the
public Roadmap page is rendered there by ``bin/gen-roadmap.py`` -- not by this
engine. What reaches us is that generator's sidecar, ``roadmap-credits.json``,
written in the same run as the page from the same dupe-merged, hidden-filtered
list. The wiki therefore reports exactly the totals the roadmap page reports,
and no unpublished idea can leak onto a player page, because the filtering
happened before we ever saw the data.

The hard part on this side is not counting, it is *naming*. A roadmap
``player:`` is a hand-typed display string ("Sync (Shync)", "Rajmund (Ray)"),
while an account is a CD key with a login name. :func:`attribute` bridges the
two and, when it cannot, says so out loud -- the same "report, never guess"
rule the rest of :mod:`nwn_wiki.players.identity` follows. An uncredited
player is a bug someone can fix by adding an alias; a *wrongly* credited one
is a lie nobody would notice.

Everything degrades to nothing: with no sidecar, ``load_credits`` returns None
and every page renders exactly as it did before this existed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


# "Sync (Shync)" -> the parenthetical, which is usually the login name. The
# roadmap's display half is what a player is called in Discord; the half in
# brackets is what the server saw them log in as.
_PAREN_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")

# Ideas with no submitter. Never a player, never a warning.
_SENTINELS = {"community", "admin", ""}


def load_credits(path: "str | Path | None") -> dict | None:
    """Read ``roadmap-credits.json``, or None when there isn't one.

    A missing, unreadable or malformed sidecar is not an error: most modules
    have no roadmap at all, and a project whose generator failed should still
    get its wiki. Every consumer treats None as "this module has no ideas".
    """
    if not path:
        return None
    p = Path(path).expanduser()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ideas"), list):
        return None
    return data


def load_aliases(path: "str | Path | None") -> dict[str, str]:
    """Read the curated ``label -> account name or CD key`` alias table."""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _account_index(roster) -> dict[str, str]:
    """Every name an account has been seen under, folded to lowercase.

    Includes the rejected login names from ``names_seen``: an account that
    played as both "Xil" and "Zam" may well be written up in the roadmap under
    either, and both should land on the one player page.
    """
    idx: dict[str, str] = {}
    for cdkey, name in roster.name.items():
        if name:
            idx.setdefault(name.strip().lower(), name)
    for cdkey, names in roster.names_seen.items():
        canon = roster.account(cdkey)
        for n in names:
            if n:
                idx.setdefault(n.strip().lower(), canon)
    return idx


def resolve_label(label: str, roster, accounts: dict[str, str]) -> str | None:
    """A roadmap ``player:`` string -> an account name, or None.

    Four attempts, in falling order of confidence:

    1. the curated alias table, which a human wrote and which always wins;
    2. the parenthetical -- "Sync (Shync)" is a player telling us their login
       name, and it is right far more often than any fuzzy match would be;
    3. the label itself, for the many entries that are just a login name;
    4. give up.

    Never a substring or edit-distance match. Two players on this server have
    names one character apart, and crediting the wrong one is worse than
    crediting nobody.
    """
    curated = roster.resolve_roadmap_player(label)
    if curated:
        # An alias may name either an account name or a CD key (the project's
        # curated file holds CD keys, learned from the merit award dialog).
        # A key is only ever a lookup INTO the roster, never an answer out of
        # it: if the roster has never seen that key -- an older player who has
        # not logged into this season yet -- we report the label as unresolved
        # rather than falling back to the key. Returning it would make a CD key
        # into a display name, which is the one thing every page in this
        # section is forbidden to do (see nwn_wiki.state on uuid/cdkey), and it
        # would be indistinguishable from a real account name to any later
        # caller.
        return roster.name.get(curated) or accounts.get(curated.strip().lower())

    m = _PAREN_RE.match(label)
    if m:
        hit = accounts.get(m.group(2).strip().lower())
        if hit:
            return hit
        hit = accounts.get(m.group(1).strip().lower())
        if hit:
            return hit

    return accounts.get(label.strip().lower())


def attribute(credits: dict | None, roster) -> tuple[dict[str, dict], list[str]]:
    """Fold the sidecar into ``{account name: their ideas}``.

    Each entry carries:

    ``submitted``  the ideas they reported or requested, newest-status-first
    ``tested``     the ideas they hold a UAT credit on
    ``counts``     ``{"Defect": n, "Enhancement": n, "Exploit": n, "Testing": n}``
    ``pivot``      ``{(type, column): n}`` -- the roadmap's own type x stage grid

    ``counts`` deliberately counts every submission whatever its status: the
    question the players index answers is "how much has this person put in",
    not "how much of it shipped". The pivot on their own page is where the
    shipped/backlog split is made.

    Returns ``(by_player, unresolved_labels)``.
    """
    if not credits:
        return {}, []

    accounts = _account_index(roster)
    types = [t["key"] for t in credits.get("types") or [] if t.get("key")]
    by_player: dict[str, dict] = {}
    unresolved: dict[str, int] = {}

    def slot(name: str) -> dict:
        return by_player.setdefault(name, {
            "submitted": [],
            "tested": [],
            "counts": dict.fromkeys(types + ["Testing"], 0),
            "pivot": {},
        })

    def resolve(label: str) -> str | None:
        if (label or "").strip().lower() in _SENTINELS:
            return None
        who = resolve_label(label, roster, accounts)
        if who is None:
            unresolved[label] = unresolved.get(label, 0) + 1
        return who

    for idea in credits["ideas"]:
        for label in idea.get("requesters") or []:
            who = resolve(label)
            if who is None:
                continue
            rec = slot(who)
            rec["submitted"].append(idea)
            t = idea.get("type")
            if t in rec["counts"]:
                rec["counts"][t] += 1
            # An idea with no type still goes in the pivot, under "", so the
            # grid totals to what the heading says the player submitted. The
            # roadmap page can drop untyped ideas from its own pivot because
            # its heading makes no claim; a per-player total that quietly
            # disagreed with its own list would just look like a bug.
            key = (t or "", idea.get("column") or "")
            rec["pivot"][key] = rec["pivot"].get(key, 0) + 1

        for credit in idea.get("uat_credits") or []:
            who = resolve(credit.get("player") or "")
            if who is None:
                continue
            rec = slot(who)
            rec["tested"].append(idea)
            rec["counts"]["Testing"] += 1

    # Within a player, order ideas the way the roadmap does: workflow rank
    # first (in progress before "some day"), then title.
    rank = {k: v.get("rank", 99) for k, v in (credits.get("statuses") or {}).items()}
    for rec in by_player.values():
        for key in ("submitted", "tested"):
            rec[key].sort(key=lambda i: (rank.get(i.get("status"), 99),
                                         (i.get("title") or "").lower()))

    return by_player, sorted(unresolved)
