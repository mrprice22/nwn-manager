"""The main derived-index pass over the loaded module tree.

``DbIndexMixin`` owns ``Db.index()``: the single pass that turns the raw
GFF-as-JSON dicts loaded by :class:`nwn_wiki.db.core.DbCore` into the derived
structures the renderers read -- faction friendliness, waypoint/door/trigger
tag maps, per-area object lists, area transitions, container inventories,
encounter spawn pools, the canonical-creature index, and the item-source
cross-references (sold at / in container / carried by).

One mixin of the stack described in :mod:`nwn_wiki.db`; see that docstring for
the rules it follows.

``_store_instance_slug`` travels with it because this package may not import
its callers; :mod:`nwn_wiki.render.stores` and :mod:`nwn_wiki.render.areas`
import it back from here so the slugs they link to match the ones indexed.
"""

from __future__ import annotations

import time
from collections import defaultdict

from nwn_wiki.gff import (
    STOCK_CREATURE_NAMES,
    STOCK_ITEM_BASE,
    STOCK_ITEM_COST,
    STOCK_ITEM_PROPS,
    fld,
    list_items,
    loc,
)
from nwn_wiki.itemprops import _creature_key
from nwn_wiki.respawn import encounter_respawn, placed_respawn


def _store_instance_slug(area_rr: str, inst: dict) -> str:
    """Stable URL slug for a placed store instance: area + tag (or resref)."""
    tag = fld(inst, "Tag", "") or fld(inst, "ResRef", "") or "unknown"
    return f"{area_rr}_{tag.lower()}"


