"""Item slot, damage, AC and value derivation.

Turns a base-item row and an item blueprint's ``PropertiesList`` into the
numbers the item pages, the counter-gear matcher and the combat simulator
consume: which inventory slots a base item fills (from baseitems.2da
``EquipableSlots``), the items-index category, weapon damage/crit strings,
AC / attack / damage / enhancement bonuses, and gold-piece value.

``BASEITEM_SLOTS`` starts as a snapshot of the stock table at import time and
is merged over in place by ``load_2da_overrides()``; never rebind it.

Depends only on stdlib, gff, lookups and itemprops -- nothing here may touch
``Db``, ``E()`` or the renderers.
"""

from __future__ import annotations

from nwn_wiki.gff import fld, list_items
from nwn_wiki.itemprops import itemprop_oneliner
from nwn_wiki.lookups import (
    IPROP_DEFS,
    WEAPONS,
    _torso_base_ac,
    baseitem_name,
)


# Inventory slot bitmasks used on Equip_ItemList[*].__struct_id.
SLOT_HEAD = 0x0001
SLOT_CHEST = 0x0002
SLOT_BOOTS = 0x0004
SLOT_ARMS = 0x0008
SLOT_RIGHT = 0x0010
SLOT_LEFT = 0x0020
SLOT_CLOAK = 0x0040
SLOT_LRING = 0x0080
SLOT_RRING = 0x0100
SLOT_NECK = 0x0200
SLOT_BELT = 0x0400
SLOT_ARROWS = 0x0800
SLOT_BULLETS = 0x1000
SLOT_BOLTS = 0x2000
SLOT_CWEAP_R = 0x4000
SLOT_CWEAP_L = 0x8000
SLOT_CWEAP_B = 0x10000
SLOT_CHIDE = 0x20000

SLOT_NAMES = {
    SLOT_HEAD: "Head", SLOT_CHEST: "Chest", SLOT_BOOTS: "Boots",
    SLOT_ARMS: "Arms", SLOT_RIGHT: "Right hand", SLOT_LEFT: "Left hand",
    SLOT_CLOAK: "Cloak", SLOT_LRING: "Left ring", SLOT_RRING: "Right ring",
    SLOT_NECK: "Neck", SLOT_BELT: "Belt", SLOT_ARROWS: "Arrows",
    SLOT_BULLETS: "Bullets", SLOT_BOLTS: "Bolts",
    SLOT_CWEAP_R: "Creature weapon (R)", SLOT_CWEAP_L: "Creature weapon (L)",
    SLOT_CWEAP_B: "Creature weapon (bite)", SLOT_CHIDE: "Creature hide",
}

WEAPON_SLOTS = {SLOT_RIGHT, SLOT_LEFT, SLOT_CWEAP_R, SLOT_CWEAP_L, SLOT_CWEAP_B}
ARMOR_SLOTS = {SLOT_CHEST, SLOT_LEFT}  # left can hold a shield
SHIELD_BASEITEMS = {14, 56, 57}  # Small / Large / Tower shields (stock 2da rows)

# Immunity to critical hits is item property 37 (Immunity: Miscellaneous) with
# subtype 8, which itemprop_format renders through iprp_immunity as this label.
# Matching the label rather than the raw subtype id keeps this working when a
# HAK renumbers the table. See nwn_homers_lotr/CLAUDE-devcrit-immunity-audit.md.
CRIT_IMMUNITY_LABEL = "Critical Hits"

# ---------------------------------------------------------------------------
# Base item → equippable slots (baseitems.2da "EquipableSlots").
#
# The 2DA column is a hex bitmask over exactly the SLOT_* constants above, so it
# is the authoritative answer to "what can wear this?" — no hand-maintained base
# item lists needed, and CEP/custom rows come along for free via --2da-dir.
# BASEITEM_SLOTS starts as the stock table and load_2da_overrides() merges any
# module baseitems.2da over it (same pattern as BASEITEMS itself).
# ---------------------------------------------------------------------------

