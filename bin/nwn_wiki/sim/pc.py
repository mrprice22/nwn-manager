"""The reference PC of the counter-gear report, and its feat budget.

The two feat pools a pure single-class Fighter build draws on, the kit
flattening helper the solver shares with :func:`reference_pc`, and
:func:`reference_pc` itself: the attack/defence profiles a level-N Fighter
wearing a given kit fields.

No wiki state, no renderers.  ``reference_pc`` takes a duck-typed ``db``
(``Db`` lives in :mod:`nwn_wiki.cli`) so this module stays importable
without it.
"""

from __future__ import annotations

from collections import defaultdict

from nwn_wiki.combat import (
    EPIC_TOUGHNESS_BASE,
    FINESSE_BASEITEMS,
    _class_bab,
    ability_mod,
    attack_schedule,
    crit_feat_effects,
    epic_toughness_hp,
    weapon_feat_id,
)
from nwn_wiki.items import weapon_damage_props
from nwn_wiki.lookups import WEAPONS
from nwn_wiki.sim.combat import attack_profile, defense_profile
from nwn_wiki.util import _try_int


# ---- feat budget ----------------------------------------------------------
#
# A pure single-class build draws on TWO pools, and they are not interchangeable:
# a class bonus feat may only be spent from that class's own list. Great
# Constitution is not on the Fighter list, so it cannot be bought with one; Epic
# Toughness is, and is where spare Fighter feats go.
#
#   Pool A, Fighter bonus feats — level 1, then every even level, continuing
#       every 2 levels into epic. Spent on the weapon chain (Focus,
#       Specialization, Improved Critical, then the epic critical feats), Epic
#       Prowess, and finally Epic Toughness.
#   Pool B, general feats — level 1, then every 3 levels. Spent on Great
#       Strength (Great Dexterity behind a finesse or ranged weapon), then Great
#       Constitution.
#
# Legendary feats are deliberately NOT modelled: they are still in development
# in nwn_homers_lotr, so simulating them would report power no character has.
# Do not add them here until that lands.
_GREAT_ABILITY_TIERS = 10       # Great <Ability> I..X
_EPIC_TOUGHNESS_TIERS = 10      # Epic Toughness I..X, +20 HP each
_FIRST_EPIC_LEVEL = 21


def _general_feat_slots(level: int) -> int:
    """General feats: one at level 1, then one every 3 levels."""
    return 1 + level // 3


def _fighter_bonus_slots(level: int) -> int:
    """Fighter bonus feats: level 1 and every even level, continuing every 2
    levels past 20."""
    return 1 + level // 2


def _great_ability_tiers(level: int, *, spent: int = 0) -> int:
    """Tiers of Great <Ability> pool B can afford at `level`.

    Only epic levels can buy them, so the pre-epic general feats are set aside
    as already spent on the ordinary prerequisites (Weapon Finesse, Toughness
    and friends) rather than counted here.
    """
    if level < _FIRST_EPIC_LEVEL:
        return 0
    epic_general = _general_feat_slots(level) - _general_feat_slots(_FIRST_EPIC_LEVEL - 1)
    return max(0, min(_GREAT_ABILITY_TIERS, epic_general - spent))


def _epic_toughness_tiers(level: int, *, spent: int) -> int:
    """Epic Toughness tiers left over in pool A once the weapon chain, the epic
    critical feats and Epic Prowess have been paid for."""
    if level < _FIRST_EPIC_LEVEL:
        return 0
    epic_bonus = _fighter_bonus_slots(level) - _fighter_bonus_slots(_FIRST_EPIC_LEVEL - 1)
    return max(0, min(_EPIC_TOUGHNESS_TIERS, epic_bonus - spent))


def _kit_pieces(kit: dict) -> list[dict]:
    """Flatten a kit (slot key → piece or list of pieces) to a piece list."""
    out: list[dict] = []
    for v in kit.values():
        if not v:
            continue
        out.extend(v if isinstance(v, list) else [v])
    return out


# ---------------------------------------------------------------------------
# The reference PC.
#
# The counter-gear report is build-agnostic in the sense that it models no
# *particular* character, but a fight needs two sides, so it models a canonical
# one: a single-class fighter at the level the tier implies, with all ability
# points in Strength and assumed specced into whatever weapon is being tested
# (so weapons stay comparable with each other).
#
# Two NWN facts keep this simple and are load-bearing:
#   * There is no epic attack bonus and no epic save bonus. BAB and base saves
#     stop advancing at class level 20 — which is why a level-60 character's
#     power comes almost entirely from gear, and why this report is worth
#     running at all.
#   * bin/serve sets always-roll-max-hitpoints-on-levelup, so HP is the maximum
#     roll per level rather than an average.
# ---------------------------------------------------------------------------

