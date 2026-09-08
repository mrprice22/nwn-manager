"""Module-index: the resolved int → string lookup tables (``enums.json``).

Every other module-index file exports resolved names *per entity* --
``appearance_name`` on a creature, ``base_item`` on an item. Nothing exports the
lookup tables themselves, so anyone working on the module's scripts or
blueprints has to re-derive "``iprp_damagecost`` row 6 means 1d4" by hand, from
``hak_2da/`` or from memory.

This writer exports those tables, resolved for *this* module: the bundled
:mod:`nwn_wiki.lookups` data with the CEP overlay and the module's own 2DA
overrides already applied, each table stamped with which of those it came from.
Where the engine has a matching NWScript constant family, the constant names are
attached alongside, so a raw integer in a ``.uti`` can be read in either
direction -- to a label for a human, or to an ``IP_CONST_*`` name for a script.

Two files, identical in shape:

  enums.json        the compact core -- the iprp_* tables, the item-property
                    map, and the small id spaces. The everyday file.
  enums_names.json  the bulk name tables (feat, spells, appearance, placeables,
                    iprp_feats) split out purely so reading enums.json stays
                    cheap.

Depends only on lookups/itemprops -- it never touches ``Db`` -- so it is pure
and costs nothing to run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nwn_wiki import state
from nwn_wiki.items import SLOT_NAMES
from nwn_wiki.lookups import (
    APPEARANCE,
    BASEITEMS,
    CLASS_ABBREV,
    CLASSES,
    FEATS,
    IPROP_DEFS,
    IPROP_TABLES,
    IPRP_FEATS,
    NWSCRIPT_CONSTS,
    PLACEABLES,
    RACE_IMMUNITIES,
    RACES,
    SKILLS,
    SPELLS,
    TILESETS,
    table_source,
)
from nwn_wiki.render.quests import _QUEST_PRIORITY_LABELS
from nwn_wiki.util import _write_json


# ---------------------------------------------------------------------------
# What goes in which file
# ---------------------------------------------------------------------------

# The bulk name tables. Together they are ~3x the size of everything else, and
# an agent resolving a damage die or an immunity never needs them, so they live
# in their own file.
_BULK_TABLES = ("feat", "spells", "appearance", "placeables", "iprp_feats")

# name -> (dict, one-line doc). The iprp_* tables are added on top of these from
# IPROP_TABLES, with docs derived from the properties that reference them.
_NAMED_TABLES: dict[str, tuple[dict, str]] = {
    "baseitems":    (BASEITEMS,    "baseitems.2da rows — an item's BaseItem field"),
    "classes":      (CLASSES,      "classes.2da rows — a creature's ClassList Class field"),
    "class_abbrev": (CLASS_ABBREV, "Short class forms, keyed like classes (curated, not from a 2DA)"),
    "racialtypes":  (RACES,        "racialtypes.2da rows — a creature's Race field"),
    "skills":       (SKILLS,       "skills.2da rows — index into a creature's SkillList"),
    "feat":         (FEATS,        "feat.2da rows — a creature's FeatList Feat field"),
    "spells":       (SPELLS,       "spells.2da rows — SPELL_* in NWScript"),
    "appearance":   (APPEARANCE,   "appearance.2da rows — a creature's Appearance_Type field"),
    "placeables":   (PLACEABLES,   "placeables.2da rows — a placeable's Appearance field"),
    "iprp_feats":   (IPRP_FEATS,   "iprp_feats.2da rows — the Bonus Feat property's subtype "
                                   "(its own row space, NOT feat.2da rows)"),
}


# ---------------------------------------------------------------------------
# NWScript constant families
# ---------------------------------------------------------------------------

# Constant family -> the table whose rows its values index. Curated, and checked
# again at build time (see _attach_consts) so a HAK that renumbers a table drops
# the join instead of shipping names that no longer fit.
#
# Only families that genuinely index a 2DA row belong here. The families that
# look like they do but do not are the dangerous ones; they are named in
# _CONST_TRAPS below so the exported file says so out loud.
_CONST_TABLE: dict[str, str] = {
    "APPEARANCE_TYPE":               "appearance",
    "BASE_ITEM":                     "baseitems",
    "CLASS_TYPE":                    "classes",
    "FEAT":                          "feat",
    "IP_CONST_ABILITY":              "iprp_abilities",
    "IP_CONST_ALIGNMENT":            "iprp_alignment",
    "IP_CONST_ALIGNMENTGROUP":       "iprp_aligngrp",
    "IP_CONST_AMMOTYPE":             "iprp_ammotype",
    "IP_CONST_ARCANE_SPELL_FAILURE": "iprp_arcspell",
    "IP_CONST_CASTSPELL":            "iprp_spells",
    "IP_CONST_CASTSPELL_NUMUSES":    "iprp_chargecost",
    "IP_CONST_CLASS":                "classes",
    "IP_CONST_DAMAGEBONUS":          "iprp_damagecost",
    "IP_CONST_DAMAGEIMMUNITY":       "iprp_immuncost",
    "IP_CONST_DAMAGEREDUCTION":      "iprp_softcost",
    "IP_CONST_DAMAGERESIST":         "iprp_damageresist",
    "IP_CONST_DAMAGETYPE":           "iprp_damagetype",
    "IP_CONST_FEAT":                 "iprp_feats",
    "IP_CONST_IMMUNITYMISC":         "iprp_immunity",
    "IP_CONST_LIGHTBRIGHTNESS":      "iprp_lightcost",
    "IP_CONST_LIGHTCOLOR":           "iprp_lightcolor",
    "IP_CONST_MONSTERDAMAGE":        "iprp_monstcost",
    "IP_CONST_ONHIT":                "iprp_onhit",
    "IP_CONST_ONHIT_CASTSPELL":      "iprp_onhitspell",
    "IP_CONST_ONHIT_SAVEDC":         "iprp_onhitcost",
    "IP_CONST_RACIALTYPE":           "racialtypes",
    "IP_CONST_SAVEBASETYPE":         "iprp_savingthrow",
    "IP_CONST_SPELLLEVEL":           "iprp_spelllevel",
    "IP_CONST_SPELLRESISTANCEBONUS": "iprp_srcost",
    "IP_CONST_WEIGHTINCREASE":       "iprp_weightinc",
    "RACIAL_TYPE":                   "racialtypes",
    "SKILL":                         "skills",
    "SPELL":                         "spells",
}


# Families whose values look joinable but are not. Each is exported under
# nwscript_constants with this warning attached, and never joined to a table.
_CONST_TRAPS: dict[str, str] = {
    "DAMAGE_TYPE":
        "Engine damage BITMASK (FIRE=256), used by EffectDamage and the DAMAGE_TYPE_* "
        "fields on effects. NOT an iprp_damagetype row — the item property Fire is row 10.",
    "INVENTORY_SLOT":
        "Slot INDEX 0-17 for GetItemInSlot(). The equippable-slot bitmask in "
        "baseitems.2da is a different space — see the inventory_slot_mask table.",
    "IMMUNITY_TYPE":
        "IMMUNITY_TYPE_* for EffectImmunity(). NOT iprp_immunity rows, which are the "
        "item property's own subtype space.",
    "SAVING_THROW":
        "SAVING_THROW_FORT/REFL/WILL for EffectSavingThrowIncrease(). "
        "Overlaps iprp_savingthrow numerically by coincidence only.",
    "ALIGNMENT":
        "ALIGNMENT_* for GetAlignmentGoodEvil(). NOT iprp_alignment rows.",
    "SPELL_SCHOOL":
        "The spells.2da School column (GENERAL=0). Off by one against iprp_school, "
        "whose row 0 is Abjuration — verified, do not join the two.",
    "ABILITY":
        "ABILITY_* for GetAbilityScore(). Happens to match iprp_abilities row-for-row, "
        "but is a separate space — do not rely on it.",
}

# Numeric overlap alone proves nothing: SPELL_SCHOOL_* and iprp_school both run
# 0..8 and are off by one from each other. Two things guard the curated map.
#
#   coverage      — the share of the family's constants that hit a real row.
#                   Gating. Below the threshold the two row spaces have plainly
#                   diverged (a HAK renumbering) and the join is dropped.
#   agreement     — the share of joined rows whose constant SUFFIX and row LABEL
#                   say the same thing. Reported, NOT gating: labels here are
#                   curated human strings ("Strength", "80% reduction") while
#                   suffixes are engine shorthand ("STR", "80_PERCENT"), and a
#                   module HAK relabels feat.2da wholesale — so a low score is
#                   often style, not error. It is surfaced as _const_agreement so
#                   a reader can weigh the join instead of trusting it blindly.
#
# A family proven to index something else belongs in _CONST_TRAPS, not here.
#
# Deliberately absent, pending a data fix rather than a mapping fix:
# IP_CONST_SAVEVS_*, IP_CONST_TRAPTYPE_* and IP_CONST_DAMAGEVULNERABILITY_*.
# Each lands exactly one row off its bundled table (SAVEVS_UNIVERSAL=0 on a
# table whose row 0 is "Acid"), which says the bundled table is shifted, not
# that the constants are wrong — and iprp_traps is shakier still: the stock 2DA
# of that name holds trap STRENGTHS (Minor/Average/Strong/Deadly/Epic) while
# itemprops.json fills it with trap TYPES. Joining any of them would dress up
# bad rows in authoritative-looking names.
_CONST_COVERAGE_THRESHOLD = 0.75

# Word forms spelled out in a constant suffix but written as a symbol in a label.
_NORM_WORDS = [("_PER_", "/"), ("PERCENT", "%"), ("PLUS", "+"), ("MINUS", "-")]


def _norm_label(text: str) -> str:
    """Squash a constant suffix or a row label to a comparable form."""
    out = text.upper()
    for a, b in _NORM_WORDS:
        out = out.replace(a, b)
    return "".join(ch for ch in out if ch.isalnum() or ch in "+-%/")


def _names_agree(suffix: str, label: str) -> bool:
    """Whether a constant suffix and a row label plausibly name the same thing.

    Containment either way, because the two conventions differ in verbosity:
    "80_PERCENT" vs "80% reduction", "LEVEL_1" vs "Level 1"."""
    a, b = _norm_label(suffix), _norm_label(label)
    if not a or not b:
        return False
    return a in b or b in a


def _attach_consts(table_name: str, rows: dict[str, str]) -> tuple[dict[str, str], str | None, str | None, str | None]:
    """Return (consts, family, agreement, note) for one table.

    ``consts`` maps row -> NWScript constant name for every constant in the
    mapped family whose value is a row of this table. A row with two constants
    pointing at it keeps the first by name order, so the output is stable."""
    if not NWSCRIPT_CONSTS:
        return {}, None, None, None

    # A table can have more than one candidate family: racialtypes is indexed by
    # both RACIAL_TYPE_* and the narrower IP_CONST_RACIALTYPE_*, classes by both
    # CLASS_TYPE_* and IP_CONST_CLASS_*. Score them and take the widest, so the
    # choice is a decision rather than an accident of dict literal order.
    candidates = [f for f, t in sorted(_CONST_TABLE.items()) if t == table_name
                  and NWSCRIPT_CONSTS.get(f)]
    if not candidates:
        return {}, None, None, None

    def _score(f: str) -> tuple[int, str]:
        members = NWSCRIPT_CONSTS[f]
        return (sum(1 for v in members.values() if str(v) in rows), f)

    family = max(candidates, key=_score)
    members = NWSCRIPT_CONSTS[family]

    hits: dict[str, tuple[str, str]] = {}   # row -> (suffix, constant name)
    for suffix, value in sorted(members.items()):
        key = str(value)
        if key in rows and key not in hits:
            hits[key] = (suffix, f"{family}_{suffix}")

    if len(hits) / len(members) < _CONST_COVERAGE_THRESHOLD:
        note = (f"NWScript family {family}_* was not attached: only {len(hits)} of "
                f"{len(members)} constants land on a row of this table, so the two "
                f"row spaces have diverged (a HAK renumbering, most likely).")
        state._module_index_summary.append(
            ("warn", f"[nwn-wiki] module-index: enums.json — {family}_* not joined to "
                     f"{table_name} ({len(hits)}/{len(members)} rows matched)"))
        return {}, None, None, note

    agreed = sum(1 for row, (suffix, _) in hits.items()
                 if _names_agree(suffix, rows[row]))
    agreement = (f"{agreed} of {len(hits)} joined rows corroborate by name "
                 f"(the rest differ in naming style, not necessarily in meaning)")
    if agreed == 0 and len(hits) > 3:
        state._module_index_summary.append(
            ("warn", f"[nwn-wiki] module-index: enums.json — {family}_* joined to "
                     f"{table_name} on row numbers alone; no row corroborates by name"))
    consts = {row: name for row, (_, name) in sorted(hits.items(), key=lambda kv: int(kv[0]))}
    return consts, family, agreement, None


# ---------------------------------------------------------------------------
# Table assembly
# ---------------------------------------------------------------------------

def _iprp_docs() -> dict[str, str]:
    """Derive a one-line doc per iprp table by inverting IPROP_DEFS.

    Computed rather than hand-written so the docs stay true as itemprops.json
    grows: a table's doc is the properties that reference it, and in which
    field, which is exactly what a reader needs to know to use it."""
    used: dict[str, dict[str, list[str]]] = {}
    for pdef in IPROP_DEFS.values():
        name = pdef.get("name") or "?"
        for field, label in (("subtype", "subtype of"),
                             ("cost_table", "CostValue of"),
                             ("param1_table", "Param1 of")):
            tbl = pdef.get(field)
            if tbl:
                used.setdefault(tbl, {}).setdefault(label, []).append(name)

    docs: dict[str, str] = {}
    for tbl, by_field in used.items():
        parts = []
        for label in ("subtype of", "CostValue of", "Param1 of"):
            props = sorted(set(by_field.get(label, [])))
            if not props:
                continue
            shown = ", ".join(props[:4])
            if len(props) > 4:
                shown += f", +{len(props) - 4} more"
            parts.append(f"{label} {shown}")
        docs[tbl] = "; ".join(parts)
    return docs


def _rows(d: dict) -> dict[str, str]:
    """Normalise a lookup dict to {row: label}, numerically sorted.

    Accepts both the int-keyed dicts (BASEITEMS et al.) and the str-keyed iprp
    tables, and drops the ``_format``/``_source`` metadata keys the latter may
    carry — those are re-exported as real fields instead."""
    out: list[tuple[int, str]] = []
    for k, v in d.items():
        if isinstance(k, str) and k.startswith("_"):
            continue
        if not isinstance(v, str):
            continue
        try:
            out.append((int(k), v))
        except (TypeError, ValueError):
            continue
    return {str(k): v for k, v in sorted(out)}


def _build_table(name: str, raw: dict, doc: str) -> dict[str, Any]:
    rows = _rows(raw)
    consts, family, agreement, note = _attach_consts(name, rows)
    entry: dict[str, Any] = {
        "_doc": doc,
        "_source": table_source(name),
        "row_count": len(rows),
    }
    fmt = raw.get("_format") if isinstance(raw, dict) else None
    if isinstance(fmt, str):
        # A pure progression table (+1, +2, ...) that lists no rows: the value
        # IS the number, formatted. Without this the table would look empty.
        entry["_value_format"] = fmt
        entry["_doc"] += (f" — has no per-row entries: the raw value is substituted "
                          f"into '{fmt}' directly")
    if family:
        entry["_const_prefix"] = f"{family}_"
    if agreement:
        entry["_const_agreement"] = agreement
    if note:
        entry["_const_note"] = note
    entry["rows"] = rows
    if consts:
        entry["consts"] = consts
    return entry


def _all_tables() -> dict[str, dict[str, Any]]:
    iprp_docs = _iprp_docs()
    tables: dict[str, dict[str, Any]] = {}
    for name, (raw, doc) in _NAMED_TABLES.items():
        tables[name] = _build_table(name, raw, doc)
    for name, raw in IPROP_TABLES.items():
        if name in tables:
            continue  # iprp_feats is a lookup dict, already handled above
        doc = iprp_docs.get(name) or "referenced by no property in itemprops.json"
        tables[name] = _build_table(name, raw, doc)

    # The equippable-slot bitmask. Deliberately a separate table from the
    # INVENTORY_SLOT_* constants: these are powers of two (baseitems.2da
    # EquipableSlots), those are indices 0-17.
    tables["inventory_slot_mask"] = {
        "_doc": "baseitems.2da EquipableSlots bits. A base item's value is the OR of "
                "every slot it fits. NOT the INVENTORY_SLOT_* indices — see "
                "nwscript_constants.INVENTORY_SLOT.",
        "_source": "stock",
        "row_count": len(SLOT_NAMES),
        "rows": {str(k): v for k, v in sorted(SLOT_NAMES.items())},
    }
    tables["quest_priority"] = {
        "_doc": "Journal category Priority field (toolset).",
        "_source": "stock",
        "row_count": len(_QUEST_PRIORITY_LABELS),
        "rows": {str(k): v for k, v in sorted(_QUEST_PRIORITY_LABELS.items())},
    }
    return tables


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

_CORE_DESCRIPTION = (
    "Integer -> string lookup tables resolved for this module: the stock NWN "
    "tables with the CEP overlay and this module's own 2DA overrides already "
    "applied. Each table's _source says which. Use this to read a raw integer "
    "out of an unpacked .uti/.utc/.utp instead of re-deriving it from "
    "hak_2da/*.2da. To read an item property: look up PropertyName in "
    "item_properties, then resolve Subtype through its 'subtype' table and "
    "CostValue through its 'cost_table'. 'consts' gives the NWScript constant "
    "name for a row where the engine has one."
)


def _envelope(now: str, module_title: str, description: str,
              tables: dict[str, dict]) -> dict[str, Any]:
    return {
        "generated_at": now,
        "module": module_title,
        "_description": description,
        "table_count": len(tables),
    }


def write_enums(module_index_dir: Path, module_title: str, now: str) -> None:
    """Write enums.json and enums_names.json."""
    tables = _all_tables()
    bulk = {k: v for k, v in sorted(tables.items()) if k in _BULK_TABLES}
    core = {k: v for k, v in sorted(tables.items()) if k not in _BULK_TABLES}

    core_doc = dict(_envelope(now, module_title, _CORE_DESCRIPTION, core))
    core_doc["see_also"] = {
        "enums_names.json": "the bulk name tables — " + ", ".join(sorted(bulk)),
    }
    core_doc["tables"] = core
    core_doc["item_properties"] = {
        str(pid): {
            "name": pdef.get("name"),
            "subtype": pdef.get("subtype"),
            "cost_table": pdef.get("cost_table"),
            "param1_table": pdef.get("param1_table"),
            "index_by_subtype_only": bool(pdef.get("index_by_subtype_only")),
        }
        for pid, pdef in sorted(IPROP_DEFS.items())
    }
    core_doc["nwscript_constants"] = _nwscript_section()
    core_doc["tilesets"] = dict(sorted(TILESETS.items()))
    core_doc["race_immunities"] = {
        str(row): list(labels) for row, labels in sorted(RACE_IMMUNITIES.items())
    }
    _write_json(module_index_dir / "enums.json", core_doc)

    bulk_doc = dict(_envelope(
        now, module_title,
        "The bulk name tables split out of enums.json (see that file first — it "
        "holds the item-property tables and the reading instructions). Same shape.",
        bulk))
    bulk_doc["see_also"] = {"enums.json": "the item-property tables and the small id spaces"}
    bulk_doc["tables"] = bulk
    _write_json(module_index_dir / "enums_names.json", bulk_doc)

    n_consts = sum(1 for t in tables.values() if t.get("consts"))
    n_over = sum(1 for t in tables.values() if t["_source"] != "stock")
    state._module_index_summary.append((
        "info",
        f"[nwn-wiki] module-index: enums.json ({len(core)} tables, {n_over} "
        f"module-overridden, {n_consts} with NWScript constants) "
        f"+ enums_names.json ({len(bulk)} tables)"))


def _nwscript_section() -> dict[str, Any]:
    """The constant families that are NOT joined to a table.

    Families already attached to a table are omitted — they are in that table's
    ``consts`` map, where the row they belong to is visible."""
    if not NWSCRIPT_CONSTS:
        return {}
    joined = set(_CONST_TABLE)
    out: dict[str, Any] = {}
    for family in sorted(NWSCRIPT_CONSTS):
        if family in joined:
            continue
        entry: dict[str, Any] = {}
        if family in _CONST_TRAPS:
            entry["_doc"] = _CONST_TRAPS[family]
        entry.update({k: v for k, v in sorted(NWSCRIPT_CONSTS[family].items())})
        out[family] = entry
    return out