_STOCK_BASEITEM_SLOTS: dict[int, int] = {}
for _rows, _mask in (
    ((17,),                                       SLOT_HEAD),
    ((16,),                                       SLOT_CHEST),
    ((26,),                                       SLOT_BOOTS),
    ((36, 78),                                    SLOT_ARMS),
    ((31, 59, 63),                                SLOT_RIGHT),
    ((14, 15, 56, 57),                            SLOT_LEFT),
    ((6, 7, 8, 11, 30, 61),                       SLOT_RIGHT | SLOT_LEFT),
    ((80,),                                       SLOT_CLOAK),
    ((52,),                                       SLOT_LRING | SLOT_RRING),
    ((19,),                                       SLOT_NECK),
    ((21,),                                       SLOT_BELT),
    ((20,),                                       SLOT_ARROWS),
    ((27,),                                       SLOT_BULLETS),
    ((25,),                                       SLOT_BOLTS),
    ((69, 70, 71, 72),                            SLOT_CWEAP_R | SLOT_CWEAP_L | SLOT_CWEAP_B),
    ((10, 12, 13, 18, 32, 33, 35, 45, 50, 55, 111),
                                                  SLOT_RIGHT | SLOT_CWEAP_R | SLOT_CWEAP_L | SLOT_CWEAP_B),
    ((0, 1, 2, 3, 4, 5, 9, 22, 28, 37, 38, 40, 41, 42, 47,
      48, 51, 53, 58, 60, 92, 93, 94, 95, 108),
                                                  SLOT_RIGHT | SLOT_LEFT | SLOT_CWEAP_R | SLOT_CWEAP_L | SLOT_CWEAP_B),
    ((73,),                                       SLOT_CHIDE),
):
    for _r in _rows:
        _STOCK_BASEITEM_SLOTS[_r] = _mask
del _rows, _mask, _r

BASEITEM_SLOTS: dict[int, int] = dict(_STOCK_BASEITEM_SLOTS)

# The slots a player character actually equips, in report order. Creature-only
# slots (claw/bite/hide) and the three ammo slots are excluded; ammo is folded
# into one pseudo-slot by PLAYER_SLOT_ORDER's "Ammo" entry below.
_AMMO_SLOTS = SLOT_ARROWS | SLOT_BULLETS | SLOT_BOLTS
PLAYER_SLOT_MASK = (SLOT_HEAD | SLOT_CHEST | SLOT_BOOTS | SLOT_ARMS | SLOT_RIGHT
                    | SLOT_LEFT | SLOT_CLOAK | SLOT_LRING | SLOT_RRING
                    | SLOT_NECK | SLOT_BELT | _AMMO_SLOTS)

# (report key, label, mask, how many can be worn at once). Rings collapse to one
# bucket wearable twice; the three ammo types collapse to one "Ammo" bucket.
PLAYER_SLOTS: list[tuple[str, str, int, int]] = [
    ("head",   "Head",              SLOT_HEAD,                 1),
    ("chest",  "Chest",             SLOT_CHEST,                1),
    ("boots",  "Boots",             SLOT_BOOTS,                1),
    ("arms",   "Arms",              SLOT_ARMS,                 1),
    ("right",  "Right hand",        SLOT_RIGHT,                1),
    ("left",   "Left hand",         SLOT_LEFT,                 1),
    ("cloak",  "Cloak",             SLOT_CLOAK,                1),
    ("neck",   "Neck",              SLOT_NECK,                 1),
    ("belt",   "Belt",              SLOT_BELT,                 1),
    ("ring",   "Ring",              SLOT_LRING | SLOT_RRING,   2),
    ("ammo",   "Ammo",              _AMMO_SLOTS,               1),
]


def baseitem_slots(bi: int | None) -> int:
    """Equippable-slot bitmask for a base item row, 0 when unknown/unequippable."""
    if bi is None or bi < 0:
        return 0
    return BASEITEM_SLOTS.get(int(bi), 0)


def item_equip_slots(item: dict | None) -> int:
    """Equippable-slot bitmask for an item blueprint."""
    if not item:
        return 0
    raw = fld(item, "BaseItem", None)
    if raw is None:
        return 0
    try:
        return baseitem_slots(int(raw))
    except (TypeError, ValueError):
        return 0


def slot_label(mask: int) -> str:
    """Human label for a slot bitmask — a single SLOT_* name where the mask is
    one known slot, otherwise the names joined with '/'."""
    if mask in SLOT_NAMES:
        return SLOT_NAMES[mask]
    parts = [SLOT_NAMES[b] for b in sorted(SLOT_NAMES) if mask & b]
    return "/".join(parts) if parts else f"0x{mask:X}"


