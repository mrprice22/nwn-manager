"""How a creature comes back after it is killed.

Respawn is a property of the *location*, not of the creature: the same
blueprint can be a never-respawning placement in one area and a 60-second
encounter spawn in the next.  Two unrelated mechanisms decide it.

Encounters use the engine's own machinery — the ``.git`` Encounter List struct
carries ``Reset`` / ``ResetTime`` / ``Respawns``, and ``ResetTime`` is in
seconds.  Nothing in this module is needed for those; see
``DbIndexMixin._index_encounter_spawns``.

Placed creatures use "Sir Elric's Simple Creature Respawns" — the module's
``se_respawn_inc.nss``.  Whether a placement respawns at all depends on where
its OnDeath script ends up:

  ``standard``  the script reaches ``SE_DoCreatureRespawn()``, which schedules
                a flat ``fDelay`` (900s module-wide) ``DelayCommand`` on the
                module.  The timers live in the running module, so a restart
                cancels them — but a restart also revives everything anyway.
  ``legacy``    the script re-creates a creature by hand (``StaticSpawn`` /
                ``CreateObject``) on a per-waypoint timer this code can't
                predict.  Only a handful of placements still do this.
  ``none``      no respawn path at all; the creature stays dead until the
                server restarts.  This is the case for every placement whose
                OnDeath is one of the XP-reward scripts, which is a much larger
                share of the module than players expect.

Two rules short-circuit the classification, both mirroring the include itself:
a tag containing ``NSP`` never respawns (``se_respawn_inc.nss`` early return),
and encounter-spawned creatures are skipped by ``GetIsEncounterCreature`` — so
a creature that spawned from an encounter never takes the SE path even if its
OnDeath would otherwise qualify.

Ported from ``bin/gen-boss-registry.py`` in the module repo, which uses the
same classification to decide what the in-game "Roll of the Fallen" board can
promise.  Keep the two in step.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nwn_wiki.db.scripts import _strip_nss_comments
from nwn_wiki.gff import fld
from nwn_wiki.util import _try_int

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nwn_wiki.cli import Db

_RESPAWN_CALL = re.compile(r"\bSE_DoCreatureRespawn\s*\(")
_EXEC_SCRIPT = re.compile(r'ExecuteScript\s*\(\s*"([^"]+)"')
_CREATE_CRE = re.compile(r"CreateObject\s*\(\s*OBJECT_TYPE_CREATURE")
_STATIC_SPAWN = re.compile(r"\bStaticSpawn\s*\(")

# float fDelay = 900.0f;  — how the SE include named its one knob before the
# module split boss and non-boss timers.  Read rather than hard-coded so the
# wiki keeps telling the truth if a builder retunes it.  The include ships with
# a commented-out random-timer example right below the live line, hence
# matching only after comments are stripped.  Modules that have moved the knob
# into boss_tune.nss (below) no longer have this literal at all.
_SE_DELAY = re.compile(r"\bfloat\s+fDelay\s*=\s*([0-9]*\.?[0-9]+)\s*f?\s*;")

# boss_tune.nss — the module's single tuning include for boss timing:
#   BOSS_RESPAWN_SECONDS  every Roll-of-the-Fallen boss, placed or encounter
#   BOSS_RESET_SECONDS    out-of-combat enrage reset (enr_inc.nss)
#   CRE_RESPAWN_SECONDS   every other placed creature (the old flat fDelay)
# Absent in older modules and in the frozen archive forks; every reader here
# degrades to the fDelay literal, then to None.
_TUNE_CONST = re.compile(r"\bconst\s+int\s+(\w+)\s*=\s*(\d+)\s*;")

# brd_db.nss seeds the Roll-of-the-Fallen registry; the first capture of each
# of these is the resref, which is all this module needs (render/creatures.py
# parses the full rows for the Bosses page).
_SEED_RESREF = re.compile(r'BRD_SeedBoss\(\s*"([^"]*)"')
_ALIAS_RESREF = re.compile(r'BRD_SeedAlias\(\s*"([^"]*)"')

SE_INCLUDE = "se_respawn_inc"
TUNE_INCLUDE = "boss_tune"
BOSS_REGISTRY_INCLUDE = "brd_db"

# Respawn kinds, and how each is worded in the wiki.
STANDARD = "standard"
LEGACY = "legacy"
NONE = "none"


def module_has_se_respawn(db: "Db") -> bool:
    """Does this module use the SE placed-creature respawn system at all?

    False for the frozen 2008/2009 archive modules, which predate it.  Callers
    use this to drop the placed-side Respawn column entirely rather than
    printing "Never" against every placement in a module that simply never had
    the mechanism.
    """
    return SE_INCLUDE in db.script_paths


def boss_tuning(db: "Db") -> dict[str, int]:
    """{constant: seconds} parsed from the module's boss_tune.nss ({} if absent)."""
    cached = getattr(db, "_boss_tune_cache", None)
    if cached is not None:
        return cached
    tune: dict[str, int] = {}
    p = db.script_paths.get(TUNE_INCLUDE)
    if p and p.is_file():
        src = _strip_nss_comments(p.read_text(errors="replace"))
        tune = {m.group(1): int(m.group(2)) for m in _TUNE_CONST.finditer(src)}
    db._boss_tune_cache = tune
    return tune


