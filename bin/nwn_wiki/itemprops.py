"""Item-property formatting and the structural dedup keys.

Translates a GFF ``PropertiesList`` entry into display fields via the bundled
``itemprops.json`` definitions in :mod:`nwn_wiki.lookups`, and provides the
small grouping/slug/tier helpers the property index pages are built from.

Also holds the content-fingerprint helpers (``_item_prop_key``,
``_creature_key``, ``_store_inv_key``) used to decide whether two blueprints
sharing a tag are genuine duplicates or conflicting variants.

Depends only on stdlib, gff, lookups and warn -- nothing here may touch ``Db``,
``E()`` or the renderers.
"""

from __future__ import annotations

import re
from typing import Any

from nwn_wiki.gff import fld, list_items
from nwn_wiki.lookups import (
    IPROP_DEFS,
    IPROP_TABLES,
    IPRP_FEATS,
    class_name,
    race_name,
    skill_name,
)
from nwn_wiki.warn import _warn_once


# ---------------------------------------------------------------------------
# Item property formatting
# ---------------------------------------------------------------------------

# Subtype lookups whose values come from non-iprp tables we already loaded.
# These point at top-level lookup dicts so 2DA overrides made through
# load_2da_overrides() remain visible (closing over the dict, not the value).
_NON_IPRP_SUBTYPES = {
    "racialtypes": lambda i: race_name(i),
    "classes":     lambda i: class_name(i),
    "iprp_feats":  lambda i: IPRP_FEATS.get(int(i)) if i is not None else None,
    "skills":      lambda i: skill_name(i),
}


def _table_lookup(table_name: str | None, idx: int | None) -> str | None:
    """Resolve a per-property numeric index to a label.

    Order of resolution:
      1. Non-iprp tables (classes, races) handled directly.
      2. Explicit row entry in the JSON (e.g. "0": "+1").
      3. Format string fallback ("_format": "+{}") — used when a table is a
         simple identity progression and we don't want to hand-list every row.

    Empirically the engine often stores the numeric *value* directly in
    CostValue rather than a row index (e.g. CostValue=4 ⇒ "+4"), so the
    format-string path matters more than per-row lookups for the common
    "+N" tables.
    """
    if not table_name or idx is None:
        return None
    if table_name in _NON_IPRP_SUBTYPES:
        return _NON_IPRP_SUBTYPES[table_name](idx)
    tbl = IPROP_TABLES.get(table_name)
    if not tbl:
        return None
    key = str(int(idx))
    if key in tbl:
        return tbl[key]
    fmt = tbl.get("_format")
    if isinstance(fmt, str):
        return fmt.format(int(idx))
    return None


