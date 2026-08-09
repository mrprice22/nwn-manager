"""BAB, iterative-attack and ability-modifier maths.

Approximates at build time what NWN computes at runtime: the base attack
bonus a creature's class progression yields (clamped to the server level cap
and to pre-epic levels), the iterative attack schedule that follows from it,
the flat to-hit bonus its feats contribute, and the standard ability-score
modifier.

Depends only on stdlib, gff and lookups -- nothing here may touch ``Db``,
``E()`` or the renderers.
"""

from __future__ import annotations

import re
from typing import Iterable

from nwn_wiki.gff import fld
from nwn_wiki.lookups import (
    BASEITEM_COLUMNS_SEEN,
    CLASS_BAB,
    STOCK_FEAT_NAMES,
    WEAPONS,
)
from nwn_wiki.util import _try_int


# ---------------------------------------------------------------------------
# Combat-stat helpers (creature page)
# ---------------------------------------------------------------------------
#
# NWN computes BAB / iterative attacks / AC at runtime from class progression
# and equipment. The wiki is static, so we approximate from the UTC + items.
# Where information is missing (custom HAKs, scripted bonuses) we surface the
# raw fields so the reader can spot it.

def _class_bab(class_id: int, level: int) -> int:
    """Base attack bonus a class contributes at the given level, looked up from
    the stock per-class progression (class_bab.json / cls_atk_*.2da).

    Only *pre-epic* levels (≤20) add BAB: epic levels grant no base attack bonus
    in NWN, so multiclass epic creatures don't keep gaining to-hit per level.
    Unknown classes (no cached table) fall back to a 3/4 progression."""
    if level <= 0:
        return 0
    eff = min(level, 20)
    arr = CLASS_BAB.get(class_id)
    if arr:
        return arr[min(eff, len(arr)) - 1]
    return (eff * 3) // 4


def creature_bab(classes: list[dict], max_level: int = 0) -> int:
    """Sum base attack bonus across a creature's class entries.

    When `max_level` > 0 (the server level cap), the creature's total class
    levels are clamped to it, consuming ClassList in order — so a boss whose
    blueprint stacks far more HD than the cap doesn't accrue unbounded BAB
    (e.g. a Fighter60/WeaponMaster10/ArcaneArcher60 on a level-40 server is
    treated as Fighter40, giving BAB 20). `_class_bab` already counts only
    pre-epic (≤20) levels per class."""
    total = 0
    remaining = max_level if max_level and max_level > 0 else None
    for cl in classes:
        cid = fld(cl, "Class")
        lvl = fld(cl, "ClassLevel", 0) or 0
        if cid is None:
            continue
        lvl = int(lvl)
        if remaining is not None:
            if remaining <= 0:
                break
            lvl = min(lvl, remaining)
            remaining -= lvl
        total += _class_bab(int(cid), lvl)
    return total


def creature_class_bab(classes: list[dict], target_cid: int,
                       max_level: int = 0) -> int:
    """Pre-epic BAB contributed by one class, using the same ClassList-order
    server-cap clamp as creature_bab (so epic/over-cap levels add nothing)."""
    remaining = max_level if max_level and max_level > 0 else None
    total = 0
    for cl in classes:
        cid = fld(cl, "Class")
        lvl = fld(cl, "ClassLevel", 0) or 0
        if cid is None:
            continue
        lvl = int(lvl)
        if remaining is not None:
            if remaining <= 0:
                break
            lvl = min(lvl, remaining)
            remaining -= lvl
        if int(cid) == target_cid:
            total += _class_bab(int(cid), lvl)
    return total