class DbIndexMixin:
    # ---- Indexing ----

    def index(self) -> None:
        t0 = time.time()
        self._index_factions()
        self._index_area_objects()
        self._index_transitions()
        self._index_containers()
        self._index_encounter_spawns()
        self._index_hidden_areas()
        self._index_canonical_creatures()
        self._index_canonical_locations()
        self._index_item_sources()
        self._backfill_stock_items()

        hidden_msg = (f", {len(self.hidden_areas)} area(s) marked WIKI_HIDDEN"
                      if self.hidden_areas else "")
        print(f"  indexed in {time.time()-t0:.1f}s — {len(self.areas)} areas, "
              f"{len(self.transitions)} transitions, "
              f"{len(self.creatures)} creatures, {len(self.items)} items{hidden_msg}")

    def _index_factions(self) -> None:
        # Faction friendliness: faction is "friendly" if its rep with PC
        # (faction id 0) is >= 50 in repute.fac.json's RepList. Default
        # friendly for ids 0/2/3/4 (PC, Commoner, Merchant, Defender by
        # engine convention). FactionList struct ids correspond to the
        # FactionID referenced from creatures.
        self.faction_friendly = {0: True, 2: True, 3: True, 4: True, 1: False}
        if self.fac:
            replist = list_items(self.fac.get("RepList"))
            for rep in replist:
                f1 = fld(rep, "FactionID1")
                f2 = fld(rep, "FactionID2")
                v = fld(rep, "FactionRep", 0) or 0
                # Reps are pairwise; consult vs faction 0 (PC)
                if f1 == 0 and isinstance(f2, int):
                    self.faction_friendly[f2] = (v >= 50)
                elif f2 == 0 and isinstance(f1, int):
                    self.faction_friendly[f1] = (v >= 50)

        # Reputation TOWARD the PC faction, which is what decides whether a
        # faction's creatures (and its encounters) treat a player as an enemy.
        # RepList rows are directional: row (FactionID1=A, FactionID2=B, v)
        # records B's reputation toward A, so the rows to read here are the ones
        # with FactionID1 == 0. In stock and module repute.fac alike, Hostile's
        # rep toward the PC is 0 — which is what makes ordinary Hostile-faction
        # encounters fire for everybody.
        self.faction_rep_toward_pc = {}
        for rep in list_items((self.fac or {}).get("RepList")):
            if fld(rep, "FactionID1") == 0:
                f2 = fld(rep, "FactionID2")
                if isinstance(f2, int):
                    self.faction_rep_toward_pc[f2] = fld(rep, "FactionRep", 0) or 0

        # Allegiance sides. The module runs a Good-vs-Evil player allegiance
        # (faction_db.nss): the Well of Eru orbs AdjustReputation the PC against
        # invisible anchor placeables tagged Goodfaction / Evilfaction /
        # Neutralfaction — AdjustReputation moves how the ANCHOR's faction feels
        # about that player, so taking a side makes that side friendly to you
        # (+1000) and leaves the other hostile. Both sides start hostile to
        # everyone, so an encounter tagged Good or Evil fires for every player
        # EXCEPT those who took its side.
        for i, f in enumerate(list_items((self.fac or {}).get("FactionList"))):
            name = (fld(f, "FactionName", "") or "").strip()
            if name.lower() in ("good", "evil", "neutral"):
                self.allegiance_sides[i] = name.capitalize()
        anchor_tags = {"goodfaction": "Good", "evilfaction": "Evil",
                       "neutralfaction": "Neutral"}
        for git in self.gits.values():
            for p in list_items(git.get("Placeable List")):
                side = anchor_tags.get((fld(p, "Tag", "") or "").lower())
                if not side:
                    continue
                fid = fld(p, "Faction")
                try:
                    self.allegiance_anchored.add(int(fid))
                except (TypeError, ValueError):
                    continue

    def _index_area_objects(self) -> None:
        # Waypoint tag → area resref. Walk every git's WaypointList.
        for area_resref, git in self.gits.items():
            for wp in list_items(git.get("WaypointList")):
                tag = fld(wp, "Tag")
                if tag and tag not in self.waypoint_area:
                    self.waypoint_area[tag] = area_resref
                self.area_waypoints[area_resref].append(wp)

        # Per-area lists for the rest.
        for area_resref, git in self.gits.items():
            for idx, c in enumerate(list_items(git.get("Creature List"))):
                self.area_npcs[area_resref].append(c)
                inst = {"area": area_resref, "idx": idx, "c": c}
                self.creature_instances.append(inst)
                self.area_creature_instances[area_resref].append(inst)
            for e in list_items(git.get("Encounter List")):
                self.area_encounters[area_resref].append(e)
            for p in list_items(git.get("Placeable List")):
                self.area_placeables[area_resref].append(p)
            for s in list_items(git.get("StoreList")):
                self.area_stores[area_resref].append(s)
            for t in list_items(git.get("TriggerList")):
                self.area_triggers[area_resref].append(t)
            for d in list_items(git.get("Door List")):
                self.area_doors[area_resref].append(d)

    def _index_transitions(self) -> None:
        door_tag_area, trigger_tag_area = self._build_link_tag_maps()
        _tag_all_objects = self._collect_tag_objects()

        def resolve_link(linked: str) -> str | None:
            return (self.waypoint_area.get(linked)
                    or door_tag_area.get(linked)
                    or trigger_tag_area.get(linked))

        # Build transitions from triggers + doors.
        # Transition X/Y is captured in *area metres* (10 m per tile) so the
        # map layout can tell which edge each transition exits from.
        for area_resref, triggers in self.area_triggers.items():
            for t in triggers:
                linked = fld(t, "LinkedTo")
                if not linked:
                    continue
                dst = resolve_link(linked)
                self.transitions.append({
                    "src_area": area_resref,
                    "dst_area": dst,
                    "dst_tag": linked,
                    "kind": "trigger",
                    "label": fld(t, "Tag", "") or fld(t, "TemplateResRef", "") or "",
                    "src_resref": fld(t, "TemplateResRef", ""),
                    "src_x": fld(t, "XPosition"),
                    "src_y": fld(t, "YPosition"),
                })
        for area_resref, doors in self.area_doors.items():
            for d in doors:
                linked = fld(d, "LinkedTo")
                if not linked:
                    continue
                dst = resolve_link(linked)
                self.transitions.append({
                    "src_area": area_resref,
                    "dst_area": dst,
                    "dst_tag": linked,
                    "kind": "door",
                    "label": fld(d, "Tag", "") or fld(d, "TemplateResRef", "") or "",
                    "src_resref": fld(d, "TemplateResRef", ""),
                    "src_x": fld(d, "X"),
                    "src_y": fld(d, "Y"),
                    "key_tag": (fld(d, "KeyName", "") or "").strip(),
                    "key_required": bool(int(fld(d, "KeyRequired", 0) or 0)),
                })

        self._annotate_dup_dest_tags(_tag_all_objects)

    def _build_link_tag_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        # Door/trigger tag → area indexes. NWN's `LinkedTo` is a tag
        # reference, not specifically a waypoint tag — door-to-door pairs
        # (each door's LinkedTo holds the *other door's* tag) are common, and
        # so are trigger-to-trigger pairs. Resolving against only waypoints
        # makes those transitions render as "(unresolved waypoint)" and
        # silently disappear from the overview map.
        door_tag_area: dict[str, str] = {}
        trigger_tag_area: dict[str, str] = {}
        for area_resref, doors in self.area_doors.items():
            for d in doors:
                tag = fld(d, "Tag")
                if tag and tag not in door_tag_area:
                    door_tag_area[tag] = area_resref
        for area_resref, triggers in self.area_triggers.items():
            for t in triggers:
                tag = fld(t, "Tag")
                if tag and tag not in trigger_tag_area:
                    trigger_tag_area[tag] = area_resref
        return door_tag_area, trigger_tag_area

    def _collect_tag_objects(self) -> dict[str, list[dict]]:
        # Multi-map: tag → ALL objects carrying it (waypoints, doors, triggers).
        # Used to detect ambiguous LinkedTo targets where the game's "first match"
        # resolution may send the player to an unexpected area.
        _tag_all_objects: dict[str, list[dict]] = defaultdict(list)
        for _ar, _git in self.gits.items():
            for _wp in list_items(_git.get("WaypointList")):
                _t = fld(_wp, "Tag")
                if _t and _ar in self.areas:
                    _tag_all_objects[_t].append({"kind": "waypoint", "area": _ar, "tag_label": _t})
        for _ar, _doors in self.area_doors.items():
            for _d in _doors:
                _t = fld(_d, "Tag")
                if _t and _ar in self.areas:
                    _tag_all_objects[_t].append({"kind": "door", "area": _ar, "tag_label": _t})
        for _ar, _triggers in self.area_triggers.items():
            for _tr in _triggers:
                _t = fld(_tr, "Tag")
                if _t and _ar in self.areas:
                    _tag_all_objects[_t].append({"kind": "trigger", "area": _ar, "tag_label": _t})
        return _tag_all_objects

    def _annotate_dup_dest_tags(self, _tag_all_objects: dict[str, list[dict]]) -> None:
        # Identify transitions whose LinkedTo tag maps to multiple objects.
        # The game engine resolves to whichever object it finds first, so the
        # destination is effectively ambiguous and may surprise the builder.
        _referenced_tags: set[str] = {tr["dst_tag"] for tr in self.transitions if tr.get("dst_tag")}
        for _tag in sorted(_referenced_tags):
            _all = _tag_all_objects.get(_tag, [])
            _unique_areas = list(dict.fromkeys(obj["area"] for obj in _all if obj["area"] in self.areas))
            if len(_unique_areas) > 1:
                self.dup_dest_tags[_tag] = _all
        # Annotate each affected transition with is_dup_tag + list of alt destinations.
        for tr in self.transitions:
            tag = tr.get("dst_tag")
            if tag and tag in self.dup_dest_tags:
                all_areas = list(dict.fromkeys(
                    obj["area"] for obj in self.dup_dest_tags[tag] if obj["area"] in self.areas
                ))
                tr["is_dup_tag"] = True
                tr["dst_area_alts"] = [a for a in all_areas if a != tr["dst_area"]]

    def _index_containers(self) -> None:
        # Containers: placeables that hold an inventory.
        for area_resref, placeables in self.area_placeables.items():
            for idx, p in enumerate(placeables):
                items = list_items(p.get("ItemList"))
                if items:
                    self.area_containers[area_resref].append({"idx": idx, "p": p})

    def _index_encounter_spawns(self) -> None:
        # Encounter spawn pools: walk every encounter placement, pull its
        # CreatureList (preferring the placement's, falling back to the
        # blueprint's), and record which creatures can spawn where. At runtime
        # NWN spawns from the area-instance (.git) CreatureList — the toolset
        # copies the blueprint list in on placement but a builder can edit it
        # afterwards, so the instance is authoritative and the blueprint is only
        # the fallback for an unedited placement that carries no list of its own.
        for area_resref, encs in self.area_encounters.items():
            for e in encs:
                rr = fld(e, "TemplateResRef", "")
                blueprint = self.encounters.get(rr, {})
                spawns = (list_items(e.get("CreatureList"))
                          or list_items(blueprint.get("CreatureList")))
                # Respawn period is a property of this placement, not of the
                # encounter blueprint: two placements of the same .ute in one
                # area routinely carry different ResetTimes.
                respawn = encounter_respawn(e, blueprint)
                for s in spawns:
                    crr = fld(s, "ResRef", "") or ""
                    if crr:
                        self.creature_encounter_spawns[crr].append(
                            {"area": area_resref, "encounter_resref": rr,
                             "cr": fld(s, "CR"), "appearance": fld(s, "Appearance"),
                             "respawn_seconds": respawn}
                        )

    def _index_hidden_areas(self) -> None:
        self.hidden_areas = {
            rr for rr, a in self.areas.items()
            if "WIKI_HIDDEN" in (fld(a, "Comments") or "").upper()
        }

    def _index_canonical_creatures(self) -> None:
        # ---- Build canonical creature index --------------------------------
        # Pass 1: register each UTC blueprint as its own canonical.
        for bp_rr, bp in self.creatures.items():
            can_rr = self._intern_creature(bp_rr, bp, bp=None)
            self.canonical_for_bp[bp_rr] = can_rr

        # Pass 2: walk each placed instance; check if it diverges from its
        # blueprint's canonical key. Instances in WIKI_HIDDEN areas are still
        # registered (needed for item_carried_by de-dup) but their areas are
        # excluded from canonical_locations.
        for area_rr, insts in self.area_creature_instances.items():
            for inst in insts:
                c_git = inst["c"]
                idx = inst["idx"]
                bp_rr = fld(c_git, "TemplateResRef", "") or ""
                bp = self.creatures.get(bp_rr)
                if bp_rr:
                    can_rr = self._intern_creature(bp_rr, c_git, bp=bp)
                else:
                    # Orphan instance — no blueprint on file. Synthesise a
                    # canonical using area+idx as a unique resref.
                    synth_rr = f"__orphan_{area_rr}_{idx:03d}"
                    key = _creature_key(c_git, bp=None)
                    reg = self._creature_key_registry.setdefault(synth_rr, {})
                    if key not in reg:
                        self.canonical_creatures[synth_rr] = {
                            "c": c_git, "bp_rr": synth_rr
                        }
                        self.canonical_bp_of[synth_rr] = synth_rr
                        reg[key] = synth_rr
                    can_rr = reg[key]
                self.canonical_for_inst[(area_rr, idx)] = can_rr

    def _index_canonical_locations(self) -> None:
        # Pass 3: canonical_locations from encounter spawn pools.
        # Aggregate by (can_rr, area_rr, enc_rr) → count.
        # Skip blueprints not found in the module's files (e.g. stock NWN
        # resrefs like nw_* that come from the game data, not the module).
        _enc_agg: dict[tuple, int] = {}
        for crr, spawns in self.creature_encounter_spawns.items():
            if crr not in self.canonical_for_bp:
                # Stock NWN/CEP creature: no module .utc on file and never placed
                # in a .git. Register it as its own canonical so it shows in the
                # creatures index and bestiary catalogue (CR/appearance from the
                # encounter CreatureList entry; name from STOCK_CREATURE_NAMES,
                # falling back to the resref).
                cr_val = next((sp["cr"] for sp in spawns
                               if sp.get("cr") is not None), 0)
                app_val = next((sp["appearance"] for sp in spawns
                                if sp.get("appearance") is not None), None)
                synth_c = {
                    "FirstName": {"type": "cexolocstring",
                                  "value": {"0": STOCK_CREATURE_NAMES.get(crr, crr)}},
                    "ChallengeRating": {"type": "float", "value": float(cr_val or 0)},
                }
                if app_val is not None:
                    synth_c["Appearance_Type"] = {"type": "word", "value": app_val}
                self.canonical_creatures[crr] = {"c": synth_c, "bp_rr": crr}
                self.canonical_bp_of[crr] = crr
                self.canonical_for_bp[crr] = crr
            can_rr = self.canonical_for_bp[crr]
            for sp in spawns:
                area_rr = sp["area"]
                if area_rr in self.hidden_areas:
                    continue
                enc_rr = sp["encounter_resref"]
                # Respawn is part of the key: placements of the same encounter
                # in one area with different ResetTimes are different answers
                # to "how long until it's back" and must not collapse together.
                agg_key = (can_rr, area_rr, enc_rr, sp.get("respawn_seconds"))
                _enc_agg[agg_key] = _enc_agg.get(agg_key, 0) + 1
        for (can_rr, area_rr, enc_rr, respawn), count in _enc_agg.items():
            self.canonical_locations[can_rr].append(
                {"area": area_rr, "kind": "encounter", "enc_rr": enc_rr,
                 "count": count, "respawn_kind": "encounter",
                 "respawn_seconds": respawn}
            )

        # Pass 4: canonical_locations from direct placements.
        _placed_agg: dict[tuple, int] = {}
        for area_rr, insts in self.area_creature_instances.items():
            if area_rr in self.hidden_areas:
                continue
            for inst in insts:
                can_rr = self.canonical_for_inst.get((area_rr, inst["idx"]))
                if can_rr:
                    # Placements in one area can disagree (one blueprint's
                    # instances may carry different ScriptDeath overrides), so
                    # respawn is part of the key and each behaviour gets a row.
                    # db.creatures is keyed by the lower-cased file stem.
                    bp_rr = (fld(inst["c"], "TemplateResRef", "") or "").lower()
                    kind, secs = placed_respawn(
                        self, inst["c"], self.creatures.get(bp_rr), bp_rr)
                    agg_key = (can_rr, area_rr, kind, secs)
                    _placed_agg[agg_key] = _placed_agg.get(agg_key, 0) + 1
                    raw_fid = fld(inst["c"], "FactionID")
                    if raw_fid is not None and raw_fid != "":
                        try:
                            self.canonical_inst_factions[can_rr].add(int(raw_fid))
                        except (TypeError, ValueError):
                            pass
        for (can_rr, area_rr, kind, secs), count in _placed_agg.items():
            self.canonical_locations[can_rr].append(
                {"area": area_rr, "kind": "placed", "enc_rr": None, "count": count,
                 "respawn_kind": kind, "respawn_seconds": secs}
            )
        # ---- End canonical creature index ----------------------------------

    def _index_item_sources(self) -> None:
        # Build item-source cross-references, absorbing inline items through
        # _intern_item() so that instances with different PropertiesList get
        # their own synthetic resref (base__v2, __v3 …) and their own accurate
        # "where to find" entries rather than being merged under one blueprint.
        self._index_store_items()
        self._index_container_items()
        self._index_carried_items()

    def _index_store_items(self) -> None:
        # Stand-alone UTM blueprints — absorb items without area cross-refs.
        for s in self.stores.values():
            for p in list_items(s.get("StoreList")):
                for it in list_items(p.get("ItemList")):
                    base_rr = (fld(it, "TemplateResRef", "")
                               or fld(it, "InventoryRes", "") or "").strip()
                    if base_rr:
                        self._intern_item(base_rr, it)

        # Area store instances — absorb and build item_sold_at.
        for area_rr, store_list in self.area_stores.items():
            skip = area_rr in self.hidden_areas
            for inst in store_list:
                rr = fld(inst, "ResRef", "") or fld(inst, "TemplateResRef", "")
                tag = fld(inst, "Tag", "")
                sname = self.store_name(rr) if rr in self.stores else (tag or rr)
                slug = _store_instance_slug(area_rr, inst)
                for p in list_items(inst.get("StoreList")):
                    for it in list_items(p.get("ItemList")):
                        base_rr = (fld(it, "TemplateResRef", "")
                                   or fld(it, "InventoryRes", "")
                                   or fld(it, "EquippedRes", "") or "").strip()
                        if base_rr:
                            actual_rr = self._intern_item(base_rr, it)
                            if not skip:
                                self.item_sold_at[actual_rr].append(
                                    {"area_rr": area_rr, "slug": slug, "name": sname}
                                )

    def _index_container_items(self) -> None:
        # Container placeables — absorb and build item_in_container.
        for area_rr, containers in self.area_containers.items():
            skip = area_rr in self.hidden_areas
            for c in containers:
                p = c["p"]
                idx = c["idx"]
                tag = fld(p, "Tag", "")
                pname = (loc(p.get("LocName")) or tag
                         or fld(p, "TemplateResRef", "") or "(unnamed)")
                locked = bool(int(fld(p, "Locked", 0) or 0))
                dc = fld(p, "OpenLockDC", 0) or 0
                for it in list_items(p.get("ItemList")):
                    base_rr = (fld(it, "TemplateResRef", "")
                               or fld(it, "InventoryRes", "")
                               or fld(it, "EquippedRes", "") or "").strip()
                    if base_rr:
                        actual_rr = self._intern_item(base_rr, it)
                        if not skip:
                            self.item_in_container[actual_rr].append(
                                {"area_rr": area_rr, "idx": idx,
                                 "pname": pname, "locked": locked, "dc": dc}
                            )

    def _index_carried_items(self) -> None:
        # Creature instances — absorb and build item_carried_by.
        # crr in item_carried_by entries is now the canonical_rr so item pages
        # can link directly to the canonical creature page.
        _seen_carrier: set[tuple[str, str, str]] = set()
        for area_rr, insts in self.area_creature_instances.items():
            skip = area_rr in self.hidden_areas
            for inst in insts:
                c = inst["c"]
                bp_rr = fld(c, "TemplateResRef", "") or ""
                bp = self.creatures.get(bp_rr, {})
                can_rr = self.canonical_for_inst.get((area_rr, inst["idx"]), bp_rr)
                cname = self.canonical_creature_name(can_rr) or bp_rr
                equip_list = (list_items(c.get("Equip_ItemList"))
                              or list_items(bp.get("Equip_ItemList")))
                for e in equip_list:
                    base_rr = (fld(e, "EquippedRes", "")
                               or fld(e, "TemplateResRef", "") or "").strip()
                    if base_rr:
                        actual_rr = self._intern_item(base_rr, e)
                        dropable = bool(int(fld(e, "Dropable", 0) or 0))
                        pickpocketable = bool(int(fld(e, "Pickpocketable", 0) or 0))
                        key = (actual_rr, area_rr, can_rr)
                        if key not in _seen_carrier and not skip:
                            _seen_carrier.add(key)
                            self.item_carried_by[actual_rr].append(
                                {"area_rr": area_rr, "crr": can_rr,
                                 "cname": cname, "dropable": dropable,
                                 "pickpocketable": pickpocketable}
                            )
                for it in list_items(c.get("ItemList")):
                    base_rr = (fld(it, "InventoryRes", "")
                               or fld(it, "TemplateResRef", "") or "").strip()
                    if base_rr:
                        actual_rr = self._intern_item(base_rr, it)
                        dropable = bool(int(fld(it, "Dropable", 0) or 0))
                        pickpocketable = bool(int(fld(it, "Pickpocketable", 0) or 0))
                        key = (actual_rr, area_rr, can_rr)
                        if key not in _seen_carrier and not skip:
                            _seen_carrier.add(key)
                            self.item_carried_by[actual_rr].append(
                                {"area_rr": area_rr, "crr": can_rr,
                                 "cname": cname, "dropable": dropable,
                                 "pickpocketable": pickpocketable}
                            )

        for crr, spawns in self.creature_encounter_spawns.items():
            bp = self.creatures.get(crr, {})
            if not bp:
                continue
            can_rr = self.canonical_for_bp.get(crr, crr)
            cname = self.canonical_creature_name(can_rr)
            # Resolve each blueprint item to its variant resref once, then reuse
            # across all spawn areas (the blueprint's items are constant).
            item_entries: list[tuple[str, bool, bool]] = []  # (actual_rr, dropable, pickpocketable)
            for e in list_items(bp.get("Equip_ItemList")):
                base_rr = (fld(e, "EquippedRes", "")
                           or fld(e, "TemplateResRef", "") or "").strip()
                if base_rr:
                    item_entries.append(
                        (self._intern_item(base_rr, e),
                         bool(int(fld(e, "Dropable", 0) or 0)),
                         bool(int(fld(e, "Pickpocketable", 0) or 0)))
                    )
            for it in list_items(bp.get("ItemList")):
                base_rr = (fld(it, "InventoryRes", "")
                           or fld(it, "TemplateResRef", "") or "").strip()
                if base_rr:
                    item_entries.append(
                        (self._intern_item(base_rr, it),
                         bool(int(fld(it, "Dropable", 0) or 0)),
                         bool(int(fld(it, "Pickpocketable", 0) or 0)))
                    )
            for spawn in spawns:
                area_rr = spawn["area"]
                if area_rr in self.hidden_areas:
                    continue
                for actual_rr, dropable, pickpocketable in item_entries:
                    key = (actual_rr, area_rr, can_rr)
                    if key not in _seen_carrier:
                        _seen_carrier.add(key)
                        self.item_carried_by[actual_rr].append(
                            {"area_rr": area_rr, "crr": can_rr,
                             "cname": cname, "dropable": dropable,
                             "pickpocketable": pickpocketable}
                        )

    def _backfill_stock_items(self) -> None:
        # Back-fill stock BaseItem/Cost/PropertiesList for plain references.
        # For variant resrefs, use the base resref for stock data lookup; skip
        # PropertiesList backfill for variants (they already have explicit props).
        for irr, it in self.items.items():
            base = self.item_is_variant_of.get(irr, irr)
            if fld(it, "BaseItem") is None and base in STOCK_ITEM_BASE:
                it["BaseItem"] = STOCK_ITEM_BASE[base]
            if fld(it, "Cost", "") in ("", None) and base in STOCK_ITEM_COST:
                it["Cost"] = STOCK_ITEM_COST[base]
            if (not list_items(it.get("PropertiesList"))
                    and irr == base and irr in STOCK_ITEM_PROPS):
                it["PropertiesList"] = STOCK_ITEM_PROPS[irr]
