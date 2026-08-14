"""Counter-gear analysis: what a player must wear to beat each creature.

Simulates every canonical creature against a level-appropriate synthetic
reference PC, solves for the best and the cheapest winning kit out of the
player-attainable item pool, and writes ``counter_gear.json`` +
``counter_gear.md`` into ``module-index/``.  Also owns the staleness
fingerprint that tells an ordinary wiki run whether the report on disk is
still current, and the module-index URL helpers shared with the module index.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from nwn_wiki import state
from nwn_wiki.combat import weapon_feat_id
from nwn_wiki.gff import fld, list_items
from nwn_wiki.htmlgen.escape import nwn_text
from nwn_wiki.itemprops import _prop_value_num
from nwn_wiki.items import (
    CRIT_IMMUNITY_LABEL,
    PLAYER_SLOT_MASK,
    PLAYER_SLOTS,
    _item_accessible,
    item_equip_slots,
    item_gp_value,
)
from nwn_wiki.lookups import BASEITEM_COLUMNS_SEEN, WEAPONS, baseitem_name
from nwn_wiki.render.creature_page import (
    extract_creature_defenses,
    extract_creature_offense,
)
from nwn_wiki.sim.combat import attack_profile, defense_profile
from nwn_wiki.sim.gear import (
    _prune_pool,
    best_in_slot_kit,
    build_gear_pool,
    minimum_viable_kit,
)
from nwn_wiki.util import _try_float, _try_int, _write_json


def _module_index_url_helpers(
    module_index_dir: Path,
    wiki_out: Path,
    base_url: str,
) -> "tuple[Any, Any, Any, Any]":
    """Return (creature_url, item_url, area_url, store_url) callables."""
    project_root = module_index_dir.parent
    if base_url:
        _b = base_url.rstrip("/")
        def _cu(rr: str) -> str: return f"{_b}/creatures/{rr}.html"
        def _iu(rr: str) -> str: return f"{_b}/items/{rr}.html"
        def _au(rr: str) -> str: return f"{_b}/areas/{rr}.html"
        def _su(rr: str) -> str: return f"{_b}/stores/{rr}.html"
    else:
        try:
            rel_wiki = wiki_out.relative_to(project_root)
        except ValueError:
            rel_wiki = wiki_out
        _wp = str(rel_wiki).rstrip("/")
        def _cu(rr: str) -> str: return f"{_wp}/creatures/{rr}.html"
        def _iu(rr: str) -> str: return f"{_wp}/items/{rr}.html"
        def _au(rr: str) -> str: return f"{_wp}/areas/{rr}.html"
        def _su(rr: str) -> str: return f"{_wp}/stores/{rr}.html"
    return _cu, _iu, _au, _su



# Challenge-Rating buckets for the progression ladder. [lo, hi) half-open; the
# last bucket's hi is open-ended. The bands past the player level cap exist to
# separate "hard" from "not intended to be soloed at cap" — every creature in
# them faces the same level-capped reference PC, which is the point.
_CR_TIERS: list[tuple[float, float, str]] = [
    (0, 5, "CR 0–5"),
    (5, 10, "CR 5–10"),
    (10, 20, "CR 10–20"),
    (20, 40, "CR 20–40"),
    (40, 80, "CR 40–80"),
    (80, 200, "CR 80–200"),
    (200, float("inf"), "CR 200+"),
]

# Bumped by hand whenever the simulation, the reference PC or the kit solver
# changes in a way that would move the numbers. Part of the staleness
# fingerprint, so an algorithm change makes existing reports report themselves
# as out of date without anyone having to remember to delete them.
_COUNTER_GEAR_ALGO_VERSION = 4

# Past this many rounds a "win" is really a war of attrition (100 rounds is ten
# minutes of unbroken melee), so the report labels it instead of just ticking it.
_ATTRITION_ROUNDS = 100


def _parse_cr(cr_raw: object) -> float:
    """Numeric Challenge Rating, or 0.0 when unparseable."""
    return _try_float(cr_raw, 0.0)


def _fmt_gp(cost: int) -> str:
    """Compact gold display: 1234 → '1.2k gp', 1500000 → '1.5M gp'."""
    if cost >= 1_000_000_000:
        return f"{cost / 1_000_000_000:.1f}B gp"
    if cost >= 1_000_000:
        return f"{cost / 1_000_000:.1f}M gp"
    if cost >= 1_000:
        return f"{cost / 1_000:.1f}k gp"
    return f"{cost} gp"


def devcrit_mode(db: "Db") -> dict:
    """Which Devastating Critical rule is in force, detected from the 2DAs.

    The engine only rolls a devastating critical for a weapon whose baseitems.2da
    row names a feat in EpicWeaponDevastatingCriticalFeat. A module that blanks
    that column across the board (as nwn_homers_lotr does, via
    bin/gen-devcrit-map.py) has disabled the save-or-die at source, for players
    and NPCs alike — so no configuration is needed to notice, and any module that
    left it alone keeps stock behaviour automatically.
    """
    col = "EpicWeaponDevastatingCriticalFeat"
    armed = sum(1 for bi in WEAPONS if weapon_feat_id(bi, col))
    if armed:
        return {"mode": "stock", "weapons_armed": armed, "bonus_dice": 0,
                "label": f"save-or-die (stock; {armed} base items armed)"}
    if col not in BASEITEM_COLUMNS_SEEN:
        # No baseitems.2da carried the column, so we never saw the evidence
        # either way. Assume the engine default rather than silently reporting
        # a mechanic as disabled — run with --2da-dir to detect it properly.
        return {"mode": "unknown", "weapons_armed": 0, "bonus_dice": 0,
                "label": ("assumed stock save-or-die — no baseitems.2da was "
                          "available to check; pass --2da-dir to detect it")}
    if db.devcrit_bonus_dice > 0:
        return {"mode": "bonus-dice", "weapons_armed": 0,
                "bonus_dice": db.devcrit_bonus_dice,
                "label": ("disabled at source; replaced by "
                          f"+{db.devcrit_bonus_dice} damage dice on a critical")}
    return {"mode": "disabled", "weapons_armed": 0, "bonus_dice": 0,
            "label": "disabled at source, with no replacement configured"}


def _pc_level_for_cr(cr: float, db: "Db") -> int:
    """Reference-PC level for a creature of this Challenge Rating: CR, clamped
    to the server's player cap. Everything above the cap gets a capped PC — so a
    boss the cap cannot beat is reported as unwinnable rather than quietly
    matched against a character who could never exist."""
    return max(1, min(int(round(cr)) or 1, db.max_player_level or 40))


# ---------------------------------------------------------------------------
# Staleness fingerprint.
#
# The simulation is far too slow to run on every wiki refresh, but a stale
# report is worse than no report. Both problems go away with a cheap hash of
# everything the analysis actually reads: if it matches, the report on disk is
# still correct; if it does not, a normal run says so and you re-run with
# --counter-gear. Module-agnostic — nothing here knows about any one module.
# ---------------------------------------------------------------------------

def _counter_gear_fingerprint(db: "Db") -> str:
    """Hash of every input the counter-gear analysis depends on."""
    h = hashlib.sha256()
    h.update(f"algo={_COUNTER_GEAR_ALGO_VERSION}\n".encode())
    h.update(f"dials={db.max_character_level},{db.max_ability_bonus},"
             f"{db.max_player_level},{db.devcrit_bonus_dice}\n".encode())

    # 2DA overrides change base item stats, slots and property tables.
    if db.twoda_dir and Path(db.twoda_dir).is_dir():
        for p in sorted(Path(db.twoda_dir).glob("*.2da")):
            st = p.stat()
            h.update(f"2da:{p.name}:{st.st_size}:{int(st.st_mtime)}\n".encode())

    # Items: what they are, what they cost, what they do, and whether a player
    # can get one (an item becoming reachable changes every kit).
    h.update(b"--items--\n")
    for rr in sorted(db.items):
        it = db.items[rr]
        h.update(f"{rr}|{fld(it, 'BaseItem', '')}|{item_gp_value(it)}|"
                 f"{int(_item_accessible(db, rr))}|".encode())
        for p in list_items(it.get("PropertiesList")):
            h.update(f"{fld(p, 'PropertyName', '')},{fld(p, 'Subtype', '')},"
                     f"{fld(p, 'CostValue', '')},{fld(p, 'Param1Value', '')};".encode())
        h.update(b"\n")

    # Creatures: the canonical set and the fields the sim reads off them.
    h.update(b"--creatures--\n")
    for rr in sorted(db.canonical_creatures):
        entry = db.canonical_creatures[rr]
        c = entry["c"]
        h.update(f"{rr}|{fld(c, 'ChallengeRating', '')}|{fld(c, 'MaxHitPoints', '')}|"
                 f"{fld(c, 'NaturalAC', '')}|{fld(c, 'ScriptDamaged', '')}|".encode())
        for key in ("Equip_ItemList", "ClassList", "FeatList", "SpecAbilityList",
                    "PropertiesList"):
            for e in list_items(c.get(key)):
                h.update(repr(sorted(
                    (k, v) for k, v in e.items() if isinstance(v, (int, str))
                )).encode())
        h.update(b"\n")
    return h.hexdigest()


def check_counter_gear_freshness(db: "Db", module_index_dir: Path) -> bool:
    """Compare the on-disk counter_gear.json against the current inputs.

    Returns True when the report is present and current. Called on every wiki
    run; pushes a warning onto the module-index summary when the report is
    missing or stale so the staleness surfaces without paying for the sim.
    """
    path = module_index_dir / "counter_gear.json"
    if not path.is_file():
        state._module_index_summary.append(("warn",
            "[nwn-wiki] module-index: counter_gear.json absent — "
            "run with --counter-gear to build it"))
        return False
    try:
        stored = json.loads(path.read_text(encoding="utf-8")).get("input_fingerprint")
    except (OSError, ValueError):
        stored = None
    if stored and stored == _counter_gear_fingerprint(db):
        state._module_index_summary.append(("info",
            "[nwn-wiki] module-index: counter_gear.json current (inputs unchanged)"))
        return True
    state._module_index_summary.append(("warn",
        "[nwn-wiki] module-index: counter_gear.json is STALE — items, creatures, "
        "2DAs or combat dials changed since it was built. Re-run with "
        "--counter-gear."))
    return False


# ---------------------------------------------------------------------------
# Report data + rendering
# ---------------------------------------------------------------------------

def _top_value_by_slot(db: "Db", _iu, limit: int = 5) -> list[dict]:
    """The most valuable player-attainable items in each equipment slot.

    A blunt but effective best-in-slot proxy: builders price power, so sorting
    a slot by gold piece value surfaces its strongest items without simulating
    anything. Attainability is the same rule the items index uses. Items that
    fit several slots appear under each of them.
    """
    buckets: dict[str, list[dict]] = {key: [] for key, _, _, _ in PLAYER_SLOTS}
    for rr in sorted(db.items):
        if not _item_accessible(db, rr):
            continue
        item = db.items[rr]
        slots = item_equip_slots(item) & PLAYER_SLOT_MASK
        if not slots:
            continue
        name = nwn_text(db.item_name(rr))
        if name.startswith("[TLK#") or name == rr:
            continue                              # broken/unnamed blueprint
        entry = {
            "resref": rr, "name": name,
            "gp_value": item_gp_value(item),
            "base_item": baseitem_name(_try_int(fld(item, "BaseItem", -1), -1)),
            "wiki_url": _iu(rr),
        }
        for key, _label, mask, _n in PLAYER_SLOTS:
            if slots & mask:
                buckets[key].append(entry)

    out: list[dict] = []
    for key, label, _mask, wearable in PLAYER_SLOTS:
        items = sorted(buckets[key],
                       key=lambda e: (-e["gp_value"], e["name"].lower()))[:limit]
        if items:
            out.append({"slot": key, "label": label,
                        "wearable": wearable, "items": items})
    return out


def _kit_entries(kit: dict, _iu) -> list[dict]:
    """Serialise a solved kit to slot-ordered JSON entries."""
    entries: list[dict] = []
    for key, label, _mask, _n in PLAYER_SLOTS:
        held = kit.get(key)
        if not held:
            continue
        for piece in (held if isinstance(held, list) else [held]):
            entries.append({
                "slot": key, "label": label,
                "resref": piece["resref"], "name": piece["name"],
                "gp_value": piece["cost"], "wiki_url": _iu(piece["resref"]),
            })
    return entries


def _fmt_rounds(sim: dict) -> str:
    """One-line verdict for a simulated fight."""
    kill = sim["rounds_to_kill"]
    die = sim["rounds_to_die"]
    kill_s = f"{kill} rds" if kill is not None else "never"
    if sim.get("outlasts_heal_cooldown"):
        # Survives longer than the heal cooldown, so the heal keeps it standing.
        die_s = f"outlasts heal ({die} rds/heal)" if die is not None else "never"
    else:
        die_s = f"dies in {die} rds" if die is not None else "never dies"
    verdict = "✅" if sim["wins"] else "❌"
    out = f"kills in {kill_s}, {die_s} {verdict}"
    # A win measured in thousands of rounds is a win on paper only — that is
    # hours of real time, and it usually means the kit only just out-paces the
    # creature's regeneration. Say so rather than let ✅ imply "go fight this".
    if kill is not None and kill > _ATTRITION_ROUNDS:
        out += f" ⏳ attrition ({kill * 6 / 60:.0f} min)"
    if sim["save_fail"] > 0:
        out += f" (save-fail {sim['save_fail'] * 100:.0f}%/rd)"
    return out


def _kit_lines(entries: list[dict]) -> list[str]:
    """Markdown table rows for a kit, or a single 'nothing needed' line."""
    if not entries:
        return ["  - _(nothing — the fight is won bare-handed)_"]
    return [f"  - {e['label']}: {e['name']} (`{e['resref']}`, {_fmt_gp(e['gp_value'])})"
            for e in entries]


def _counter_gear_markdown(data: dict) -> str:
    """Render the counter_gear.json payload as a human-readable report."""
    L: list[str] = []
    L.append(f"# Counter-gear analysis — {data['module']}")
    L.append("")
    L.append(f"_Generated {data['generated_at']}. Every creature is fought by a "
             f"synthetic reference PC — a single-class fighter at level "
             f"min(CR, {data['max_player_level']}), every level-up point and all "
             "ten tiers of Great Strength (Great Dexterity behind a finesse or "
             "ranged weapon) in its attacking stat, specced into whatever weapon "
             "it is holding, with a free full heal every 150s — using only items "
             "players can actually obtain. Ability bonuses stack across items up "
             f"to this module's +{data['max_ability_bonus']} cap. Rounds are "
             "expected values, not rolls. No spells, potions or summons are "
             "modelled, so **\"unwinnable\" means unwinnable on gear alone**, and "
             "save DCs are estimates (innate spell level isn't in the data)._")
    L.append("")
    L.append(f"_Critical hits: threat range and multiplier come from each "
             "weapon's own `baseitems.2da` row, and a critical needs a "
             "threatening roll that hits followed by a confirmation roll. The PC "
             "takes Improved Critical (doubling the threat range) and, where the "
             "weapon allows it and Strength reaches 25, Overwhelming and "
             "Devastating Critical; creatures get only the critical feats their "
             "blueprint actually carries. Devastating Critical in this module: "
             f"**{data['devastating_critical']['label']}**. Creatures immune to "
             "critical hits take none of it. Legendary feats are not simulated._")
    L.append("")
    creatures = data["creatures"]
    won = [c for c in creatures if c["sim"]["wins"]]
    L.append("## Summary")
    L.append("")
    L.append(f"- Creatures analysed: **{len(creatures)}**")
    L.append(f"- Beatable with obtainable gear: **{len(won)}**")
    L.append(f"- Unwinnable at the level cap: **{len(data['unbeatable'])}**")
    L.append(f"- Needs manual review: **{len(data['manual_review'])}**")
    L.append(f"- Damage types dealt by obtainable weapons: "
             f"{', '.join(data['damage_types']) or '(none found)'}")
    L.append("")

    # ---- best in slot by GP ------------------------------------------------
    L.append("## Best in slot (by GP value)")
    L.append("")
    L.append("_The most valuable items a player can actually get hold of, per "
             "equipment slot. Gold is a proxy for power, not a measure of it — "
             "use the per-creature kits below for what actually wins fights._")
    L.append("")
    for grp in data.get("top_value_by_slot", []):
        heading = grp["label"]
        if grp["wearable"] > 1:
            heading += f" (×{grp['wearable']} wearable)"
        L.append(f"### {heading}")
        L.append("")
        L.append("| # | Item | ResRef | GP Value |")
        L.append("|--:|------|--------|---------:|")
        for i, e in enumerate(grp["items"], 1):
            L.append(f"| {i} | {e['name']} | `{e['resref']}` | {e['gp_value']:,} |")
        L.append("")

    if data["unbeatable"]:
        L.append("## ⛔ Unwinnable at the level cap")
        L.append("")
        for u in data["unbeatable"]:
            L.append(f"- **{u['name']}** (`{u['canonical_resref']}`) — "
                     + "; ".join(u["reasons"]))
        L.append("")

    if data["manual_review"]:
        L.append("## ⚠️ Needs manual review")
        L.append("")
        for u in data["manual_review"]:
            L.append(f"- **{u['name']}** (`{u['canonical_resref']}`) — "
                     + "; ".join(u["notes"]))
        L.append("")

    # ---- progression ladder ------------------------------------------------
    L.append("## Progression ladder (by Challenge Rating)")
    L.append("")
    L.append("_Each tier's kit is the minimum kit of the band's most gear-hungry "
             "winnable fight — the band's worst equipment requirement, not an "
             "average. The hardest fight is listed separately: it is usually a "
             "different creature, one that needs skill rather than shopping._")
    L.append("")
    for t in data.get("progression_tiers", []):
        prof = t["defeat_profile"]
        lo, hi = prof["ac_target_range"]
        L.append(f"### {t['label']} — {t['creature_count']} creatures "
                 f"(reference PC level {t['pc_level']})")
        bits = []
        if prof["min_enhancement_to_bypass_dr"]:
            bits.append(f"weapon ≥ +{prof['min_enhancement_to_bypass_dr']} (DR)")
        bits.append(f"AC target {lo}–{hi}")
        if prof["resist_priority"]:
            bits.append("resist " + "/".join(prof["resist_priority"]))
        if prof["has_special_attacks"]:
            bits.append("save gear (special attacks)")
        L.append(f"- Profile: {' · '.join(bits)}")
        if t.get("hardest"):
            L.append(f"- Hardest fight: **{t['hardest']['name']}** "
                     f"(CR {t['hardest']['cr']}) in the module's best gear — "
                     f"{_fmt_rounds(t['hardest']['sim'])}")
        if t.get("benchmark"):
            L.append(f"- Most gear-hungry fight: **{t['benchmark']['name']}** "
                     f"(CR {t['benchmark']['cr']}) — {_fmt_rounds(t['benchmark']['sim'])}; "
                     "its minimum kit is the tier kit below")
        L.append(f"- Kit ({_fmt_gp(t['kit_cost'])} total):")
        L.extend(_kit_lines(t["recommended_kit"]))
        if t["unwinnable"]:
            L.append(f"- ⛔ Unwinnable in this band: "
                     + ", ".join(u["name"] for u in t["unwinnable"][:10])
                     + (f" (+{len(t['unwinnable']) - 10} more)"
                        if len(t["unwinnable"]) > 10 else ""))
        L.append("")

    L.append("## Per-creature counter-gear")
    L.append("")
    for c in creatures:
        sim = c["sim"]
        flag = ""
        if c["unbeatable"]:
            flag = " ⛔ UNWINNABLE"
        elif c["needs_manual_review"]:
            flag = " ⚠️ review"
        L.append(f"### {c['name']} (CR {c['cr']}){flag}")
        d = c["defenses"]
        dr = d.get("dr")
        dr_txt = (f"{dr.get('soak') or ''} {('bypass ' + dr['bypass']) if dr.get('bypass') else ''}".strip()
                  if dr else "—")
        L.append(f"- HP {c['hp'] if c['hp'] is not None else '?'} · AC {d['ac']} · "
                 f"Fort {d['fort']:+d}/Ref {d['ref']:+d}/Will {d['will']:+d}"
                 f" · SR {d['sr']} · DR {dr_txt or '—'}")
        sv = c["survival"]
        off_bits = [f"attacks +{sv['attack_bonus']}"]
        if sv.get("damage_types_dealt"):
            off_bits.append("deals " + "/".join(sv["damage_types_dealt"]))
        if sv.get("save_threats"):
            st = sv["save_threats"][0]
            off_bits.append(
                f"special DC ~{st['dc_est']} est. ({len(sv['save_threats'])} abilities)")
        L.append(f"- Offense: {' · '.join(off_bits)}")
        req = c["defeat_requirements"]
        if d.get("crit_immune"):
            L.append("- **Immune to critical hits** — no multiplier, no "
                     "Overwhelming or Devastating Critical damage")
        if req["immune_types"]:
            L.append(f"- Immune to: {', '.join(req['immune_types'])}")
        if req["mandatory_weapon_tags"]:
            L.append(f"- **Requires weapon tag:** {', '.join(req['mandatory_weapon_tags'])}")
        if req["min_enhancement_to_bypass_dr"]:
            L.append(f"- Needs +{req['min_enhancement_to_bypass_dr']} enhancement to bypass DR")
        for u in c["unbeatable_reasons"]:
            L.append(f"- ⛔ {u}")
        for n in c["review_notes"]:
            L.append(f"- ⚠️ {n}")
        L.append(f"- Simulated (L{c['pc_level']} PC, best gear): {_fmt_rounds(c['best_sim'])}")
        if c["minimum_kit"] is not None:
            L.append(f"- Minimum kit that wins ({_fmt_gp(c['minimum_kit_cost'])}) — "
                     f"{_fmt_rounds(sim)}:")
            L.extend(_kit_lines(c["minimum_kit"]))
        L.append("- Best obtainable kit:")
        L.extend(_kit_lines(c["best_kit"]))
        L.append("")
    return "\n".join(L)


def generate_counter_gear_index(
    db: "Db",
    module_index_dir: Path,
    module_title: str,
    now: str,
    _cu,
    _iu,
) -> None:
    """Simulate every canonical creature against a level-appropriate reference PC
    and report the gear needed to beat it.

    For each creature this derives its objective defences and offensive threat
    (via extract_creature_defenses / extract_creature_offense, shared with the
    creature pages so the combat maths never forks), then solves for two kits
    out of the player-attainable item pool: the strongest one available, and the
    cheapest one that still wins. Creatures are bucketed by Challenge Rating into
    a progression ladder, and the report opens with the highest-GP attainable
    item in every slot. Writes counter_gear.json + counter_gear.md.
    """
    t0 = time.time()
    pool = _prune_pool(build_gear_pool(db))
    print("[nwn-wiki] counter-gear: gear pool "
          + ", ".join(f"{k}={len(v)}" for k, v in pool.items() if v))

    # Damage types any obtainable weapon can deal — used to spot creatures that
    # nothing in the module can hurt.
    module_weapon_dtypes: set[str] = set()
    for pieces in pool.values():
        for p in pieces:
            if p["is_weapon"]:
                module_weapon_dtypes.update(p["off"]["damage_dtypes"])

    def _tag_has_obtainable(tag: str) -> bool:
        return any(_item_accessible(db, irr)
                   for irr in db.item_tag_groups.get(tag.lower(), []))

    creatures_out: list[dict] = []
    unbeatable_out: list[dict] = []
    review_out: list[dict] = []

    ordered = [r for r in sorted(
        db.canonical_creatures,
        key=lambda r: nwn_text(db.canonical_creature_name(r)).lower())
        if not r.startswith("__orphan_")]

    for n, can_rr in enumerate(ordered, 1):
        if n % 100 == 0:
            print(f"[nwn-wiki] counter-gear: {n}/{len(ordered)} creatures "
                  f"({time.time() - t0:.0f}s)")
        entry = db.canonical_creatures[can_rr]
        c = entry["c"]
        bp_rr = entry["bp_rr"]
        bp = db.creatures.get(bp_rr) if bp_rr != can_rr else None
        name = nwn_text(db.canonical_creature_name(can_rr))
        cr_raw = fld(c, "ChallengeRating") or (fld(bp, "ChallengeRating") if bp else "") or ""
        D = extract_creature_defenses(db, c, bp)
        O = extract_creature_offense(db, c, bp, D)

        immune_types = sorted(t for t, pct in D["immunities"].items() if pct >= 100)
        dr = D["dr"]
        min_enh = _prop_value_num(dr["bypass"]) if dr and dr.get("bypass") else 0
        mandatory_tags = sorted({t.lower() for t in D["hard_required_tags"]})

        # ---- the creature as a simulation opponent -------------------------
        # Its best weapon is the one that hits hardest; with none, the profile
        # list is empty and it can only be worn down by the PC.
        profiles = O["attack_profiles"]
        creature_att = (max(profiles, key=lambda p: p["schedule"][0] if p["schedule"] else -99)
                        if profiles else
                        attack_profile([], num_dice=0, die=0, flat=0, crit_threat=1,
                                       crit_mult=2, phys_types=[], elem={}, enhancement=0))
        crit_immune = CRIT_IMMUNITY_LABEL in D["misc_immunities"]
        creature_sim = {
            "attack": creature_att,
            "defense": defense_profile(
                ac=D["ac"], hp=D["hp"] or 1,
                dr_soak=_prop_value_num(dr["soak"]) if dr and dr.get("soak") else 0,
                dr_bypass=min_enh,
                resist=D["resistances"], immune=D["immunities"],
                regen=D["regen"], fort=D["fort"], ref=D["ref"], will=D["will"],
                crit_immune=crit_immune),
            "save_threats": O["save_threats"],
        }

        cr = _parse_cr(cr_raw)
        pc_level = _pc_level_for_cr(cr, db)
        best_kit, best_sim = best_in_slot_kit(pc_level, creature_sim, pool, db)
        min_kit, min_sim = minimum_viable_kit(pc_level, creature_sim, pool, db, best_kit)

        best_entries = _kit_entries(best_kit, _iu)
        min_entries = _kit_entries(min_kit, _iu) if best_sim["wins"] else None

        # ---- unwinnable / review detection ---------------------------------
        unbeatable_reasons: list[str] = []
        if mandatory_tags and not any(_tag_has_obtainable(t) for t in mandatory_tags):
            unbeatable_reasons.append(
                "requires a weapon tagged "
                + " or ".join(f"'{t}'" for t in mandatory_tags)
                + " but no obtainable item has that tag")
        if module_weapon_dtypes and not (module_weapon_dtypes - set(immune_types)):
            unbeatable_reasons.append(
                "immune to every damage type any obtainable weapon deals ("
                + ", ".join(sorted(module_weapon_dtypes)) + ")")
        if not best_sim["wins"]:
            if best_sim["no_damage"]:
                unbeatable_reasons.append(
                    f"takes no damage from a level-{pc_level} PC in the best "
                    "obtainable gear — its immunities, resistances and damage "
                    "reduction absorb every attack")
            elif best_sim["rounds_to_kill"] is None:
                unbeatable_reasons.append(
                    f"regenerates faster than a level-{pc_level} PC in the best "
                    "obtainable gear can deal damage")
            else:
                unbeatable_reasons.append(
                    f"kills a level-{pc_level} PC in the best obtainable gear "
                    f"({best_sim['rounds_to_die']} rounds) before it can be killed "
                    f"({best_sim['rounds_to_kill']} rounds)")
        if best_sim["save_fail"] > 0.5:
            review_notes_save = (
                f"special-attack save DC ~{O['save_threats'][0]['dc_est']} beats even "
                f"the best obtainable save gear ({best_sim['save_fail'] * 100:.0f}% "
                "failure per round) — survivable only if those abilities are not "
                "save-or-die, which the blueprint data cannot tell us")
        else:
            review_notes_save = ""

        review_notes: list[str] = []
        if review_notes_save:
            review_notes.append(review_notes_save)
        if D["mitigates_damage"]:
            review_notes.append(
                f"OnDamaged handler '{D['dmg_script']}' self-heals or grants itself "
                "damage immunity — the simulation cannot see that, so verify the "
                "result by hand")

        rec = {
            "canonical_resref": can_rr,
            "blueprint_resref": bp_rr,
            "name": name,
            "wiki_url": _cu(can_rr),
            "cr": str(cr_raw),
            "hp": D["hp"],
            "pc_level": pc_level,
            "defenses": {
                "ac": D["ac"], "fort": D["fort"], "ref": D["ref"], "will": D["will"],
                "sr": D["sr"], "dr": dr,
                "resistances": D["resistances"], "immunities": D["immunities"],
                "spell_immunities": D["spell_immunities"],
                "regen": D["regen"], "vampiric": D["vampiric"],
                "crit_immune": crit_immune,
            },
            "defeat_requirements": {
                "damageable_types": sorted(module_weapon_dtypes - set(immune_types)),
                "immune_types": immune_types,
                "min_enhancement_to_bypass_dr": min_enh,
                "mandatory_weapon_tags": mandatory_tags,
                "hard_gate": bool(mandatory_tags),
            },
            "survival": {
                "attack_bonus": O["attack_bonus"],
                "ac_target": O["ac_target"],
                "damage_types_dealt": O["damage_types_dealt"],
                "save_threats": O["save_threats"],
            },
            "sim": min_sim if min_entries is not None else best_sim,
            "best_sim": best_sim,
            "best_kit": best_entries,
            "best_kit_cost": sum(e["gp_value"] for e in best_entries),
            "minimum_kit": min_entries,
            "minimum_kit_cost": (sum(e["gp_value"] for e in min_entries)
                                 if min_entries else 0),
            "unbeatable": bool(unbeatable_reasons),
            "unbeatable_reasons": unbeatable_reasons,
            "needs_manual_review": bool(review_notes),
            "review_notes": review_notes,
        }
        creatures_out.append(rec)
        if unbeatable_reasons:
            unbeatable_out.append({"canonical_resref": can_rr, "name": name,
                                   "reasons": unbeatable_reasons})
        if review_notes:
            review_out.append({"canonical_resref": can_rr, "name": name,
                               "notes": review_notes})

    # ---- progression ladder ------------------------------------------------
    # A tier's kit has to beat the *hardest* creature in the band, not a typical
    # one — a kit that clears the average and dies to the boss is not a kit.
    progression_tiers: list[dict] = []
    by_resref = {r["canonical_resref"]: r for r in creatures_out}
    for lo, hi, label in _CR_TIERS:
        members = [r for r in creatures_out if lo <= _parse_cr(r["cr"]) < hi]
        if not members:
            continue
        winnable = [r for r in members if not r["unbeatable"]]
        ac_targets = [r["survival"]["ac_target"] for r in members]
        resist_counter: dict[str, int] = defaultdict(int)
        for r in members:
            for dt in r["survival"]["damage_types_dealt"]:
                resist_counter[dt] += 1
        top_resist = [dt for dt, _ in sorted(
            resist_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]

        # Two different questions, two different answers — conflating them is
        # what made the draft report useless.
        #
        # "Most demanding" is the fight in the band that needs the most gear, so
        # its minimum kit is the one to publish for the tier: buy that and you
        # have covered the band's worst equipment requirement.
        #
        # "Hardest" is the closest-run fight in the module's best gear. It is a
        # separate creature more often than not, and it cannot be found from the
        # minimum kits: those are shrunk until they only just win, so every
        # creature's margin there sits at ~1.0 by construction.
        benchmark = hardest = None
        kit_entries: list[dict] = []
        if winnable:
            demanding = max(winnable, key=lambda r: (r["minimum_kit_cost"],
                                                     r["canonical_resref"]))
            kit_entries = demanding["minimum_kit"] or []
            benchmark = {
                "canonical_resref": demanding["canonical_resref"],
                "name": demanding["name"], "cr": demanding["cr"],
                "sim": demanding["sim"],
            }
            tightest = min(winnable, key=lambda r: (r["best_sim"]["margin"],
                                                    -(r["best_sim"]["rounds_to_kill"] or 0)))
            hardest = {
                "canonical_resref": tightest["canonical_resref"],
                "name": tightest["name"], "cr": tightest["cr"],
                "sim": tightest["best_sim"],
            }
        progression_tiers.append({
            "label": label,
            "creature_count": len(members),
            "pc_level": _pc_level_for_cr(max(_parse_cr(r["cr"]) for r in members), db),
            "defeat_profile": {
                "min_enhancement_to_bypass_dr": max(
                    (r["defeat_requirements"]["min_enhancement_to_bypass_dr"]
                     for r in members), default=0),
                "ac_target_range": [min(ac_targets), max(ac_targets)],
                "resist_priority": top_resist,
                "has_special_attacks": any(r["survival"]["save_threats"] for r in members),
            },
            "benchmark": benchmark,
            "hardest": hardest,
            "recommended_kit": kit_entries,
            "kit_cost": sum(e["gp_value"] for e in kit_entries),
            "unwinnable": [{"canonical_resref": r["canonical_resref"], "name": r["name"]}
                           for r in members if r["unbeatable"]],
        })

    payload = {
        "generated_at": now,
        "module": module_title,
        "input_fingerprint": _counter_gear_fingerprint(db),
        "algo_version": _COUNTER_GEAR_ALGO_VERSION,
        "max_player_level": db.max_player_level,
        "max_ability_bonus": db.max_ability_bonus,
        "devastating_critical": devcrit_mode(db),
        "damage_types": sorted(module_weapon_dtypes),
        "top_value_by_slot": _top_value_by_slot(db, _iu),
        "creatures": creatures_out,
        "unbeatable": unbeatable_out,
        "manual_review": review_out,
        "progression_tiers": progression_tiers,
    }
    _write_json(module_index_dir / "counter_gear.json", payload)
    (module_index_dir / "counter_gear.md").write_text(
        _counter_gear_markdown(payload), encoding="utf-8")
    sev = "warn" if unbeatable_out else "info"
    state._module_index_summary.append((sev,
        f"[nwn-wiki] module-index: counter_gear.json "
        f"({len(creatures_out)} creatures, {len(unbeatable_out)} unwinnable, "
        f"{len(review_out)} review, {time.time() - t0:.0f}s)"))