# BaseItem sets for items-index grouping.
_ARMOR_BASEITEMS:      frozenset[int] = frozenset({16})
_HELMET_BASEITEMS:     frozenset[int] = frozenset({17})
_AMMO_BASEITEMS:       frozenset[int] = frozenset({20, 25, 27})   # Arrow Bolt Bullet
_WAND_BASEITEMS:       frozenset[int] = frozenset({46, 106})      # Magic Wand, Crafted Wand
_POTION_BASEITEMS:     frozenset[int] = frozenset({49, 104})      # Potion, Brewed Potion
_SCROLL_BASEITEMS:     frozenset[int] = frozenset({75, 105})      # Scroll, Enchanted Scroll
_GRENADE_BASEITEMS:    frozenset[int] = frozenset({81})           # Grenadelike

# Maps specific accessory BaseItem rows to category keys.
# Bracers (78) and Gauntlets (36) share one key per the items index design.
_ACCESSORY_BASEITEM_MAP: dict[int, str] = {
    19: "amulet",
    21: "belt",
    26: "boots",
    36: "bracers_gauntlets",
    52: "ring",
    78: "bracers_gauntlets",
    80: "cloak",
}

# Base items that are creature-only (not equippable by players).
_CREATURE_WEAPON_BASEITEMS: frozenset[int] = frozenset({69, 70, 71, 72})
_CREATURE_ITEM_BASEITEMS:   frozenset[int] = frozenset({73})

# Named misc subtypes matched by base item ID.
_MISC_TYPE_MAP: dict[int, str] = {
    39: "tool_kit",   # Healer's Kit
    44: "magic_rod",
    45: "magic_staff",
    62: "tool_kit",   # Thieves' Tools
    64: "tool_kit",   # Trap Kit
    74: "book",
    77: "gem",
}

# Miscellaneous-subtype base items that get name-based sub-categorization.
_MISC_LIKE_BASEITEMS: frozenset[int] = frozenset({24, 29, 34, 79, 83, 84, 85, 86, 307, 311})

# CEP weapon base item IDs not present in weapons.json (stock IDs only cover
# 0–112). Now only a fallback: with --2da-dir, load_2da_overrides merges the
# module's own baseitems.2da weapon columns into WEAPONS, so these rows get a
# real WeaponType (and dice, and crit stats) and are recognised on their own.
# This list still carries runs with no 2DA override.
_CEP_WEAPON_BASEITEMS: frozenset[int] = frozenset({
    300, 301, 302, 303, 304, 305, 308, 309, 310,
    312, 313, 316, 317, 318, 319, 320, 321, 322, 323, 324,
})

# Fixed display labels for non-weapon category keys.
_CATEGORY_LABELS: dict[str, str] = {
    "armor_heavy":       "Heavy Armor",
    "armor_medium":      "Medium Armor",
    "armor_light":       "Light Armor",
    "armor_cloth":       "Cloth & Robes",
    "shield_small":      "Small Shields",
    "shield_large":      "Large Shields",
    "shield_tower":      "Tower Shields",
    "helmet":            "Helmets",
    "amulet":            "Amulets",
    "belt":              "Belts",
    "boots":             "Boots",
    "bracers_gauntlets": "Bracers & Gauntlets",
    "ring":              "Rings",
    "cloak":             "Cloaks",
    "ammo":              "Ammunition",
    "wand":              "Wands",
    "potion":            "Potions",
    "scroll":            "Scrolls",
    "grenade":           "Grenades",
    "book":              "Books",
    "magic_rod":         "Magic Rods",
    "magic_staff":       "Magic Staves",
    "gem":               "Gems",
    "dye":               "Dyes",
    "poison":            "Poisons & Venoms",
    "tool_kit":          "Tools & Kits",
    "misc":              "Miscellaneous",
    "creature_item":     "Creature Items",
}

# TOC group headings. Sentinels "WEAPONS" and "CREATURE_WEAPONS" expand to
# dynamically-discovered weapon_* keys at render time.
_TOC_GROUPS: list[tuple[str, list[str]]] = [
    ("Weapons",  ["WEAPONS", "ammo"]),
    ("Armor",    ["armor_heavy", "armor_medium", "armor_light", "armor_cloth", "shield_small", "shield_large", "shield_tower"]),
    ("Gear",     ["amulet", "belt", "boots", "bracers_gauntlets", "ring", "cloak", "helmet"]),
    ("Misc.",    ["wand", "potion", "scroll", "grenade", "book", "magic_rod", "magic_staff", "gem", "dye", "poison", "tool_kit", "misc"]),
]
# Special heading covers creature weapons, creature items, inaccessible, and broken.


