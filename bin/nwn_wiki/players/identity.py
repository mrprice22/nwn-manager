"""The identity bridge — three ID spaces, one player roster.

The season's data does not agree with itself about what a "player" is:

1. **CD key** (``AB12CD34``) — the vault directory name, and the key in
   ``bestiarydb.kills``, ``meritdb``, ``admindb.houses``, ``fam_*_<CDKEY>`` bank
   entries and ``activity-sessions.json``. This is the real account identity, and
   the one everything is normalised onto.
2. **playerid string** — how the NWNX ``db`` key/value tables (``bankdb``,
   ``meaningwave``) key their rows: ``GetPCPlayerName() + GetName()``, *truncated*
   by the engine (observed max 29 chars), e.g. ``-Methonash-Methonash, -Unho``.
   There is no separator, so it can only be resolved by reconstructing the
   candidate from a known account + character pair and matching by prefix.
3. **roadmap.yaml ``player:``** — a hand-typed display string
   (``Piskan (Alek Cain)``). Resolved through the ROADMAP_ALIASES table installed by configure(),
   never guessed.

Nothing here silently drops a row it cannot place: unmatched playerids are
collected and reported, because a missing bridge shows up as a player mysteriously
having no banked gold rather than as an error.
"""

from __future__ import annotations

from collections import Counter, defaultdict

# --------------------------------------------------------------------------- #
# Curated, per-season tables. In the Hall of Fame these lived in a hand-tuned
# categories.py inside the season repo; here the package is shared by every
# realm's build, so they start EMPTY and are supplied by the project being built
# (see configure()). Empty is the correct default: nothing is merged and no
# roadmap name resolves unless a human said so, which is the same "report, never
# guess" rule the original table documented.
# --------------------------------------------------------------------------- #

ACCOUNT_MERGES: dict[str, str] = {}   # alt CD key -> the key to fold it onto
ROADMAP_ALIASES: dict[str, str] = {}  # roadmap.yaml `player:` -> account name


def configure(account_merges: dict | None = None,
              roadmap_aliases: dict | None = None) -> None:
    """Install one project's curated identity tables.

    Called once per build, before build_roster(). Replaces rather than merges,
    so a rebuild in the same process can never inherit another realm's table.
    """
    global ACCOUNT_MERGES, ROADMAP_ALIASES
    ACCOUNT_MERGES = dict(account_merges or {})
    ROADMAP_ALIASES = dict(roadmap_aliases or {})

# The engine truncates the concatenated playerid. We never rely on the exact
# limit — matching is done by prefix — but a candidate shorter than the stored key
# can never be the right one, and this bound keeps the search honest.
MAX_PLAYERID = 32


