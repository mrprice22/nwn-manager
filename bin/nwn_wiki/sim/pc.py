"""Feat budget for the reference PC of the counter-gear report.

The two feat pools a pure single-class Fighter build draws on, and the kit
flattening helper the solver shares with :func:`nwn_wiki.cli.reference_pc`.

Pure stdlib -- no ``Db``, no wiki state, no renderers. ``reference_pc`` itself
still lives in :mod:`nwn_wiki.cli` because it needs the item-property and
weapon-feat helpers that have not been extracted yet.
"""

from __future__ import annotations


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
