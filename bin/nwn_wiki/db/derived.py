"""Secondary indexes derived from the dialog/script cross-reference pass.

``DbDerivedMixin`` owns the follow-on index passes that run after
:meth:`nwn_wiki.db.dialogs.DbDialogsMixin.index_dialogs`: which script
creates which item and in what context, which containers hold procedural
random treasure, which items act as keys, where scripts check for an item,
the dialog -> journal-quest reverse index, and the store-opener table.

One mixin of the stack described in :mod:`nwn_wiki.db`; see that docstring
for the rules it follows.  The ``*_EVENT_FIELDS`` tables several of these
passes read stay on the concrete ``Db`` in :mod:`nwn_wiki.cli` and resolve
through ``self`` at runtime.

``_quest_slug`` and ``_quest_hidden`` (and the regex the latter uses) travel
with ``_build_dialog_quest_index`` because this package may not import its
callers; :mod:`nwn_wiki.render.quests` and :mod:`nwn_wiki.cli` import them
back from here so the slugs and the hidden-quest rule stay in one place.
"""

from __future__ import annotations

import re
from collections import defaultdict

from nwn_wiki.gff import fld, list_items, loc

_RE_QUEST_HIDDEN = re.compile(r"@(?:hidden|retired|inactive)\b", re.IGNORECASE)