def boss_respawn_seconds(db: "Db") -> int | None:
    """How long every tracked boss stays dead, or None if the module has no
    boss_tune.nss (older modules gave each boss its own timer)."""
    return boss_tuning(db).get("BOSS_RESPAWN_SECONDS")


def boss_reset_seconds(db: "Db") -> int | None:
    """How long a damaged, abandoned boss must stay out of combat before the
    enrage system fully restores it.  None if the module has no boss_tune.nss."""
    return boss_tuning(db).get("BOSS_RESET_SECONDS")


def se_delay_seconds(db: "Db") -> int | None:
    """The flat SE respawn delay for an ordinary creature, or None if the
    module lacks it.

    Two shapes, both read rather than assumed: the historical
    ``float fDelay = 900.0f;`` literal inside se_respawn_inc.nss, and — once a
    module has split boss timers out — ``CRE_RESPAWN_SECONDS`` in boss_tune.nss.
    """
    cached = getattr(db, "_se_delay_cache", ...)
    if cached is not ...:
        return cached
    delay: int | None = None
    p = db.script_paths.get(SE_INCLUDE)
    if p and p.is_file():
        m = _SE_DELAY.search(_strip_nss_comments(p.read_text(errors="replace")))
        if m:
            delay = int(float(m.group(1)))
    if delay is None:
        delay = boss_tuning(db).get("CRE_RESPAWN_SECONDS")
    db._se_delay_cache = delay
    return delay


def _boss_resrefs(db: "Db") -> frozenset[str]:
    """Every resref the Roll-of-the-Fallen registry tracks, canonical rows and
    variant aliases alike, read straight from brd_db.nss.

    Deliberately NOT read from ``state._BOSS_REGISTRY``: placement respawn is
    computed during ``db.load()`` indexing, which happens before the renderer
    loads that state.  Same seed rows, own cache.
    """
    cached = getattr(db, "_boss_resref_cache", None)
    if cached is not None:
        return cached
    refs: set[str] = set()
    p = db.script_paths.get(BOSS_REGISTRY_INCLUDE)
    if p and p.is_file():
        src = _strip_nss_comments(p.read_text(errors="replace"))
        refs = {m.group(1).lower() for m in _SEED_RESREF.finditer(src)}
        refs |= {m.group(1).lower() for m in _ALIAS_RESREF.finditer(src)}
    db._boss_resref_cache = frozenset(refs)
    return db._boss_resref_cache


def _is_registry_boss(db: "Db", resref: str | None) -> bool:
    """Is this blueprint one of the Roll-of-the-Fallen bosses? (Bosses respawn
    on their own timer, so a placement of one must not be labelled with the
    ordinary-creature delay.)"""
    return bool(resref) and resref.lower() in _boss_resrefs(db)


def death_respawn_kind(db: "Db", resref: str, _seen: tuple = ()) -> str:
    """Classify OnDeath script ``resref`` as STANDARD / LEGACY / NONE.

    Follows ``ExecuteScript`` chains — the module's own OnDeath scripts are
    mostly thin wrappers that delegate to ``nw_c2_default7`` — and guards
    against cycles via ``_seen``.
    """
    if not resref or resref in _seen:
        return NONE
    cache = db.__dict__.setdefault("_respawn_kind_cache", {})
    if resref in cache:
        return cache[resref]
    # Provisional NONE guards against a script that recurses back into itself
    # through a chain rather than directly.
    cache[resref] = NONE

    p = db.script_paths.get(resref)
    if not p or not p.is_file():
        return NONE
    src = _strip_nss_comments(p.read_text(errors="replace"))
    if _RESPAWN_CALL.search(src):
        cache[resref] = STANDARD
        return STANDARD

    kind = LEGACY if (_CREATE_CRE.search(src) or _STATIC_SPAWN.search(src)) else NONE
    seen = _seen + (resref,)
    for m in _EXEC_SCRIPT.finditer(src):
        k = death_respawn_kind(db, m.group(1).lower(), seen)
        if k == STANDARD:
            kind = STANDARD
            break
        if k == LEGACY:
            kind = LEGACY
    cache[resref] = kind
    return kind


