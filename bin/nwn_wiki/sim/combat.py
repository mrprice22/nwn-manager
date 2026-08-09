"""Expected-value combat simulation for the counter-gear report.

One damage model for both sides of a fight: an attack profile (iterative
to-hit schedule, damage dice, criticals, elemental damage) is resolved against
a defence profile (AC, hit points, damage reduction, resistances, immunities,
regeneration, saves) to give damage per round, rounds to a kill, and the
scalar score the kit solver maximises.

Pure stdlib -- no ``Db``, no wiki state, no renderers. The weapon-side inputs
are built by the caller in :mod:`nwn_wiki.cli`.
"""

from __future__ import annotations

import math
import re
from typing import Iterable


# ---------------------------------------------------------------------------
# Combat simulation primitives.
#
# One damage model, used for both sides of a fight: the creature swinging at the
# reference PC and the reference PC swinging back. Everything here is expected
# value (no dice are rolled) — the question the report answers is "does this kit
# win", not "what happened in one particular fight".
# ---------------------------------------------------------------------------

_DICE_RE = re.compile(r"^\s*(\d+)\s*d\s*(\d+)\s*$", re.I)


def avg_roll(cost_str: str) -> float:
    """Average value of an iprp cost label. '1d6' → 3.5, '2d10' → 11.0, '5' → 5.0.

    Note this is NOT _prop_value_num, which pulls the first integer out of the
    string and would read '1d6' as 1 — fine for '+N' tables, wrong for damage.
    """
    if not cost_str:
        return 0.0
    m = _DICE_RE.match(cost_str)
    if m:
        n, d = int(m.group(1)), int(m.group(2))
        return n * (d + 1) / 2.0
    m = re.search(r"\d+", cost_str)
    return float(m.group()) if m else 0.0


def hit_chance(ab: int, ac: int) -> float:
    """Probability a single attack lands. A natural 1 always misses and a
    natural 20 always hits, so the result is clamped to [0.05, 0.95]."""
    return min(0.95, max(0.05, (21 - (ac - ab)) / 20.0))


def attack_profile(schedule: list[int], *, num_dice: int, die: int,
                   flat: float, crit_threat: int, crit_mult: int,
                   phys_types: Iterable[str], elem: dict[str, float],
                   enhancement: int, crit_bonus: float = 0.0,
                   devcrit_save_dc: int = 0) -> dict:
    """Everything the simulator needs about one weapon in one wielder's hands.

    schedule     iterative to-hit bonuses (from attack_schedule)
    num_dice/die base weapon damage dice
    flat         flat physical damage added (Str mod, Damage Bonus props, feats)
    crit_threat  threat range width in natural-roll numbers (2 ⇒ 19-20)
    crit_mult    critical multiplier
    phys_types   physical damage types the weapon can deal; the engine uses the
                 attacker-favourable one, so the simulator mitigates with the
                 *least* resisted of them
    elem         extra non-physical damage, average per hit, keyed by type
    enhancement  weapon enhancement, for damage-reduction bypass
    crit_bonus   average extra *physical* damage on a confirmed critical only,
                 from Overwhelming Critical and (where the module replaces the
                 engine's save-or-die) Devastating Critical. Not multiplied by
                 the crit multiplier — these are flat extra dice.
    devcrit_save_dc  when non-zero, a confirmed critical forces a Fortitude save
                 at this DC or the target dies outright — the stock Devastating
                 Critical. Zero when the base item has no devastating-critical
                 feat, which is how a module disables the mechanic wholesale.
    """
    return {
        "schedule": list(schedule),
        "num_dice": max(0, num_dice), "die": max(0, die),
        "flat": flat,
        "crit_threat": max(1, crit_threat), "crit_mult": max(1, crit_mult),
        "phys_types": sorted(set(phys_types)),
        "elem": dict(elem),
        "enhancement": enhancement,
        "crit_bonus": max(0.0, crit_bonus),
        "devcrit_save_dc": max(0, devcrit_save_dc),
    }


def defense_profile(*, ac: int, hp: int, dr_soak: int = 0, dr_bypass: int = 0,
                    resist: dict[str, int] | None = None,
                    immune: dict[str, int] | None = None,
                    regen: int = 0, fort: int = 0, ref: int = 0,
                    will: int = 0, crit_immune: bool = False) -> dict:
    """The receiving end of a fight: what has to be chewed through.

    `crit_immune` suppresses critical hits outright — no multiplier, no
    Overwhelming Critical damage, no Devastating Critical — which is the whole
    point of the property.
    """
    return {
        "ac": ac, "hp": max(1, hp),
        "dr_soak": dr_soak, "dr_bypass": dr_bypass,
        "resist": dict(resist or {}), "immune": dict(immune or {}),
        "regen": regen, "fort": fort, "ref": ref, "will": will,
        "crit_immune": crit_immune,
    }


