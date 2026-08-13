"""Dialog cross-referencing: who starts each conversation, and where it leads.

``DbDialogsMixin`` owns :meth:`index_dialogs`, the pass that walks every
``.dlg`` and every thing that can open one -- blueprint ``Conversation``
fields, per-instance overrides, blueprint/area/module event scripts and
item tag scripts -- to build ``dialog_callers``, ``dialog_scripts``,
``dialog_teleports`` and the conversation edges (plus global pseudo-nodes)
the area map draws.  It then chains the dependent index passes.

One mixin of the stack described in :mod:`nwn_wiki.db`; see that docstring
for the rules it follows.  The ``*_EVENT_FIELDS`` tables it reads stay on
the concrete ``Db`` in :mod:`nwn_wiki.cli` because several mixins share
them; they resolve through ``self`` at runtime.
"""

from __future__ import annotations

import time
from collections import defaultdict

from nwn_wiki.gff import fld, list_items


class DbDialogsMixin:
    def index_dialogs(self) -> None:
        """Build dialog_callers, dialog_teleports, dialog_scripts and the
        pseudo-node + conv_transitions edges for the area map. Must run
        after index() (relies on tag/transition state) and after
        _parse_scripts (relies on script→dialog and script→teleport-tag
        lookups)."""
        t0 = time.time()
        self._parse_scripts()
        self._build_tag_to_area()
        self._resolve_script_creature_spawns()
        self._synthesize_zdialogs()
        self._index_dialog_node_scripts()
        self._index_dialog_callers()
        n_excluded_edges = self._build_conv_transitions()

        n_glob = len(self.global_convo_pseudo)
        n_conv_edges = len(self.conv_transitions)
        n_callers = sum(len(v) for v in self.dialog_callers.values())
        excl_msg = (f", {n_excluded_edges} edges suppressed by --exclude-conv-option"
                    if n_excluded_edges else "")
        print(f"  dialog xref in {time.time()-t0:.1f}s — {len(self.dialogs)} dialogs, "
              f"{n_callers} callers, {n_glob} global pseudo-nodes, "
              f"{n_conv_edges} conv map edges{excl_msg}")

        self._build_direct_teleport_transitions()
        self._build_store_openers()
        self._index_script_item_sources()
        self._index_random_treasure_containers()
        self._index_key_items()
        self._index_script_item_checks()
        self._build_dialog_quest_index()

    def _index_dialog_node_scripts(self) -> None:
        # Per-dialog: action / active scripts and teleport destinations.
        for dlg_resref, dlg in self.dialogs.items():
            for kind, idx, node in self._walk_dialog_nodes(dlg):
                action = fld(node, "Script", "")
                if action:
                    self.dialog_scripts[dlg_resref].append({
                        "resref": action, "kind": "action",
                        "node_kind": kind, "node_index": idx,
                    })
                    # sorted: script_teleport_tags values are sets, and this
                    # append order is the row order of the conversation page's
                    # "Teleport destinations" table.
                    for tag in sorted(self.script_teleport_tags.get(action, ())):
                        self.dialog_teleports[dlg_resref].append({
                            "tag": tag,
                            "area": self.tag_to_area.get(tag),
                            "via_script": action,
                            "node_kind": kind,
                            "node_index": idx,
                        })
                # Dialog-native quest grant: Quest/QuestEntry fields set a journal
                # entry directly without a script (e.g. b_miller_convo1.dlg).
                q_tag = (fld(node, "Quest", "") or "").strip().lower()
                if q_tag:
                    q_step = fld(node, "QuestEntry", 0) or 0
                    self.quest_dialog_grants[q_tag][q_step].add(dlg_resref)
                # Conditional / starting-list "Active" gates also have
                # their own scripts, surfaced for completeness.
                for cond in list_items(node.get("RepliesList")) + list_items(node.get("EntriesList")):
                    a = fld(cond, "Active", "")
                    if a:
                        self.dialog_scripts[dlg_resref].append({
                            "resref": a, "kind": "active",
                            "node_kind": kind, "node_index": idx,
                        })
            for s in list_items(dlg.get("StartingList")):
                a = fld(s, "Active", "")
                if a:
                    self.dialog_scripts[dlg_resref].append({
                        "resref": a, "kind": "active",
                        "node_kind": "start", "node_index": -1,
                    })

    # Caller bookkeeping helper.
    def _add_caller(self, dlg_resref: str, caller: dict) -> None:
        if dlg_resref in self.dialogs:
            self.dialog_callers[dlg_resref].append(caller)

    def _index_dialog_callers(self) -> None:
        (creature_areas, placeable_areas,
         door_areas, trigger_areas) = self._blueprint_area_index()
        self._index_conversation_field_callers(creature_areas, placeable_areas,
                                               door_areas)
        self._index_event_script_callers(creature_areas, placeable_areas,
                                         door_areas, trigger_areas)
        self._index_item_script_callers()
        self._dedupe_dialog_callers()

    def _blueprint_area_index(self) -> tuple[dict[str, set[str]], dict[str, set[str]],
                                             dict[str, set[str]], dict[str, set[str]]]:
        # Where each blueprint is placed (resref → set of area resrefs).
        creature_areas: dict[str, set[str]] = defaultdict(set)
        placeable_areas: dict[str, set[str]] = defaultdict(set)
        door_areas: dict[str, set[str]] = defaultdict(set)
        trigger_areas: dict[str, set[str]] = defaultdict(set)
        for ar, npcs in self.area_npcs.items():
            for c in npcs:
                rr = fld(c, "TemplateResRef", "")
                if rr:
                    creature_areas[rr].add(ar)
        for ar, ps in self.area_placeables.items():
            for p in ps:
                rr = fld(p, "TemplateResRef", "")
                if rr:
                    placeable_areas[rr].add(ar)
        for ar, ds in self.area_doors.items():
            for d in ds:
                rr = fld(d, "TemplateResRef", "")
                if rr:
                    door_areas[rr].add(ar)
        for ar, ts in self.area_triggers.items():
            for t in ts:
                rr = fld(t, "TemplateResRef", "")
                if rr:
                    trigger_areas[rr].add(ar)
        return creature_areas, placeable_areas, door_areas, trigger_areas

    def _index_conversation_field_callers(
            self, creature_areas: dict[str, set[str]],
            placeable_areas: dict[str, set[str]],
            door_areas: dict[str, set[str]]) -> None:
        # 1. Direct Conversation field on creature/placeable/door blueprints.
        for rr, c in self.creatures.items():
            dlg = (fld(c, "Conversation", "") or "").lower()
            if dlg and dlg in self.dialogs:
                self._add_caller(dlg, {
                    "kind": "creature", "resref": rr,
                    "areas": sorted(creature_areas.get(rr, set())),
                })

        # 1a. Per-instance Conversation override on placed creatures. The
        # toolset lets a builder swap the conversation on a single placement
        # (Crazy Maggie in Bree uses `maggie.dlg` even though her blueprint
        # `nw_oldwoman` points at the default commoner dialog), so we walk
        # every placement and capture its effective conversation.
        for inst in self.creature_instances:
            c = inst["c"]
            dlg = (fld(c, "Conversation", "") or "").lower()
            if dlg and dlg in self.dialogs:
                self._add_caller(dlg, {
                    "kind": "creature-instance",
                    "area": inst["area"], "idx": inst["idx"],
                    "resref": fld(c, "TemplateResRef", "") or "",
                    "areas": [inst["area"]],
                })
        for rr, p in self.placeables.items():
            dlg = (fld(p, "Conversation", "") or "").lower()
            if dlg and dlg in self.dialogs:
                self._add_caller(dlg, {
                    "kind": "placeable", "resref": rr,
                    "areas": sorted(placeable_areas.get(rr, set())),
                })
        for rr, d in self.doors.items():
            dlg = (fld(d, "Conversation", "") or "").lower()
            if dlg and dlg in self.dialogs:
                self._add_caller(dlg, {
                    "kind": "door", "resref": rr,
                    "areas": sorted(door_areas.get(rr, set())),
                })

    def _index_event_script_callers(
            self, creature_areas: dict[str, set[str]],
            placeable_areas: dict[str, set[str]],
            door_areas: dict[str, set[str]],
            trigger_areas: dict[str, set[str]]) -> None:
        # 2. Scripts bound to per-blueprint event slots that statically
        #    call ActionStartConversation (or StartDlg) with a literal
        #    dlg / z-dialog handler resref.
        self._add_blueprint_event_callers(self.creatures, self.CREATURE_EVENT_FIELDS,
                                          "creature-event", creature_areas)
        self._add_blueprint_event_callers(self.placeables, self.PLACEABLE_EVENT_FIELDS,
                                          "placeable-event", placeable_areas)
        self._add_blueprint_event_callers(self.doors, self.DOOR_EVENT_FIELDS,
                                          "door-event", door_areas)
        self._add_blueprint_event_callers(self.triggers, self.TRIGGER_EVENT_FIELDS,
                                          "trigger-event", trigger_areas)

        # 2a. Per-instance overrides on placeables/doors/triggers. The
        #     toolset lets a builder swap any blueprint script slot (or, for
        #     placeables/doors, the Conversation field) on a single
        #     placement — used heavily in HoMERs LotR for things like the
        #     legendary leveler statue, where a generic statue blueprint is
        #     overridden per-placement with `OnUsed = hgll_start_dlg`.
        self._add_instance_event_callers(self.area_placeables, self.PLACEABLE_EVENT_FIELDS,
                                         "placeable-event-instance", "placeable-instance")
        self._add_instance_event_callers(self.area_doors, self.DOOR_EVENT_FIELDS,
                                         "door-event-instance", "door-instance")
        self._add_instance_event_callers(self.area_triggers, self.TRIGGER_EVENT_FIELDS,
                                         "trigger-event-instance", None)

        # 3. Per-area event scripts.
        for rr, area in self.areas.items():
            for fld_name, label in self.AREA_EVENT_FIELDS.items():
                s = (fld(area, fld_name, "") or "").lower()
                if not s:
                    continue
                for dlg in self._script_dialog_targets(s):
                    self._add_caller(dlg, {
                        "kind": "area-event", "resref": rr,
                        "event": label, "script": s,
                        "areas": [rr],
                    })

        # 4. Module-level event scripts.
        if self.ifo:
            for fld_name, label in self.MODULE_EVENT_FIELDS.items():
                s = (fld(self.ifo, fld_name, "") or "").lower()
                if not s:
                    continue
                for dlg in self._script_dialog_targets(s):
                    self._add_caller(dlg, {
                        "kind": "module-event", "event": label, "script": s,
                    })

    def _add_blueprint_event_callers(self, blueprints: dict, fields: dict[str, str],
                                     kind: str, areas_idx: dict[str, set[str]]):
        for rr, bp in blueprints.items():
            for fld_name, label in fields.items():
                s = (fld(bp, fld_name, "") or "").lower()
                if not s:
                    continue
                for dlg in self._script_dialog_targets(s):
                    self._add_caller(dlg, {
                        "kind": kind, "resref": rr,
                        "event": label, "script": s,
                        "areas": sorted(areas_idx.get(rr, set())),
                    })

    def _add_instance_event_callers(self, area_lists: dict, fields: dict[str, str],
                                    kind_event: str, kind_conv: str | None):
        for area_rr, items in area_lists.items():
            for idx, inst in enumerate(items):
                tag = fld(inst, "Tag", "") or ""
                bp_rr = (fld(inst, "TemplateResRef", "") or "").lower()
                if kind_conv is not None:
                    dlg = (fld(inst, "Conversation", "") or "").lower()
                    if dlg and dlg in self.dialogs:
                        self._add_caller(dlg, {
                            "kind": kind_conv,
                            "area": area_rr, "idx": idx, "tag": tag,
                            "resref": bp_rr,
                            "areas": [area_rr],
                        })
                for fld_name, label in fields.items():
                    s = (fld(inst, fld_name, "") or "").lower()
                    if not s:
                        continue
                    for dlg in self._script_dialog_targets(s):
                        self._add_caller(dlg, {
                            "kind": kind_event,
                            "area": area_rr, "idx": idx, "tag": tag,
                            "resref": bp_rr,
                            "event": label, "script": s,
                            "areas": [area_rr],
                        })

    def _index_item_script_callers(self) -> None:
        # 5. Item tag-based scripting: when a script's resref equals an item's
        #    tag (or resref) and that script statically starts a conversation,
        #    treat the item as a caller. This catches the common "wand /
        #    activate-item" pattern without needing to parse the central
        #    Mod_OnActvtItem dispatcher's tag table.
        for rr, item in self.items.items():
            tag = (fld(item, "Tag", "") or "").lower()
            for candidate in sorted({rr.lower(), tag}):
                if not candidate:
                    continue
                for dlg in self._script_dialog_targets(candidate):
                    self._add_caller(dlg, {
                        "kind": "item-script", "resref": rr,
                        "script": candidate,
                    })

    def _dedupe_dialog_callers(self) -> None:
        # Dedupe caller lists (the same blueprint can match via several
        # event scripts; collapse identical descriptors).
        for dlg, callers in self.dialog_callers.items():
            seen = set()
            uniq = []
            for c in callers:
                key = (c.get("kind"), c.get("resref"), c.get("event"),
                       c.get("script"), tuple(c.get("areas", [])),
                       c.get("area"), c.get("idx"))
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(c)
            self.dialog_callers[dlg] = uniq

    def _build_conv_transitions(self) -> int:
        """Build the map's conversation edges; returns the count of edges
        suppressed by --exclude-conv-option."""
        # Pseudo-nodes + conv_transitions for the area map.
        # A conversation contributes to the map if it has at least one
        # teleport destination resolvable to a real area.
        n_excluded_edges = 0
        for dlg_resref, teleports in self.dialog_teleports.items():
            if self.exclude_option_texts and dlg_resref in self.dialogs:
                excluded = self._excluded_dialog_nodes(self.dialogs[dlg_resref])
                if excluded:
                    kept = [t for t in teleports
                            if (t.get("node_kind"), t.get("node_index"))
                            not in excluded]
                    n_excluded_edges += len(teleports) - len(kept)
                    teleports = kept
            dest_areas = sorted({t["area"] for t in teleports
                                 if t["area"] and t["area"] in self.areas})
            if not dest_areas:
                continue
            callers = self.dialog_callers.get(dlg_resref, [])
            # Source areas: every area that hosts an entity caller.
            src_areas: set[str] = set()
            global_kinds: list[dict] = []
            for c in callers:
                if c["kind"] in ("module-event",):
                    global_kinds.append(c)
                elif c["kind"] == "item-script":
                    # Items can be carried anywhere; treat as global too,
                    # unless we later add inventory→area cross-refs.
                    global_kinds.append(c)
                else:
                    for a in c.get("areas", []):
                        if a in self.areas:
                            src_areas.add(a)
            for src in sorted(src_areas):
                for dst in dest_areas:
                    if dst == src:
                        continue
                    self.conv_transitions.append({
                        "src": src,
                        "dst_area": dst,
                        "conv_resref": dlg_resref,
                        "kind": "convo",
                        "label": dlg_resref,
                    })
            if global_kinds:
                # Pick a short label: prefer a module-event name, else
                # "Convo: <resref>".
                label = None
                for c in global_kinds:
                    if c["kind"] == "module-event":
                        label = c["event"]
                        break
                if label is None:
                    label = "Item activation"
                pseudo_id = f"__convo:{dlg_resref}"
                self.global_convo_pseudo[pseudo_id] = {
                    "label": label,
                    "conv_resref": dlg_resref,
                    "dests": dest_areas,
                }
                for dst in dest_areas:
                    self.conv_transitions.append({
                        "src": pseudo_id,
                        "dst_area": dst,
                        "conv_resref": dlg_resref,
                        "kind": "convo",
                        "label": label,
                    })

        return n_excluded_edges