def itemprop_format(prop: dict) -> dict[str, str]:
    """Translate one PropertiesList entry to display fields.

    Returns: {property, subtype, cost, param, chance}
    Falls back to raw numbers when a lookup is missing.
    """
    pname_id = fld(prop, "PropertyName")
    subtype_id = fld(prop, "Subtype")
    cost_table_id = fld(prop, "CostTable")
    cost_value = fld(prop, "CostValue")
    param1 = fld(prop, "Param1")
    param1_value = fld(prop, "Param1Value")
    chance = fld(prop, "ChanceAppear", 100)

    pdef = IPROP_DEFS.get(int(pname_id), {}) if pname_id is not None else {}
    if pname_id is not None and not pdef:
        _warn_once(f"item property {pname_id} not found in itemprops.json — itemprops.json may be outdated")
    pname = pdef.get("name") or (f"Property #{pname_id}" if pname_id is not None else "")

    # Subtype: per-property table (e.g. Damage Bonus → damage type name).
    # When the lookup misses (custom HAK row or table we don't ship), surface
    # the table name alongside the raw index so the reader can extract that
    # 2DA and rerun with --2da-dir, instead of staring at a bare integer.
    # Properties without a subtype table drop a subtype of 0 (the engine's
    # "no subtype" sentinel for those props); properties that *do* have a
    # subtype table treat 0 as a real value (e.g. Bonus Feat 0 = Alertness).
    subtype_str = ""
    if subtype_id is not None:
        st_tbl = pdef.get("subtype")
        if st_tbl:
            sval = _table_lookup(st_tbl, subtype_id)
            if sval is not None:
                subtype_str = sval
            else:
                short = st_tbl.replace("iprp_", "")
                subtype_str = f"{short} #{subtype_id}"
                _warn_once(f"item property '{pname}': {st_tbl} row {subtype_id} not found — add --2da-dir with an override to resolve")
        elif int(subtype_id) not in (0, 65535):
            subtype_str = str(subtype_id)
            _warn_once(f"item property '{pname}': subtype {subtype_id} has no lookup table in itemprops.json — itemprops.json may be outdated")

    # Cost: indexes a per-property cost table (e.g. +1..+20).
    # Only fall back to raw when a cost table is defined but the lookup misses
    # (e.g. custom HAK rows); when no cost table is configured at all, the raw
    # CostValue is an internal engine index with no user-visible meaning.
    cost_str = ""
    if cost_value is not None:
        ct_tbl = pdef.get("cost_table")
        if ct_tbl:
            cval = _table_lookup(ct_tbl, cost_value)
            if cval is not None:
                cost_str = cval
            elif ct_tbl == "iprp_chargecost":
                # Standard table ends at row 12 (5/Day); extend the /Day pattern.
                n = int(cost_value)
                cost_str = f"{n - 7}/Day" if n >= 8 else f"{n} charges"
            else:
                cost_str = str(cost_value)
                _warn_once(f"item property '{pname}': {ct_tbl} row {cost_value} not found — add --2da-dir with an override to resolve")

    # Param1: 255 is the engine "no param" sentinel.
    # For certain (property, subtype) combos the Param1Value further qualifies
    # the subtype — e.g. "Slay Racial Group" becomes "Slay Racial Group: Elf".
    # For properties with a direct param1_table (e.g. Light → iprp_lightcolor),
    # the param column shows the resolved label (e.g. "Green") instead of "9/4".
    param_str = ""
    if param1 is not None and int(param1) != 255:
        p1_for_sub = pdef.get("param1_for_subtype", {})
        p1_tbl = p1_for_sub.get(subtype_str) if subtype_str else None
        if p1_tbl and param1_value is not None:
            p1_label = _table_lookup(p1_tbl, int(param1_value))
            if p1_label:
                subtype_str = f"{subtype_str}: {p1_label}"
        else:
            direct_p1_tbl = pdef.get("param1_table")
            if direct_p1_tbl and param1_value is not None:
                p1_label = _table_lookup(direct_p1_tbl, int(param1_value))
                param_str = p1_label if p1_label is not None else f"{param1}/{param1_value}"
            else:
                param_str = f"{param1}/{param1_value if param1_value is not None else 0}"

    return {
        "property": pname,
        "subtype": subtype_str,
        "cost": cost_str,
        "param": param_str,
        "chance": "" if chance == 100 else str(chance),
    }


def _item_prop_key(props: list) -> tuple:
    """Stable hashable key for a PropertiesList, used to detect property variants."""
    parts = []
    for p in props:
        parts.append((
            fld(p, "PropertyName"),
            fld(p, "Subtype"),
            fld(p, "CostTable"),
            fld(p, "CostValue"),
            fld(p, "Param1"),
            fld(p, "Param1Value"),
        ))
    return tuple(sorted(parts))


def _creature_key(c: dict, bp: dict | None = None) -> tuple:
    """Stable fingerprint for a creature's combat identity.

    Compares abilities, classes, feats, special abilities, equipment resrefs,
    NaturalAC, and Race.  Deliberately excludes name, tag, faction,
    conversation, scripts, and position — those are presentation/placement
    details, not combat identity.

    `c` is the primary struct (UTC blueprint or GIT placement).
    `bp` is the blueprint to fall back to when `c` is a GIT instance,
    mirroring the fallback pattern in _creature_detail_sections().
    """
    def _f(key):
        v = fld(c, key)
        if v is None and bp is not None:
            v = fld(bp, key)
        return v

    def _lst(key):
        items = list_items(c.get(key))
        if not items and bp is not None:
            items = list_items(bp.get(key))
        return items

    abilities = tuple(_f(a) for a in ("Str", "Dex", "Con", "Int", "Wis", "Cha"))
    classes   = tuple(sorted(
        (fld(cl, "Class"), fld(cl, "ClassLevel"))
        for cl in _lst("ClassList")
    ))
    feats     = tuple(sorted(
        int(fld(f, "Feat"))
        for f in _lst("FeatList")
        if fld(f, "Feat") is not None
    ))
    spec_ab   = tuple(sorted(
        (fld(s, "Spell"), fld(s, "SpellCasterLevel"))
        for s in _lst("SpecAbilityList")
    ))
    equip     = tuple(sorted(
        (fld(e, "__struct_id"),
         fld(e, "EquippedRes", "") or fld(e, "TemplateResRef", "") or "")
        for e in _lst("Equip_ItemList")
    ))
    return (abilities, classes, feats, spec_ab, equip, _f("NaturalAC"), _f("Race"))