def _mitigate(amount: float, dtype: str, dfn: dict, *, physical: bool,
              enhancement: int) -> float:
    """Apply one defender's mitigation to `amount` damage of type `dtype`.

    Order matches the engine: percentage immunity first, then flat damage
    resistance, then (physical only, and only when the attacker's enhancement
    is below the bypass) damage reduction. Never goes below zero.
    """
    if amount <= 0:
        return 0.0
    pct = dfn["immune"].get(dtype, 0)
    if pct >= 100:
        return 0.0
    if pct:
        amount *= (1 - pct / 100.0)
    amount -= dfn["resist"].get(dtype, 0)
    if physical and dfn["dr_soak"] and enhancement < dfn["dr_bypass"]:
        amount -= dfn["dr_soak"]
    return max(0.0, amount)


def _hit_damage(att: dict, dfn: dict, *, crit: bool) -> float:
    """Post-mitigation damage from one landed hit.

    Critical hits multiply the *physical* portion only — NWN does not multiply
    item-property elemental damage. Mitigation is applied per hit rather than to
    the round's expectation, because flat resistance and DR soak each attack
    separately (applying them to an average would understate a many-small-hits
    defence).
    """
    phys = att["num_dice"] * (att["die"] + 1) / 2.0 + att["flat"]
    phys = max(0.0, phys)
    if crit:
        # The multiplier applies to the weapon's own damage; the crit-feat dice
        # are added afterwards, not multiplied.
        phys = phys * att["crit_mult"] + att["crit_bonus"]
    total = 0.0
    if phys > 0:
        types = att["phys_types"] or ["Bludgeoning"]
        # The wielder gets the attacker-favourable physical type.
        total += max(
            _mitigate(phys, t, dfn, physical=True, enhancement=att["enhancement"])
            for t in types
        )
    for t, amt in att["elem"].items():
        total += _mitigate(amt, t, dfn, physical=False, enhancement=att["enhancement"])
    return total


def dpr(att: dict, dfn: dict) -> float:
    """Expected damage per round for one weapon against one defender."""
    if not att["schedule"]:
        return 0.0
    p_norm_hit = _hit_damage(att, dfn, crit=False)
    if dfn["crit_immune"]:
        return sum(hit_chance(ab, dfn["ac"]) for ab in att["schedule"]) * p_norm_hit
    p_crit_hit = _hit_damage(att, dfn, crit=True)
    threat_p = min(1.0, att["crit_threat"] / 20.0)
    total = 0.0
    for ab in att["schedule"]:
        p_hit = hit_chance(ab, dfn["ac"])
        # A crit needs a threatening roll that hits, then a confirmation roll
        # against the same AC.
        p_crit = min(p_hit, threat_p) * p_hit
        total += p_crit * p_crit_hit + (p_hit - p_crit) * p_norm_hit
    return total


def crit_chance_per_round(att: dict, dfn: dict) -> float:
    """Probability that at least one attack this round lands a confirmed crit.

    Needed on its own by the stock Devastating Critical rule, which is a
    save-or-die triggered per critical rather than a lump of damage. Zero
    against a crit-immune defender.
    """
    if not att["schedule"] or dfn["crit_immune"]:
        return 0.0
    threat_p = min(1.0, att["crit_threat"] / 20.0)
    p_none = 1.0
    for ab in att["schedule"]:
        p_hit = hit_chance(ab, dfn["ac"])
        p_none *= 1.0 - min(p_hit, threat_p) * p_hit
    return 1.0 - p_none


def _rounds_to_drop(att: dict, dfn: dict) -> tuple[float, float]:
    """Rounds to take a defender from full HP to zero, and the net damage per
    round behind that figure.

    Returns (rounds, raw_dpr). `rounds` is math.inf when the damage never
    outpaces regeneration. The raw (pre-regeneration) DPR is returned alongside
    because the kit solver needs a value that keeps moving even while the fight
    is unwinnable: if it only saw "infinity" it would see every candidate item
    as equally useless and equip nothing at all.
    """
    raw = dpr(att, dfn)
    net = raw - dfn["regen"]
    if net <= 0.01:
        return math.inf, raw
    return dfn["hp"] / net, raw


