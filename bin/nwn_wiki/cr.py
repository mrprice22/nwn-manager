"""Reproduce the NWN toolset's creature challenge-rating formula.

The toolset stores a creature's CR in its blueprint (``ChallengeRating``) and
recomputes it ONLY when the creature's Properties dialog is opened and OK'd.
Nothing else refreshes it: not a module save, not a repack, not the server.
So a creature edited outside the toolset (by a script, or by hand in an
unpacked JSON tree) keeps a stale CR, and the CR feeds kill XP, encounter
budgets and anything else that reads ``GetChallengeRating``.  This module
computes what the toolset would store, so drift can be measured and fixed.

THE FORMULA (nwn.fandom.com/wiki/Challenge_rating), summed, then multiplied
by the race's ``CRModifier`` (racialtypes.2da):

    HD * 0.15
    NaturalAC * 0.1
    item value / (HD * 20000 + 100000) * 0.2 * HD        items NOT in creature slots
    base HP / average HP * 0.2 * HD * walk / PC walk     base HP = ``HitPoints`` (no Con, no feats)
    ability total / (HD + 50) * 0.1 * HD                  the raw Str..Cha fields
    special-ability innate levels / (HD + 6) * 0.15
    usable-spell innate levels / (HD + 1) * 0.15          level-0 spells count 0.5
    (innate save bonuses / class base saves + 1) * 0.15 * HD
    feat CR values / (HD * 0.5 + 7) * 0.1 * HD

CALIBRATED HERE against a module's stored CRs (``nwn-cr --fit``), because the
wiki leaves three things out:

  * ``CRAdjust`` (the toolset's "CR adjustment") is ADDED after the racial
    multiplier.  Additive fits 111 of 175 adjusted blueprints within 10% in
    Homer's LOTR; a percentage fits 8.
  * A result of 1 or more is ROUNDED to a whole number (84 matches vs 57 for
    floor or ceil among near hits); stored CRs >= 1 are always whole.
  * Below 1 the toolset stores a fraction, picked by the thresholds in
    ``FRACTIONS`` (read off the stored 1/6, 1/4, 1/3 and 1/2 creatures).
  * WalkRate 7 ("Default") takes the walk rate of the appearance's
    ``MOVERATE`` (appearance.2da -> creaturespeed.2da ``2DAName``).

Item value is ``Cost + AddCost`` (items.item_gp_value) counted ONCE per item,
whatever its stack size: module ``.uti`` files first, then the bundled stock
item costs.  Multiplying by StackSize made a 99-arrow quiver worth 99 items
and fitted worse (580 vs 614 of 825 blueprints within 10%); Legolas computed
to CR 180,363 on his arrows alone.  An item found in neither (a hak-only blueprint)
is counted as unresolved; such a creature's result is a lower bound.

Data: ``wiki_data/cr_info.json`` (stock tables, ``build_cr_info.py``) with a
module's own 2DAs (``--2da-dir``) layered over them row by row.

Stdlib + nwn_wiki.gff / twoda / items / paths only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from nwn_wiki.gff import fld
from nwn_wiki.items import item_gp_value
from nwn_wiki.paths import DATA_DIR
from nwn_wiki.twoda import parse_2da

# Equip_ItemList __struct_id is the slot bitmask; 1<<14 and up are the
# creature slots (claws, bite, creature hide), which the formula excludes.
CREATURE_SLOT_MIN = 1 << 14

# (upper bound of the computed CR, stored fraction) - below 1 the toolset
# stores one of these.  Read off this module's stored-fraction creatures:
# computed 0.51 -> 1/6, 0.56 -> 1/4, 0.64-0.67 -> 1/3, 0.78-0.92 -> 1/2.
FRACTIONS = ((0.45, 0.125), (0.535, 0.1667), (0.60, 0.25), (0.70, 0.3333), (0.95, 0.5))

TERMS = ("hd", "natural_ac", "items", "hit_points", "abilities", "special_abilities",
         "spells", "saves", "feats")


def _num(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


@dataclass
class Tables:
    hit_die: dict[int, int] = field(default_factory=dict)
    save_table: dict[int, str] = field(default_factory=dict)
    save_tables: dict[str, list] = field(default_factory=dict)
    race_mod: dict[int, float] = field(default_factory=dict)
    feat_cr: dict[int, float] = field(default_factory=dict)
    spell_innate: dict[int, float] = field(default_factory=dict)
    walk: dict[int, float] = field(default_factory=dict)
    walk_by_name: dict[str, float] = field(default_factory=dict)
    appearance_move: dict[int, str] = field(default_factory=dict)

    def base_saves(self, cls: int, level: int) -> int:
        tbl = self.save_tables.get(self.save_table.get(cls, ""), [])
        if not tbl or level < 1:
            return 0
        return sum(tbl[min(level, len(tbl)) - 1])


def _rows(path: Path):
    hdrs, rows = parse_2da(path)
    for row in rows:
        try:
            rid = int(row[0])
        except (ValueError, IndexError):
            continue
        yield rid, {h: row[i + 1] for i, h in enumerate(hdrs)
                    if i + 1 < len(row) and row[i + 1] != ""}


def load_tables(two_da_dir: Path | None = None) -> Tables:
    """Stock cr_info.json, then any of the module's own 2DAs in two_da_dir."""
    raw = json.loads((DATA_DIR / "cr_info.json").read_text(encoding="utf-8"))
    t = Tables()
    for k, v in raw["classes"].items():
        t.hit_die[int(k)] = v["hit_die"]
        if v.get("save_table"):
            t.save_table[int(k)] = v["save_table"]
    t.save_tables = dict(raw["save_tables"])
    t.race_mod = {int(k): v for k, v in raw["races"].items()}
    t.feat_cr = {int(k): v for k, v in raw["feats"].items()}
    t.spell_innate = {int(k): v for k, v in raw["spells"].items()}
    for k, v in raw["speeds"].items():
        t.walk[int(k)] = v["walk"]
        if v["name"]:
            t.walk_by_name[v["name"]] = v["walk"]
    t.appearance_move = {int(k): v for k, v in raw["appearance_move"].items()}
    if not two_da_dir or not Path(two_da_dir).is_dir():
        return t
    d = Path(two_da_dir)

    def over(name):
        p = d / name
        return _rows(p) if p.is_file() else ()

    for rid, c in over("classes.2da"):
        if "HitDie" in c:
            t.hit_die[rid] = int(_num(c["HitDie"], 8))
        if "SavingThrowTable" in c:
            t.save_table[rid] = c["SavingThrowTable"].lower()
    for tbl in set(t.save_table.values()):
        p = d / (tbl + ".2da")
        if p.is_file():
            lv = {}
            for _rid, c in _rows(p):
                n = int(_num(c.get("Level"), 0) or 0)
                if n > 0:
                    lv[n] = [int(_num(c.get(k), 0) or 0) for k in ("FortSave", "RefSave", "WillSave")]
            t.save_tables[tbl] = [lv.get(i, [0, 0, 0]) for i in range(1, max(lv, default=0) + 1)]
    for rid, c in over("racialtypes.2da"):
        if "CRModifier" in c:
            t.race_mod[rid] = _num(c["CRModifier"], 1.0)
    for rid, c in over("feat.2da"):
        t.feat_cr[rid] = _num(c.get("CRValue"), 0.0) or 0.0
    for rid, c in over("spells.2da"):
        if _num(c.get("Innate")) is not None:
            t.spell_innate[rid] = _num(c["Innate"])
    for rid, c in over("creaturespeed.2da"):
        t.walk[rid] = _num(c.get("WALKRATE"), 0.0)
        if c.get("2DAName"):
            t.walk_by_name[c["2DAName"].upper()] = t.walk[rid]
    for rid, c in over("appearance.2da"):
        if c.get("MOVERATE"):
            t.appearance_move[rid] = c["MOVERATE"].upper()
    return t