def _item_category(item: dict, name: str = "") -> str:
    """Return the items-index category key for an item dict."""
    _raw = fld(item, "BaseItem", None)
    bi = -1 if _raw is None else int(_raw)
    if bi in _ARMOR_BASEITEMS:
        ac = _torso_base_ac(item)
        if ac <= 0:
            return "armor_cloth"
        if ac <= 3:
            return "armor_light"
        if ac <= 5:
            return "armor_medium"
        return "armor_heavy"
    if bi == 14:
        return "shield_small"
    if bi == 56:
        return "shield_large"
    if bi == 57:
        return "shield_tower"
    if bi in _HELMET_BASEITEMS:
        return "helmet"
    if bi in _ACCESSORY_BASEITEM_MAP:
        return _ACCESSORY_BASEITEM_MAP[bi]
    if bi in _WAND_BASEITEMS:    return "wand"
    if bi in _POTION_BASEITEMS:  return "potion"
    if bi in _SCROLL_BASEITEMS:  return "scroll"
    if bi in _GRENADE_BASEITEMS: return "grenade"
    if bi in _AMMO_BASEITEMS:
        return "ammo"
    stats = WEAPONS.get(bi)
    if stats and stats.get("WeaponType", "0") not in ("0", "", None):
        return f"weapon_{bi}"
    if bi in _CEP_WEAPON_BASEITEMS:
        return f"weapon_{bi}"
    if bi in _CREATURE_ITEM_BASEITEMS:
        return "creature_item"
    mapped = _MISC_TYPE_MAP.get(bi)
    if mapped:
        return mapped
    if bi in _MISC_LIKE_BASEITEMS or bi == -1:
        nl = name.lower()
        if "gem" in nl or " agate" in nl: return "gem"
        if "dye" in nl:                   return "dye"
        if "poison" in nl or "venom" in nl: return "poison"
    return "misc"


def _item_category_label(key: str) -> str:
    if key in _CATEGORY_LABELS:
        return _CATEGORY_LABELS[key]
    if key.startswith("weapon_"):
        try:
            bi = int(key[7:])
            return baseitem_name(bi) or "Weapon"
        except ValueError:
            pass
    return "Miscellaneous"


def is_ranged_weapon(base_row: int | None) -> bool:
    if base_row is None:
        return False
    stats = WEAPONS.get(int(base_row))
    if not stats:
        return False
    rw = stats.get("RangedWeapon")
    return bool(rw and rw not in ("0", ""))


def weapon_damage_string(base_row: int | None,
                         str_mod: int = 0,
                         is_ranged: bool = False,
                         iprop_dmg_bonus: int = 0,
                         iprop_extra: list[str] | None = None) -> str:
    """Human-readable base damage. Doesn't model two-handed (1.5x Str) or
    weapon-finesse — those are runtime decisions."""
    stats = WEAPONS.get(int(base_row)) if base_row is not None else None
    if not stats:
        return "—"
    try:
        n = int(stats.get("NumDice", "0") or 0)
        d = int(stats.get("DieToRoll", "0") or 0)
    except ValueError:
        n, d = 0, 0
    if n <= 0 or d <= 0:
        return "—"
    bits = [f"{n}d{d}"]
    flat = (0 if is_ranged else str_mod) + iprop_dmg_bonus
    if flat > 0:
        bits.append(f"+{flat}")
    elif flat < 0:
        bits.append(str(flat))
    base = "".join(bits)
    lo = n + max(flat, -n * d + n)  # min damage clamps at "all ones + flat"
    hi = n * d + flat
    rng = f"({lo}–{hi})" if hi > lo else f"({hi})"
    extras = ", ".join(iprop_extra or [])
    out = f"{base} {rng}"
    if extras:
        out += f" + {extras}"
    return out


def weapon_crit_string(base_row: int | None) -> str:
    stats = WEAPONS.get(int(base_row)) if base_row is not None else None
    if not stats:
        return ""
    threat = stats.get("CritThreat", "")
    mult = stats.get("CritHitMult", "")
    if not threat or not mult:
        return ""
    try:
        t = int(threat)
        m = int(mult)
    except ValueError:
        return ""
    if t <= 1:
        return f"20/x{m}"
    return f"{20 - t + 1}-20/x{m}"