def _quest_slug(tag: str, name: str, used: set[str]) -> str:
    """Stable, unique, filename-safe slug for a quest line. Prefers the quest
    Tag (the plot id scripts reference); falls back to the display name.
    Disambiguates collisions with a numeric suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", (tag or name or "").lower()).strip("-") or "quest"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    used.add(slug)
    return slug


def _quest_hidden(comment: str) -> bool:
    """True when a quest's Comment marks it retired/inactive via @hidden (or
    the @retired / @inactive synonyms). Such quests are dropped from all wiki
    output, mirroring the area-level WIKI_HIDDEN convention."""
    return bool(_RE_QUEST_HIDDEN.search(comment or ""))


class DbDerivedMixin:
    def _index_script_item_sources(self) -> None:
        """Trace CreateItemOnObject calls back to their trigger context and
        populate item_from_script. Sources in WIKI_HIDDEN areas are excluded.
        Must run after _build_store_openers (index_dialogs fully done)."""
        if not self.script_creates_items:
            return
        scripts_set = set(self.script_creates_items.keys())

        # Inverted dialog_scripts: action-script resref → set of dialog resrefs
        script_to_dlgs: dict[str, set[str]] = defaultdict(set)
        for dlg_rr, entries in self.dialog_scripts.items():
            for e in entries:
                if e.get("kind") == "action" and e.get("resref") in scripts_set:
                    script_to_dlgs[e["resref"]].add(dlg_rr)

        # Module events: script resref → human event label.
        # Skip Mod_OnActvtItem (item activation) — that fires for DM item tools.
        _SKIP_MOD_FIELDS = {"Mod_OnActvtItem"}
        script_module_event: dict[str, str] = {}
        if self.ifo:
            for field, label in self.MODULE_EVENT_FIELDS.items():
                if field in _SKIP_MOD_FIELDS:
                    continue
                val = (fld(self.ifo, field, "") or "").lower()
                if val in scripts_set:
                    script_module_event[val] = label

        # All UTC event fields (superset of CREATURE_EVENT_FIELDS).
        _ALL_CREATURE_EVENTS: dict[str, str] = {
            "ScriptDeath": "OnDeath", "ScriptSpawn": "OnSpawn",
            "ScriptHeartbeat": "OnHeartbeat", "ScriptOnNotice": "OnPerception",
            "ScriptUserDefine": "OnUserDefined", "ScriptDialogue": "OnConversation",
            "ScriptAttacked": "OnAttacked", "ScriptDamaged": "OnDamaged",
            "ScriptDisturbed": "OnDisturbed", "ScriptEndRound": "OnEndCombatRound",
            "ScriptOnBlocked": "OnBlocked", "ScriptRested": "OnRested",
            "ScriptSpellAt": "OnSpellCastAt",
        }
        # Visible areas per creature blueprint resref.
        crr_vis: dict[str, set[str]] = defaultdict(set)
        for area_rr, insts in self.area_creature_instances.items():
            if area_rr not in self.hidden_areas:
                for inst in insts:
                    crr = (fld(inst["c"], "TemplateResRef", "") or "").lower()
                    if crr:
                        crr_vis[crr].add(area_rr)
        for crr, spawns in self.creature_encounter_spawns.items():
            for sp in spawns:
                if sp["area"] not in self.hidden_areas:
                    crr_vis[crr.lower()].add(sp["area"])
        # script resref → list of {crr, cname, event_label, areas}
        script_creature_srcs: dict[str, list[dict]] = defaultdict(list)
        for crr, c in self.creatures.items():
            vis_areas = crr_vis.get(crr.lower(), set())
            if not vis_areas:
                continue
            for field, ev_label in _ALL_CREATURE_EVENTS.items():
                val = (fld(c, field, "") or "").lower()
                if val in scripts_set:
                    script_creature_srcs[val].append({
                        "crr": crr, "cname": self.creature_name(crr),
                        "event_label": ev_label, "areas": sorted(vis_areas),
                    })

        # Visible areas per placeable blueprint resref.
        prr_vis: dict[str, set[str]] = defaultdict(set)
        for area_rr, pls in self.area_placeables.items():
            if area_rr not in self.hidden_areas:
                for pl in pls:
                    prr = (fld(pl, "TemplateResRef", "") or "").lower()
                    if prr:
                        prr_vis[prr].add(area_rr)
        # script resref → list of {prr, pname, event_label, areas}
        script_placeable_srcs: dict[str, list[dict]] = defaultdict(list)
        for prr, p in self.placeables.items():
            vis_areas = prr_vis.get(prr.lower(), set())
            if not vis_areas:
                continue
            pname = loc(p.get("LocName")) or fld(p, "Tag") or prr
            for field, ev_label in self.PLACEABLE_EVENT_FIELDS.items():
                val = (fld(p, field, "") or "").lower()
                if val in scripts_set:
                    script_placeable_srcs[val].append({
                        "prr": prr, "pname": pname,
                        "event_label": ev_label, "areas": sorted(vis_areas),
                    })
        # Also catch GIT-instance-level event overrides (no blueprint entry).
        git_plc_script_areas: dict[str, set[str]] = defaultdict(set)
        for area_rr, pls in self.area_placeables.items():
            if area_rr not in self.hidden_areas:
                for pl in pls:
                    for field in self.PLACEABLE_EVENT_FIELDS:
                        val = (fld(pl, field, "") or "").lower()
                        if val in scripts_set:
                            git_plc_script_areas[val].add(area_rr)
        for val, areas in git_plc_script_areas.items():
            if not script_placeable_srcs[val]:
                script_placeable_srcs[val].append({
                    "prr": None, "pname": None,
                    "event_label": "interaction", "areas": sorted(areas),
                })

        # Build item_from_script, deduplicating by (irr, kind, dlg-or-crr-or-prr-or-script).
        seen: set[tuple[str, str, str | None, str]] = set()
        for script_rr, item_resrefs in self.script_creates_items.items():
            sources: list[dict] = []

            if script_rr in script_module_event:
                sources.append({
                    "kind": "module-event", "script": script_rr,
                    "label": script_module_event[script_rr],
                    "areas": [], "dlg": None, "crr": None, "prr": None,
                })
            for dlg_rr in sorted(script_to_dlgs.get(script_rr, set())):
                callers = self.dialog_callers.get(dlg_rr, [])
                vis: set[str] = set()
                for caller in callers:
                    for a in (caller.get("areas") or []):
                        if a not in self.hidden_areas:
                            vis.add(a)
                    if caller.get("area") and caller["area"] not in self.hidden_areas:
                        vis.add(caller["area"])
                if callers and not vis:
                    continue  # every caller in a hidden area
                sources.append({
                    "kind": "dialog-action", "script": script_rr,
                    "label": self.dialog_label(dlg_rr),
                    "areas": sorted(vis), "dlg": dlg_rr, "crr": None, "prr": None,
                })
            for cs in script_creature_srcs.get(script_rr, []):
                sources.append({
                    "kind": "creature-event", "script": script_rr,
                    "label": cs["event_label"], "areas": cs["areas"],
                    "dlg": None, "crr": cs["crr"], "prr": None,
                })
            for ps in script_placeable_srcs.get(script_rr, []):
                sources.append({
                    "kind": "placeable-event", "script": script_rr,
                    "label": ps["event_label"], "areas": ps["areas"],
                    "dlg": None, "crr": None, "prr": ps["prr"],
                })
            if not sources:
                continue
            for irr in item_resrefs:
                if irr not in self.items:
                    continue
                for src in sources:
                    dk = (irr, src["kind"],
                          src.get("dlg") or src.get("crr") or src.get("prr") or "",
                          src["script"])
                    if dk not in seen:
                        seen.add(dk)
                        self.item_from_script[irr].append(src)

    def _index_random_treasure_containers(self) -> None:
        """Mark containers whose OnOpen script generates procedural random treasure.
        Populates random_treasure_containers. Must run after _parse_scripts."""
        for area_rr, containers in self.area_containers.items():
            for c in containers:
                p = c["p"]
                idx = c["idx"]
                on_open = (fld(p, "OnOpen", "") or "").lower()
                if not on_open:
                    bp_rr = (fld(p, "TemplateResRef", "") or "").lower()
                    on_open = (fld(self.placeables.get(bp_rr, {}), "OnOpen", "") or "").lower()
                if on_open and on_open in self.script_generates_treasure:
                    self.random_treasure_containers.add((area_rr, idx))

    def _index_key_items(self) -> None:
        """Build item_is_key_for: maps item resref → containers/doors that require
        that item's tag as a key (KeyRequired=1, KeyName=<item tag>)."""
        # Build a lowercase-tag → list[resref] map from known items.
        tag_to_resrefs: dict[str, list[str]] = defaultdict(list)
        for rr, it in self.items.items():
            tag = (fld(it, "Tag", "") or "").strip().lower()
            if tag:
                tag_to_resrefs[tag].append(rr)

        for area_rr, containers in self.area_containers.items():
            if area_rr in self.hidden_areas:
                continue
            for c in containers:
                p = c["p"]
                idx = c["idx"]
                key_tag = (fld(p, "KeyName", "") or "").strip().lower()
                if not key_tag:
                    continue
                required = bool(int(fld(p, "KeyRequired", 0) or 0))
                name = (loc(p.get("LocName")) or fld(p, "Tag", "") or
                        fld(p, "TemplateResRef", "") or "(unnamed)")
                for rr in tag_to_resrefs.get(key_tag, []):
                    self.item_is_key_for[rr].append(
                        {"area_rr": area_rr, "kind": "container", "name": name,
                         "idx": idx, "required": required}
                    )

        for area_rr, doors in self.area_doors.items():
            if area_rr in self.hidden_areas:
                continue
            for d in doors:
                key_tag = (fld(d, "KeyName", "") or "").strip().lower()
                if not key_tag:
                    continue
                required = bool(int(fld(d, "KeyRequired", 0) or 0))
                name = (loc(d.get("LocName")) or fld(d, "Tag", "") or
                        fld(d, "TemplateResRef", "") or "(unnamed door)")
                linked_to = fld(d, "LinkedTo", "") or ""
                dst_area = self.tag_to_area.get(linked_to) if linked_to else None
                if dst_area == area_rr:
                    dst_area = None  # same-area transition, not worth showing
                for rr in tag_to_resrefs.get(key_tag, []):
                    self.item_is_key_for[rr].append(
                        {"area_rr": area_rr, "kind": "door", "name": name,
                         "required": required, "dst_area": dst_area}
                    )

    def _index_script_item_checks(self) -> None:
        """Populate item_script_checks: item resref → contexts where
        GetItemPossessedBy is called with the item's tag (event scripts on
        triggers, placeables, doors, areas, and dialog action nodes).
        Must run after _parse_scripts, _build_tag_to_area, and index_dialogs."""
        if not self.script_checks_item_tags:
            return

        tag_to_resrefs: dict[str, list[str]] = defaultdict(list)
        for rr, it in self.items.items():
            tag = (fld(it, "Tag", "") or "").strip()
            if tag:
                tag_to_resrefs[tag.lower()].append(rr)

        def _emit(script_rr: str, entry: dict) -> None:
            for tag in self.script_checks_item_tags.get(script_rr, set()):
                for item_rr in tag_to_resrefs.get(tag.lower(), []):
                    self.item_script_checks[item_rr].append(entry)

        # Blueprint → placed areas
        trigger_areas: dict[str, set[str]] = defaultdict(set)
        placeable_areas: dict[str, set[str]] = defaultdict(set)
        door_areas: dict[str, set[str]] = defaultdict(set)
        for ar, ts in self.area_triggers.items():
            for t in ts:
                rr = (fld(t, "TemplateResRef", "") or "").lower()
                if rr:
                    trigger_areas[rr].add(ar)
        for ar, ps in self.area_placeables.items():
            for p in ps:
                rr = (fld(p, "TemplateResRef", "") or "").lower()
                if rr:
                    placeable_areas[rr].add(ar)
        for ar, ds in self.area_doors.items():
            for d in ds:
                rr = (fld(d, "TemplateResRef", "") or "").lower()
                if rr:
                    door_areas[rr].add(ar)

        # Blueprint triggers
        for rr, bp in self.triggers.items():
            for fld_name, label in self.TRIGGER_EVENT_FIELDS.items():
                s = (fld(bp, fld_name, "") or "").lower()
                if s in self.script_checks_item_tags:
                    _emit(s, {"kind": "trigger", "resref": rr, "event": label,
                               "script": s, "areas": sorted(trigger_areas.get(rr, set()))})

        # Instance trigger overrides
        for area_rr, ts in self.area_triggers.items():
            for idx, inst in enumerate(ts):
                bp_rr = (fld(inst, "TemplateResRef", "") or "").lower()
                inst_tag = fld(inst, "Tag", "") or ""
                for fld_name, label in self.TRIGGER_EVENT_FIELDS.items():
                    s = (fld(inst, fld_name, "") or "").lower()
                    if s in self.script_checks_item_tags:
                        _emit(s, {"kind": "trigger-instance", "resref": bp_rr,
                                   "tag": inst_tag, "event": label, "script": s,
                                   "area": area_rr, "areas": [area_rr]})

        # Blueprint placeables
        for rr, bp in self.placeables.items():
            for fld_name, label in self.PLACEABLE_EVENT_FIELDS.items():
                s = (fld(bp, fld_name, "") or "").lower()
                if s in self.script_checks_item_tags:
                    name = loc(bp.get("LocName")) or rr
                    _emit(s, {"kind": "placeable", "resref": rr, "name": name,
                               "event": label, "script": s,
                               "areas": sorted(placeable_areas.get(rr, set()))})

        # Instance placeables
        for area_rr, ps in self.area_placeables.items():
            for inst in ps:
                bp_rr = (fld(inst, "TemplateResRef", "") or "").lower()
                for fld_name, label in self.PLACEABLE_EVENT_FIELDS.items():
                    s = (fld(inst, fld_name, "") or "").lower()
                    if s in self.script_checks_item_tags:
                        name = loc(inst.get("LocName")) or bp_rr
                        _emit(s, {"kind": "placeable-instance", "resref": bp_rr,
                                   "name": name, "event": label, "script": s,
                                   "area": area_rr, "areas": [area_rr]})

        # Blueprint doors
        for rr, bp in self.doors.items():
            for fld_name, label in self.DOOR_EVENT_FIELDS.items():
                s = (fld(bp, fld_name, "") or "").lower()
                if s in self.script_checks_item_tags:
                    name = loc(bp.get("LocName")) or rr
                    _emit(s, {"kind": "door", "resref": rr, "name": name,
                               "event": label, "script": s,
                               "areas": sorted(door_areas.get(rr, set()))})

        # Instance doors
        for area_rr, ds in self.area_doors.items():
            for inst in ds:
                bp_rr = (fld(inst, "TemplateResRef", "") or "").lower()
                for fld_name, label in self.DOOR_EVENT_FIELDS.items():
                    s = (fld(inst, fld_name, "") or "").lower()
                    if s in self.script_checks_item_tags:
                        name = loc(inst.get("LocName")) or bp_rr
                        _emit(s, {"kind": "door-instance", "resref": bp_rr,
                                   "name": name, "event": label, "script": s,
                                   "area": area_rr, "areas": [area_rr]})

        # Area events
        for area_rr, area in self.areas.items():
            if area_rr in self.hidden_areas:
                continue
            for fld_name, label in self.AREA_EVENT_FIELDS.items():
                s = (fld(area, fld_name, "") or "").lower()
                if s in self.script_checks_item_tags:
                    _emit(s, {"kind": "area-event", "event": label, "script": s,
                               "area": area_rr, "areas": [area_rr]})

        # Dialog action/condition scripts → item_script_checks + dialog_item_checks
        # Both "action" (node fires) and "active" (condition gate) scripts can check items.
        dlg_script_to_dlgs: dict[str, dict[str, set[str]]] = {
            "action": defaultdict(set),
            "active": defaultdict(set),
        }
        for dlg_rr, entries in self.dialog_scripts.items():
            for e in entries:
                kind = e["kind"]
                if kind in dlg_script_to_dlgs:
                    dlg_script_to_dlgs[kind][e["resref"]].add(dlg_rr)

        # Build dialog_item_checks: dialog → item resrefs checked in any script
        for script_kind, script_to_dlgs in dlg_script_to_dlgs.items():
            for script_rr, dlg_set in script_to_dlgs.items():
                if script_rr not in self.script_checks_item_tags:
                    continue
                for tag in self.script_checks_item_tags[script_rr]:
                    for item_rr in tag_to_resrefs.get(tag.lower(), []):
                        for dlg_rr in dlg_set:
                            if item_rr not in self.dialog_item_checks[dlg_rr]:
                                self.dialog_item_checks[dlg_rr].append(item_rr)

        # Emit item_script_checks entries for dialog-action and dialog-condition.
        # If a dialog has no static caller, still emit an entry so the item page
        # can link back to the conversation (areas will be empty).
        item_check_kind = {"action": "dialog-action", "active": "dialog-condition"}
        for script_kind, script_to_dlgs in dlg_script_to_dlgs.items():
            entry_kind = item_check_kind[script_kind]
            for script_rr, dlg_set in script_to_dlgs.items():
                if script_rr not in self.script_checks_item_tags:
                    continue
                for dlg_rr in dlg_set:
                    callers = self.dialog_callers.get(dlg_rr, [])
                    if callers:
                        for caller in callers:
                            _emit(script_rr, {
                                "kind": entry_kind, "script": script_rr,
                                "dialog": dlg_rr,
                                "caller_kind": caller.get("kind", ""),
                                "caller_resref": caller.get("resref", ""),
                                "areas": caller.get("areas", []),
                            })
                    else:
                        _emit(script_rr, {
                            "kind": entry_kind, "script": script_rr,
                            "dialog": dlg_rr,
                            "caller_kind": "", "caller_resref": "", "areas": [],
                        })

        # Deduplicate per item
        for rr in list(self.item_script_checks.keys()):
            seen: set[tuple] = set()
            uniq = []
            for entry in self.item_script_checks[rr]:
                key = (entry.get("kind"), entry.get("resref"), entry.get("script"),
                       entry.get("area"), entry.get("event"), entry.get("dialog"))
                if key not in seen:
                    seen.add(key)
                    uniq.append(entry)
            self.item_script_checks[rr] = uniq

    def _build_dialog_quest_index(self) -> None:
        """Populate dialog_quest_grants_rev and quest_tag_to_info.
        Must run after _index_script_item_checks (all quest_grants/quest_dialog_grants built)."""
        if not self.jrl:
            return

        # Build quest_tag_to_info from the journal categories.
        used_slugs: set[str] = set()
        for c in list_items(self.jrl.get("Categories")):
            if _quest_hidden(fld(c, "Comment", "")):
                continue
            q_tag = (fld(c, "Tag", "") or "").strip().lower()
            q_name = loc(c.get("Name")) or q_tag
            slug = _quest_slug(q_tag, q_name, used_slugs)
            if q_tag:
                self.quest_tag_to_info[q_tag] = (q_name, slug)

        # Invert quest_dialog_grants: q_tag/eid/dlg_rr → dialog_quest_grants_rev
        for q_tag, eid_map in self.quest_dialog_grants.items():
            if q_tag not in self.quest_tag_to_info:
                continue
            for eid, dlg_set in eid_map.items():
                for dlg_rr in dlg_set:
                    self.dialog_quest_grants_rev[dlg_rr][q_tag].add(eid)

        # Script-based grants: action scripts in a dialog that call AddJournalQuestEntry
        script_to_dlgs: dict[str, set[str]] = defaultdict(set)
        for dlg_rr, entries in self.dialog_scripts.items():
            for e in entries:
                if e["kind"] == "action":
                    script_to_dlgs[e["resref"]].add(dlg_rr)
        for q_tag, eid_map in self.quest_grants.items():
            if q_tag not in self.quest_tag_to_info:
                continue
            for eid, script_set in eid_map.items():
                for script_rr in script_set:
                    for dlg_rr in script_to_dlgs.get(script_rr, set()):
                        self.dialog_quest_grants_rev[dlg_rr][q_tag].add(eid)

    def _build_store_openers(self) -> None:
        """Populate store_tag_openers. Must be called after index_dialogs so
        that dialog_callers and dialog_scripts are fully built."""
        # 1. Dialog action scripts that open stores: trace caller → dialog → script → store.
        for dlg_resref, script_list in self.dialog_scripts.items():
            for s in script_list:
                if s["kind"] != "action":
                    continue
                store_tags = self.script_store_tags.get(s["resref"], set())
                if not store_tags:
                    continue
                for caller in self.dialog_callers.get(dlg_resref, []):
                    for tag in store_tags:
                        self.store_tag_openers[tag.lower()].append({
                            **caller,
                            "via_script": s["resref"],
                            "via_dialog": dlg_resref,
                        })

        # 1b. Proximity-based store openers (Bedlamson's Dynamic Merchant
        # System and similar patterns): the action script calls OpenStore on a
        # runtime local-variable object, not by tag. At spawn the NPC stores
        # GetNearestObject(OBJECT_TYPE_STORE) in a local var, so in practice
        # the store opened is the one placed in the same area. Link the dialog
        # caller to every store in its placement area(s).
        for dlg_resref, script_list in self.dialog_scripts.items():
            bdm_script = next(
                (s["resref"] for s in script_list
                 if s["kind"] == "action" and s["resref"] in self.script_bdm_open),
                None,
            )
            if bdm_script is None:
                continue
            for caller in self.dialog_callers.get(dlg_resref, []):
                caller_areas = caller.get("areas") or (
                    [caller["area"]] if "area" in caller else []
                )
                for area_rr in caller_areas:
                    for store_inst in self.area_stores.get(area_rr, []):
                        tag = fld(store_inst, "Tag", "")
                        if not tag:
                            continue
                        self.store_tag_openers[tag.lower()].append({
                            **caller,
                            "via_script": bdm_script,
                            "via_dialog": dlg_resref,
                        })

        # 2. Entity event scripts that directly open stores (trigger OnEnter, etc.).
        creature_areas: dict[str, set[str]] = defaultdict(set)
        trigger_areas: dict[str, set[str]] = defaultdict(set)
        placeable_areas: dict[str, set[str]] = defaultdict(set)
        for ar, npcs in self.area_npcs.items():
            for c in npcs:
                rr = fld(c, "TemplateResRef", "")
                if rr:
                    creature_areas[rr].add(ar)
        for ar, ts in self.area_triggers.items():
            for t in ts:
                rr = fld(t, "TemplateResRef", "")
                if rr:
                    trigger_areas[rr].add(ar)
        for ar, ps in self.area_placeables.items():
            for p in ps:
                rr = fld(p, "TemplateResRef", "")
                if rr:
                    placeable_areas[rr].add(ar)

        def _add_direct(blueprints: dict, fields: dict[str, str],
                        kind: str, areas_idx: dict[str, set[str]]) -> None:
            for rr, bp in blueprints.items():
                for fld_name, label in fields.items():
                    s = (fld(bp, fld_name, "") or "").lower()
                    if not s:
                        continue
                    for tag in self.script_store_tags.get(s, set()):
                        self.store_tag_openers[tag.lower()].append({
                            "kind": kind, "resref": rr, "event": label, "script": s,
                            "areas": sorted(areas_idx.get(rr, set())),
                        })

        _add_direct(self.creatures, self.CREATURE_EVENT_FIELDS, "creature-event", creature_areas)
        _add_direct(self.triggers, self.TRIGGER_EVENT_FIELDS, "trigger-event", trigger_areas)
        _add_direct(self.placeables, self.PLACEABLE_EVENT_FIELDS, "placeable-event", placeable_areas)

        # 2a. Per-instance trigger overrides.
        for area_rr, ts in self.area_triggers.items():
            for idx, inst in enumerate(ts):
                inst_tag = fld(inst, "Tag", "") or ""
                bp_rr = (fld(inst, "TemplateResRef", "") or "").lower()
                for fld_name, label in self.TRIGGER_EVENT_FIELDS.items():
                    s = (fld(inst, fld_name, "") or "").lower()
                    if not s:
                        continue
                    for store_tag in self.script_store_tags.get(s, set()):
                        self.store_tag_openers[store_tag.lower()].append({
                            "kind": "trigger-event-instance",
                            "area": area_rr, "idx": idx, "tag": inst_tag,
                            "resref": bp_rr, "event": label, "script": s,
                            "areas": [area_rr],
                        })

        # Dedupe: prefer instance entries over blueprint entries for the same
        # NPC+area+dialog combination so each NPC only shows once.
        for tag, openers in self.store_tag_openers.items():
            # First pass: collect by exact key
            exact_seen: set[tuple] = set()
            exact_uniq = []
            for o in openers:
                key = (o.get("kind"), o.get("resref"), o.get("event"),
                       o.get("script"), o.get("area"), o.get("idx"),
                       o.get("via_dialog"))
                if key not in exact_seen:
                    exact_seen.add(key)
                    exact_uniq.append(o)
            # Second pass: drop blueprint-level entries when an instance entry
            # for the same resref+area+dialog already exists (instance is more precise).
            instance_keys: set[tuple] = {
                (o["resref"], o.get("area") or (o.get("areas") or [""])[0],
                 o.get("via_dialog"))
                for o in exact_uniq
                if o.get("kind") in ("creature-instance", "placeable-instance",
                                     "door-instance", "trigger-event-instance",
                                     "placeable-event-instance", "door-event-instance")
            }
            uniq = []
            for o in exact_uniq:
                kind = o.get("kind", "")
                is_bp = kind in ("creature", "creature-event", "placeable",
                                 "placeable-event", "door", "door-event",
                                 "trigger-event")
                if is_bp:
                    # Drop if a corresponding instance entry already covers it.
                    areas = o.get("areas") or []
                    via_dlg = o.get("via_dialog")
                    rr = o.get("resref", "")
                    covered = any(
                        (rr, a, via_dlg) in instance_keys for a in areas
                    )
                    if covered:
                        continue
                uniq.append(o)
            self.store_tag_openers[tag] = uniq