def placed_respawn(db: "Db", inst_c: dict, bp: dict | None,
                   resref: str | None = None) -> tuple[str, int | None]:
    """(kind, seconds) for one placed creature instance.

    ``seconds`` is only meaningful for STANDARD; LEGACY timers are per-waypoint
    and NONE has none.  The instance's own ScriptDeath wins over the
    blueprint's — GIT instances do override it.

    ``resref`` is the creature's blueprint resref where the caller knows it:
    a Roll-of-the-Fallen boss respawns on BOSS_RESPAWN_SECONDS rather than the
    ordinary-creature delay, and the same SE_DoCreatureRespawn call decides
    which at runtime.
    """
    if not module_has_se_respawn(db):
        return NONE, None

    tag = fld(inst_c, "Tag", "") or (fld(bp, "Tag", "") if bp else "") or ""
    # Case-sensitive on purpose: the include tests
    # FindSubString(GetTag(OBJECT_SELF), "NSP"), and NWScript's FindSubString
    # is case-sensitive. Lower-case "nsp" occurring inside an ordinary tag —
    # "Eowynspersonlgaurd" (Eowyn's personal guard) is the real example — does
    # NOT stop those creatures respawning in game, so it must not here either.
    if "NSP" in tag:
        return NONE, None

    script = fld(inst_c, "ScriptDeath", "") or ""
    if not script and bp:
        script = fld(bp, "ScriptDeath", "") or ""
    kind = death_respawn_kind(db, script.lower())
    if kind != STANDARD:
        return kind, None
    if _is_registry_boss(db, resref):
        return kind, (boss_respawn_seconds(db) or se_delay_seconds(db))
    return kind, se_delay_seconds(db)


def encounter_respawn(e: dict, blueprint: dict) -> int | None:
    """Respawn period in seconds for an encounter placement, or None if it
    never re-arms.

    The ``.git`` instance is authoritative and the ``.ute`` blueprint is only
    the fallback — instance ResetTimes exist that no blueprint has, and some
    instances name a blueprint that isn't in the module at all.  Same
    precedence rule ``_index_encounter_spawns`` already uses for CreatureList.
    """
    def pick(key, default=None):
        v = fld(e, key, None)
        if v is None or v == "":
            v = fld(blueprint, key, default)
        return v

    if _try_int(pick("Reset", 0)) != 1:
        return None
    if _try_int(pick("Respawns", 0)) == 0:
        # 0 = exhausted for good; -1 = infinite; >0 = a finite number of
        # respawns, which still means "it comes back" for our purposes.
        return None
    secs = _try_int(pick("ResetTime", 0))
    return secs or None


def format_respawn(seconds: int | None) -> str:
    """Humanise a respawn period: 900 -> '15 min', 200 -> '3 min 20 s'."""
    if not seconds:
        return "—"
    if seconds < 60:
        return f"{seconds} s"
    mins, secs = divmod(seconds, 60)
    if mins >= 60 and not secs:
        hours, rem = divmod(mins, 60)
        return f"{hours} h" if not rem else f"{hours} h {rem} min"
    return f"{mins} min" if not secs else f"{mins} min {secs} s"


def placed_respawn_label(kind: str, seconds: int | None) -> str:
    """Wiki wording for a placed row's Respawn cell."""
    if kind == STANDARD:
        # No flat delay to report: the 2009-era include takes the period as a
        # per-call nMinutes argument (plus a random spread), so the creature
        # does come back but not on a period this analysis can name.
        return format_respawn(seconds) if seconds else "Varies (per-spawn timer)"
    if kind == LEGACY:
        return "Varies (legacy spawner)"
    return "Never (restart only)"


def encounter_respawn_label(seconds: int | None) -> str:
    """Wiki wording for an encounter row's Respawn cell."""
    return format_respawn(seconds) if seconds else "Never (restart only)"