def _store_inv_key(store: dict) -> tuple:
    """Stable fingerprint of a UTM blueprint's inventory and pricing.

    Used to decide whether two stores with the same Tag are genuine duplicates
    (identical key) or conflicting variants (differing key).
    """
    items: list[tuple] = []
    for page in list_items(store.get("StoreList")):
        for it in list_items(page.get("ItemList")):
            rr = (fld(it, "TemplateResRef", "") or fld(it, "InventoryRes", "") or "").strip().lower()
            stack = fld(it, "StackSize", 1) or 1
            infinite = fld(it, "Infinite", 0) or 0
            items.append((rr, stack, infinite))
    return (
        tuple(sorted(items)),
        fld(store, "MarkUp", 0),
        fld(store, "MarkDown", 0),
        fld(store, "StoreGold", -1),
    )


def itemprop_oneliner(prop: dict) -> str:
    """Compact single-line summary (used in tooltips / loot lists)."""
    f = itemprop_format(prop)
    bits = [f["property"]]
    if f["subtype"]:
        bits.append(f["subtype"])
    if f["cost"]:
        bits.append(f["cost"])
    return " ".join(b for b in bits if b)


def _prop_slug(prop_name: str, subtype: str) -> str:
    s = (prop_name + ("-" + subtype if subtype else "")).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _prop_value_num(cost_str: str) -> int:
    m = re.search(r"\d+", cost_str.strip())
    return int(m.group()) if m else 0


def _prop_group(prop_name: str) -> str:
    n = prop_name.lower()
    if any(w in n for w in ("resistance", "immunity", "vulnerability", "reduction")):
        return "Resistances & Immunities"
    if "saving throw" in n:
        return "Saving Throws"
    if any(w in n for w in ("ability", "skill")):
        return "Ability & Skill Bonuses"
    if any(w in n for w in ("ac bonus", "attack bonus", "enhancement bonus", "damage bonus",
                             "damage penalty", "decreased damage", "massive criticals",
                             "extra melee", "extra ranged", "mighty")):
        return "AC & Combat"
    if any(w in n for w in ("spell", "cast", "bonus spell")):
        return "Spell & Magic"
    return "Other"


def _cost_anchor(cost_str: str) -> str:
    """Stable URL-fragment for a cost tier (e.g. 'DC 14' → 'dc-14')."""
    return re.sub(r"[^a-z0-9]+", "-", cost_str.lower()).strip("-")


def _cost_tiers(
    entries: list[tuple[str, str, str, int]],
) -> list[tuple[str, str, list[tuple[str, str, str, int]]]]:
    """Group a property's items by cost tier and return sorted (cost_str, anchor, items) triples.

    Tiers are sorted: numeric-descending first (so '+20' before '+1', 'DC 26' before 'DC 14'),
    then alphabetically for non-numeric tiers.  The empty-string tier (properties with no
    cost value) is placed last."""
    from collections import defaultdict as _dd
    buckets: dict[str, list] = _dd(list)
    for e in entries:
        buckets[e[2]].append(e)
    non_empty_keys = [k for k in buckets if k]
    _is_dr = bool(non_empty_keys) and all(
        re.match(r"^\d+/\+\d+$", k.strip()) for k in non_empty_keys
    )
    def _tier_key(x: tuple) -> tuple:
        c = x[0]
        if not c:
            return (1, 0, 0, "")
        if _is_dr:
            m = re.match(r"^(\d+)/\+(\d+)$", c.strip())
            if m:
                return (0, -int(m.group(2)), -int(m.group(1)), "")
        return (0, 0, -_prop_value_num(c), c.lower())
    result = []
    for cost_str, items in sorted(buckets.items(), key=_tier_key):
        result.append((cost_str, _cost_anchor(cost_str), items))
    return result


def _is_raw_subtype(subtype: str) -> bool:
    """True when subtype is an unresolved numeric fallback (e.g. '37' or 'spells #5').
    Used to merge property variants that share a name but differ only in a
    charge count or other numeric parameter the lookup table didn't resolve."""
    s = subtype.strip()
    return bool(re.match(r"^\d+$", s) or re.match(r"^[\w\s]+ #\d+$", s))


def _fmt_hp(val: Any) -> str:
    """Format HP with comma separators; returns '' for missing values."""
    if val in (None, ''):
        return ''
    try:
        return f"{int(val):,}"
    except (TypeError, ValueError):
        return str(val)


def _yn(flag: bool) -> str:
    """Color-coded Yes/No span for boolean flags in tables."""
    if flag:
        return '<span style="color:#4a8;font-weight:bold">Yes</span>'
    return '<span style="color:#888">No</span>'