def item_ac_bonus(item: dict | None) -> tuple[int, list[str]]:
    """Sum AC-related item properties on a single item. Returns
    (numeric_bonus, descriptors) — descriptors lists deflection / dodge /
    natural / etc. so the reader can see the breakdown."""
    if not item:
        return 0, []
    total = 0
    notes: list[str] = []
    for p in list_items(item.get("PropertiesList")):
        pname_id = fld(p, "PropertyName")
        if pname_id is None:
            continue
        pdef = IPROP_DEFS.get(int(pname_id), {})
        pname = (pdef.get("name") or "").lower()
        cost = fld(p, "CostValue", 0) or 0
        try:
            cost_int = int(cost)
        except (TypeError, ValueError):
            cost_int = 0
        if "ac bonus" in pname or pname == "ac":
            total += cost_int
            notes.append(f"+{cost_int} {pdef.get('name','AC')}")
    return total, notes


def item_attack_bonus(item: dict | None) -> int:
    """To-hit bonus from a weapon's Enhancement Bonus (#6) and Attack Bonus (#56).

    These do NOT stack on the attack roll: NWN's enhancement bonus is internally
    attack+damage bundled, and a separate Attack Bonus property uses the *higher*
    of the two for to-hit, not the sum (see enhancement-attack-bonus-conflict-
    report.txt). Same-type props don't stack either, so take the max within each
    type, then the max across the two. Conditional attack bonuses vs.
    alignment/racial group (#57/#58) are situational and excluded here."""
    if not item:
        return 0
    enh = atk = 0
    for p in list_items(item.get("PropertiesList")):
        pname_id = fld(p, "PropertyName")
        if pname_id is None:
            continue
        try:
            cv = int(fld(p, "CostValue", 0) or 0)
        except (TypeError, ValueError):
            cv = 0
        pid = int(pname_id)
        if pid == 6:      # Enhancement Bonus
            enh = max(enh, cv)
        elif pid == 56:   # Attack Bonus (unconditional)
            atk = max(atk, cv)
    return max(enh, atk)


def item_damage_bonus(item: dict | None) -> tuple[int, list[str]]:
    """Sum flat 'Damage Bonus' iprops; collect non-numeric extras (elemental,
    massive crit, etc.) as descriptive strings."""
    if not item:
        return 0, []
    flat = 0
    extras: list[str] = []
    for p in list_items(item.get("PropertiesList")):
        pname_id = fld(p, "PropertyName")
        if pname_id is None:
            continue
        pdef = IPROP_DEFS.get(int(pname_id), {})
        pname = (pdef.get("name") or "").lower()
        if "damage bonus" in pname:
            try:
                cost = int(fld(p, "CostValue", 0) or 0)
            except (TypeError, ValueError):
                cost = 0
            line = itemprop_oneliner(p)
            # Damage Bonus w/ a "physical" or generic subtype usually flat;
            # elemental subtypes display as bonus-line extras.
            sub_id = fld(p, "Subtype")
            if sub_id in (None, 0, 7):  # 7 = Physical (most flat)
                flat += cost
            else:
                extras.append(line)
        elif "massive criticals" in pname or "on hit" in pname or "vampiric" in pname:
            extras.append(itemprop_oneliner(p))
    return flat, extras


def weapon_enhancement(item: dict | None) -> int:
    """Highest Enhancement Bonus (item-property #6) on the item.

    Unlike item_attack_bonus (which folds in Attack Bonus #56 for to-hit), this
    is the value that matters for bypassing a creature's Damage Reduction: NWN
    DR of 'X/+Y' is ignored only by a weapon whose *enhancement* is ≥ Y."""
    if not item:
        return 0
    enh = 0
    for p in list_items(item.get("PropertiesList")):
        if fld(p, "PropertyName") != 6:
            continue
        try:
            cv = int(fld(p, "CostValue", 0) or 0)
        except (TypeError, ValueError):
            cv = 0
        enh = max(enh, cv)
    return enh


def item_gp_value(item: dict | None) -> int:
    """Gold-piece value the engine reports for an item: the toolset-computed
    `Cost` plus the builder-set `AddCost`. This is what GetGoldPieceValue()
    returns; reading `Cost` alone under-values every item a builder priced by
    hand (288 of this module's blueprints carry a non-zero AddCost)."""
    if not item:
        return 0
    total = 0
    for key in ("Cost", "AddCost"):
        try:
            total += int(fld(item, key, 0) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _item_accessible(db: "Db", rr: str) -> bool:
    """True when players can obtain this item: sold in a store, found in a
    container, dropped/pickpocketed off a creature, or granted by a script.
    Mirrors the item_index.json accessibility rule."""
    return (
        rr in db.item_sold_at
        or rr in db.item_in_container
        or any(e.get("dropable") or e.get("pickpocketable")
               for e in db.item_carried_by.get(rr, []))
        or rr in db.item_from_script
    )
