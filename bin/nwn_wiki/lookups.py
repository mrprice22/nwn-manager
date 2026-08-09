"""Bundled 2DA-ish lookups and the id->name helpers built on them.

Loads the JSON caches shipped in ``bin/wiki_data`` (baseitems, classes, races,
feats, skills, spells, item properties, weapon stats, ...) at import time and
exposes the row -> human-name helpers that read them.

The tables are patched in place at runtime by ``load_2da_overrides()`` (still in
``cli``), which merges a module's extracted 2DAs on top.  They are only ever
mutated, never rebound, so importing them by name is safe.

``STOCK_BASEITEMS`` and ``STOCK_FEAT_NAMES`` are snapshots taken here at import
time -- i.e. before any override runs -- and the stock-vs-custom row distinction
depends on that ordering.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from nwn_wiki.gff import fld, list_items
from nwn_wiki.paths import DATA_DIR
from nwn_wiki.warn import _warn_once


# ---------------------------------------------------------------------------
# Bundled 2DA-ish lookups (row → human name).
# ---------------------------------------------------------------------------

def _load_lookup(name: str) -> dict[int, str]:
    p = DATA_DIR / f"{name}.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except Exception as e:
        print(f"warn: could not parse {p}: {e}", file=sys.stderr)
        return {}
    return {int(k): str(v) for k, v in raw.items() if not k.startswith("_")}


BASEITEMS = _load_lookup("baseitems")
# Snapshot of the stock baseitems.2da rows, captured before load_2da_overrides()
# extends BASEITEMS in place with the module's CEP/custom rows. Used to recognise
# a store's "buy nothing" sentinel: builders restrict a store's WillOnlyBuy list to
# an out-of-range base item row (one no normal player merchandise uses) so the store
# refuses every sale. Such a row is absent from this stock set. See _store_buy_summary.
STOCK_BASEITEMS: frozenset[int] = frozenset(BASEITEMS)
CLASSES = _load_lookup("classes")
RACES = _load_lookup("racialtypes")
APPEARANCE = _load_lookup("appearance")
PLACEABLES: dict[int, str] = {}  # populated by overlays only — stock placeables.2da
                                  # rows are rarely interesting and we don't bundle them.
IPRP_FEATS = _load_lookup("iprp_feats")
FEATS = _load_lookup("feat")
# Snapshot of the stock feat names before load_2da_overrides() relabels FEATS
# from a module's feat.2da. Used to recognise Weapon Focus / Epic Weapon Focus
# feats by their consistent stock names (some HAKs abbreviate the labels, e.g.
# "WeapFocRapier", which would defeat name matching against the live FEATS).
# Feat IDs are fixed by the engine, so matching stock names by id is reliable.
STOCK_FEAT_NAMES = dict(FEATS)
SKILLS = _load_lookup("skills")
SPELLS = _load_lookup("spells")


def _load_spell_info() -> dict[str, dict[int, dict]]:
    """Load spell_info.json — {table_name: {row_id: {innate_level, caster_level, class_levels}}}.

    Both iprp_spells and iprp_onhitspell are stored under their table names
    to keep their independent row-ID spaces separate."""
    p = DATA_DIR / "spell_info.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    result: dict[str, dict[int, dict]] = {}
    for k, v in raw.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        # Top-level keys are table names ("iprp_spells", "iprp_onhitspell").
        tbl: dict[int, dict] = {}
        for rk, rv in v.items():
            if isinstance(rv, dict):
                try:
                    tbl[int(rk)] = rv
                except ValueError:
                    pass
        result[k] = tbl
    return result


SPELL_INFO: dict[str, dict[int, dict]] = _load_spell_info()


def _load_weapons() -> dict[int, dict[str, str]]:
    p = DATA_DIR / "weapons.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    return {int(k): v for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, dict)}


WEAPONS = _load_weapons()

# baseitems.2da columns merged into WEAPONS from a --2da-dir override. Keep in
# step with WEAPON_COLS in bin/wiki_data/_build_stock.py, which writes the same
# set into the bundled weapons.json.
#
# The five feat columns name the per-base-item feat a wielder would take (the
# engine looks the feat up here rather than by weapon name), so a base item with
# a blank column has no such feat to take — which is exactly how this module
# disables Devastating Critical: bin/gen-devcrit-map.py blanks
# EpicWeaponDevastatingCriticalFeat for every row.
_WEAPON_2DA_COLS = (
    "WeaponType", "WeaponSize", "RangedWeapon", "MinRange", "MaxRange",
    "NumDice", "DieToRoll", "CritThreat", "CritHitMult", "WeaponWield",
    "AmmunitionType", "BaseAC", "ArmorCheckPen", "AC_Enchant",
    "WeaponFocusFeat", "EpicWeaponFocusFeat",
    "WeaponSpecializationFeat", "EpicWeaponSpecializationFeat",
    "WeaponImprovedCriticalFeat", "EpicWeaponOverwhelmingCriticalFeat",
    "EpicWeaponDevastatingCriticalFeat",
)

# Which of the above a real baseitems.2da actually supplied. Only non-blank
# cells reach WEAPONS, so this is the only way to tell "the column is there and
# deliberately empty" (a module that disabled a mechanic) from "we never had
# that column" (the bundled weapons.json predates it) — a distinction the
# devastating-critical detection depends on.
BASEITEM_COLUMNS_SEEN: set[str] = set()


def _load_class_bab() -> dict[int, list[int]]:
    """Load class_bab.json → {class_id: [bab_at_lvl1, bab_at_lvl2, ...]}.
    Per-class base attack bonus progression from stock cls_atk_*.2da."""
    p = DATA_DIR / "class_bab.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    return {int(k): v for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, list)}


CLASS_BAB: dict[int, list[int]] = _load_class_bab()


def _load_race_adjust() -> dict[int, dict[str, int]]:
    """Load race_adjust.json → {race_id: {'Str': n, 'Dex': n, ...}}.
    Racial ability-score adjustments the engine applies on top of the stored
    UTC scores (e.g. Elf +2 Dex / -2 Con). A racialtypes.2da override (custom
    races) is merged on top in load_2da_overrides()."""
    p = DATA_DIR / "race_adjust.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    out: dict[int, dict[str, int]] = {}
    for k, v in raw.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        try:
            out[int(k)] = {ab: int(n) for ab, n in v.items()}
        except (ValueError, TypeError):
            pass
    return out


RACE_ABILITY_ADJ: dict[int, dict[str, int]] = _load_race_adjust()


def _load_parts_chest() -> dict[int, int]:
    """Load parts_chest.2da → {torso_id: base_ac}.
    ACBONUS values may be fractional (CEP encodes model subtype in decimals);
    we take the integer part as the game-effective base AC.
    '****' rows (undefined model) map to 0 (treated as cloth)."""
    p = DATA_DIR / "parts_chest.2da"
    if not p.exists():
        return {}
    result: dict[int, int] = {}
    header_done = False
    acbonus_col: int | None = None
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("2DA"):
            continue
        parts = line.split()
        if not header_done:
            # Header row has no leading row-id token; find ACBONUS position.
            # Data rows prepend a row-id token, so data col = header_idx + 1.
            try:
                acbonus_col = parts.index("ACBONUS") + 1
            except ValueError:
                return {}
            header_done = True
            continue
        if acbonus_col is None or len(parts) <= acbonus_col:
            continue
        try:
            row_id = int(parts[0])
        except ValueError:
            continue
        raw_val = parts[acbonus_col]
        if raw_val == "****":
            result[row_id] = 0
        else:
            try:
                result[row_id] = int(float(raw_val))
            except ValueError:
                result[row_id] = 0
    return result


PARTS_CHEST_AC: dict[int, int] = _load_parts_chest()


def _torso_base_ac(item: dict | None) -> int:
    """Return the base AC for an armor item from its ArmorPart_Torso model."""
    if item is None:
        return 0
    torso = fld(item, "ArmorPart_Torso")
    if torso is None:
        return 0
    return PARTS_CHEST_AC.get(int(torso), 0)


def _load_itemprops() -> dict:
    p = DATA_DIR / "itemprops.json"
    if not p.exists():
        return {"properties": {}, "tables": {}}
    return json.loads(p.read_text())


ITEMPROPS = _load_itemprops()
IPROP_DEFS: dict[int, dict] = {int(k): v for k, v in ITEMPROPS.get("properties", {}).items()}
IPROP_TABLES: dict[str, dict] = ITEMPROPS.get("tables", {})


_OVERLAY_TARGETS: dict[str, dict[int, str]] = {
    "baseitems":   BASEITEMS,
    "racialtypes": RACES,
    "appearance":  APPEARANCE,
    "placeables":  PLACEABLES,
    "iprp_feats":  IPRP_FEATS,
    "classes":     CLASSES,
    "feat":        FEATS,
    "skills":      SKILLS,
    "spells":      SPELLS,
}


def load_json_overlay(d: Path, label: str) -> int:
    """Merge pre-built `<name>.json` files in `d` onto the in-memory lookup
    dicts. JSON layout: {"<row>": "<pretty name>", "_source": "..."}.
    Keys starting with "_" are metadata and ignored. Returns total rows merged."""
    total = 0
    if not d.is_dir():
        return 0
    for name, target in _OVERLAY_TARGETS.items():
        p = d / f"{name}.json"
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  warn: could not parse {p}: {e}", file=sys.stderr)
            continue
        n = 0
        for k, v in data.items():
            if k.startswith("_") or not isinstance(v, str):
                continue
            try:
                target[int(k)] = v
            except ValueError:
                continue
            n += 1
        total += n
        print(f"  {label}: {name}.json → {n} rows")
    return total


def baseitem_name(row: int | None) -> str:
    """Stock baseitems.2da lookup. Custom HAKs (CEP, etc.) often add rows
    beyond stock; for those we surface the row number explicitly so the
    reader knows the label is a guess."""
    if row is None:
        return ""
    r = int(row)
    if r in BASEITEMS:
        return BASEITEMS[r]
    _warn_once(f"baseitems.2da row {r} not found — add --2da-dir with an override to resolve")
    return f"BaseItem #{r}"


def baseitem_label(row: int | None) -> str:
    """Human label for an item's base type, with the row number always
    preserved (CEP/custom HAKs override many rows so the stock name is a hint
    rather than ground truth)."""
    if row is None:
        return ""
    r = int(row)
    if r in BASEITEMS:
        return f"{BASEITEMS[r]} <small class=\"muted\">(row {r})</small>"
    return f"BaseItem #{r}"


def class_name(row: int | None) -> str:
    if row is None:
        return ""
    r = int(row)
    if r in CLASSES:
        return CLASSES[r]
    _warn_once(f"classes.2da row {r} not found — add --2da-dir with an override to resolve")
    return f"Class #{r}"


def race_name(row: int | None) -> str:
    if row is None:
        return ""
    r = int(row)
    if r in RACES:
        return RACES[r]
    _warn_once(f"racialtypes.2da row {r} not found — add --2da-dir with an override to resolve")
    return f"Race #{r}"


# Immunities the engine grants from a creature's racial type. These are *not*
# stored anywhere in the .utc — NWN applies them by creature type at runtime, so
# a crit-immune undead has no "Immunity: Critical Hits" item property to find.
# Labels deliberately reuse the iprp_immunity vocabulary (see
# wiki_data/itemprops.json) so racial and equipment immunities merge into one
# namespace on the creature pages and in the creature search index.
# Keyed by racialtypes.2da row.
RACE_IMMUNITIES: dict[int, tuple[str, ...]] = {
    10: (  # Construct
        "Critical Hits", "Sneak Attack", "Mind-Affecting Spells", "Paralysis",
        "Poison", "Disease", "Death Magic", "Level/Ability Drain", "Fear",
    ),
    16: (  # Elemental
        "Critical Hits", "Sneak Attack", "Paralysis", "Poison", "Disease",
    ),
    24: (  # Undead
        "Critical Hits", "Sneak Attack", "Mind-Affecting Spells", "Paralysis",
        "Poison", "Disease", "Death Magic", "Level/Ability Drain", "Fear",
    ),
    25: (  # Vermin — mindless, but *not* crit immune
        "Mind-Affecting Spells",
    ),
    29: (  # Ooze
        "Critical Hits", "Sneak Attack", "Mind-Affecting Spells", "Paralysis",
        "Poison", "Fear",
    ),
}


def creature_race_immunities(race_raw: Any) -> list[str]:
    """Immunity labels a creature gets purely from its racial type (see
    RACE_IMMUNITIES). Returns [] for races with no engine-granted immunities."""
    if race_raw is None:
        return []
    try:
        rid = int(race_raw)
    except (TypeError, ValueError):
        return []
    return list(RACE_IMMUNITIES.get(rid, ()))


def appearance_name(row: int | None) -> str:
    if row is None:
        return ""
    r = int(row)
    if r in APPEARANCE:
        return APPEARANCE[r]
    _warn_once(f"appearance.2da row {r} not found — add --2da-dir with an override to resolve")
    return f"Appearance #{r}"


def placeable_name(row: int | None) -> str:
    """Human label for a placeables.2da row. Falls back to a numeric stub when
    the row isn't bundled — placeables.2da overlays must be loaded for this to
    return real names (CEP modules get them via the auto-detected overlay)."""
    if row is None:
        return ""
    r = int(row)
    if r in PLACEABLES:
        return PLACEABLES[r]
    _warn_once(f"placeables.2da row {r} not found — add --2da-dir with an override to resolve")
    return f"Placeable #{r}"


def feat_name(row: int | None) -> str:
    if row is None:
        return ""
    r = int(row)
    if r in FEATS:
        return FEATS[r]
    _warn_once(f"feat.2da row {r} not found — add --2da-dir with an override to resolve")
    return f"Feat #{r}"


def skill_name(row: int | None) -> str:
    if row is None:
        return ""
    r = int(row)
    if r in SKILLS:
        return SKILLS[r]
    _warn_once(f"skills.2da row {r} not found — add --2da-dir with an override to resolve")
    return f"Skill #{r}"


def spell_name(row: int | None) -> str:
    if row is None:
        return ""
    r = int(row)
    if r in SPELLS:
        return SPELLS[r]
    _warn_once(f"spells.2da row {r} not found — add --2da-dir with an override to resolve")
    return f"Spell #{r}"


# PropertyName id for the Cast Spell item property.
_CAST_SPELL_PROP_ID = 15
# Maps property name → iprp table name for properties that carry spell data.
_SPELL_PROP_TABLES: dict[str, str] = {
    "Cast Spell":               "iprp_spells",
    "On Hit Cast Spell":        "iprp_onhitspell",
    "Immunity: Specific Spell": "iprp_spells",
}

# Properties whose subtypes are all rendered on a single combined page instead
# of individual per-subtype files.  Key = property name, value = page slug
# (filename without .html).
_COMBINED_PROP_PAGES: dict[str, str] = {
    "Cast Spell": "cast-spell",
}


def _iprp_spell_info(tbl_name: str, row_id: int | None) -> dict | None:
    """Return spell info dict for a given iprp table row, or None if unavailable."""
    if row_id is None or not SPELL_INFO:
        return None
    return SPELL_INFO.get(tbl_name, {}).get(row_id)


def _spell_level_classes(info: dict | None) -> tuple[str, str]:
    """Return (level_str, classes_str) for a spell info dict.

    classes_str is 'N/A' when the spell is not castable by any PC class."""
    if not info:
        return ("", "")
    lvl = info.get("innate_level")
    classes = info.get("class_levels", {})
    level_str = "Cantrip" if lvl == 0 else (str(lvl) if lvl is not None else "")
    classes_str = ", ".join(classes.keys()) if classes else "N/A"
    return (level_str, classes_str)


def _scroll_cast_spell_info(item: dict, name: str = "") -> dict | None:
    """Return the SPELL_INFO entry for a scroll item's Cast Spell property subtype.

    Falls back to name-based lookup when PropertiesList is absent (e.g. stock
    items that appear only as sparse store-inventory references)."""
    for p in list_items(item.get("PropertiesList")):
        pname_id = fld(p, "PropertyName")
        if pname_id is not None and int(pname_id) == _CAST_SPELL_PROP_ID:
            subtype = fld(p, "Subtype")
            if subtype is not None:
                return _iprp_spell_info("iprp_spells", int(subtype))
    if name and not list_items(item.get("PropertiesList")):
        return _iprp_name_spell_info("iprp_spells", name)
    return None


def _iprp_name_spell_info(tbl_name: str, spell_name_str: str) -> dict | None:
    """Resolve spell info by matching a spell name in an iprp_spells-type table.

    Scans IPROP_TABLES[tbl_name] for the first row whose resolved name equals
    spell_name_str, then returns the corresponding SPELL_INFO entry."""
    if not SPELL_INFO:
        return None
    tbl = SPELL_INFO.get(tbl_name, {})
    iprp_names = IPROP_TABLES.get(tbl_name, {})
    for k, name in iprp_names.items():
        if k.startswith("_") or name != spell_name_str:
            continue
        try:
            row_id = int(k)
        except ValueError:
            continue
        info = tbl.get(row_id)
        if info:
            return info
    return None


# Stock NWN1 tileset resrefs → human-readable names. The Aurora Toolset
# stores the resref in the area's `Tileset` field; the friendly name is
# kept in the corresponding .set file (which we don't read here).
TILESETS: dict[str, str] = {
    "tcn01": "City Exterior 1",
    "tcn02": "City Exterior 2",
    "tdc01": "Castle Interior 1",
    "tdc02": "Castle Interior 2",
    "tde01": "Castle Interior, Illuminated",
    "tdm01": "Mines and Caverns",
    "tdr01": "Rural",
    "tds01": "Sewers",
    "tib01": "Beholder Caves",
    "tic01": "City Interior",
    "til01": "Lizardfolk Interior",
    "tin01": "Crypt",
    "tni01": "Mines, Lower",
    "tno01": "Forest",
    "ttd01": "Desert",
    "tte01": "Forest, Drow",
    "ttf01": "Frozen Wastes",
    "ttp01": "Microset",
    "ttr01": "Rural Winter",
    "tts01": "Snow",
    "ttu01": "Underdark",
    "ttz01": "Mountains",
    "twc01": "Castle Exterior, Rural",
}


def tileset_name(resref: str) -> str:
    """Friendly tileset name, falling back to the raw resref for unknowns."""
    if not resref:
        return ""
    return TILESETS.get(resref.lower(), resref)


# Friendly labels for NWN DAMAGE_TYPE_* constants used in retaliation summaries.
_DAMAGE_TYPE_LABELS = {
    "BLUDGEONING": "Bludgeoning", "PIERCING": "Piercing", "SLASHING": "Slashing",
    "MAGICAL": "Magical", "ACID": "Acid", "COLD": "Cold", "DIVINE": "Divine",
    "ELECTRICAL": "Electrical", "FIRE": "Fire", "NEGATIVE": "Negative",
    "POSITIVE": "Positive", "SONIC": "Sonic",
}
