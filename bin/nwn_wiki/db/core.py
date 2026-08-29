"""Db construction, loading and the interning registries.

``DbCore`` owns the attribute schema (declared as dataclass fields), the pass
that reads the unpacked GFF-as-JSON tree off disk (``load``), and the two
interning helpers that decide when an inline item or a placed creature is
different enough from its blueprint to deserve its own synthetic resref and
wiki page.

It is the base of the mixin stack described in :mod:`nwn_wiki.db` -- the
dataclass decorator generates ``__init__`` here, so it must stay last in the
concrete class's bases.  The dataclass is deliberately neither frozen nor
slotted: loaders and renderers add attributes to a live ``Db`` at runtime.

``_conversation_key`` lives here because ``load`` and ``_intern_creature`` are
its only callers and this package may not import :mod:`nwn_wiki.cli` or the
renderers.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.escape import nwn_text
from nwn_wiki.itemprops import _creature_key, _item_prop_key


def _conversation_key(dlg: dict) -> tuple:
    """Stable fingerprint of a dialog tree's content.

    Captures node texts, action scripts, and tree adjacency.  Deliberately
    excludes animation, sound, delay, and comments — those are presentation
    details, not conversation identity, matching the philosophy of _creature_key().
    """
    entries = list_items(dlg.get("EntryList"))
    replies  = list_items(dlg.get("ReplyList"))

    node_parts: list[tuple] = []
    for i, e in enumerate(entries):
        text = nwn_text(loc(e.get("Text")))
        script = (fld(e, "Script", "") or "").strip().lower()
        node_parts.append(("entry", i, text, script))
    for i, r in enumerate(replies):
        text = nwn_text(loc(r.get("Text")))
        script = (fld(r, "Script", "") or "").strip().lower()
        node_parts.append(("reply", i, text, script))

    adj_parts: list[tuple] = []
    for i, e in enumerate(entries):
        reply_idxs = tuple(sorted(
            fld(ref, "Index") for ref in list_items(e.get("RepliesList"))
            if fld(ref, "Index") is not None
        ))
        adj_parts.append(("entry", i, reply_idxs))
    for i, r in enumerate(replies):
        entry_idxs = tuple(sorted(
            fld(ref, "Index") for ref in list_items(r.get("EntriesList"))
            if fld(ref, "Index") is not None
        ))
        adj_parts.append(("reply", i, entry_idxs))

    return (tuple(node_parts), tuple(adj_parts))


def _new(factory, /):
    """Schema entry whose default is a fresh ``factory()`` per Db instance."""
    return field(default_factory=factory)


def _index(value_factory, /):
    """Schema entry whose default is a fresh ``defaultdict(value_factory)``.

    Autovivifying index; ``value_factory`` builds the per-key collection.
    """
    return field(default_factory=partial(defaultdict, value_factory))


# eq/repr are off deliberately: the generated ones would deep-compare (and print)
# the whole module database, and __eq__ would drop Db's identity hash.
@dataclass(eq=False, repr=False)
class DbCore:
    src: Path
    areas: dict[str, dict] = _new(dict)
    gits: dict[str, dict] = _new(dict)
    gics: dict[str, dict] = _new(dict)
    creatures: dict[str, dict] = _new(dict)
    items: dict[str, dict] = _new(dict)
    doors: dict[str, dict] = _new(dict)
    triggers: dict[str, dict] = _new(dict)
    encounters: dict[str, dict] = _new(dict)
    placeables: dict[str, dict] = _new(dict)
    stores: dict[str, dict] = _new(dict)
    waypoints: dict[str, dict] = _new(dict)
    dialogs: dict[str, dict] = _new(dict)
    ifo: dict | None = None
    jrl: dict | None = None
    fac: dict | None = None

    # Derived
    waypoint_area: dict[str, str] = _new(dict)
    # Transitions: list of {src_area, dst_area, dst_tag, kind, label, src_resref,
    #   is_dup_tag?, dst_area_alts?}
    transitions: list[dict] = _new(list)
    # tag → list of {"kind", "area", "tag_label"} for every object carrying that
    # tag. Only populated for tags that are referenced by a transition AND appear
    # on more than one object (i.e. the destination is ambiguous).
    dup_dest_tags: dict[str, list[dict]] = _new(dict)
    # Direct-teleport transitions from placeable OnUsed scripts (not dialog-based).
    script_transitions: list[dict] = _new(list)
    area_npcs: dict[str, list[dict]] = _index(list)
    area_encounters: dict[str, list[dict]] = _index(list)
    area_placeables: dict[str, list[dict]] = _index(list)
    area_stores: dict[str, list[dict]] = _index(list)
    area_triggers: dict[str, list[dict]] = _index(list)
    area_doors: dict[str, list[dict]] = _index(list)
    area_waypoints: dict[str, list[dict]] = _index(list)
    # Containers = placeables with a non-empty ItemList. Stored as a
    # parallel list keyed by area resref + ordinal for stable URLs.
    area_containers: dict[str, list[dict]] = _index(list)
    # Creature instances: actual placements of creatures inside areas, as
    # opposed to the blueprint definitions in `creatures`. Each instance
    # is {"area": area_resref, "idx": ord_in_area, "c": git_creature}.
    # Keyed by area + ordinal for stable URLs.
    creature_instances: list[dict] = _new(list)
    area_creature_instances: dict[str, list[dict]] = _index(list)
    # Creature blueprint resref → list of {"area": area_rr, "encounter_resref": rr}
    # entries, one per (encounter placement, creature in its spawn pool).
    # Lets the creature page surface "spawned from encounter X in area Y"
    # alongside direct GIT placements.
    creature_encounter_spawns: dict[str, list[dict]] = _index(list)
    faction_friendly: dict[int, bool] = _new(dict)  # FactionID → friendly to PC?
    # FactionID → that faction's reputation TOWARD the PC faction (0-100, from
    # repute.fac's RepList). <= 10 means its creatures and encounters treat an
    # untouched player as an enemy.
    faction_rep_toward_pc: dict[int, int] = _new(dict)
    # Good/Evil allegiance sides (the Well of Eru orbs). FactionID → side name
    # ("Good"/"Evil"/"Neutral") for every faction the module treats as a player
    # allegiance, and the subset of those that actually have an invisible anchor
    # placeable (tagged Goodfaction/Evilfaction/Neutralfaction) placed in a GIT.
    # Taking a side at an anchor makes that side friendly to you, which is what
    # suppresses its encounters. See encounter_trigger_audience().
    allegiance_sides: dict[int, str] = _new(dict)
    allegiance_anchored: set = field(default_factory=set)

    # nss script index (resref → relative path) — used for "this script
    # exists" checks; not deep parsed.
    scripts: set[str] = _new(set)
    script_paths: dict[str, Path] = _new(dict)

    # ---- Dialog / script cross-references (index_scripts_and_dialogs) ----
    # script resref → set of dialog resrefs the script calls
    # ActionStartConversation against with a literal first arg.
    script_dialogs: dict[str, set[str]] = _index(set)
    # script resref → set of z-dialog handler names the script dispatches
    # via the HoMERs `StartDlg(pc, target, "handler", ...)` helper from
    # zdlg_include_i.nss. Z-dialogs are an entirely script-driven dialog
    # system: only `zdlg_converse.dlg` exists as a real GFF dialog, while
    # the actual conversation logic lives in NWScript handler files. We
    # synthesize a pseudo-dialog per handler so the wiki can surface them
    # alongside conventional .dlg conversations.
    script_zdialogs: dict[str, set[str]] = _index(set)
    # z-dialog handler resref → set of dispatcher script resrefs that
    # called StartDlg with that handler. Surfaced on the synthesized
    # conversation page so the reader can trace where the dialog opens.
    zdlg_handler_dispatchers: dict[str, set[str]] = _index(set)
    # script resref → set of waypoint/object tags the script jumps to.
    script_teleport_tags: dict[str, set[str]] = _index(set)
    # tag (waypoint, door, trigger, placeable, creature) → area resref.
    tag_to_area: dict[str, str] = _new(dict)
    # Tags added via WP_-prefix stripping fallback (see _build_tag_to_area).
    # Transitions resolved through these tags may not work in-game if the
    # script uses the short form but the actual waypoint tag has the WP_ prefix.
    fallback_tags: set[str] = _new(set)
    # dlg resref → list of caller descriptors:
    #   {"kind": "creature"|"placeable"|"door", "resref": ..., "areas": [...]}
    #   {"kind": "module-event", "event": "OnPlayerRest", "script": "..."}
    #   {"kind": "creature-event", "resref": ..., "event": "ScriptDialogue", "script": ...}
    #   {"kind": "placeable-event", "resref": ..., "event": "OnUsed", "script": ...}
    #   {"kind": "trigger-event", "resref": ..., "event": "OnEnter", "script": ...}
    #   {"kind": "area-event", "resref": ..., "event": "OnEnter", "script": ...}
    #   {"kind": "item-script", "resref": ..., "script": ...}  (tag-based scripting)
    dialog_callers: dict[str, list[dict]] = _index(list)
    # dlg resref → list of teleport destinations reachable from the dialog:
    #   {"tag": <waypoint/object tag>, "area": <area resref or None>,
    #    "via_script": <script resref>, "node_kind": "entry"|"reply",
    #    "node_index": <int>}
    dialog_teleports: dict[str, list[dict]] = _index(list)
    # dlg resref → list of {"resref": script, "kind": "active"|"action",
    #                       "node_kind": "entry"|"reply", "node_index": int}
    dialog_scripts: dict[str, list[dict]] = _index(list)
    # script resref → set of store tags the script opens via OpenStore().
    script_store_tags: dict[str, set[str]] = _index(set)
    # scripts that call OpenStore but without a literal tag lookup —
    # typically Bedlamson's Dynamic Merchant System (bdm_cnv_opn_stor),
    # which opens whichever store object was stored in a local var at spawn.
    script_bdm_open: set[str] = _new(set)
    # store instance tag (lowercase) → list of opener descriptors, same
    # shape as dialog_callers entries plus optional "via_dialog"/"via_script".
    store_tag_openers: dict[str, list[dict]] = _index(list)
    # Pseudo-map nodes for conversations triggered "globally" (rest menu etc.)
    # that contain teleports. id → {label, conv_resref, dests: [area resrefs]}
    global_convo_pseudo: dict[str, dict] = _new(dict)
    # Edges (area-or-pseudo-id → area resref) introduced by conversation
    # teleports, drawn alongside trigger/door transitions on the map.
    conv_transitions: list[dict] = _new(list)
    # Player-option labels (exact, post-strip, color-tokens removed) whose
    # subtree of teleport scripts should NOT contribute to the area map.
    # Lets a builder hide admin/DM teleport menus (e.g. "[Admin Options]")
    # from cluttering map edges while still surfacing them on the
    # conversation page. Set via --exclude-conv-option on the CLI.
    exclude_option_texts: list[str] = _new(list)
    # Combat-stat dials (module/server-specific), set from the CLI in main().
    #   max_character_level: server level cap; a creature's total class
    #     levels are clamped to this when summing base attack bonus (so a
    #     130-HD boss on a level-40 server gets BAB for ~40 levels, not 130).
    #     0 disables the cap. See README "Combat-stat configuration".
    #   max_ability_bonus: cap on the ability-score bonus an item may grant
    #     (NWN default +12; some modules raise it, e.g. +24).
    #   max_player_level: the level cap a *player* can actually reach, used
    #     only to cap the counter-gear report's reference PC. Deliberately
    #     separate from max_character_level: a server can raise the player
    #     cap (NWN_MAXLEVEL=60 here, via NWNX MaxLevel) while creature BAB
    #     still wants clamping at the engine's own 40 — changing the latter
    #     would move published creature attack figures.
    max_character_level: int = 40
    max_ability_bonus: int = 12
    max_player_level: int = 40
    # Set from main(): the resolved --2da-dir (part of the counter-gear
    # staleness fingerprint) and whether --counter-gear asked for a re-run.
    twoda_dir: "Path | None" = None
    run_counter_gear: bool = False
    # Replacement dice a module grants on an ordinary critical when it has
    # disabled the engine's save-or-die Devastating Critical (detected from
    # baseitems.2da, not configured). 0 = stock behaviour.
    devcrit_bonus_dice: int = 0
    # Areas whose Comments field contains "WIKI_HIDDEN" (case-insensitive).
    # These areas, their stores, and their creature placements are omitted
    # from all wiki output (map, index pages, cross-references).
    hidden_areas: set[str] = _new(set)
    # Item source cross-references (built in index())
    # item resref → stores that sell it: [{"area_rr", "slug", "name"}]
    item_sold_at: dict[str, list[dict]] = _index(list)
    # item resref → containers that hold it: [{"area_rr", "idx", "pname", "locked", "dc"}]
    item_in_container: dict[str, list[dict]] = _index(list)
    # item resref → creatures that carry it: [{"area_rr", "crr", "cname", "dropable"}]
    # dropable=True means Dropable=1 on that item's equip/inventory entry — it will
    # appear in the loot bag when the creature dies.
    item_carried_by: dict[str, list[dict]] = _index(list)
    # script resref → list of item resrefs created via CreateItemOnObject
    script_creates_items: dict[str, list[str]] = _index(list)
    # scripts that call GenerateTreasure/GenerateHighTreasure/etc. (random loot)
    script_generates_treasure: set[str] = _new(set)
    # custom token number → set of script resrefs that call SetCustomToken(N, ...)
    token_setters: dict[int, set[str]] = _index(set)
    # quest plot tag (lowercased) → {entry id → set of script resrefs that
    # award that entry via AddJournalQuestEntry("tag", id, ...)}. Surfaced on
    # the per-quest pages so the reader can trace where each step is granted.
    quest_grants: dict[str, dict[int, set[str]]] = _index(partial(defaultdict, set))
    # quest plot tag (lowercased) → {entry id → set of dialog resrefs} for quests
    # granted via a dialog node's built-in Quest/QuestEntry fields (no script needed).
    quest_dialog_grants: dict[str, dict[int, set[str]]] = _index(partial(defaultdict, set))
    # dialog resref → {quest_tag_lower → set of entry_ids granted in that dialog}
    # (reverse of quest_grants + quest_dialog_grants; built by _build_dialog_quest_index)
    dialog_quest_grants_rev: dict[str, dict[str, set[int]]] = _index(partial(defaultdict, set))
    # quest_tag_lower → (display_name, slug) for link generation
    quest_tag_to_info: dict[str, tuple[str, str]] = _new(dict)
    # item resref → list of script-source dicts for the item page
    # {"kind", "script", "label", "areas", "dlg", "crr", "prr"}
    item_from_script: dict[str, list[dict]] = _index(list)
    # item resref → [{"area_rr", "kind" (container|door), "name", "idx" (containers only),
    #                  "required" (bool), "dst_area" (doors only, may be None)}]
    item_is_key_for: dict[str, list[dict]] = _index(list)
    # script resref → set of item tags checked via GetItemPossessedBy
    script_checks_item_tags: dict[str, set[str]] = _index(set)
    # script resref → set of item tags that a damage-affecting script (OnDamaged)
    # requires the damager to wield/hold (e.g. dunharrowking.nss heals back all
    # damage unless the attacker's weapon Tag == "narsil"). Surfaced on creature
    # pages as a "can only be damaged by" requirement.
    script_damage_req_tags: dict[str, set[str]] = _index(set)
    # script resrefs that mitigate the creature's own incoming damage (self-heal,
    # HP restore, or self-applied damage immunity). Used together with
    # script_damage_req_tags to decide whether to warn on the creature page.
    script_mitigates_damage: set[str] = _new(set)
    # script resref → retaliation analysis dict (see _analyze_retaliation) for
    # OnDamaged scripts that strike back at the attacker / nearby creatures.
    script_retaliation: dict[str, dict] = _new(dict)
    # item resref → list of {kind, script, event, area(s), …} for script-based key checks
    item_script_checks: dict[str, list[dict]] = _index(list)
    # dialog resref → sorted list of item resrefs whose tags are checked in that dialog
    dialog_item_checks: dict[str, list[str]] = _index(list)
    # (area_rr, container_idx) pairs for containers with random-treasure OnOpen
    random_treasure_containers: set[tuple[str, int]] = _new(set)
    # Property-variant tracking: inline items with the same TemplateResRef but
    # different PropertiesList get synthetic resrefs (base__v2, base__v3 …).
    # item_variants_of: base_rr → [variant_rr, ...]
    # item_is_variant_of: variant_rr → base_rr
    # _item_prop_registry: base_rr → {prop_key → assigned_rr} (internal)
    item_variants_of: dict[str, list[str]] = _index(list)
    item_is_variant_of: dict[str, str] = _new(dict)
    _item_prop_registry: dict[str, dict[tuple, str]] = _new(dict)

    # Canonical creature tracking (built in index() after encounter_spawns).
    # canonical_rr → {"c": gff_struct, "bp_rr": source_blueprint_rr}
    # For a blueprint-only canonical, "c" == db.creatures[bp_rr].
    # For a variant, "c" is the instance GFF struct that first introduced
    # the differing key, and "bp_rr" is the source blueprint resref.
    canonical_creatures: dict[str, dict] = _new(dict)
    # blueprint_rr → canonical_rr  (equals rr for non-variants)
    canonical_for_bp: dict[str, str] = _new(dict)
    # (area_rr, idx) → canonical_rr
    canonical_for_inst: dict[tuple, str] = _new(dict)
    # canonical_rr → source blueprint_rr (rr → rr for non-variants)
    canonical_bp_of: dict[str, str] = _new(dict)
    # internal: blueprint_rr → {creature_key_tuple → canonical_rr}
    _creature_key_registry: dict[str, dict[tuple, str]] = _new(dict)
    # canonical_rr → [{"area", "kind": "placed"|"encounter"|"script", "enc_rr",
    #                  "count", "respawn_kind", "respawn_seconds"}]
    # Respawn is per-location, not per-creature: the same blueprint can be a
    # never-respawning placement in one area and a 60s encounter spawn in the
    # next, so it is part of the aggregation key (see _index_canonical_locations).
    canonical_locations: dict[str, list[dict]] = _index(list)
    # canonical_rr → set of FactionIDs seen across its placed GIT instances
    canonical_inst_factions: dict[str, set[int]] = _index(set)
    # Script-spawned creatures (built in index_dialogs() after _build_tag_to_area()).
    # area_rr → [{"bp_rr": str, "can_rr": str, "script": str}]
    area_script_spawns: dict[str, list[dict]] = _index(list)

    # Tag-conflict tracking for stores, areas, and conversations.
    # store_tag_groups: tag_lower → [resref, ...]  (populated during load())
    store_tag_groups: dict[str, list[str]] = _index(list)
    # area_tag_groups: tag_lower → [resref, ...]  (populated during load())
    area_tag_groups: dict[str, list[str]] = _index(list)
    # item_tag_groups: tag_lower → [resref, ...]  (populated during load()).
    # Used to resolve an item Tag (e.g. a damage-gate requirement parsed from a
    # script) back to the item blueprint(s) so the wiki can link to its page.
    item_tag_groups: dict[str, list[str]] = _index(list)
    # _dialog_key_registry: conversation_key → [resref, ...]  (populated during load())
    _dialog_key_registry: dict[tuple, list[str]] = _index(list)
    # script resref → [{"creature_resref": str, "waypoint_tag": str}] raw pairs
    # extracted during _parse_scripts(); resolved to areas after _build_tag_to_area().
    script_creature_waypoint_spawns: dict[str, list[dict]] = _index(list)

    # ---- Loading ----

    def load(self) -> None:
        t0 = time.time()
        files = sorted(self.src.iterdir())
        n_loaded = 0
        for path in files:
            if path.is_dir():
                continue
            name = path.name
            if name.endswith(".nss"):
                resref = name[:-4]
                self.scripts.add(resref)
                self.script_paths[resref] = path
                continue
            if not name.endswith(".json"):
                continue
            # Examples: bree.are.json, 001.uti.json, module.ifo.json
            stem = name[:-5]  # strip ".json"
            # parts[-1] is the gff type tag (lowercase)
            parts = stem.split(".")
            if len(parts) < 2:
                continue
            kind = parts[-1].lower()
            resref = ".".join(parts[:-1])
            try:
                obj = json.loads(path.read_text())
            except Exception as e:
                print(f"  warn: failed to parse {name}: {e}", file=sys.stderr)
                continue
            n_loaded += 1
            if kind == "are":
                self.areas[resref] = obj
                _area_tag = (fld(obj, "Tag") or resref).strip().lower()
                self.area_tag_groups[_area_tag].append(resref)
            elif kind == "git":
                self.gits[resref] = obj
            elif kind == "gic":
                self.gics[resref] = obj
            elif kind == "utc":
                self.creatures[resref] = obj
            elif kind == "uti":
                self.items[resref] = obj
                _item_tag = (fld(obj, "Tag") or resref).strip().lower()
                if _item_tag:
                    self.item_tag_groups[_item_tag].append(resref)
            elif kind == "utd":
                self.doors[resref] = obj
            elif kind == "utt":
                self.triggers[resref] = obj
            elif kind == "ute":
                self.encounters[resref] = obj
            elif kind == "utp":
                self.placeables[resref] = obj
            elif kind == "utm":
                self.stores[resref] = obj
                _store_tag = (fld(obj, "Tag") or resref).strip().lower()
                self.store_tag_groups[_store_tag].append(resref)
            elif kind == "utw":
                self.waypoints[resref] = obj
            elif kind == "dlg":
                self.dialogs[resref] = obj
                _dlg_key = _conversation_key(obj)
                self._dialog_key_registry[_dlg_key].append(resref)
            elif kind == "ifo" and resref == "module":
                self.ifo = obj
            elif kind == "jrl" and resref == "module":
                self.jrl = obj
            elif kind == "fac" and resref == "repute":
                self.fac = obj
            # everything else (palettes, etc.) ignored
        print(f"  loaded {n_loaded} json files + {len(self.scripts)} scripts in {time.time()-t0:.1f}s")

    # ---- Item variant registry ----

    def _intern_item(self, base_rr: str, it: dict) -> str:
        """Register an inline item and return the resref to use for cross-references.

        Inline items with no PropertiesList defer to the blueprint — they map to
        base_rr unchanged.  Items with PropertiesList are compared to what is already
        stored for base_rr; if the properties differ, a synthetic variant resref
        (base_rr__v2, __v3 …) is created so each unique property set gets its own
        wiki page and accurate "where to find" listings.
        """
        inline_props = list_items(it.get("PropertiesList"))

        if not inline_props:
            # Thin reference — ensure the base is registered, return it as-is.
            if base_rr not in self.items:
                self.items[base_rr] = it
            return base_rr

        prop_key = _item_prop_key(inline_props)
        registry = self._item_prop_registry.setdefault(base_rr, {})

        if prop_key in registry:
            return registry[prop_key]

        if base_rr not in self.items:
            # First rich instance for this resref — store as the base.
            self.items[base_rr] = it
            registry[prop_key] = base_rr
            return base_rr

        stored_key = _item_prop_key(list_items(self.items[base_rr].get("PropertiesList")))

        if not stored_key:
            # Stored version is a thin reference (no props) — promote this rich
            # version to the base so thin refs don't spawn spurious variants.
            self.items[base_rr] = it
            registry[prop_key] = base_rr
            return base_rr

        if stored_key == prop_key:
            registry[prop_key] = base_rr
            return base_rr

        # Properties differ — create a numbered variant resref.
        n = len(self.item_variants_of[base_rr]) + 2
        variant_rr = f"{base_rr}__v{n}"
        self.items[variant_rr] = it
        self.item_variants_of[base_rr].append(variant_rr)
        self.item_is_variant_of[variant_rr] = base_rr
        registry[prop_key] = variant_rr
        return variant_rr

    def _intern_creature(self, bp_rr: str, c: dict, *, bp: dict | None = None) -> str:
        """Register a creature (blueprint or GIT instance) and return its
        canonical_rr.

        If the creature's _creature_key() matches what is already stored for
        bp_rr, the canonical_rr equals bp_rr (or the previously-assigned rr).
        If the key differs from the blueprint's key, a variant canonical is
        created: bp_rr__v2, bp_rr__v3, etc.

        Conversation content is also included in the key: a creature instance
        whose assigned dialog tree differs from the blueprint's becomes its own
        canonical variant, exactly like a stat-differing instance.
        """
        conv_resref = (fld(c, "Conversation", "") or "").strip().lower()
        if not conv_resref and bp is not None:
            conv_resref = (fld(bp, "Conversation", "") or "").strip().lower()
        conv_key = _conversation_key(self.dialogs[conv_resref]) if conv_resref in self.dialogs else ()
        # Display name is part of identity: a blueprint placed under different
        # FirstName overrides (e.g. cultmember002 as both "Numarok The Black Hand"
        # and "Numanan Numerocks Second Hand") should get one wiki page per name
        # rather than collapsing to a single combat-identical canonical.
        first = loc(c.get("FirstName")) or (loc(bp.get("FirstName")) if bp else None)
        last = loc(c.get("LastName")) or (loc(bp.get("LastName")) if bp else None)
        name_key = ((first or "") + " " + (last or "")).strip().lower()
        key = (_creature_key(c, bp), conv_key, name_key)
        registry = self._creature_key_registry.setdefault(bp_rr, {})

        if key in registry:
            return registry[key]

        if bp_rr not in self.canonical_creatures:
            # First registration for this blueprint — it is its own canonical.
            self.canonical_creatures[bp_rr] = {"c": c, "bp_rr": bp_rr}
            self.canonical_bp_of[bp_rr] = bp_rr
            registry[key] = bp_rr
            return bp_rr

        # Blueprint already registered; check whether this key matches it.
        existing_key = next(iter(registry))
        if existing_key == key:
            registry[key] = bp_rr
            return bp_rr

        # Different key — create a numbered variant.
        n = sum(1 for v, src in self.canonical_bp_of.items()
                if src == bp_rr and v != bp_rr) + 2
        variant_rr = f"{bp_rr}__v{n}"
        self.canonical_creatures[variant_rr] = {"c": c, "bp_rr": bp_rr}
        self.canonical_bp_of[variant_rr] = bp_rr
        registry[key] = variant_rr
        return variant_rr
