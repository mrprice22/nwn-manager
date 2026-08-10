"""Creature detail rendering for the wiki.

The per-creature combat offence/defence extraction shared with the
counter-gear analysis, the body sections of a creature page, the
variant-diff table, :func:`render_creature_page` itself, and the creature
search page with its client-side filter script.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from nwn_wiki.bestiary import _utc_to_local
from nwn_wiki.combat import (
    FINESSE_BASEITEMS,
    WEAPON_FINESSE_FEAT,
    ability_mod,
    attack_schedule,
    creature_bab,
    creature_class_bab,
    crit_feat_effects,
    epic_toughness_hp,
    feat_attack_bonus,
)
from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.chrome import _creature_cr_value, page, write
from nwn_wiki.htmlgen.escape import E, colorize_damage_words, nwn_html, nwn_text
from nwn_wiki.htmlgen.links import _conv_link, _faction_cell, _race_link, link
from nwn_wiki.itemprops import (
    _fmt_hp,
    _prop_slug,
    _prop_value_num,
    _yn,
    itemprop_format,
)
from nwn_wiki.items import (
    SHIELD_BASEITEMS,
    SLOT_CHEST,
    SLOT_CWEAP_B,
    SLOT_CWEAP_L,
    SLOT_CWEAP_R,
    SLOT_LEFT,
    SLOT_NAMES,
    SLOT_RIGHT,
    _ABIL_NAME_KEY,
    _CWEAP_SLOTS,
    _item_category,
    _item_category_label,
    baseitem_name,
    extract_item_offense,
    is_ranged_weapon,
    item_ac_bonus,
    item_attack_bonus,
    item_damage_bonus,
    weapon_crit_string,
    weapon_damage_props,
    weapon_damage_string,
)
from nwn_wiki.lookups import (
    RACE_ABILITY_ADJ,
    WEAPONS,
    _torso_base_ac,
    appearance_name,
    class_name,
    creature_race_immunities,
    feat_name,
    race_name,
    skill_name,
    spell_name,
)
from nwn_wiki.render.creatures import _pic_figures, creature_max_hp
from nwn_wiki.render.stores import _creature_store_section
from nwn_wiki.sim.combat import attack_profile
from nwn_wiki.util import _try_int

from nwn_wiki import state


# Stock 2da row ids used in combat-stat derivation.
TUMBLE_SKILL_ID = 21       # skills.2da row; Tumble grants +1 dodge AC per 5 ranks
ARMOR_SKIN_FEAT = 490      # feat.2da FEAT_EPIC_ARMOR_SKIN → +2 natural AC
MONK_CLASS_ID = 5          # classes.2da row; monk unarmed gets a faster schedule


# --- Retaliation (OnDamaged strike-back) analysis ------------------------------
def _retaliation_sentence(info: dict) -> str:
    """Render a one-sentence, player-facing description of a retaliation effect."""
    if info.get("slay"):
        who = (info["align"] + " ") if info.get("align") else ""
        whom = "player characters" if info.get("pc_only") else "creatures"
        core = f"instantly slay {who}{whom} that strike it"
    else:
        dmg = " ".join(x for x in (info.get("amount", ""), info.get("dtype", "")) if x)
        dmg = (dmg + " damage") if dmg else "damage"
        scope = "everything nearby (area of effect)" if info.get("aoe") else "its attacker"
        verb = "unleash" if info.get("aoe") else "deal"
        core = f"{verb} {dmg} to {scope}"
    if info.get("chance_pct"):
        return f"When struck, this creature has a ~{info['chance_pct']}% chance to {core}."
    return f"When struck, this creature will {core}."


def extract_creature_defenses(db: Db, c: dict, bp: dict | None = None) -> dict:
    """Compute a creature's effective combat defenses once, as plain data, so
    both the HTML detail page and the counter-gear analysis read from the same
    source of truth (the NWN combat rules here are subtle — duplicating them
    would drift).  When `bp` is provided, fields missing from the instance
    struct fall back to the blueprint.

    Returns a dict carrying the rendered-display values the creature page needs
    (ac_total/ac_breakdown, fort/ref/will, sr, hp_display, cprop_by_pid, …) as
    well as structured defenses for analysis (dr, resistances, immunities,
    spell_immunities, regen, vampiric, hard_required_tags, mitigates_damage)."""

    def _f(key: str, default: Any = None) -> Any:
        v = fld(c, key, None)
        if v is None and bp is not None:
            v = fld(bp, key, default)
        return default if v is None else v

    def _list(key: str) -> list[dict]:
        items = list_items(c.get(key))
        if not items and bp is not None:
            items = list_items(bp.get(key))
        return items

    classes = _list("ClassList")
    feats = _list("FeatList")
    skills = _list("SkillList")
    equip = _list("Equip_ItemList")

    def _equip_resref(e: dict) -> str:
        return fld(e, "EquippedRes", "") or fld(e, "TemplateResRef", "") or ""

    def _equip_item(e: dict) -> dict | None:
        irr = _equip_resref(e)
        if irr and irr in db.items:
            return db.items[irr]
        return e if "BaseItem" in e else None

    # Effective ability scores = stored UTC score + racial adjustment + item
    # bonus. The UTC stores pre-racial scores; the engine adds the race's +/- at
    # runtime (e.g. Elf +2 Dex / -2 Con) plus any Ability Bonus from equipped
    # gear (capped per the module's max).
    _race_id = _f("Race")
    race_adj = (RACE_ABILITY_ADJ.get(int(_race_id), {})
                if _race_id is not None else {})

    item_abil: dict[str, int] = {}
    for _e in equip:
        _it = _equip_item(_e)
        if _it is None:
            continue
        for _p in list_items(_it.get("PropertiesList")):
            if fld(_p, "PropertyName") != 0:  # 0 = Ability Bonus
                continue
            _pf = itemprop_format(_p)
            _key = _ABIL_NAME_KEY.get(_pf["subtype"])
            if not _key:
                continue
            _val = _prop_value_num(_pf["cost"]) if _pf["cost"] else 0
            if _val > item_abil.get(_key, 0):  # same-ability enhancement: max
                item_abil[_key] = _val
    _abil_cap = db.max_ability_bonus
    if _abil_cap and _abil_cap > 0:
        item_abil = {k: min(v, _abil_cap) for k, v in item_abil.items()}

    def _ability(ab: str) -> int | None:
        base = _f(ab)
        if base is None:
            return None
        return int(base) + race_adj.get(ab, 0) + item_abil.get(ab, 0)

    ability_scores = {ab: _ability(ab) for ab in
                      ("Str", "Dex", "Con", "Int", "Wis", "Cha")}

    # ----- Combat: saves / AC / BAB -----------------------------------------
    str_score = ability_scores["Str"] or 10
    dex_score = ability_scores["Dex"] or 10
    con_score = ability_scores["Con"] or 10
    wis_score = ability_scores["Wis"] or 10
    str_mod = ability_mod(str_score)
    dex_mod = ability_mod(dex_score)
    con_mod = ability_mod(con_score)
    wis_mod = ability_mod(wis_score)

    # Saves: NWN UTC stores per-save *bonus* fields; the per-class progression
    # is added at runtime. We surface the override + ability mod as a floor.
    fort = (_f("fortbonus", 0) or 0) + con_mod
    ref = (_f("refbonus", 0) or 0) + dex_mod
    will = (_f("willbonus", 0) or 0) + wis_mod

    bab = creature_bab(classes, db.max_character_level)

    equip_by_slot: dict[int, dict] = {}
    for e in equip:
        slot = fld(e, "__struct_id")
        if isinstance(slot, int):
            equip_by_slot[slot] = e

    def _equipped_item(slot: int) -> dict | None:
        e = equip_by_slot.get(slot)
        return _equip_item(e) if e else None

    # ----- Combined equipment combat properties -----------------------------
    # Creature weapons (R/L/bite) share the same item blueprint; skip duplicates
    # to avoid triple-counting properties. AC Bonus (ID 1) on armor/shield slots
    # is the armor/shield type (handled separately); on other slots it is dodge.
    _seen_cweap_resrefs: set[str] = set()
    _ARMOR_SHIELD_SLOTS = frozenset({SLOT_CHEST, SLOT_LEFT})
    cprop_by_pid: dict[int, list[tuple[dict, int]]] = {}
    cprop_dodge_ac: list[int] = []

    for _ce in equip:
        _cslot = fld(_ce, "__struct_id")
        if isinstance(_cslot, int) and _cslot in _CWEAP_SLOTS:
            _cirr = _equip_resref(_ce)
            if _cirr in _seen_cweap_resrefs:
                continue
            if _cirr:
                _seen_cweap_resrefs.add(_cirr)
        _ci = _equip_item(_ce)
        if _ci is None:
            continue
        _is_armor_shield = isinstance(_cslot, int) and _cslot in _ARMOR_SHIELD_SLOTS
        for _cp in list_items(_ci.get("PropertiesList")):
            _cpid_raw = fld(_cp, "PropertyName")
            if _cpid_raw is None:
                continue
            _cpid = int(_cpid_raw)
            _cpf = itemprop_format(_cp)
            _ccv = _prop_value_num(_cpf["cost"]) if _cpf["cost"] else 0
            if _cpid not in cprop_by_pid:
                cprop_by_pid[_cpid] = []
            cprop_by_pid[_cpid].append((_cpf, _ccv))
            if _cpid == 1 and not _is_armor_shield:
                cprop_dodge_ac.append(_ccv)

    armor_item = _equipped_item(SLOT_CHEST)
    armor_base_ac = _torso_base_ac(armor_item)
    shield_item = None
    left = equip_by_slot.get(SLOT_LEFT)
    if left:
        irr = fld(left, "EquippedRes", "")
        li = db.items.get(irr)
        if li and int(fld(li, "BaseItem", -1) or -1) in SHIELD_BASEITEMS:
            shield_item = li
    shield_base_ac = 0
    if shield_item is not None:
        s_stats = WEAPONS.get(int(fld(shield_item, "BaseItem", -1) or -1))
        if s_stats:
            try:
                shield_base_ac = int(s_stats.get("BaseAC", "0") or 0)
            except ValueError:
                shield_base_ac = 0

    armor_ac_bonus, armor_notes = item_ac_bonus(armor_item)
    shield_ac_bonus, shield_notes = item_ac_bonus(shield_item)
    feat_ids = [int(fld(f, "Feat")) for f in feats if fld(f, "Feat") is not None]
    natural_ac = _f("NaturalAC", 0) or 0
    _armor_skin_ac = 2 if ARMOR_SKIN_FEAT in feat_ids else 0
    _tumble_rank = 0
    if TUMBLE_SKILL_ID < len(skills):
        _tumble_rank = int(fld(skills[TUMBLE_SKILL_ID], "Rank", 0) or 0)
    _tumble_ac = _tumble_rank // 5
    _item_dodge = min(sum(cprop_dodge_ac), 20) if cprop_dodge_ac else 0
    _haste_ac = 4 if 35 in cprop_by_pid else 0
    ac_total = (10 + dex_mod + int(natural_ac) + _armor_skin_ac
                + armor_base_ac + armor_ac_bonus
                + shield_base_ac + shield_ac_bonus
                + _item_dodge + _haste_ac + _tumble_ac)

    ac_breakdown = [
        "10 base",
        f"{dex_mod:+d} Dex",
    ]
    if natural_ac:
        ac_breakdown.append(f"+{natural_ac} natural (NaturalAC)")
    if _armor_skin_ac:
        ac_breakdown.append(f"+{_armor_skin_ac} natural (Armor Skin)")
    if armor_base_ac:
        ac_breakdown.append(f"+{armor_base_ac} armor")
    if armor_ac_bonus:
        ac_breakdown.append(f"+{armor_ac_bonus} armor enchant")
    if shield_base_ac:
        ac_breakdown.append(f"+{shield_base_ac} shield")
    if shield_ac_bonus:
        ac_breakdown.append(f"+{shield_ac_bonus} shield enchant")
    if _item_dodge:
        ac_breakdown.append(f"+{_item_dodge} dodge (items)")
    if _haste_ac:
        ac_breakdown.append(f"+{_haste_ac} dodge (haste)")
    if _tumble_ac:
        ac_breakdown.append(f"+{_tumble_ac} dodge (Tumble, {_tumble_rank} ranks)")
    ac_extra = armor_notes + shield_notes

    # Item saving throw bonuses (ID 41) — max per save type (don't stack).
    _iprp_save_univ = max((cv for _pf, cv in cprop_by_pid.get(41, [])
                           if not _pf["subtype"] or _pf["subtype"] == "Universal"), default=0)
    _iprp_save_fort = max((cv for _pf, cv in cprop_by_pid.get(41, [])
                           if _pf["subtype"] == "Fortitude"), default=0)
    _iprp_save_ref = max((cv for _pf, cv in cprop_by_pid.get(41, [])
                          if _pf["subtype"] == "Reflex"), default=0)
    _iprp_save_will = max((cv for _pf, cv in cprop_by_pid.get(41, [])
                           if _pf["subtype"] == "Will"), default=0)
    fort = fort + _iprp_save_univ + _iprp_save_fort
    ref = ref + _iprp_save_univ + _iprp_save_ref
    will = will + _iprp_save_univ + _iprp_save_will

    # Monk Wisdom AC bonus: ≥1 monk level and neither armor nor shield.
    _monk_levels = sum(
        int(fld(cl, "ClassLevel", 0) or 0)
        for cl in classes
        if int(fld(cl, "Class", -1) or -1) == MONK_CLASS_ID
    )
    _monk_bab = creature_class_bab(classes, MONK_CLASS_ID, db.max_character_level)
    _monk_wis_ac = 0
    if _monk_levels >= 1 and armor_base_ac == 0 and shield_item is None:
        _monk_wis_ac = max(wis_mod, 0)
        if _monk_wis_ac:
            ac_total += _monk_wis_ac
            ac_breakdown.append(f"+{_monk_wis_ac} Wis (monk)")
        _monk_lvl_ac = _monk_levels // 5
        if _monk_lvl_ac:
            ac_total += _monk_lvl_ac
            ac_breakdown.append(f"+{_monk_lvl_ac} dodge (monk level)")

    # Spell resistance: max across sources (they don't stack).
    _ISR_FEAT_BASE = 699
    _feat_sr = max(
        (12 + 2 * (fid - _ISR_FEAT_BASE) for fid in feat_ids
         if _ISR_FEAT_BASE <= fid <= _ISR_FEAT_BASE + 9),
        default=0,
    )
    _diamond_soul_sr = (10 + _monk_levels) if 215 in feat_ids else 0
    _item_sr = max((cv for _, cv in cprop_by_pid.get(39, [])), default=0)
    sr_total = max(_feat_sr, _diamond_soul_sr, _item_sr)

    _hp_raw = _f('MaxHitPoints', _f('HitPoints', ''))
    try:
        _base_hp = int(_hp_raw)
    except (TypeError, ValueError):
        _base_hp = None
    _et_hp = epic_toughness_hp(feat_ids)
    if _base_hp is not None and _et_hp:
        hp_display = (f"{_fmt_hp(_base_hp + _et_hp)} "
                      f"({_fmt_hp(_base_hp)} + {_et_hp} Epic Toughness)")
    else:
        hp_display = _fmt_hp(_hp_raw)
    hp_value = (_base_hp + _et_hp) if _base_hp is not None else None

    # ----- Structured defenses (for analysis/export) ------------------------
    # The HTML "combined combat properties" table is built separately in
    # _creature_detail_sections from cprop_by_pid; these mirror the same
    # max-per-subtype rules into plain data.
    dr: dict | None = None
    if 22 in cprop_by_pid:
        _best = None
        for _pf, _cv in cprop_by_pid[22]:
            if _best is None or _cv > _best[0]:
                _best = (_cv, _pf["subtype"], _pf["cost"])
        if _best:
            dr = {"soak": (_best[2] or "").strip(), "bypass": (_best[1] or "").strip()}

    resistances: dict[str, int] = {}
    for _pf, _cv in cprop_by_pid.get(23, []):
        _sub = _pf["subtype"] or "?"
        if _cv > resistances.get(_sub, 0):
            resistances[_sub] = _cv

    immunities: dict[str, int] = {}
    for _pf, _cv in cprop_by_pid.get(20, []):
        _sub = _pf["subtype"] or "?"
        _pct = _prop_value_num(_pf["cost"]) if _pf["cost"] else _cv
        if _pct > immunities.get(_sub, 0):
            immunities[_sub] = _pct

    spell_immunities = sorted({_pf["cost"] for _pf, _ in cprop_by_pid.get(53, [])
                               if _pf["cost"]})

    # Miscellaneous immunities (crit, sneak attack, mind-affecting, …) come from
    # two independent sources: equipped gear (property 37) and the creature's
    # racial type (engine rule, nothing in the .utc). Merge them into one map so
    # callers can filter on the effective immunity and still see where it came from.
    race_immunities = creature_race_immunities(_race_id)
    misc_immunities: dict[str, str] = {}
    for _lbl in race_immunities:
        misc_immunities[_lbl] = "race"
    for _pf, _ in cprop_by_pid.get(37, []):
        _lbl = _pf["subtype"] or "?"
        misc_immunities[_lbl] = "race+gear" if _lbl in misc_immunities else "gear"

    regen = sum(cv for _, cv in cprop_by_pid.get(51, []))
    vampiric = sum(cv for _, cv in cprop_by_pid.get(67, []))

    # Damage gate: a non-stock OnDamaged handler can change how this creature
    # takes damage (require a specific weapon tag, self-heal, etc.).
    dmg_script = (_f("ScriptDamaged", "") or "").strip()
    _is_custom = bool(dmg_script) and db.is_custom_damage_script(dmg_script)
    hard_required_tags = (sorted(db.script_damage_req_tags.get(dmg_script, set()))
                          if _is_custom else [])
    mitigates_damage = bool(_is_custom and dmg_script in db.script_mitigates_damage)
    retaliation = db.script_retaliation.get(dmg_script) if _is_custom else None

    return {
        "ability_scores": ability_scores,
        "race_adj": race_adj,
        "item_abil": item_abil,
        "str_mod": str_mod, "dex_mod": dex_mod,
        "con_mod": con_mod, "wis_mod": wis_mod,
        "bab": bab,
        "feat_ids": feat_ids,
        "equip_by_slot": equip_by_slot,
        "monk_levels": _monk_levels, "monk_bab": _monk_bab,
        "ac": ac_total, "ac_breakdown": ac_breakdown, "ac_extra": ac_extra,
        "fort": fort, "ref": ref, "will": will,
        "sr": sr_total, "sr_feat": _feat_sr,
        "sr_diamond_soul": _diamond_soul_sr, "sr_item": _item_sr,
        "hp_display": hp_display, "hp": hp_value,
        "cprop_by_pid": cprop_by_pid, "cprop_dodge_ac": cprop_dodge_ac,
        "dr": dr, "resistances": resistances, "immunities": immunities,
        "race_immunities": race_immunities, "misc_immunities": misc_immunities,
        "spell_immunities": spell_immunities, "regen": regen, "vampiric": vampiric,
        "dmg_script": dmg_script, "hard_required_tags": hard_required_tags,
        "mitigates_damage": mitigates_damage, "retaliation": retaliation,
    }


def extract_creature_offense(db: "Db", c: dict, bp: "dict | None", D: dict) -> dict:
    """Compute a creature's *offensive* threat (what the player must survive), as
    plain data for the counter-gear survivability matcher. Reuses the ability
    mods / BAB / feats / equipment already resolved by extract_creature_defenses
    (passed in as `D`) so the combat math is computed once. Returns:

      attack_bonus       best (first-iterative) to-hit across its weapons
      ac_target          AC at which the creature misses ~half the time
      damage_types_dealt damage types its attacks deal (weapon physical + extras)
      save_threats       special abilities the player must save against, with an
                         *estimated* DC (innate spell level is not in our caches,
                         so it is derived from caster level — labelled an estimate)
      attack_profiles    one attack_profile() per wielded weapon (see that
                         function) — the full iterative schedule plus damage
                         dice/crit/elemental data the combat simulator needs.
                         `attack_bonus` is still just the best schedule[0], so
                         the published creature-page figure is unaffected.
    """
    equip_by_slot: dict[int, dict] = D["equip_by_slot"]
    str_mod, dex_mod = D["str_mod"], D["dex_mod"]
    bab, feat_ids = D["bab"], D["feat_ids"]
    monk_bab, monk_levels = D["monk_bab"], D["monk_levels"]

    def _resref(e: dict) -> str:
        return fld(e, "EquippedRes", "") or fld(e, "TemplateResRef", "") or ""

    def _item(e: dict) -> dict | None:
        irr = _resref(e)
        if irr and irr in db.items:
            return db.items[irr]
        return e if "BaseItem" in e else None

    best_atk: int | None = None
    dtypes: set[str] = set()
    have_weapon = False
    profiles: list[dict] = []

    # Creatures get only the critical feats they were actually built with — no
    # "assume specced" here, unlike the reference PC.
    _feat_set = set(feat_ids)
    _str_score = D.get("ability_scores", {}).get("Str") or 10

    def _profile(sched: list[int], bi: int, item: dict | None, off: dict,
                 str_bonus: int) -> dict:
        """Build the simulator's attack_profile for one wielded weapon."""
        stats = WEAPONS.get(bi) or {}
        prop_flat, elem = weapon_damage_props(item)
        crit = crit_feat_effects(
            bi, bab=bab, str_score=_str_score,
            has_feat=lambda fid: fid in _feat_set,
            devcrit_bonus_dice=db.devcrit_bonus_dice)
        return attack_profile(
            sched,
            num_dice=_try_int(stats.get("NumDice"), 0),
            die=_try_int(stats.get("DieToRoll"), 0),
            flat=str_bonus + prop_flat,
            crit_threat=(_try_int(stats.get("CritThreat"), 1) or 1) * crit["threat_mult"],
            crit_mult=_try_int(stats.get("CritHitMult"), 2) or 2,
            phys_types=off["physical_dtypes"],
            elem=elem,
            enhancement=off["enhancement"],
            crit_bonus=crit["crit_bonus"],
            devcrit_save_dc=crit["devcrit_save_dc"],
        )

    for slot in (SLOT_RIGHT, SLOT_LEFT, SLOT_CWEAP_R, SLOT_CWEAP_L, SLOT_CWEAP_B):
        e = equip_by_slot.get(slot)
        if not e:
            continue
        item = _item(e)
        if item is None:
            continue
        base_row = fld(item, "BaseItem")
        bi = int(base_row) if base_row is not None else -1
        if slot == SLOT_LEFT and bi in SHIELD_BASEITEMS:
            continue
        off = extract_item_offense(db, item, _resref(e) or "")
        if not off["is_weapon"]:
            continue
        have_weapon = True
        dtypes.update(off["damage_dtypes"])
        ranged = off["is_ranged"]
        _is_cweap = slot in _CWEAP_SLOTS
        _finesse = (WEAPON_FINESSE_FEAT in feat_ids and dex_mod > str_mod
                    and (_is_cweap or bi in FINESSE_BASEITEMS))
        _melee_ab = dex_mod if _finesse else str_mod
        _feat_ab = feat_attack_bonus(feat_ids, baseitem_name(bi) if bi >= 0 else "")
        atk_mod = (dex_mod if ranged else _melee_ab) + off["attack_bonus"] + _feat_ab
        _monk_atk_bab = monk_bab if (_is_cweap and monk_levels >= 1 and not ranged) else 0
        sched = attack_schedule(bab, ability_mod=atk_mod, monk_unarmed_bab=_monk_atk_bab)
        if sched and (best_atk is None or sched[0] > best_atk):
            best_atk = sched[0]
        if sched:
            # Ranged weapons add no Str to damage unless Mighty; melee adds the
            # same ability the attack used (finesse switches to Dex for to-hit
            # only — damage stays Str in NWN).
            profiles.append(_profile(sched, bi, item, off, 0 if ranged else str_mod))

    if not have_weapon:
        dtypes = {"Bludgeoning"}  # unarmed
        if bab > 0:
            _u_finesse = WEAPON_FINESSE_FEAT in feat_ids and dex_mod > str_mod
            _u_ability = (dex_mod if _u_finesse else str_mod) + feat_attack_bonus(feat_ids, "unarmed")
            sched = attack_schedule(
                bab, ability_mod=_u_ability,
                monk_unarmed_bab=(monk_bab if monk_levels >= 1 else 0))
            if sched:
                best_atk = sched[0]
                # Unarmed: 1d3 bludgeoning + Str, no crit multiplier beyond x2.
                profiles.append(attack_profile(
                    sched, num_dice=1, die=3, flat=str_mod,
                    crit_threat=1, crit_mult=2,
                    phys_types=["Bludgeoning"], elem={}, enhancement=0))

    attack_bonus = best_atk if best_atk is not None else 0

    # ----- Special abilities: what the player must save against --------------
    spec = list_items(c.get("SpecAbilityList"))
    if not spec and bp is not None:
        spec = list_items(bp.get("SpecAbilityList"))
    scores = D.get("ability_scores", {})
    _mental = max((ability_mod(scores.get(a)) for a in ("Int", "Wis", "Cha")
                   if scores.get(a) is not None), default=0)
    seen: set[tuple] = set()
    save_threats: list[dict] = []
    for s in spec:
        sid = fld(s, "Spell")
        if sid is None:
            continue
        cl = fld(s, "SpellCasterLevel", 0) or 0
        try:
            cl = int(cl)
        except (TypeError, ValueError):
            cl = 0
        key = (sid, cl)
        if key in seen:
            continue
        seen.add(key)
        # Innate spell level isn't in our caches; estimate it from caster level
        # (≈ half, capped at 9) for a build-agnostic DC ballpark.
        lvl_est = max(1, min(9, (cl + 1) // 2))
        save_threats.append({
            "name": nwn_text(spell_name(sid)),
            "caster_level": cl,
            "dc_est": 10 + lvl_est + _mental,
        })
    save_threats.sort(key=lambda t: t["dc_est"], reverse=True)

    return {
        "attack_bonus": attack_bonus,
        "ac_target": attack_bonus + 10,
        "damage_types_dealt": sorted(dtypes),
        "save_threats": save_threats,
        "attack_profiles": profiles,
    }


def _creature_detail_sections(
    db: Db,
    c: dict,
    *,
    bp: dict | None = None,
    root_rel: str = "..",
) -> list[str]:
    """Body sections (abilities, combat, weapons, equipment, inventory,
    feats, skills, spells, scripts) shared by blueprint and instance pages.
    When `bp` is provided, fields missing from the instance struct fall
    back to the blueprint — most GIT placements keep the UTC defaults."""
    items_dir = f"{root_rel}/items"

    def _f(key: str, default: Any = None) -> Any:
        v = fld(c, key, None)
        if v is None and bp is not None:
            v = fld(bp, key, default)
        return default if v is None else v

    def _list(key: str) -> list[dict]:
        items = list_items(c.get(key))
        if not items and bp is not None:
            items = list_items(bp.get(key))
        return items

    classes = _list("ClassList")
    feats = _list("FeatList")
    skills = _list("SkillList")
    equip = _list("Equip_ItemList")
    inv = list_items(c.get("ItemList"))

    def _equip_resref(e: dict) -> str:
        # Blueprint equip entries reference an external UTI via EquippedRes;
        # GIT instances inline the item but still carry TemplateResRef.
        return fld(e, "EquippedRes", "") or fld(e, "TemplateResRef", "") or ""

    def _equip_item(e: dict) -> dict | None:
        irr = _equip_resref(e)
        if irr and irr in db.items:
            return db.items[irr]
        # GIT instances embed the full item struct (BaseItem, PropertiesList,
        # LocalizedName, …) directly into the equip entry.
        return e if "BaseItem" in e else None

    # Combat defenses (AC, saves, SR, HP, resistances, damage gate) are computed
    # once by extract_creature_defenses() so this page and the counter-gear
    # analysis stay in lockstep; see that function for the NWN rule details.
    D = extract_creature_defenses(db, c, bp)
    race_adj = D["race_adj"]
    item_abil = D["item_abil"]
    ability_scores = D["ability_scores"]

    def _abil_cell(ab: str) -> str:
        val = ability_scores.get(ab)
        if val is None:
            return ""
        adj = race_adj.get(ab, 0)
        item = item_abil.get(ab, 0)
        notes = []
        if adj:
            notes.append(f"{adj:+d} racial")
        if item:
            notes.append(f"+{item} item")
        note = (f" <small class=\"muted\">({', '.join(notes)})</small>" if notes else "")
        return f"{val}{note}"

    sections: list[str] = [
        '<h2>Abilities</h2>',
        '<table class="data"><tr>'
        f"<th>Str</th><td>{_abil_cell('Str')}</td>"
        f"<th>Dex</th><td>{_abil_cell('Dex')}</td>"
        f"<th>Con</th><td>{_abil_cell('Con')}</td>"
        f"<th>Int</th><td>{_abil_cell('Int')}</td>"
        f"<th>Wis</th><td>{_abil_cell('Wis')}</td>"
        f"<th>Cha</th><td>{_abil_cell('Cha')}</td>"
        "</tr></table>",
    ]

    if classes:
        cls_rows = "".join(
            f"<tr><td>{E(class_name(fld(cl, 'Class')))}</td>"
            f"<td>{E(fld(cl, 'ClassLevel', ''))}</td></tr>"
            for cl in classes
        )
        sections.append(
            "<h2>Classes</h2>"
            '<table class="data"><thead><tr><th>Class</th><th>Level</th></tr></thead>'
            f"<tbody>{cls_rows}</tbody></table>"
        )

    desc = loc(c.get("Description"))
    if not desc and bp is not None:
        desc = loc(bp.get("Description"))
    if desc:
        sections.append(f'<p class="desc">{nwn_html(desc)}</p>')

    # ----- Combat: read the precomputed defenses (see extract_creature_defenses) -----
    str_mod = D["str_mod"]
    dex_mod = D["dex_mod"]
    bab = D["bab"]
    feat_ids = D["feat_ids"]
    equip_by_slot = D["equip_by_slot"]
    _monk_levels = D["monk_levels"]
    _monk_bab = D["monk_bab"]
    cprop_by_pid = D["cprop_by_pid"]
    cprop_dodge_ac = D["cprop_dodge_ac"]
    race_immunities = D["race_immunities"]
    misc_immunities = D["misc_immunities"]
    ac_total = D["ac"]
    ac_breakdown = D["ac_breakdown"]
    ac_extra = D["ac_extra"]
    fort, ref, will = D["fort"], D["ref"], D["will"]
    sr_total = D["sr"]
    _feat_sr = D["sr_feat"]
    _diamond_soul_sr = D["sr_diamond_soul"]
    _item_sr = D["sr_item"]
    _hp_display = D["hp_display"]

    sections.append("<h2>Combat</h2>")
    _combat_rows = (
        '<tr>'
        f"<th>HP</th><td>{E(_hp_display)}</td>"
        f"<th>AC</th><td>{ac_total} <small class=\"muted\">"
        f"({E(' + '.join(ac_breakdown))})</small></td>"
        f"<th>BAB</th><td>+{bab}</td>"
        '</tr><tr>'
        f"<th>Fort</th><td>{fort:+d}</td>"
        f"<th>Ref</th><td>{ref:+d}</td>"
        f"<th>Will</th><td>{will:+d}</td>"
        '</tr>'
    )
    if sr_total:
        _sr_sources: list[str] = []
        if _feat_sr:
            _sr_sources.append(f"feats ({_feat_sr})")
        if _diamond_soul_sr:
            _sr_sources.append(f"Diamond Soul ({_diamond_soul_sr})")
        if _item_sr:
            _sr_sources.append(f"items ({_item_sr})")
        _sr_note = f" <small class=\"muted\">(max of: {E(', '.join(_sr_sources))})</small>" if len(_sr_sources) > 1 else ""
        _combat_rows += (
            '<tr>'
            f"<th>Spell Resistance</th><td>{sr_total}{_sr_note}</td>"
            '<td></td><td></td>'
            '</tr>'
        )
    sections.append(f'<table class="data">{_combat_rows}</table>')
    if ac_extra:
        sections.append(f"<p class=\"muted\">AC extras: {E(', '.join(ac_extra))}</p>")
    # Saves caveat — class-progression bonuses (good-save tables) are added
    # by the engine at load time, so the wiki can't reproduce them exactly.
    sections.append('<p class="muted">'
                    'Saves shown = stored bonus + ability mod + item bonuses '
                    '(Saving Throw Bonus: Specific); class-table progression '
                    'and feat bonuses (Great Fortitude, etc.) are applied at runtime.</p>')

    # Damage-gate warning: a non-stock OnDamaged handler can change how this
    # creature takes damage (e.g. heal back all damage from the "wrong" weapon).
    dmg_script = (_f("ScriptDamaged", "") or "").strip()
    dmg_req_tags = db.script_damage_req_tags.get(dmg_script) if dmg_script else None
    if db.is_custom_damage_script(dmg_script) and (
        dmg_script in db.script_mitigates_damage or dmg_req_tags
    ):
        req_links: list[str] = []
        for tag in sorted(db.script_damage_req_tags.get(dmg_script, set())):
            resrefs = db.item_tag_groups.get(tag.lower(), [])
            if resrefs:
                req_links.extend(
                    link(f"{items_dir}/{irr}.html", db.item_name(irr))
                    for irr in resrefs
                )
            else:
                req_links.append(f"<code>{E(tag)}</code>")
        banner = (
            '<p class="warn-damage-gate"><strong>Note: this creature uses a custom '
            "damage script.</strong> Its <code>OnDamaged</code> handler "
            f"(<code>{E(dmg_script)}</code>) changes how it takes damage — reported "
            "damage may be healed back or ignored."
        )
        if req_links:
            banner += (
                " It can only be reliably harmed by a character wielding: "
                + ", ".join(req_links) + "."
            )
        banner += "</p>"
        sections.append(banner)

    # Retaliation: a custom OnDamaged handler that strikes back at attackers.
    ret_info = db.script_retaliation.get(dmg_script) if dmg_script else None
    if db.is_custom_damage_script(dmg_script) and ret_info:
        sections.append(
            '<p class="note-retaliation"><strong>Retaliation.</strong> '
            f"{E(_retaliation_sentence(ret_info))} "
            f'<span class="muted">(<code>OnDamaged</code>: <code>{E(dmg_script)}</code>)</span></p>'
        )

    # ----- Combined abilities / combat properties display --------------------
    # (cprop_by_pid and cprop_dodge_ac are built earlier, before ac_total.)
    # Racial immunities have no gear behind them, so this section renders even
    # for a creature carrying nothing.
    cprop_rows: list[str] = []
    if cprop_by_pid or misc_immunities:
        # Dodge AC from non-armor/non-shield accessories (already in main AC above).
        if cprop_dodge_ac:
            _dodge_sum = sum(cprop_dodge_ac)
            _dodge_eff = min(_dodge_sum, 20)
            _dodge_cap = f" (capped from +{_dodge_sum})" if _dodge_sum > 20 else ""
            cprop_rows.append(f"<tr><th>AC Bonus (Dodge, items)</th><td>+{_dodge_eff}{E(_dodge_cap)}</td></tr>")

        # Saving Throw Bonus (IDs 40, 41) — max per subtype
        _st_max: dict[str, int] = {}
        for _stpid in (40, 41):
            for _pf, _cv in cprop_by_pid.get(_stpid, []):
                _sub = _pf["subtype"] or "All"
                _st_max[_sub] = max(_st_max.get(_sub, 0), _cv)
        for _sub, _val in sorted(_st_max.items()):
            _lbl = f"Save vs. {_sub}" if _sub != "All" else "Saving Throw (all)"
            cprop_rows.append(f"<tr><th>{E(_lbl)}</th><td>+{_val}</td></tr>")

        # Ability Bonus (ID 0) — max per ability (enhancement doesn't stack)
        if 0 in cprop_by_pid:
            _ab_max: dict[str, int] = {}
            for _pf, _cv in cprop_by_pid[0]:
                _sub = _pf["subtype"] or "?"
                _ab_max[_sub] = max(_ab_max.get(_sub, 0), _cv)
            _ab_parts = [f"{_sub} +{_val}" for _sub, _val in sorted(_ab_max.items())]
            cprop_rows.append(f"<tr><th>Ability Bonuses</th><td>{E(', '.join(_ab_parts))}</td></tr>")

        # Skill Bonus (ID 52) — max per skill
        if 52 in cprop_by_pid:
            _sk_max: dict[str, int] = {}
            for _pf, _cv in cprop_by_pid[52]:
                _sub = _pf["subtype"] or "?"
                _sk_max[_sub] = max(_sk_max.get(_sub, 0), _cv)
            _sk_parts = [f"{_sub} +{_val}" for _sub, _val in sorted(_sk_max.items())]
            cprop_rows.append(f"<tr><th>Skill Bonuses</th><td>{E(', '.join(_sk_parts))}</td></tr>")

        # Damage Resistance (ID 23) — max per damage type
        if 23 in cprop_by_pid:
            _dr_max: dict[str, tuple[int, str]] = {}
            for _pf, _cv in cprop_by_pid[23]:
                _sub = _pf["subtype"] or "?"
                _cstr = _pf["cost"] or str(_cv)
                if _sub not in _dr_max or _cv > _dr_max[_sub][0]:
                    _dr_max[_sub] = (_cv, _cstr)
            _dr_parts = [f"{_sub} {_cstr}" for _sub, (_, _cstr) in sorted(_dr_max.items())]
            cprop_rows.append(f"<tr><th>Damage Resistance</th><td>{E(', '.join(_dr_parts))}</td></tr>")

        # Damage Reduction (ID 22) — physical soak X / bypassed by +Y; best only.
        if 22 in cprop_by_pid:
            _best_dr: tuple[int, str, str] | None = None
            for _pf, _cv in cprop_by_pid[22]:
                if _best_dr is None or _cv > _best_dr[0]:
                    _best_dr = (_cv, _pf["subtype"], _pf["cost"])
            if _best_dr:
                _, _byp, _soak = _best_dr
                _drtxt = (_soak or "").strip()
                if _byp:
                    _drtxt = f"{_drtxt}, bypass {_byp}" if _drtxt else f"bypass {_byp}"
                cprop_rows.append(f"<tr><th>Damage Reduction</th><td>{E(_drtxt or '—')}</td></tr>")

        # Immunity: Damage Type (ID 20) — max % per damage type
        if 20 in cprop_by_pid:
            _imm_max: dict[str, tuple[int, str]] = {}
            for _pf, _cv in cprop_by_pid[20]:
                _sub = _pf["subtype"] or "?"
                _cstr = _pf["cost"] or str(_cv)
                if _sub not in _imm_max or _cv > _imm_max[_sub][0]:
                    _imm_max[_sub] = (_cv, _cstr)
            _imm_parts = [f"{_sub} ({_cstr})" for _sub, (_, _cstr) in sorted(_imm_max.items())]
            cprop_rows.append(f"<tr><th>Damage Immunity</th><td>{E(', '.join(_imm_parts))}</td></tr>")

        # Immunity: Miscellaneous — gear (ID 37) merged with racial-type
        # immunities, tagged so the two sources stay distinguishable.
        if misc_immunities:
            _SRC_TAG = {"race": " (racial)", "race+gear": " (racial, gear)", "gear": ""}
            _misc = [f"{_lbl}{_SRC_TAG[_src]}"
                     for _lbl, _src in sorted(misc_immunities.items())]
            cprop_rows.append(f"<tr><th>Misc Immunity</th><td>{E(', '.join(_misc))}</td></tr>")

        # Immunity: Specific Spell (ID 53) — list every spell the creature is
        # immune to (spell name lives in the formatted cost field).
        # Entries like "Breath, Petrification" / "Gaze, Petrification" are grouped
        # by spell name and shown as "Petrification (Breath, Gaze)".
        if 53 in cprop_by_pid:
            _DELIVERY_PREFIXES = {"Breath", "Gaze", "Touch"}
            _spell_deliveries: dict[str, list[str]] = {}
            for _pf, _ in cprop_by_pid[53]:
                _cost = _pf["cost"]
                if not _cost:
                    continue
                _parts = _cost.split(", ", 1)
                if len(_parts) == 2 and _parts[0] in _DELIVERY_PREFIXES:
                    _sname, _prefix = _parts[1], _parts[0]
                else:
                    _sname, _prefix = _cost, None
                if _sname not in _spell_deliveries:
                    _spell_deliveries[_sname] = []
                if _prefix and _prefix not in _spell_deliveries[_sname]:
                    _spell_deliveries[_sname].append(_prefix)
            _imm_spells = [
                f"{_sn} ({', '.join(sorted(_ps))})" if _ps else _sn
                for _sn, _ps in sorted(_spell_deliveries.items())
            ]
            if _imm_spells:
                cprop_rows.append(f"<tr><th>Immunity: Spells</th><td>{E(', '.join(_imm_spells))}</td></tr>")

        # Immunity: Spells by Level (ID 78) — list the level thresholds
        if 78 in cprop_by_pid:
            _imm_lvls = sorted({_pf["cost"] for _pf, _ in cprop_by_pid[78] if _pf["cost"]})
            if _imm_lvls:
                cprop_rows.append(f"<tr><th>Immunity: Spell Level</th><td>{E(', '.join(_imm_lvls))}</td></tr>")

        # Regeneration (ID 51) — stacks without limit
        if 51 in cprop_by_pid:
            _regen = sum(cv for _, cv in cprop_by_pid[51])
            cprop_rows.append(f"<tr><th>Regeneration</th><td>+{_regen} HP/round</td></tr>")

        # Vampiric Regeneration (ID 67) — stacks without limit
        if 67 in cprop_by_pid:
            _vregen = sum(cv for _, cv in cprop_by_pid[67])
            cprop_rows.append(f"<tr><th>Regen: Vampiric</th><td>+{_vregen} HP/hit</td></tr>")

        # Turn Resistance (ID 73) — stacks
        if 73 in cprop_by_pid:
            _tr = sum(cv for _, cv in cprop_by_pid[73])
            cprop_rows.append(f"<tr><th>Turn Resistance</th><td>+{_tr}</td></tr>")

        # Flag properties — present/absent, rolled into one Misc Abilities row.
        _misc_abil: list[str] = []
        if 35 in cprop_by_pid:
            _misc_abil.append("Haste (+4 dodge AC, factored into main AC)")
        for _fp_id, _fp_name in (
            (75, "Freedom of Movement"), (71, "True Seeing"), (26, "Darkvision"),
        ):
            if _fp_id in cprop_by_pid:
                _misc_abil.append(_fp_name)
        if _misc_abil:
            cprop_rows.append(f"<tr><th>Misc Abilities</th><td>{E(', '.join(_misc_abil))}</td></tr>")

    if cprop_rows:
        sections.append(
            "<h3>Abilities &amp; combat properties</h3>"
            '<table class="data"><tbody>' + "\n".join(cprop_rows) + "</tbody></table>"
            '<p class="muted">Dodge AC and save bonuses are factored into the main '
            'combat stats above. Dodge AC stacks (cap +20); haste adds +4 more (separate). '
            'All other same-type bonuses do not stack — only the highest is shown. '
            'Regeneration stacks without limit. Immunities marked <em>(racial)</em> are '
            'granted by the creature\'s racial type by the engine, not by its equipment.</p>'
        )

    # ----- Weapons / attack schedule ----------------------------------------
    weapon_rows: list[str] = []
    for slot in (SLOT_RIGHT, SLOT_LEFT, SLOT_CWEAP_R, SLOT_CWEAP_L, SLOT_CWEAP_B):
        e = equip_by_slot.get(slot)
        if not e:
            continue
        irr = _equip_resref(e)
        item = _equip_item(e)
        base_row = fld(item, "BaseItem") if item else None
        # Skip non-weapons in the left-hand slot (shields).
        if slot == SLOT_LEFT and item and int(base_row or -1) in SHIELD_BASEITEMS:
            continue
        ranged = is_ranged_weapon(base_row)
        kind = "Ranged" if ranged else "Melee"
        ab_iprop = item_attack_bonus(item)
        dmg_flat, dmg_extras = item_damage_bonus(item)
        # Weapon Finesse lets creature weapons (claw/bite) and finessable light
        # weapons use the Dex modifier — but the engine uses the *better* of Str
        # and Dex, so only switch to Dex when it actually beats Str.
        _is_cweap = slot in _CWEAP_SLOTS
        _finesse = (
            WEAPON_FINESSE_FEAT in feat_ids and dex_mod > str_mod
            and (_is_cweap or int(base_row or -1) in FINESSE_BASEITEMS)
        )
        _melee_ab = dex_mod if _finesse else str_mod
        _wname = baseitem_name(base_row) if base_row is not None else ""
        _feat_ab = feat_attack_bonus(feat_ids, _wname)
        atk_mod = (dex_mod if ranged else _melee_ab) + ab_iprop + _feat_ab
        # Monk unarmed/creature-weapon flurry: faster schedule with ≥1 monk level.
        _monk_atk_bab = _monk_bab if (_is_cweap and _monk_levels >= 1 and not ranged) else 0
        attacks = attack_schedule(bab, ability_mod=atk_mod, monk_unarmed_bab=_monk_atk_bab)
        atk_str = "/".join(f"{a:+d}" for a in attacks)
        dmg_str = weapon_damage_string(
            base_row, str_mod=str_mod, is_ranged=ranged,
            iprop_dmg_bonus=dmg_flat, iprop_extra=dmg_extras,
        )
        crit_str = weapon_crit_string(base_row)
        if irr in db.items:
            wname = db.item_name(irr)
        else:
            wname = loc(e.get("LocalizedName")) or irr or "(empty)"
        wlink = (link(f"{items_dir}/{irr}.html", wname)
                 if irr in db.items else nwn_html(wname))
        bname = baseitem_name(base_row) if base_row is not None else ""
        rng_str = ""
        stats = WEAPONS.get(int(base_row)) if base_row is not None else None
        if ranged and stats:
            rng_str = f"{stats.get('MaxRange', '?')}"
        weapon_rows.append(
            f"<tr><td>{E(SLOT_NAMES.get(slot, slot))}</td>"
            f"<td>{wlink}</td>"
            f"<td>{E(bname)}</td>"
            f"<td>{E(kind)}{(' (' + rng_str + ')') if rng_str else ''}</td>"
            f"<td>{E(atk_str)}</td>"
            f"<td>{colorize_damage_words(E(dmg_str))}</td>"
            f"<td>{E(crit_str)}</td></tr>"
        )
    if weapon_rows:
        sections.append(
            "<h3>Weapons</h3>"
            '<table class="data"><thead><tr>'
            "<th>Slot</th><th>Item</th><th>Base</th><th>Type</th>"
            "<th>Attack schedule</th><th>Damage</th><th>Crit</th>"
            "</tr></thead><tbody>" + "\n".join(weapon_rows) + "</tbody></table>"
        )
    elif bab > 0:
        # Unarmed — show the schedule with Str-mod damage so the reader still
        # has something to work with. Unarmed is finessable (Dex when it beats
        # Str), and ≥1 monk level gives the faster unarmed flurry.
        _u_finesse = WEAPON_FINESSE_FEAT in feat_ids and dex_mod > str_mod
        _u_ability = (dex_mod if _u_finesse else str_mod) + feat_attack_bonus(feat_ids, "unarmed")
        attacks = attack_schedule(
            bab, ability_mod=_u_ability,
            monk_unarmed_bab=(_monk_bab if _monk_levels >= 1 else 0))
        atk_str = "/".join(f"{a:+d}" for a in attacks)
        sections.append(
            "<h3>Weapons</h3>"
            f"<p>Unarmed: {E(atk_str)} · 1d{3 if str_mod < 5 else 6} "
            f"{('+' + str(str_mod)) if str_mod > 0 else (str(str_mod) if str_mod < 0 else '')}"
            "</p>"
        )

    # ----- Spells (memorized + known) ---------------------------------------
    spell_blocks: list[str] = []
    for cl in classes:
        cid = fld(cl, "Class")
        if cid is None:
            continue
        # Detect any per-level memorized / known list on this class entry.
        per_level: list[tuple[int, str, list[dict]]] = []
        for lvl in range(10):
            mem = list_items(cl.get(f"MemorizedList{lvl}"))
            if mem:
                per_level.append((lvl, "Memorized", mem))
            known = list_items(cl.get(f"KnownList{lvl}"))
            if known:
                per_level.append((lvl, "Known", known))
        if not per_level:
            continue
        cls_label = class_name(int(cid))
        block = [f"<h3>{E(cls_label)}</h3>"]
        rows: list[str] = []
        for lvl, kind, lst in per_level:
            _sc_order: list[tuple] = []
            _sc_count: dict[tuple, int] = {}
            for s in lst:
                sid = fld(s, "Spell")
                meta = fld(s, "SpellMetaMagic", 0) or 0
                key = (sid, meta)
                if key not in _sc_count:
                    _sc_count[key] = 0
                    _sc_order.append(key)
                _sc_count[key] += 1
            spell_cells: list[str] = []
            for (sid, meta) in _sc_order:
                n = _sc_count[(sid, meta)]
                label = colorize_damage_words(spell_name(sid))
                if meta:
                    label = f"{label} <small>[mm:{meta}]</small>"
                if n > 1:
                    label = f"{label} ×{n}"
                spell_cells.append(label)
            label = ("Cantrips" if lvl == 0 else f"Level {lvl}") + f" ({kind})"
            rows.append(
                f"<tr><th>{E(label)}</th>"
                f"<td>{', '.join(spell_cells)}</td></tr>"
            )
        block.append(
            '<table class="data"><tbody>' + "\n".join(rows) + "</tbody></table>"
        )
        spell_blocks.append("\n".join(block))

    # SpecAbilityList — innate spell-likes (SR feats, monster abilities).
    # Group identical (spell, caster_level) entries; many UTCs list the same
    # ability ten times to give it ten uses/day.
    spec_abil = list_items(c.get("SpecAbilityList"))
    if not spec_abil and bp is not None:
        spec_abil = list_items(bp.get("SpecAbilityList"))
    if spec_abil:
        bucket: dict[tuple[Any, Any], int] = defaultdict(int)
        for s in spec_abil:
            key = (fld(s, "Spell"), fld(s, "SpellCasterLevel", ""))
            bucket[key] += 1
        rows = []
        for (sid, slvl), n in sorted(
                bucket.items(),
                key=lambda kv: spell_name(kv[0][0]).lower()):
            uses = f"{n}/day" if n > 1 else "1/day"
            rows.append(
                f"<tr><td>{colorize_damage_words(E(spell_name(sid)))}</td>"
                f"<td>{E(slvl)}</td>"
                f"<td>{E(uses)}</td></tr>"
            )
        spell_blocks.append(
            "<h3>Special abilities</h3>"
            '<table class="data"><thead><tr>'
            "<th>Spell/ability</th><th>Caster level</th><th>Uses</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    if spell_blocks:
        sections.append("<h2>Spells</h2>")
        sections.extend(spell_blocks)

    # Equipment
    if equip:
        sections.append("<h2>Equipment</h2>")
        rows = []
        for e in equip:
            irr = _equip_resref(e)
            if irr in db.items:
                cell = link(f"{items_dir}/{irr}.html", db.item_name(irr))
            else:
                disp = loc(e.get("LocalizedName")) or irr or "(empty)"
                cell = nwn_html(disp)
            slot = fld(e, "__struct_id", "")
            slot_label = SLOT_NAMES.get(int(slot), str(slot)) if isinstance(slot, int) else str(slot)
            droppable = bool(int(fld(e, "Dropable", 0) or 0))
            rows.append(f"<tr><td>{E(slot_label)}</td><td>{cell}</td><td>{_yn(droppable)}</td></tr>")
        sections.append(
            '<table class="data"><thead><tr><th>Slot</th><th>Item</th><th>Lootable</th></tr></thead>'
            "<tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # Inventory
    if inv:
        sections.append(f"<h2>Inventory <small class=\"muted\">({len(inv)})</small></h2>")
        rows = []
        for it in inv:
            irr = (fld(it, "InventoryRes", "") or
                   fld(it, "TemplateResRef", "") or
                   fld(it, "EquippedRes", ""))
            if irr in db.items:
                iname = db.item_name(irr)
                iobj = db.items[irr]
                cat_key = _item_category(iobj, nwn_text(iname))
                cat_label = _item_category_label(cat_key)
                cat_slug = cat_key.replace("_", "-")
                type_cell = link(f"{items_dir}/index.html#{E(cat_slug)}", cat_label)
            else:
                iname = loc(it.get("LocalizedName")) or irr or "(unknown)"
                type_cell = ""
            cell = (link(f"{items_dir}/{irr}.html", iname)
                    if irr in db.items else nwn_html(iname))
            droppable = bool(int(fld(it, "Dropable", 0) or 0))
            pickpocketable = bool(int(fld(it, "Pickpocketable", 0) or 0))
            rows.append(
                f"<tr><td>{cell}</td><td>{type_cell}</td>"
                f"<td>{_yn(droppable)}</td><td>{_yn(pickpocketable)}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            '<th>Item</th><th>Item Type</th><th>Lootable</th><th>Pickpocketable</th>'
            '</tr></thead>'
            "<tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # ----- Feats ------------------------------------------------------------
    if feats:
        # Group identical feat ids (some appear multiple times for stacking
        # epic feats like Epic Toughness 1..10).
        counts = Counter(feat_ids)
        feat_cells: list[str] = []
        for fid, n in sorted(counts.items(), key=lambda kv: feat_name(kv[0]).lower()):
            label = feat_name(fid)
            cell = colorize_damage_words(E(label)) + (f" ×{n}" if n > 1 else "")
            feat_cells.append(f"<li>{cell}</li>")
        sections.append(
            f"<h2>Feats <small class=\"muted\">({len(feats)})</small></h2>"
            f'<ul class="featlist">{"".join(feat_cells)}</ul>'
        )

    # ----- Skills (skip zero ranks) -----------------------------------------
    if skills:
        skill_rows: list[str] = []
        for idx, s in enumerate(skills):
            rank = fld(s, "Rank", 0) or 0
            if int(rank) <= 0:
                continue
            skill_rows.append(
                f"<tr><td>{E(skill_name(idx))}</td><td>{E(rank)}</td></tr>"
            )
        if skill_rows:
            sections.append(
                "<h2>Skills</h2>"
                '<table class="data"><thead><tr>'
                "<th>Skill</th><th>Ranks</th>"
                "</tr></thead><tbody>" + "\n".join(skill_rows) + "</tbody></table>"
            )

    # Scripts
    sections.append("<h2>Scripts</h2>")
    script_fields = [
        "ScriptHeartbeat", "ScriptOnNotice", "ScriptEndRound", "ScriptDialogue",
        "ScriptAttacked", "ScriptDamaged", "ScriptDeath", "ScriptDisturbed",
        "ScriptSpawn", "ScriptRested", "ScriptSpellAt", "ScriptUserDefine",
        "ScriptOnBlocked",
    ]
    rows = []
    for sf in script_fields:
        v = _f(sf, "")
        rows.append(f"<tr><td>{E(sf)}</td><td>{E(v)}</td></tr>")
    sections.append(
        '<table class="data"><thead><tr><th>Event</th><th>Script</th></tr></thead>'
        "<tbody>" + "\n".join(rows) + "</tbody></table>"
    )

    return sections


_CREATURE_SEARCH_JS = r"""(function(){
var N=4,data=[],form=document.getElementById('cf'),results=document.getElementById('cr_out');
var modeSels=[],propSels=[],subSels=[],minVals=[];
for(var i=1;i<=N;i++){
  modeSels.push(document.getElementById('cm'+i));
  propSels.push(document.getElementById('cp'+i));
  subSels.push(document.getElementById('cs'+i));
  minVals.push(document.getElementById('cv'+i));
}
function $(id){return document.getElementById(id);}
function num(id){var v=parseFloat($(id).value);return isNaN(v)?null:v;}

fetch('search-index.json').then(function(r){return r.json();}).then(function(d){
  data=d; populateFilters();
  results.innerHTML='<p class="muted">'+data.length+' creatures indexed. Set filters above and click Search.</p>';
});

function fillSel(sel,vals){
  vals.forEach(function(v){sel.appendChild(new Option(v,v));});
}

function populateFilters(){
  var props={},subs={},races={},classes={},areas={},factions={};
  data.forEach(function(cr){
    if(cr.race)races[cr.race]=1;
    if(cr.faction)factions[cr.faction]=1;
    (cr.classes||[]).forEach(function(c){classes[c.n]=1;});
    (cr.areas||[]).forEach(function(a){areas[a]=1;});
    (cr.props||[]).forEach(function(p){
      props[p.p]=1;
      if(p.s){if(!subs[p.p])subs[p.p]={};subs[p.p][p.s]=1;}
    });
  });
  window._csubs=subs;
  fillSel($('frace'),Object.keys(races).sort());
  fillSel($('fclass'),Object.keys(classes).sort());
  fillSel($('farea'),Object.keys(areas).sort());
  fillSel($('ffac'),Object.keys(factions).sort());
  var propOpts=Object.keys(props).sort();
  propSels.forEach(function(sel){fillSel(sel,propOpts);});
}

propSels.forEach(function(sel,idx){
  sel.addEventListener('change',function(){
    var chosen=sel.value,sub=(window._csubs||{})[chosen]||{};
    subSels[idx].innerHTML='<option value="">— any —</option>';
    Object.keys(sub).sort().forEach(function(s){subSels[idx].appendChild(new Option(s,s));});
    subSels[idx].disabled=!chosen||!Object.keys(sub).length;
  });
});

function propMatches(cr,c){
  return (cr.props||[]).filter(function(p){
    if(c.prop&&p.p!==c.prop)return false;
    if(c.sub&&p.s!==c.sub)return false;
    if(c.minv>0&&(p.v||0)<c.minv)return false;
    return true;
  });
}

form.addEventListener('submit',function(e){
  e.preventDefault();
  var q=($('fq').value||'').trim().toLowerCase();
  var race=$('frace').value,cls=$('fclass').value,area=$('farea').value,fac=$('ffac').value;
  var clvl=parseInt($('fclvl').value,10)||0;
  var crMin=num('fcrmin'),crMax=num('fcrmax');
  var hpMin=num('fhpmin'),hpMax=num('fhpmax');
  var acMin=num('facmin'),babMin=num('fbabmin'),srMin=num('fsrmin');
  var saveMin=num('fsavemin');
  var inMod=$('fim').checked;
  var bossEl=$('fboss'),bossOnly=bossEl&&bossEl.checked;
  var sortBy=$('fo').value,asc=$('fd').value==='asc';

  var conds=[];
  for(var i=0;i<N;i++){
    var prop=propSels[i].value,sub=subSels[i].value;
    var minv=parseInt(minVals[i].value,10)||0;
    if(prop||sub||minv>0)conds.push({prop:prop,sub:sub,minv:minv,neg:modeSels[i].value==='lacks'});
  }

  var out=[];
  data.forEach(function(cr){
    if(q&&cr.name.toLowerCase().indexOf(q)<0)return;
    if(race&&cr.race!==race)return;
    if(fac&&cr.faction!==fac)return;
    if(area&&(cr.areas||[]).indexOf(area)<0)return;
    if(inMod&&!cr.count)return;
    if(bossOnly&&!cr.boss)return;
    if(cls){
      var hit=(cr.classes||[]).some(function(c){return c.n===cls&&c.l>=clvl;});
      if(!hit)return;
    }else if(clvl>0){
      if(!(cr.classes||[]).some(function(c){return c.l>=clvl;}))return;
    }
    if(crMin!==null&&(cr.cr===null||cr.cr<crMin))return;
    if(crMax!==null&&(cr.cr===null||cr.cr>crMax))return;
    if(hpMin!==null&&(cr.hp===null||cr.hp<hpMin))return;
    if(hpMax!==null&&(cr.hp===null||cr.hp>hpMax))return;
    if(acMin!==null&&(cr.ac||0)<acMin)return;
    if(babMin!==null&&(cr.bab||0)<babMin)return;
    if(srMin!==null&&(cr.sr||0)<srMin)return;
    if(saveMin!==null&&Math.min(cr.fort||0,cr.ref||0,cr.will||0)<saveMin)return;

    var matched=[];
    var ok=conds.every(function(c){
      var mp=propMatches(cr,c);
      if(c.neg)return mp.length===0;
      if(!mp.length)return false;
      matched.push(mp[0]);
      return true;
    });
    if(!ok)return;
    out.push({c:cr,matched:matched});
  });

  out.sort(function(a,b){
    var v;
    if(sortBy==='name')v=a.c.name.localeCompare(b.c.name);
    else if(sortBy==='value')v=(a.matched[0]?(a.matched[0].v||0):0)-(b.matched[0]?(b.matched[0].v||0):0);
    else v=(a.c[sortBy]===null?-Infinity:(a.c[sortBy]||0))-(b.c[sortBy]===null?-Infinity:(b.c[sortBy]||0));
    if(v===0)v=a.c.name.localeCompare(b.c.name);
    return asc?v:-v;
  });
  render(out,conds.length>0);
});

function crBucket(cr){
  if(cr===null)return 'CR unset';
  var lo=Math.floor(cr/5)*5;
  return 'CR '+lo+'–'+(lo+4);
}

function tally(rows,keyFn){
  var m={};
  rows.forEach(function(r){var k=keyFn(r.c);m[k]=(m[k]||0)+1;});
  return Object.keys(m).sort(function(a,b){return m[b]-m[a]||a.localeCompare(b);})
    .map(function(k){return '<li>'+esc(k)+' <strong>'+m[k]+'</strong></li>';}).join('');
}

function render(rows,showProps){
  var total=data.length;
  var head='<p><strong>'+rows.length+'</strong> of '+total+' creature'+(total!==1?'s':'')
    +' match'+(rows.length===1?'es':'')
    +' <span class="muted">('+(total-rows.length)+' excluded)</span></p>';
  if(!rows.length){results.innerHTML=head+'<p class="muted">No creatures match.</p>';return;}
  head+='<details class="search-breakdown"><summary>Breakdown of the '+rows.length+' matches</summary>'
    +'<div class="breakdown-cols">'
    +'<div><h4>By race</h4><ul>'+tally(rows,function(c){return c.race||'(unset)';})+'</ul></div>'
    +'<div><h4>By challenge rating</h4><ul>'+tally(rows,function(c){return crBucket(c.cr);})+'</ul></div>'
    +'</div></details>';

  var cols='<th>Name</th><th>Count</th><th>Race</th><th>Class</th>'
    +'<th>CR</th><th>HP</th><th>AC</th>'
    +(showProps?'<th>Matched</th>':'');
  var html=head+'<table class="data"><thead><tr>'+cols+'</tr></thead><tbody>';
  rows.forEach(function(r){
    var c=r.c;
    var cls=(c.classes||[]).map(function(x){return esc(x.n)+' '+x.l;}).join('/');
    var props=r.matched.map(function(p){
      var label=p.a
        ?'<a href="../items/properties/index.html#'+esc(p.a)+'">'+esc(p.p)+'</a>'
        :esc(p.p);
      var det='';
      if(p.s&&p.c)det=': '+esc(p.s)+' — '+esc(p.c);
      else if(p.s)det=': '+esc(p.s);
      else if(p.c)det=' — '+esc(p.c);
      var tag=p.src==='race'?' <span class="muted">(racial)</span>':'';
      return '<span>'+label+det+tag+'</span>';
    }).join('<br>');
    var boss=c.boss?' <span class="badge-boss">boss</span>':'';
    html+='<tr><td><a href="'+esc(c.url)+'">'+esc(c.name)+'</a>'+boss+'</td>'
      +'<td>'+(c.count?c.count:'—')+'</td>'
      +'<td>'+esc(c.race||'')+'</td>'
      +'<td>'+cls+'</td>'
      +'<td>'+(c.cr===null?'—':c.cr)+'</td>'
      +'<td>'+(c.hp===null?'—':c.hp.toLocaleString())+'</td>'
      +'<td>'+(c.ac||'—')+'</td>'
      +(showProps?'<td>'+props+'</td>':'')
      +'</tr>';
  });
  results.innerHTML=html+'</tbody></table>';
}

function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
})();"""


def _variant_diff_items(
    c: dict, base_c: dict, db: "Db", *, bp: dict | None = None, base_bp: dict | None = None
) -> list[str]:
    """Return human-readable difference strings between a variant creature and its base.

    `c`/`bp` are the variant's instance struct + blueprint fallback.
    `base_c`/`base_bp` are the base canonical's struct + blueprint fallback.
    """
    def _fv(key):
        v = fld(c, key)
        return fld(bp, key) if v is None and bp is not None else v

    def _fb(key):
        v = fld(base_c, key)
        return fld(base_bp, key) if v is None and base_bp is not None else v

    def _lstv(key):
        items = list_items(c.get(key))
        if not items and bp is not None:
            items = list_items(bp.get(key))
        return items

    def _lstb(key):
        items = list_items(base_c.get(key))
        if not items and base_bp is not None:
            items = list_items(base_bp.get(key))
        return items

    diffs: list[str] = []

    # Abilities
    ABILITY_KEYS = ("Str", "Dex", "Con", "Int", "Wis", "Cha")
    ab_diffs = []
    for key in ABILITY_KEYS:
        bv, vv = _fb(key), _fv(key)
        if bv != vv and vv is not None:
            if bv is not None:
                delta = int(vv) - int(bv)
                ab_diffs.append(f"{key} {int(bv)}→{int(vv)} ({'+' if delta > 0 else ''}{delta})")
            else:
                ab_diffs.append(f"{key} +{int(vv)}")
    if ab_diffs:
        diffs.append("Abilities: " + ", ".join(ab_diffs))

    # Classes
    base_cls = {fld(cl, "Class"): fld(cl, "ClassLevel") for cl in _lstb("ClassList")}
    var_cls  = {fld(cl, "Class"): fld(cl, "ClassLevel") for cl in _lstv("ClassList")}
    cls_diffs = []
    for cid in sorted(set(base_cls) | set(var_cls), key=lambda x: int(x) if x is not None else 0):
        bl, vl = base_cls.get(cid), var_cls.get(cid)
        cname = class_name(cid)
        if bl is None:
            cls_diffs.append(f"+{cname} {vl}")
        elif vl is None:
            cls_diffs.append(f"−{cname}")
        elif bl != vl:
            cls_diffs.append(f"{cname} {bl}→{vl}")
    if cls_diffs:
        diffs.append("Classes: " + ", ".join(cls_diffs))

    # Feats
    base_feats = {int(fld(f, "Feat")) for f in _lstb("FeatList") if fld(f, "Feat") is not None}
    var_feats  = {int(fld(f, "Feat")) for f in _lstv("FeatList") if fld(f, "Feat") is not None}
    feat_diffs = (
        [f"+{feat_name(f)}" for f in sorted(var_feats - base_feats)] +
        [f"−{feat_name(f)}" for f in sorted(base_feats - var_feats)]
    )
    if feat_diffs:
        diffs.append("Feats: " + ", ".join(feat_diffs))

    # Special abilities
    base_sab = {(fld(s, "Spell"), fld(s, "SpellCasterLevel")) for s in _lstb("SpecAbilityList")}
    var_sab  = {(fld(s, "Spell"), fld(s, "SpellCasterLevel")) for s in _lstv("SpecAbilityList")}
    sab_diffs = (
        [f"+{spell_name(sp)} (CL {lv})" for sp, lv in sorted(var_sab - base_sab, key=lambda x: str(x))] +
        [f"−{spell_name(sp)} (CL {lv})" for sp, lv in sorted(base_sab - var_sab, key=lambda x: str(x))]
    )
    if sab_diffs:
        diffs.append("Special abilities: " + ", ".join(sab_diffs))

    # Equipment
    base_eq = {fld(e, "__struct_id"): (fld(e, "EquippedRes", "") or fld(e, "TemplateResRef", "") or "")
               for e in _lstb("Equip_ItemList")}
    var_eq  = {fld(e, "__struct_id"): (fld(e, "EquippedRes", "") or fld(e, "TemplateResRef", "") or "")
               for e in _lstv("Equip_ItemList")}
    eq_diffs = []
    for slot_id in sorted(set(base_eq) | set(var_eq), key=lambda x: int(x) if x is not None else 0):
        bi, vi = base_eq.get(slot_id, ""), var_eq.get(slot_id, "")
        if bi == vi:
            continue
        slot_label = SLOT_NAMES.get(int(slot_id) if slot_id is not None else 0, f"Slot {slot_id}")
        bn = db.item_name(bi) or bi
        vn = db.item_name(vi) or vi
        if not bi:
            eq_diffs.append(f"{slot_label}: +{vn}")
        elif not vi:
            eq_diffs.append(f"{slot_label}: −{bn}")
        else:
            eq_diffs.append(f"{slot_label}: {bn} → {vn}")
    if eq_diffs:
        diffs.append("Equipment: " + ", ".join(eq_diffs))

    # Natural AC
    bac, vac = _fb("NaturalAC"), _fv("NaturalAC")
    if bac != vac and vac is not None:
        diffs.append(f"Natural AC: {bac}→{vac}" if bac is not None else f"Natural AC: +{vac}")

    # Race
    br, vr = _fb("Race"), _fv("Race")
    if br != vr and vr is not None:
        diffs.append(f"Race: {race_name(br)} → {race_name(vr)}")

    # Display name (a renamed placement of an otherwise-identical blueprint)
    def _disp(fc: dict, fbp: dict | None) -> str:
        f = loc(fc.get("FirstName")) or (loc(fbp.get("FirstName")) if fbp else None)
        l = loc(fc.get("LastName")) or (loc(fbp.get("LastName")) if fbp else None)
        return ((f or "") + " " + (l or "")).strip()
    vname, bname = _disp(c, bp), _disp(base_c, base_bp)
    if vname and vname != bname:
        diffs.append(f'Named "{vname}" (vs "{bname}")' if bname else f'Named "{vname}"')

    return diffs


def render_creature_page(db: Db, canonical_rr: str, out: Path) -> None:
    """One page per unique creature (canonical entity).  Replaces the old
    separate blueprint page + instance page model."""
    entry = db.canonical_creatures.get(canonical_rr)
    if not entry:
        return
    c = entry["c"]
    bp_rr = entry["bp_rr"]
    bp = db.creatures.get(bp_rr) if bp_rr != canonical_rr else None
    name = db.canonical_creature_name(canonical_rr)

    def _meta(key: str, default: Any = None) -> Any:
        v = fld(c, key, None)
        if v is None and bp is not None:
            v = fld(bp, key, default)
        return default if v is None else v

    classes = list_items(c.get("ClassList")) or (list_items(bp.get("ClassList")) if bp else [])
    cls_str = "/".join(
        f"{class_name(fld(cl, 'Class'))} {fld(cl, 'ClassLevel', '')}"
        for cl in classes
    )
    conv = _meta("Conversation", "")
    _eff_hp_meta = creature_max_hp(c, bp)

    sections = [
        f"<h1>{nwn_html(name)}</h1>",
        '<dl class="meta">',
        f"<dt>ResRef</dt><dd>{E(canonical_rr)}</dd>",
        f"<dt>Tag</dt><dd>{E(_meta('Tag', ''))}</dd>",
        f"<dt>Race</dt><dd>{_race_link(_meta('Race'), root_rel='..')}</dd>",
        f"<dt>Appearance</dt><dd>{E(appearance_name(_meta('Appearance_Type')))}</dd>",
        f"<dt>Class(es)</dt><dd>{E(cls_str)}</dd>",
        f"<dt>HP</dt><dd>{E(_fmt_hp(_eff_hp_meta) if _eff_hp_meta is not None else _fmt_hp(_meta('MaxHitPoints', _meta('HitPoints', ''))))}</dd>",
        f"<dt>CR</dt><dd>{E(_meta('ChallengeRating', ''))}</dd>",
        f"<dt>Faction</dt><dd>{_faction_cell(db, canonical_rr, _meta('FactionID', ''), root_rel='..')}</dd>",
        f"<dt>Conversation</dt><dd>{_conv_link(db, conv, root_rel='..') if conv else '—'}</dd>",
    ]

    # Variant notice
    is_variant = db.canonical_bp_of.get(canonical_rr) != canonical_rr
    if is_variant:
        base_rr = db.canonical_bp_of[canonical_rr]
        base_entry = db.canonical_creatures.get(base_rr, {})
        base_c = base_entry.get("c", {})
        base_bp_rr = base_entry.get("bp_rr", base_rr)
        base_bp = db.creatures.get(base_bp_rr) if base_bp_rr != base_rr else None
        diff_items = _variant_diff_items(c, base_c, db, bp=bp, base_bp=base_bp)
        if diff_items:
            diff_detail = "; ".join(diff_items)
        else:
            diff_detail = "differs in equipment, class levels, or feats"
        sections.append(
            f'<dt>Variant of</dt><dd>'
            f'{link(f"{base_rr}.html", db.canonical_creature_name(base_rr))}'
            f'<br><small class="muted">{E(diff_detail)}</small></dd>'
        )
    sections.append('</dl>')

    # Creature artwork (from creature-pics/), just after the meta box.
    pics = state._CREATURE_PICS.get(canonical_rr)
    if pics:
        sections.append('<div class="creature-pics">'
                        + _pic_figures(pics, name) + "</div>")

    # Bestiary kill stats (when the module runs the kill-tracking system).
    if state._BESTIARY_ACTIVE:
        sf = next((s for s in state._SERVER_FIRSTS if s["resref"] == canonical_rr), None)
        if sf:
            slayer = E(sf["name"])
            if sf.get("player_name"):
                slayer += f" [{E(sf['player_name'])}]"
            sections.append(
                '<p class="server-first-badge"><strong>&#9733; Server First:</strong> '
                f"first slain by {slayer}"
                + (f" on {E(_utc_to_local(sf['at']))}" if sf["at"] else "") + ".</p>"
            )
        k = state._BESTIARY_KILLS.get(canonical_rr)
        if k:
            sections.append(
                "<h2>Kills</h2>"
                '<table class="data"><thead><tr>'
                "<th>Total</th><th>Solo</th><th>Party</th></tr></thead><tbody>"
                f"<tr><td>{k['total']}</td><td>{k['solo']}</td>"
                f"<td>{k['party']}</td></tr></tbody></table>"
            )
        else:
            sections.append("<h2>Kills</h2><p>Not yet slain by any adventurer.</p>")
        # Top Killers leaderboard — per-character kill counts for this
        # creature. Merge boss variant blueprints into their canonical (same
        # summing the Bosses index does) so e.g. the leveled Xanith .utcs
        # don't split a character's count. Omitted when nobody has a kill.
        variant_rrs = [canonical_rr] + [v for v, canon in state._BOSS_ALIASES.items()
                                        if canon == canonical_rr]
        merged: dict[str, dict] = {}
        for v_rr in variant_rrs:
            for r in state._BESTIARY_TOP.get(v_rr, []):
                m = merged.get(r["uuid"])
                if m is None:
                    merged[r["uuid"]] = dict(r)
                else:
                    m["solo"] += r["solo"]
                    m["party"] += r["party"]
                    m["total"] += r["total"]
                    if r["last"] >= m["last"]:
                        m["last"] = r["last"]
                        m["name"] = r["name"] or m["name"]
                        m["player"] = r["player"] or m["player"]
        top = sorted(merged.values(),
                     key=lambda r: (-r["total"], r["name"].lower()))[:10]
        if top:
            tk_rows = "\n".join(
                f"<tr><td>{i}</td><td>{E(r.get('player', ''))}</td>"
                f"<td>{E(r['name'])}</td>"
                f"<td>{r['solo']}</td><td>{r['party']}</td>"
                f"<td>{r['total']}</td>"
                f"<td>{E(_utc_to_local(r['last'])[:10] if r['last'] else '')}</td></tr>"
                for i, r in enumerate(top, 1)
            )
            sections.append(
                "<h3>Top Killers</h3>"
                '<table class="data"><thead><tr>'
                "<th>#</th><th>Player</th><th>Character</th><th>Solo</th><th>Party</th>"
                "<th>Total</th><th>Last Kill</th>"
                "</tr></thead><tbody>" + tk_rows + "</tbody></table>"
            )

    # "Where to find" section
    locs = db.canonical_locations.get(canonical_rr, [])
    placed_locs  = [l for l in locs if l["kind"] == "placed"]
    enc_locs     = [l for l in locs if l["kind"] == "encounter"]
    script_locs  = [l for l in locs if l["kind"] == "script"]

    if placed_locs or enc_locs or script_locs:
        sections.append("<h2>Where to find</h2>")
        if placed_locs:
            # Aggregate by area
            placed_by_area: dict[str, int] = {}
            for l in placed_locs:
                placed_by_area[l["area"]] = placed_by_area.get(l["area"], 0) + l["count"]
            place_rows = []
            for area_rr in sorted(placed_by_area.keys(),
                                  key=lambda r: db.area_name(r).lower()):
                cnt = placed_by_area[area_rr]
                place_rows.append(
                    f"<tr><td>{link(f'../areas/{area_rr}.html', db.area_name(area_rr))}</td>"
                    f"<td>{cnt}</td></tr>"
                )
            sections.append(
                "<h3>Placed directly</h3>"
                '<table class="data"><thead><tr>'
                "<th>Area</th><th>Count</th>"
                "</tr></thead><tbody>" + "\n".join(place_rows) + "</tbody></table>"
            )
        if enc_locs:
            # Aggregate by (area, enc_rr)
            enc_by_key: dict[tuple[str, str], int] = {}
            for l in enc_locs:
                k = (l["area"], l["enc_rr"] or "")
                enc_by_key[k] = enc_by_key.get(k, 0) + l["count"]
            enc_rows = []
            for (area_rr, enc_rr) in sorted(
                enc_by_key.keys(),
                key=lambda k: (db.area_name(k[0]).lower(), k[1]),
            ):
                blueprint = db.encounters.get(enc_rr, {})
                ename = loc(blueprint.get("LocalizedName")) or enc_rr or "(unnamed)"
                enc_rows.append(
                    f"<tr><td>{link(f'../areas/{area_rr}.html', db.area_name(area_rr))}</td>"
                    f"<td>{nwn_html(ename)}</td>"
                    f"<td><code>{E(enc_rr)}</code></td></tr>"
                )
            sections.append(
                "<h3>Encounter pools</h3>"
                '<table class="data"><thead><tr>'
                "<th>Area</th><th>Encounter</th><th>Encounter ResRef</th>"
                "</tr></thead><tbody>" + "\n".join(enc_rows) + "</tbody></table>"
            )
        if script_locs:
            script_by_area: dict[str, list[str]] = defaultdict(list)
            for l in script_locs:
                srcs = [e["script"] for e in db.area_script_spawns.get(l["area"], [])
                        if e["can_rr"] == canonical_rr]
                script_by_area[l["area"]].extend(srcs)
            script_rows = []
            for area_rr in sorted(script_by_area.keys(),
                                  key=lambda r: db.area_name(r).lower()):
                scripts = sorted(set(script_by_area[area_rr]))
                script_cell = (", ".join(f"<code>{E(s)}</code>" for s in scripts)
                               if scripts else "—")
                script_rows.append(
                    f"<tr><td>{link(f'../areas/{area_rr}.html', db.area_name(area_rr))}</td>"
                    f"<td>{script_cell}</td></tr>"
                )
            sections.append(
                "<h3>Script-spawned</h3>"
                '<table class="data"><thead><tr>'
                "<th>Area</th><th>Spawn script</th>"
                "</tr></thead><tbody>" + "\n".join(script_rows) + "</tbody></table>"
            )
    else:
        sections.append("<h2>Where to find</h2><p>Not placed in any area.</p>")

    store_section = _creature_store_section(db, bp_rr, filter_area=None, root_rel="..")
    if store_section:
        sections.append(store_section)

    sections.extend(_creature_detail_sections(db, c, bp=bp, root_rel=".."))

    write(out / "creatures" / f"{canonical_rr}.html",
          page(name, "\n".join(sections), root_rel=".."))


def render_creatures_search(db: Db, out: Path) -> None:
    """creatures/search.html — client-side search over every unique creature.

    Mirrors the item search (render_items_search): a JSON index plus inline JS.
    Each creature's searchable "abilities" are its equipped-item properties
    (already resolved by extract_creature_defenses, so the combat rules stay in
    one place) merged with the immunities its racial type grants. Conditions can
    be negated, which is what makes "everything that is *not* crit immune"
    answerable.
    """
    boss_rrs = {b["resref"] for b in state._BOSS_REGISTRY}
    index: list[dict] = []

    for can_rr in sorted(db.canonical_creatures,
                         key=lambda r: nwn_text(db.canonical_creature_name(r)).lower()):
        entry = db.canonical_creatures[can_rr]
        c = entry["c"]
        bp_rr = entry["bp_rr"]
        bp = db.creatures.get(bp_rr) if bp_rr != can_rr else None
        state._current_context = f"creature:{can_rr} ({db.canonical_creature_name(can_rr)})"

        def _f(key, default=None, _c=c, _bp=bp):
            v = fld(_c, key)
            if v is None and _bp is not None:
                v = fld(_bp, key)
            return default if v is None else v

        D = extract_creature_defenses(db, c, bp)

        classes = (list_items(c.get("ClassList"))
                   or (list_items(bp.get("ClassList")) if bp else []))
        cls_list = []
        for cl in classes:
            cid = fld(cl, "Class")
            try:
                lvl = int(fld(cl, "ClassLevel", 0) or 0)
            except (TypeError, ValueError):
                lvl = 0
            cls_list.append({"n": class_name(cid), "l": lvl})

        # Searchable properties: one row per distinct (property, subtype, cost),
        # keeping the highest value when several items grant the same thing.
        props: dict[tuple, dict] = {}

        def _add(p: str, s: str, cost: str, val: int, src: str, anchor: str = "",
                 _props=props):
            key = (p, s, cost, src)
            prev = _props.get(key)
            if prev is None or val > prev["v"]:
                row = {"p": p, "s": s, "c": cost, "v": val, "src": src}
                if anchor:
                    row["a"] = anchor
                _props[key] = row

        for _pid, _entries in D["cprop_by_pid"].items():
            for _pf, _cv in _entries:
                pname = _pf["property"]
                if not pname:
                    continue
                subtype, cost = _pf["subtype"], _pf["cost"]
                # Same quirk the item index handles: the spell name lands in the
                # cost field, which makes the subtype dropdown useless without it.
                if pname == "Immunity: Specific Spell" and not subtype and cost:
                    subtype, cost = cost, ""
                _add(pname, subtype, cost, _prop_value_num(_pf["cost"]) if _pf["cost"] else _cv,
                     "gear", "pn-" + _prop_slug(pname, ""))

        for _lbl in D["race_immunities"]:
            _add("Immunity: Miscellaneous", _lbl, "", 0, "race")

        locs = db.canonical_locations.get(can_rr, [])
        _hp = D["hp"]
        _cr = _creature_cr_value(db, can_rr)

        index.append({
            "rr": can_rr,
            "name": nwn_text(db.canonical_creature_name(can_rr)),
            "url": f"{can_rr}.html",
            "race": race_name(_f("Race")),
            "classes": cls_list,
            "cr": None if _cr < 0 else _cr,
            "hp": _hp,
            "ac": D["ac"],
            "bab": D["bab"],
            "fort": D["fort"], "ref": D["ref"], "will": D["will"],
            "sr": D["sr"],
            "faction": db.faction_name(_f("FactionID", "")),
            "count": sum(l["count"] for l in locs),
            "areas": sorted({db.area_name(l["area"]) for l in locs if l.get("area")}),
            "boss": can_rr in boss_rrs,
            "props": sorted(props.values(), key=lambda r: (r["p"], r["s"], r["c"])),
        })

    state._current_context = ""
    write(out / "creatures" / "search-index.json",
          json.dumps(index, ensure_ascii=False, separators=(",", ":")))

    def _cond_row(n: int) -> str:
        return (
            f'<div class="prop-row">'
            f'<select id="cm{n}" class="mode-sel">'
            f'<option value="has">has</option><option value="lacks">lacks</option>'
            f'</select>'
            f'<select id="cp{n}" class="prop-sel"><option value="">— any —</option></select>'
            f'<select id="cs{n}" class="subtype-sel" disabled>'
            f'<option value="">— any —</option></select>'
            f'<input id="cv{n}" type="number" min="0" placeholder="min value">'
            f'</div>'
        )

    cond_rows = "".join(_cond_row(n) for n in range(1, 5))
    boss_box = (
        '<label class="checkbox-label"><input type="checkbox" id="fboss"> Bosses only</label>'
        if state._BOSS_REGISTRY else ""
    )

    body = (
        "<h1>Search Creatures</h1>"
        "<p>All conditions must be satisfied simultaneously. Leave fields blank to skip. "
        "Set a row to <em>lacks</em> to find creatures <em>without</em> an ability — "
        "the result header reports how many of the total matched, so a "
        "<em>has</em>/<em>lacks</em> pair always sums to the full roster.</p>"
        '<form id="cf" class="item-search-form creature-search-form">'
        '<div class="search-row">'
        '<label for="fq">Name</label>'
        '<input id="fq" type="search" placeholder="substring">'
        '<label for="frace">Race</label>'
        '<select id="frace"><option value="">— all —</option></select>'
        '<label for="fclass">Class</label>'
        '<select id="fclass"><option value="">— any —</option></select>'
        '<input id="fclvl" type="number" min="0" placeholder="min level">'
        '</div>'
        '<div class="search-row">'
        '<label for="farea">Area</label>'
        '<select id="farea"><option value="">— all —</option></select>'
        '<label for="ffac">Faction</label>'
        '<select id="ffac"><option value="">— all —</option></select>'
        '<label class="checkbox-label" title="Only creatures actually placed in the module '
        '(spawned by an encounter, placed in an area, or created by a script)">'
        '<input type="checkbox" id="fim"> Placed in module only</label>'
        f'{boss_box}'
        '</div>'
        '<div class="search-row stat-row">'
        '<label for="fcrmin">CR</label>'
        '<input id="fcrmin" type="number" step="0.5" placeholder="min">'
        '<input id="fcrmax" type="number" step="0.5" placeholder="max">'
        '<label for="fhpmin">HP</label>'
        '<input id="fhpmin" type="number" placeholder="min">'
        '<input id="fhpmax" type="number" placeholder="max">'
        '<label for="facmin">AC</label>'
        '<input id="facmin" type="number" placeholder="min">'
        '<label for="fbabmin">BAB</label>'
        '<input id="fbabmin" type="number" placeholder="min">'
        '<label for="fsrmin">SR</label>'
        '<input id="fsrmin" type="number" placeholder="min">'
        '<label for="fsavemin" title="Every saving throw must be at least this high">Saves</label>'
        '<input id="fsavemin" type="number" placeholder="min">'
        '</div>'
        f'<div class="prop-rows">{cond_rows}</div>'
        '<div class="search-row">'
        '<label for="fo">Sort by</label>'
        '<select id="fo">'
        '<option value="cr">Challenge Rating</option>'
        '<option value="name">Name</option>'
        '<option value="hp">HP</option>'
        '<option value="ac">AC</option>'
        '<option value="bab">BAB</option>'
        '<option value="sr">Spell Resistance</option>'
        '<option value="count">Count in module</option>'
        '<option value="value">Matched Property Value</option>'
        '</select>'
        '<select id="fd">'
        '<option value="desc">Descending</option>'
        '<option value="asc">Ascending</option>'
        '</select>'
        '<button type="submit">Search</button>'
        '</div>'
        '</form>'
        '<div id="cr_out"><p class="muted">Loading creature index…</p></div>'
        '<p class="muted">Abilities come from a creature\'s equipped items plus the '
        'immunities its racial type grants (marked <em>racial</em>) — see any creature\'s '
        '<em>Abilities &amp; combat properties</em> table for the full picture.</p>'
        f"<script>{_CREATURE_SEARCH_JS}</script>"
    )
    write(out / "creatures" / "search.html",
          page("Search Creatures", body, root_rel=".."))
