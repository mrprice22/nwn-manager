"""Gear pool and kit solver for the counter-gear report.

Buckets every player-attainable item into the inventory slots it can fill,
prunes each slot to the plausible winners, and greedily solves for the
strongest kit the reference PC can field against a creature -- then strips
that kit down to the cheapest gear that still wins the fight.

Depends only on stdlib, items, lookups and the sim package -- nothing here may
touch Db's renderers or E().
"""

from __future__ import annotations

from typing import Callable

from nwn_wiki.items import (
    PLAYER_SLOTS,
    PLAYER_SLOT_MASK,
    SHIELD_BASEITEMS,
    _item_accessible,
    extract_item_defense,
    extract_item_offense,
    item_equip_slots,
    weapon_damage_props,
)
from nwn_wiki.lookups import WEAPONS
from nwn_wiki.sim.combat import simulate
from nwn_wiki.sim.pc import _kit_pieces, reference_pc
from nwn_wiki.util import _try_int


# ---------------------------------------------------------------------------
# Gear pool + kit solver.
# ---------------------------------------------------------------------------

def build_gear_pool(db: "Db") -> dict[str, list[dict]]:
    """Bucket every player-attainable item by the slots it can fill.

    A piece carries its blueprint plus the pre-computed offense/defense
    extracts, so the solver never re-parses item properties. Items appear under
    every slot they fit (a longsword under both hands). Right hand takes weapons
    only; left hand takes weapons and shields.
    """
    pool: dict[str, list[dict]] = {key: [] for key, _, _, _ in PLAYER_SLOTS}
    for rr in sorted(db.items):
        if not _item_accessible(db, rr):
            continue
        item = db.items[rr]
        slots = item_equip_slots(item) & PLAYER_SLOT_MASK
        if not slots:
            continue
        off = extract_item_offense(db, item, rr)
        if off["name"].startswith("[TLK#") or off["name"] == rr:
            continue                              # broken/unnamed blueprint
        d = extract_item_defense(db, item, rr)
        piece = {
            "resref": rr, "name": off["name"], "cost": off["cost"],
            "slots": slots, "item": item, "off": off, "def": d,
            "is_weapon": off["is_weapon"],
        }
        is_shield = off["base_item_id"] in SHIELD_BASEITEMS
        for key, _label, mask, _n in PLAYER_SLOTS:
            if not (slots & mask):
                continue
            if key == "right":
                keep = off["is_weapon"]
            elif key == "left":
                keep = off["is_weapon"] or is_shield
            else:
                keep = d["relevant"]
            if keep:
                pool[key].append(piece)
    return pool


