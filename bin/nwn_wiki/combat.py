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
from nwn_wiki.lookups import CLASS_BAB, STOCK_FEAT_NAMES


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