def attack_schedule(bab: int, ability_mod: int = 0,
                    bonus: int = 0, *, monk_unarmed_bab: int = 0) -> list[int]:
    """Iterative attack bonuses. NWN caps at 4 attacks/round from BAB; the extra
    attack from haste / dual-wield is intentionally not modelled (it isn't shown
    on the in-game character sheet either).

    A monk attacking unarmed (or with creature/claw weapons) instead uses the
    monk progression: attacks spaced 3 apart (not 5), with the count derived from
    the monk's pre-epic (cap-clamped) unarmed BAB (monkBAB // 3) — so a level-20+
    monk (BAB 15) gets five unarmed attacks and epic levels add no further
    attacks. `monk_unarmed_bab` > 0 selects this mode."""
    if monk_unarmed_bab > 0:
        n = max(1, monk_unarmed_bab // 3)
        return [max(bab, 0) - 3 * i + ability_mod + bonus for i in range(n)]
    if bab <= 0:
        return [ability_mod + bonus]
    n = min(4, 1 + (bab - 1) // 5)
    return [bab - 5 * i + ability_mod + bonus for i in range(n)]


# Feats granting a flat to-hit bonus regardless of weapon (feat id → bonus).
# Epic Prowess applies to all attacks; Superior Weapon Focus is the Weapon
# Master's +1 with its chosen weapon (treated as applying to the wielded weapon).
# Epic Superior Weapon Focus (1071) is intentionally excluded — the engine does
# not grant a second flat +1 for it on top of Superior Weapon Focus.
_UNIVERSAL_ATTACK_FEATS = {584: 1, 884: 1}


def feat_attack_bonus(feat_ids: Iterable[int], weapon_name: str = "") -> int:
    """To-hit bonus a creature's feats add for the weapon it's wielding.

    Universal feats (Epic Prowess, Superior / Epic Superior Weapon Focus) always
    count. (Epic) Weapon Focus is weapon-specific: only counted when the feat's
    friendly name names the wielded weapon (e.g. 'Epic Weapon Focus (rapier)'
    with `weapon_name='Rapier'`) — +2 for the epic form, +1 otherwise. Weapon
    Specialization and crit feats add no to-hit and are ignored."""
    feat_ids = list(feat_ids)
    bonus = sum(_UNIVERSAL_ATTACK_FEATS[f] for f in feat_ids
                if f in _UNIVERSAL_ATTACK_FEATS)
    # Normalise to alphanumerics so this matches whether FEATS holds the friendly
    # name ("Epic Weapon Focus (rapier)") or a 2DA-override LABEL
    # ("FEAT_EPIC_WEAPON_FOCUS_RAPIER") — both reduce to "epicweaponfocusrapier".
    _norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    wnorm = _norm(weapon_name)
    if wnorm:
        for fid in feat_ids:
            fn = _norm(STOCK_FEAT_NAMES.get(fid, ""))
            if "weaponfocus" in fn and "superior" not in fn and wnorm in fn:
                bonus += 2 if "epic" in fn else 1
    return bonus


def ability_mod(score: int | None) -> int:
    if score is None:
        return 0
    return (int(score) - 10) // 2


WEAPON_FINESSE_FEAT = 42   # feat.2da FEAT_WEAPON_FINESSE → light weapons use Dex
EPIC_TOUGHNESS_BASE = 754  # feat.2da Epic Toughness I; I..X = 754..763, +20 HP/tier


def epic_toughness_hp(feat_ids: Iterable[int]) -> int:
    """HP NWN:EE adds on spawn from Epic Toughness, on top of the blueprint's
    stored MaxHitPoints (the toolset's stored value excludes it). Modelled as
    20 × the highest tier owned (the tiers form a prereq ladder). Returns 0 if
    none are present."""
    tiers = [fid - EPIC_TOUGHNESS_BASE + 1 for fid in feat_ids
             if EPIC_TOUGHNESS_BASE <= fid <= EPIC_TOUGHNESS_BASE + 9]
    return 20 * max(tiers) if tiers else 0


# Stock light/finessable weapon base-item rows (Weapon Finesse uses Dex on
# these); creature weapons (claw/bite) are also finessable and handled by slot.
FINESSE_BASEITEMS = frozenset({22, 37, 38, 40, 42, 51, 60, 111})
# Dagger, Light Hammer, Handaxe, Kama, Kukri, Rapier, Sickle, Whip.


# ---------------------------------------------------------------------------
# Critical-hit feats.
#
# The engine resolves these per base item: baseitems.2da names the exact feat id
# for this weapon's Weapon Focus, Improved Critical, Overwhelming Critical and
# Devastating Critical. A blank column means the feat does not exist for that
# weapon and cannot be taken — which is precisely how nwn_homers_lotr disables
# Devastating Critical server-wide (bin/gen-devcrit-map.py blanks the column, so
# the engine's own check can never succeed and no save is ever rolled).
#
# Reading the columns rather than hardcoding a rule keeps this module-agnostic:
# a stock module keeps its save-or-die, this one does not, and neither has to be
# told which it is.
# ---------------------------------------------------------------------------

# Overwhelming Critical adds damage scaling with the weapon's crit multiplier:
# +1d6 for a x2 weapon, +2d6 for x3, +3d6 for x4 — i.e. (mult - 1) six-sided
# dice. Isolated here so it can be corrected in one place if a module's own
# rules differ.
_OVERWHELMING_DIE = 6

# Devastating Critical's replacement damage, when a module has disabled the
# engine's save-or-die and re-implemented it as bonus dice (--devcrit-bonus-dice).
# Die size follows baseitems.2da WeaponSize, matching this module's
# unpacked/devcrit_inc.nss: 1-2 small, 3 medium, 4+ large.
_DEVCRIT_DIE_BY_SIZE = {1: 6, 2: 6, 3: 8}
_DEVCRIT_DIE_LARGE = 10

# Feat prerequisites the simulation actually enforces. Improved Critical needs
# BAB +8; the epic critical chain needs Strength 25, which is what stops a
# Dexterity/finesse build from taking it.
_IMPROVED_CRIT_MIN_BAB = 8
_EPIC_CRIT_MIN_STR = 25


def _devcrit_die(weapon_size: int) -> int:
    """Die size for a replacement devastating-critical bonus die."""
    return _DEVCRIT_DIE_BY_SIZE.get(weapon_size, _DEVCRIT_DIE_LARGE)


def weapon_feat_id(bi: int, column: str) -> int:
    """Feat id this base item names in `column`, or 0 when it names none.

    Zero is the answer that matters: it means the engine has no feat to check,
    so nobody wielding this weapon can have it.
    """
    return _try_int((WEAPONS.get(bi) or {}).get(column), 0)


def crit_feat_effects(bi: int, *, bab: int, str_score: int,
                      has_feat: "Callable[[int], bool] | None" = None,
                      devcrit_bonus_dice: int = 0) -> dict:
    """Resolve the critical-hit feats for one weapon in one wielder's hands.

    `has_feat(feat_id) -> bool` decides whether the wielder actually has a given
    feat; pass None for the reference PC, which is assumed specced into whatever
    it holds (subject to the real prerequisites below). Creatures pass a lookup
    over their own FeatList so they only get what they were built with.

    Returns {threat_mult, crit_bonus, devcrit_save_dc, feats} where `feats` names
    what was granted, for the report.
    """
    stats = WEAPONS.get(bi) or {}
    size = _try_int(stats.get("WeaponSize"), 0)
    crit_mult = _try_int(stats.get("CritHitMult"), 2) or 2

    def _granted(column: str, *, prereq: bool) -> bool:
        feat_id = weapon_feat_id(bi, column)
        if not feat_id or not prereq:
            return False
        return has_feat(feat_id) if has_feat is not None else True

    names: list[str] = []
    threat_mult = 1
    crit_bonus = 0.0
    devcrit_dc = 0

    if _granted("WeaponImprovedCriticalFeat", prereq=bab >= _IMPROVED_CRIT_MIN_BAB):
        threat_mult = 2          # NWN doubles the range: 20 -> 19-20, 19-20 -> 17-20
        names.append("Improved Critical")

    epic_ok = str_score >= _EPIC_CRIT_MIN_STR and threat_mult > 1
    overwhelming = _granted("EpicWeaponOverwhelmingCriticalFeat", prereq=epic_ok)
    if overwhelming:
        crit_bonus += max(1, crit_mult - 1) * (_OVERWHELMING_DIE + 1) / 2.0
        names.append("Overwhelming Critical")

    # Devastating Critical requires Overwhelming. Which of its two forms applies
    # is decided by the 2DA, not by configuration: a named feat means the engine
    # still rolls its save-or-die, a blank column means the module disabled it
    # and any replacement damage comes from --devcrit-bonus-dice.
    if overwhelming:
        _col = "EpicWeaponDevastatingCriticalFeat"
        # A blank column means the module disabled the mechanic; a column we
        # never loaded means we have no evidence, so assume the engine default.
        if weapon_feat_id(bi, _col) or _col not in BASEITEM_COLUMNS_SEEN:
            # Stock: Fort save or die. DC 10 + half character level + Str mod;
            # the caller supplies the level via `bab` for creatures whose level
            # we do not track separately.
            devcrit_dc = 10 + bab // 2 + ability_mod(str_score)
            names.append("Devastating Critical (save-or-die)")
        elif devcrit_bonus_dice > 0:
            die = _devcrit_die(size)
            crit_bonus += devcrit_bonus_dice * (die + 1) / 2.0
            names.append(f"Devastating Critical ({devcrit_bonus_dice}d{die})")

    return {"threat_mult": threat_mult, "crit_bonus": crit_bonus,
            "devcrit_save_dc": devcrit_dc, "feats": names}