class ItemValues:
    """resref -> Cost + AddCost of one item (a stack counts once): module .uti
    first, then stock."""

    def __init__(self, unpacked: Path):
        self.unpacked = Path(unpacked)
        self._cache: dict[str, int | None] = {}
        p = DATA_DIR / "stock_item_names.json"
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        self._stock = {k.lower(): v for k, v in raw.items()
                       if not k.startswith("_") and isinstance(v, dict)}

    def __call__(self, resref: str) -> int | None:
        rr = (resref or "").lower()
        if not rr:
            return 0
        if rr not in self._cache:
            p = self.unpacked / f"{rr}.uti.json"
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                self._cache[rr] = item_gp_value(d)
            elif rr in self._stock:
                self._cache[rr] = int(self._stock[rr].get("cost", 0) or 0)
            else:
                self._cache[rr] = None
        return self._cache[rr]


@dataclass
class CRResult:
    cr: float                 # what the toolset would store
    additive: float           # the racial-multiplied sum, before CRAdjust
    cr_adjust: int
    race_mod: float
    terms: dict[str, float]   # each term BEFORE the racial multiplier
    hd: int
    item_value: int
    unresolved_items: list[str]
    top_item: tuple[str, int] | None   # the most valuable counted item

    @property
    def dominant(self) -> str:
        return max(self.terms, key=self.terms.get) if self.terms else ""