def _devcrit_rounds(rounds: float, att: dict, dfn: dict) -> float:
    """Shorten `rounds` by the stock Devastating Critical save-or-die.

    Each confirmed critical forces a Fortitude save; failing it ends the fight
    immediately, so the expected number of rounds to a kill is 1 / (chance of a
    crit landing x chance of the save failing). Returns whichever is sooner:
    grinding the target's hit points down, or rolling the kill.
    """
    dc = att["devcrit_save_dc"]
    if not dc:
        return rounds
    p = crit_chance_per_round(att, dfn) * save_fail_chance(dc, dfn["fort"])
    if p <= 0.0:
        return rounds
    return min(rounds, 1.0 / p)


def save_fail_chance(dc: int, save_bonus: int) -> float:
    """Probability of failing a saving throw. A natural 1 always fails and a
    natural 20 always succeeds, so this is clamped to [0.05, 0.95]."""
    return min(0.95, max(0.05, (dc - save_bonus) / 20.0))


def simulate(pc: dict, creature: dict) -> dict:
    """Run one fight: the PC's best weapon against the creature, and the
    creature's best weapon back.

    `pc` is a reference_pc() result; `creature` is a dict with "attack" (an
    attack_profile), "defense" (a defense_profile) and "save_threats".

    `wins` means the PC drops the creature before the creature drops the PC.
    Saving throws deliberately do NOT gate that: the data cannot tell a
    save-or-die apart from a save-for-half, so failing one is reported as a risk
    (`save_fail`) and folded into `score` rather than counted as a loss.

    `score` is the scalar the kit solver maximises, and it must be strictly
    monotonic in "how much better did that item make things" even when the
    fight is hopeless. A score that flattened to zero for every losing kit
    would leave the solver unable to tell a legendary sword from a stick, so
    the outgoing damage is kept in the score directly.
    """
    to_kill, raw_out = _rounds_to_drop(pc["attack"], creature["defense"])
    unhealed_to_die, _raw_in = _rounds_to_drop(creature["attack"], pc["defense"])

    # Stock Devastating Critical is not damage — it is a Fortitude save or die
    # on every confirmed critical. Expressed as a per-round kill probability, it
    # caps how long the fight can last regardless of hit points. Both directions,
    # because the rule is symmetric.
    to_kill = _devcrit_rounds(to_kill, pc["attack"], creature["defense"])
    unhealed_to_die = _devcrit_rounds(unhealed_to_die, creature["attack"], pc["defense"])

    # The PC has a free full heal off a fixed cooldown. Outlasting one cooldown
    # therefore means outlasting the fight: the heal lands before the damage
    # does, and then the clock restarts. Below that threshold the heal never
    # arrives in time and changes nothing. The pre-heal figure is still what
    # gets reported, since "survives 40 rounds per heal" is the useful number.
    heal_rounds = pc.get("full_heal_rounds", 0)
    outlasts = bool(heal_rounds) and unhealed_to_die >= heal_rounds
    to_die = math.inf if outlasts else unhealed_to_die

    # The creature gets at most one special ability per round; assume it leads
    # with its highest-DC one against the PC's weakest relevant save.
    fail = 0.0
    for threat in creature.get("save_threats", ())[:1]:
        worst = min(pc["defense"]["fort"], pc["defense"]["ref"], pc["defense"]["will"])
        fail = save_fail_chance(int(threat.get("dc_est", 0) or 0), worst)

    survive = 999.0 if math.isinf(to_die) else to_die
    if math.isinf(to_kill):
        # Hopeless so far, but still rank kits by raw progress: outgoing damage
        # first (even below the regeneration threshold), then survivability.
        margin = 0.0
        score = raw_out / (creature["defense"]["hp"] + 1.0) + survive / 1e6
    elif math.isinf(to_die):
        # The creature cannot hurt this PC at all, so however long the kill
        # takes the fight is never in doubt — a flat maximum, not survive/to_kill,
        # which would otherwise rank a harmless-but-tanky creature as the
        # hardest fight in its band.
        margin = 999.0
        score = margin + 1.0
    else:
        margin = survive / to_kill
        score = margin + 1.0
    return {
        "rounds_to_kill": None if math.isinf(to_kill) else round(to_kill, 1),
        "rounds_to_die": (None if math.isinf(unhealed_to_die)
                          else round(unhealed_to_die, 1)),
        "outlasts_heal_cooldown": outlasts,
        "no_damage": raw_out <= 0.01,
        "save_fail": round(fail, 3),
        "margin": round(margin, 3),
        "wins": margin >= 1.0,
        "score": score / (1.0 + fail),
    }