def _prune_pool(pool: dict[str, list[dict]], per_axis: int = 6) -> dict[str, list[dict]]:
    """Cut each slot down to the items that could plausibly be the best pick.

    An exhaustive search over thousands of items is pointless: for any given
    axis (AC, a save, resistance to one damage type, raw damage, price) only the
    leaders can ever win. Keep the top `per_axis` on every axis and union them —
    a few dozen candidates per slot instead of hundreds, with no realistic
    chance of dropping the item the solver would have chosen.
    """
    out: dict[str, list[dict]] = {}
    for key, pieces in pool.items():
        axes: list[Callable[[dict], float]] = [
            lambda p: p["def"]["ac_bonus"],
            lambda p: p["def"]["regen"],
            lambda p: p["def"]["dr_soak"],
            lambda p: -p["cost"],                       # cheapest
            lambda p: max(p["def"]["abilities"].values(), default=0),
            lambda p: p["def"]["abilities"].get("Str", 0),
            lambda p: p["def"]["abilities"].get("Con", 0),
            lambda p: p["def"]["abilities"].get("Dex", 0),
        ]
        axes += [(lambda p, _k=k: p["def"]["saves"].get(_k, 0))
                 for k in ("Fortitude", "Reflex", "Will", "Universal")]
        dtypes = {t for p in pieces for t in
                  set(p["def"]["resist"]) | set(p["def"]["immune"])}
        axes += [(lambda p, _t=t: max(p["def"]["immune"].get(_t, 0),
                                      p["def"]["resist"].get(_t, 0))) for t in sorted(dtypes)]
        if key in ("right", "left"):
            axes += [
                lambda p: p["off"]["enhancement"],
                lambda p: p["off"]["attack_bonus"],
                lambda p: _weapon_raw_damage(p),
                lambda p: len(p["off"]["damage_dtypes"]),
            ]
        keep: dict[str, dict] = {}
        for axis in axes:
            for p in sorted(pieces, key=axis, reverse=True)[:per_axis]:
                keep[p["resref"]] = p
        # Plus a price ladder: evenly spaced samples across the slot's whole
        # cost range. Without it every candidate is either top-end or bargain-
        # bin, and the "cheapest kit that still wins" pass has nothing to step
        # down to — it would report a 2M gp sword as the minimum for a CR 3 orc
        # simply because no mid-priced weapon survived pruning.
        by_cost = sorted(pieces, key=lambda p: (p["cost"], p["resref"]))
        rungs = min(len(by_cost), per_axis * 3)
        for i in range(rungs):
            p = by_cost[i * (len(by_cost) - 1) // max(1, rungs - 1)]
            keep[p["resref"]] = p
        out[key] = sorted(keep.values(), key=lambda p: p["resref"])
    return out


def _weapon_raw_damage(piece: dict) -> float:
    """Average un-mitigated damage of a weapon piece — a pruning heuristic."""
    stats = WEAPONS.get(piece["off"]["base_item_id"]) or {}
    n, d = _try_int(stats.get("NumDice"), 0), _try_int(stats.get("DieToRoll"), 0)
    flat, elem = weapon_damage_props(piece["item"])
    return n * (d + 1) / 2.0 + flat + sum(elem.values())


def _kit_conflicts(kit: dict, key: str, piece: dict) -> bool:
    """True when installing `piece` in `key` is illegal for the current kit.

    The only rule that matters here: a two-handed weapon leaves no hand for a
    shield or off-hand weapon.
    """
    def _two_handed(p: dict | None) -> bool:
        if not p or not p["is_weapon"] or p["off"]["is_ranged"]:
            return False
        return _try_int((WEAPONS.get(p["off"]["base_item_id"]) or {}).get("WeaponSize"), 0) >= 4

    if key == "left":
        return _two_handed(kit.get("right"))
    if key == "right" and _two_handed(piece):
        return bool(kit.get("left"))
    return False


def best_in_slot_kit(level: int, creature: dict, pool: dict[str, list[dict]],
                     db: "Db") -> tuple[dict, dict]:
    """The strongest kit the reference PC can field against `creature`.

    Returns (kit, sim). One pass over the slots in a fixed order, taking the
    candidate that most improves simulate()'s score given what is already
    equipped — greedy, but the slots barely interact so it lands on the same
    answer a full search would, at a fraction of the cost. Cost is ignored: this
    is the ceiling, not the shopping list (see minimum_viable_kit).
    """
    kit: dict[str, dict | None] = {}

    def _sim(k: dict) -> dict:
        return simulate(reference_pc(level, k, db), creature)

    for key, _label, _mask, count in PLAYER_SLOTS:
        for _ in range(count):
            best_piece, best_score = None, _sim(kit)["score"]
            already = {p["resref"] for p in _kit_pieces(kit)}
            for piece in pool.get(key, ()):
                if piece["resref"] in already or _kit_conflicts(kit, key, piece):
                    continue
                trial = dict(kit)
                existing = trial.get(key)
                trial[key] = ((existing if isinstance(existing, list) else [existing])
                              + [piece]) if existing and count > 1 else piece
                score = _sim(trial)["score"]
                if score > best_score + 1e-9:
                    best_piece, best_score = piece, score
            if best_piece is None:
                break
            existing = kit.get(key)
            kit[key] = ((existing if isinstance(existing, list) else [existing])
                        + [best_piece]) if existing and count > 1 else best_piece

    return kit, _sim(kit)


def minimum_viable_kit(level: int, creature: dict, pool: dict[str, list[dict]],
                       db: "Db", start_kit: dict) -> tuple[dict, dict]:
    """Strip `start_kit` (a best_in_slot_kit result) down to the cheapest gear
    that still beats `creature`.

    Every slot is first tested empty — if the fight is still won, the slot was
    never needed — and otherwise re-filled with the cheapest candidate that
    keeps the win. This is what separates "the minimum kit that actually beats
    this creature" from the old report's "cheapest item that isn't literally
    useless", which is how every tier ended up recommending a 2 gp club.

    Returns the input kit unchanged when it does not win in the first place.
    """
    kit = dict(start_kit)

    def _sim(k: dict) -> dict:
        return simulate(reference_pc(level, k, db), creature)

    sim = _sim(kit)
    if not sim["wins"]:
        return kit, sim

    for key, _label, _mask, _count in PLAYER_SLOTS:
        if not kit.get(key):
            continue
        held = kit[key]
        trial = dict(kit)
        trial[key] = None
        if _sim(trial)["wins"]:
            kit[key] = None                       # the slot was never needed
            continue
        # Needed, but maybe a cheaper item does the job.
        current_cost = sum(p["cost"] for p in (held if isinstance(held, list) else [held]))
        for piece in sorted(pool.get(key, ()), key=lambda p: (p["cost"], p["resref"])):
            if piece["cost"] >= current_cost:
                break
            if _kit_conflicts(trial, key, piece):
                continue
            cand = dict(kit)
            cand[key] = piece
            if _sim(cand)["wins"]:
                kit[key] = piece
                break
    return kit, _sim(kit)