def store_value(x: float) -> float:
    """Map a raw CR to what the toolset stores: a fraction below 1, else whole."""
    if x >= FRACTIONS[-1][0]:
        return float(max(1, round(x)))
    for bound, frac in FRACTIONS:
        if x < bound:
            return frac
    return 0.5


def creature_cr(utc: dict, tables: Tables, item_value: ItemValues) -> CRResult:
    classes = [(int(fld(c, "Class", 0) or 0), int(fld(c, "ClassLevel", 0) or 0))
               for c in fld(utc, "ClassList", []) or []]
    hd = max(1, sum(lv for _, lv in classes))
    avg_hp = sum(lv * (tables.hit_die.get(c, 8) + 1) / 2.0 for c, lv in classes) or 1.0

    pc_walk = tables.walk.get(0, 2.0) or 2.0
    wr = int(fld(utc, "WalkRate", 0) or 0)
    if wr == 7:
        move = tables.appearance_move.get(int(fld(utc, "Appearance_Type", 0) or 0), "NORM")
        walk = tables.walk_by_name.get(move, tables.walk_by_name.get("NORM", 1.75))
    else:
        walk = tables.walk.get(wr, pc_walk)

    value, unresolved, top = 0, [], None

    def count(rr):
        nonlocal value, top
        v = item_value(rr)
        if v is None:
            unresolved.append(rr)
            return
        value += v
        if top is None or v > top[1]:
            top = (rr, v)

    for e in fld(utc, "Equip_ItemList", []) or []:
        if int(e.get("__struct_id", 0)) >= CREATURE_SLOT_MIN:
            continue
        count(fld(e, "EquippedRes", ""))
    for e in fld(utc, "ItemList", []) or []:
        count(fld(e, "InventoryRes", "") or fld(e, "TemplateResRef", ""))

    abil = sum(int(fld(utc, k, 0) or 0) for k in ("Str", "Dex", "Con", "Int", "Wis", "Cha"))

    def innate(spell_id):
        v = tables.spell_innate.get(int(spell_id or 0), 0.0) or 0.0
        return 0.5 if v == 0 else v

    spab = sum(innate(fld(s, "Spell", 0)) for s in fld(utc, "SpecAbilityList", []) or [])
    spells = 0.0
    for c in fld(utc, "ClassList", []) or []:
        for i in range(10):
            for key in ("KnownList%d" % i, "MemorizedList%d" % i):
                spells += sum(innate(fld(s, "Spell", 0)) for s in fld(c, key, []) or [])
    base_saves = sum(tables.base_saves(c, lv) for c, lv in classes)
    innate_saves = sum(int(fld(utc, k, 0) or 0) for k in ("fortbonus", "refbonus", "willbonus"))
    feat_cr = sum(tables.feat_cr.get(int(fld(f, "Feat", 0) or 0), 0.0)
                  for f in fld(utc, "FeatList", []) or [])

    terms = {
        "hd": hd * 0.15,
        "natural_ac": int(fld(utc, "NaturalAC", 0) or 0) * 0.1,
        "items": value / (hd * 20000.0 + 100000.0) * 0.2 * hd,
        "hit_points": int(fld(utc, "HitPoints", 0) or 0) / avg_hp * 0.2 * hd * walk / pc_walk,
        "abilities": abil / (hd + 50.0) * 0.1 * hd,
        "special_abilities": spab / (hd + 6.0) * 0.15,
        "spells": spells / (hd + 1.0) * 0.15,
        "saves": (innate_saves / max(1, base_saves) + 1) * 0.15 * hd,
        "feats": feat_cr / (hd * 0.5 + 7.0) * 0.1 * hd,
    }
    mod = tables.race_mod.get(int(fld(utc, "Race", 0) or 0), 1.0) or 1.0
    additive = sum(terms.values()) * mod
    adj = int(fld(utc, "CRAdjust", 0) or 0)
    return CRResult(cr=store_value(additive + adj), additive=additive, cr_adjust=adj,
                    race_mod=mod, terms=terms, hd=hd, item_value=value,
                    unresolved_items=unresolved, top_item=top)