REFERENCE_PC_CLASS = 4      # classes.2da row 4 = Fighter (d10, full BAB)
_PC_HIT_DIE = 10
_PRE_EPIC_CAP = 20          # class level past which BAB and saves stop growing

# Fighter weapon/combat feats bought out of pool A before anything else. The
# epic critical feats are counted here too, but only claim a slot when the
# weapon and the build actually qualify for them (see crit_feat_effects).
_FIGHTER_CORE_FEATS = 4         # Weapon Focus, Specialization + their epic tiers
_EPIC_PROWESS_FEATS = 1

# The reference PC is assumed to have a free full heal off a 150-second
# cooldown, which is 25 six-second rounds. Surviving one cooldown therefore
# means surviving indefinitely.
_FULL_HEAL_ROUNDS = 150 // 6


def reference_pc(level: int, kit: dict, db: "Db") -> dict:
    """Build the reference PC at `level` wearing `kit`.

    Returns {"level", "attack", "defense", "cost", "weapon", ...} where "attack"
    is an attack_profile and "defense" a defense_profile, ready for simulate().

    Known simplifications, all in the conservative direction: armour maximum-Dex
    limits are not modelled (baseitems.2da's Dex cap is not in our caches), AC
    bonuses from different sources are summed rather than resolved by AC type,
    and no spells, potions or class abilities are used.
    """
    pieces = _kit_pieces(kit)
    lvl = max(1, int(level))
    pre_epic = min(lvl, _PRE_EPIC_CAP)
    bab = _class_bab(REFERENCE_PC_CLASS, pre_epic)

    # ----- weapon -----------------------------------------------------------
    # Resolved before ability scores, because a finesse weapon changes which
    # ability the whole build pumps.
    weapon = kit.get("right")
    if isinstance(weapon, list):
        weapon = weapon[0] if weapon else None
    have_weapon = bool(weapon is not None and weapon["off"] and weapon["off"]["is_weapon"])
    off = weapon["off"] if have_weapon else None
    bi = off["base_item_id"] if have_weapon else -1
    stats = (WEAPONS.get(bi) or {}) if have_weapon else {}
    ranged = bool(off["is_ranged"]) if have_weapon else False
    two_handed = have_weapon and _try_int(stats.get("WeaponSize"), 0) >= 4 and not ranged
    # Weapon Finesse on a light weapon puts Dexterity on the attack roll, so a
    # character wielding one builds Dex instead of Strength. Damage still comes
    # from Strength either way — NWN's finesse affects to-hit only.
    finesse = have_weapon and not ranged and bi in FINESSE_BASEITEMS
    primary = "Dex" if (finesse or ranged) else "Str"

    # ----- feats: two pools, spent in priority order -------------------------
    # Pool A (Fighter bonus feats) buys the weapon chain first. The epic
    # critical feats each claim a slot only when the weapon names them and the
    # build qualifies, which is resolved below once Strength is known — so
    # provisionally reserve them and correct after.
    great_tiers = _great_ability_tiers(lvl)

    # ----- ability scores ---------------------------------------------------
    # Base array, with every level-up point and every tier of Great <Ability>
    # from pool B spent on the attacking stat.
    scores = {"Str": 14, "Dex": 14, "Con": 16, "Wis": 12}
    scores[primary] = 18 + lvl // 4 + great_tiers

    # Ability bonuses from *different* items stack; the module's
    # --max-ability-bonus dial caps the resulting total per ability (NWN's
    # default is +12, this module raises it to +24). So two +12 belts-and-rings
    # reach the cap where one cannot — which is why this sums and then clamps
    # rather than taking the largest single item.
    item_abil: dict[str, int] = defaultdict(int)
    for p in pieces:
        for ab, val in p["def"]["abilities"].items():
            item_abil[ab] += val
    cap = db.max_ability_bonus
    for ab, val in item_abil.items():
        scores[ab] = scores.get(ab, 10) + (min(val, cap) if cap and cap > 0 else val)

    str_mod = ability_mod(scores["Str"])
    dex_mod = ability_mod(scores["Dex"])
    con_mod = ability_mod(scores["Con"])
    wis_mod = ability_mod(scores["Wis"])

    # Weapon Focus / Specialization only exist for a base item that names them,
    # so a weapon with a blank column correctly grants nothing here rather than
    # a blanket bonus. The PC is assumed specced into whatever it holds.
    def _has(column: str, min_level: int = 1) -> bool:
        return lvl >= min_level and bool(weapon_feat_id(bi, column))

    feat_hit = (1 if _has("WeaponFocusFeat") else 0) \
        + (2 if _has("EpicWeaponFocusFeat", _FIRST_EPIC_LEVEL) else 0) \
        + (1 if lvl >= _FIRST_EPIC_LEVEL else 0)          # Epic Prowess
    feat_dmg = (2 if _has("WeaponSpecializationFeat", 4) else 0) \
        + (4 if _has("EpicWeaponSpecializationFeat", _FIRST_EPIC_LEVEL) else 0)

    crit = crit_feat_effects(bi, bab=bab, str_score=scores["Str"],
                             devcrit_bonus_dice=db.devcrit_bonus_dice)

    if have_weapon:
        # Two-handed melee gets 1.5x Strength to damage; ranged gets none.
        str_dmg = 0 if ranged else int(str_mod * (1.5 if two_handed else 1.0))
        prop_flat, elem = weapon_damage_props(weapon["item"])
        # Finesse uses the better of the two, matching the engine.
        melee_ab = max(str_mod, dex_mod) if finesse else str_mod
        atk_mod = (dex_mod if ranged else melee_ab) + off["attack_bonus"] + feat_hit
        attack = attack_profile(
            attack_schedule(bab, ability_mod=atk_mod),
            num_dice=_try_int(stats.get("NumDice"), 0),
            die=_try_int(stats.get("DieToRoll"), 0),
            flat=str_dmg + feat_dmg + prop_flat,
            crit_threat=(_try_int(stats.get("CritThreat"), 1) or 1) * crit["threat_mult"],
            crit_mult=_try_int(stats.get("CritHitMult"), 2) or 2,
            phys_types=off["physical_dtypes"] or ["Bludgeoning"],
            elem=elem,
            enhancement=off["enhancement"],
            crit_bonus=crit["crit_bonus"],
            devcrit_save_dc=crit["devcrit_save_dc"],
        )
    else:
        attack = attack_profile(
            attack_schedule(bab, ability_mod=str_mod + feat_hit),
            num_dice=1, die=3, flat=str_mod + feat_dmg,
            crit_threat=1, crit_mult=2,
            phys_types=["Bludgeoning"], elem={}, enhancement=0)

    # Pool A's leftovers go to Epic Toughness. The epic critical feats only cost
    # a slot when they were actually granted.
    epic_crit_spent = sum(1 for f in crit["feats"] if f != "Improved Critical")
    toughness = _epic_toughness_tiers(
        lvl, spent=_FIGHTER_CORE_FEATS + _EPIC_PROWESS_FEATS + epic_crit_spent)

    # ----- defences ---------------------------------------------------------
    ac = 10 + dex_mod
    resist: dict[str, int] = {}
    immune: dict[str, int] = {}
    save_bonus = {"Fortitude": 0, "Reflex": 0, "Will": 0, "Universal": 0}
    regen = 0
    dr_soak = dr_bypass = 0
    cost = 0
    crit_immune = False
    for p in pieces:
        d = p["def"]
        ac += d["ac_bonus"]
        cost += p["cost"]
        regen += d["regen"]
        crit_immune = crit_immune or d["crit_immune"]
        if d["dr_soak"] > dr_soak:
            dr_soak, dr_bypass = d["dr_soak"], d["dr_bypass"]
        for t, v in d["resist"].items():
            resist[t] = max(resist.get(t, 0), v)      # same-type resist: max
        for t, v in d["immune"].items():
            immune[t] = max(immune.get(t, 0), v)
        for k, v in d["saves"].items():
            if k in save_bonus:
                save_bonus[k] = max(save_bonus[k], v)

    univ = save_bonus["Universal"]
    # Epic Toughness adds a flat block on top of the per-level HP, exactly as it
    # does for creatures — reusing that helper so the two sides cannot drift.
    toughness_hp = epic_toughness_hp(
        range(EPIC_TOUGHNESS_BASE, EPIC_TOUGHNESS_BASE + toughness))
    defense = defense_profile(
        ac=ac,
        hp=lvl * (_PC_HIT_DIE + con_mod) + toughness_hp,
        dr_soak=dr_soak, dr_bypass=dr_bypass,
        resist=resist, immune=immune, regen=regen,
        crit_immune=crit_immune,
        # Fighter: good Fortitude (2 + L/2), poor Reflex and Will (L/3).
        fort=2 + pre_epic // 2 + con_mod + save_bonus["Fortitude"] + univ,
        ref=pre_epic // 3 + dex_mod + save_bonus["Reflex"] + univ,
        will=pre_epic // 3 + wis_mod + save_bonus["Will"] + univ,
    )
    return {
        "level": lvl, "bab": bab, "attack": attack, "defense": defense,
        "cost": cost, "two_handed": two_handed, "finesse": finesse,
        "full_heal_rounds": _FULL_HEAL_ROUNDS,
        "crit_feats": crit["feats"], "epic_toughness": toughness,
        "great_ability": (primary, great_tiers),
        "weapon": weapon, "kit": kit, "scores": scores,
    }