class Roster:
    """Every account that appears anywhere in the season's data, keyed by CD key."""

    def __init__(self):
        self.name: dict[str, str] = {}          # cdkey -> account display name
        self.chars: dict[str, set[str]] = {}    # cdkey -> character names
        self.uuid_owner: dict[str, str] = {}    # character UUID -> cdkey
        self.unmatched: list[str] = []          # playerid strings we could not place
        # account name -> cdkey, for the keyless sessions in the old cache format.
        self.session_key_for_name: dict[str, str] = {}
        # Every account name a CD key ever logged in under. One key legitimately
        # has several ("Xil" also played as "Zam"), and the *rejected* names still
        # appear as the prefix of that account's playerid rows -- so the display
        # name alone is not enough to resolve them.
        self.names_seen: dict[str, set[str]] = defaultdict(set)

    def key_for_session(self, s: dict) -> str | None:
        """CD key for one session row, backfilling by name when the row has none."""
        ck = s.get("cdkey")
        if ck:
            return ACCOUNT_MERGES.get(ck, ck)
        return self.session_key_for_name.get(s.get("player") or "")

    # -- construction ------------------------------------------------------ #

    def note_account(self, cdkey: str, name: str | None) -> None:
        if not cdkey:
            return
        self.chars.setdefault(cdkey, set())
        if name and (cdkey not in self.name or not self.name[cdkey]):
            self.name[cdkey] = name

    def note_char(self, cdkey: str, char_name: str | None, uuid: str | None = None) -> None:
        if not cdkey:
            return
        self.chars.setdefault(cdkey, set())
        if char_name:
            self.chars[cdkey].add(char_name)
        if uuid:
            self.uuid_owner[uuid] = cdkey

    # -- lookup ------------------------------------------------------------ #

    def account(self, cdkey: str) -> str:
        """Display name for a CD key. Falls back to the key itself only when the
        account never appears in a session log — which would be a player who never
        logged in, so it should never happen in practice."""
        return self.name.get(cdkey) or cdkey

    def all_cdkeys(self) -> list[str]:
        return sorted(self.chars)

    def owner_of_uuid(self, uuid: str) -> str | None:
        return self.uuid_owner.get(uuid)

    # -- the playerid bridge ----------------------------------------------- #

    def build_playerid_map(self) -> dict[str, str]:
        """Map every reconstructible ``playerid`` string to its CD key.

        Candidates are ``account + character`` for every known pair. The stored key
        may be a truncation of the candidate, so the map is keyed by *every prefix*
        that is long enough to be unambiguous — cheaper and more reliable than
        guessing the engine's exact cut-off. Where two accounts would produce the
        same prefix the entry is dropped rather than assigned to either.
        """
        pid: dict[str, str] = {}
        clashes: set[str] = set()
        for cdkey, chars in self.chars.items():
            acct = self.name.get(cdkey)
            if not acct:
                continue
            for char in chars:
                candidate = f"{acct}{char}"
                for cut in range(len(candidate), 0, -1):
                    if cut > MAX_PLAYERID:
                        continue
                    prefix = candidate[:cut]
                    if prefix in pid and pid[prefix] != cdkey:
                        clashes.add(prefix)
                    else:
                        pid[prefix] = cdkey
        for c in clashes:
            pid.pop(c, None)
        return pid

    def resolve_playerid(self, playerid: str, pid_map: dict[str, str]) -> str | None:
        """CD key for a ``db`` table playerid, or None (recorded as unmatched).

        The exact-candidate map only knows characters that still exist. A player who
        deleted a character keeps its banked gold, so a second pass matches on the
        *account name* alone: the playerid always begins with it. The longest
        matching account wins, so an account whose name is a prefix of another
        ("ray" vs "ray2") cannot steal the other's rows.
        """
        hit = pid_map.get(playerid)
        if hit is not None:
            return hit

        best_key, best_len = None, 0
        for cdkey, names in self.names_seen.items():
            for name in names:
                if name and len(name) > best_len and playerid.startswith(name):
                    best_key, best_len = cdkey, len(name)
        if best_key is not None:
            return best_key

        self.unmatched.append(playerid)
        return None

    # -- roadmap names ------------------------------------------------------ #

    @staticmethod
    def resolve_roadmap_player(label: str) -> str | None:
        """roadmap.yaml ``player:`` -> account name, via the curated alias table.

        Returns None for a name with no entry; the caller reports those so an
        unmapped suggester is loud rather than quietly uncredited.
        """
        return ROADMAP_ALIASES.get(label)


def canon_cdkey(cdkey: str) -> str:
    """Fold an alt CD key onto its primary, per the curated ACCOUNT_MERGES table."""
    return ACCOUNT_MERGES.get(cdkey, cdkey)


def build_roster(sessions: list[dict], kills: list[dict], chars: list[dict]) -> Roster:
    """Assemble the roster from all three places accounts are recorded.

    Two wrinkles the raw data forces on us:

    * **One CD key can log in under several account names** (one key on this
      server appears as both "Xil" and "Zam"). The canonical name is whichever
      the account used for the most sessions, not whichever was seen first.
    * **Older sessions have no CD key at all** — the pre-v2 activity cache stored
      only the account name. Those are backfilled by name once the named sessions
      have established a name -> key mapping, and dropped if the name is ambiguous.
    """
    r = Roster()

    # Pass 1: name frequency per CD key, from sessions that carry one.
    freq: dict[str, Counter] = defaultdict(Counter)
    name_to_keys: dict[str, set[str]] = defaultdict(set)
    for s in sessions:
        ck, nm = canon_cdkey(s.get("cdkey") or ""), s.get("player")
        if ck and nm:
            freq[ck][nm] += 1
            name_to_keys[nm].add(ck)

    for ck, counter in freq.items():
        r.note_account(ck, counter.most_common(1)[0][0])
        r.names_seen[ck].update(counter)

    # Pass 2: backfill keyless sessions by name, but only where unambiguous.
    r.session_key_for_name = {
        nm: next(iter(keys)) for nm, keys in name_to_keys.items() if len(keys) == 1
    }

    for k in kills:
        ck = canon_cdkey(k["cdkey"])
        r.note_account(ck, None)
        r.note_char(ck, k["char_name"], k["uuid"])
    for c in chars:
        ck = canon_cdkey(c["cdkey"])
        r.note_account(ck, None)
        r.note_char(ck, c["name"], c.get("uuid"))
    return r
