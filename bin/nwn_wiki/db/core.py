"""Db construction, loading and the interning registries.

``DbCore`` owns the attribute schema (``__init__``), the pass that reads the
unpacked GFF-as-JSON tree off disk (``load``), and the two interning helpers
that decide when an inline item or a placed creature is different enough from
its blueprint to deserve its own synthetic resref and wiki page.

It is the base of the mixin stack described in :mod:`nwn_wiki.db` -- it defines
``__init__``, so it must stay last in the concrete class's bases.

``_conversation_key`` lives here rather than in :mod:`nwn_wiki.cli` because
``load`` and ``_intern_creature`` are its only callers and this package may not
import the monolith.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
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


class DbCore:
    def __init__(self, src: Path):
        self.src = src
        self.areas: dict[str, dict] = {}
        self.gits: dict[str, dict] = {}
        self.gics: dict[str, dict] = {}
        self.creatures: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.doors: dict[str, dict] = {}
        self.triggers: dict[str, dict] = {}
        self.encounters: dict[str, dict] = {}
        self.placeables: dict[str, dict] = {}
        self.stores: dict[str, dict] = {}
        self.waypoints: dict[str, dict] = {}
        self.dialogs: dict[str, dict] = {}
        self.ifo: dict | None = None
        self.jrl: dict | None = None
        self.fac: dict | None = None

        # Derived
        self.waypoint_area: dict[str, str] = {}
        # Transitions: list of {src_area, dst_area, dst_tag, kind, label, src_resref,
        #   is_dup_tag?, dst_area_alts?}
        self.transitions: list[dict] = []
        # tag → list of {"kind", "area", "tag_label"} for every object carrying that
        # tag. Only populated for tags that are referenced by a transition AND appear
        # on more than one object (i.e. the destination is ambiguous).
        self.dup_dest_tags: dict[str, list[dict]] = {}
        # Direct-teleport transitions from placeable OnUsed scripts (not dialog-based).
        self.script_transitions: list[dict] = []
        self.area_npcs: dict[str, list[dict]] = defaultdict(list)
        self.area_encounters: dict[str, list[dict]] = defaultdict(list)
        self.area_placeables: dict[str, list[dict]] = defaultdict(list)
        self.area_stores: dict[str, list[dict]] = defaultdict(list)
        self.area_triggers: dict[str, list[dict]] = defaultdict(list)
        self.area_doors: dict[str, list[dict]] = defaultdict(list)
        self.area_waypoints: dict[str, list[dict]] = defaultdict(list)
        # Containers = placeables with a non-empty ItemList. Stored as a
        # parallel list keyed by area resref + ordinal for stable URLs.
        self.area_containers: dict[str, list[dict]] = defaultdict(list)
        # Creature instances: actual placements of creatures inside areas, as
        # opposed to the blueprint definitions in `creatures`. Each instance
        # is {"area": area_resref, "idx": ord_in_area, "c": git_creature}.
        # Keyed by area + ordinal for stable URLs.
        self.creature_instances: list[dict] = []
        self.area_creature_instances: dict[str, list[dict]] = defaultdict(list)
        # Creature blueprint resref → list of {"area": area_rr, "encounter_resref": rr}
        # entries, one per (encounter placement, creature in its spawn pool).
        # Lets the creature page surface "spawned from encounter X in area Y"
        # alongside direct GIT placements.
        self.creature_encounter_spawns: dict[str, list[dict]] = defaultdict(list)
        self.faction_friendly: dict[int, bool] = {}  # FactionID → friendly to PC?

        # nss script index (resref → relative path) — used for "this script
        # exists" checks; not deep parsed.
        self.scripts: set[str] = set()
        self.script_paths: dict[str, Path] = {}

        # ---- Dialog / script cross-references (index_scripts_and_dialogs) ----
        # script resref → set of dialog resrefs the script calls
        # ActionStartConversation against with a literal first arg.
        self.script_dialogs: dict[str, set[str]] = defaultdict(set)
        # script resref → set of z-dialog handler names the script dispatches
        # via the HoMERs `StartDlg(pc, target, "handler", ...)` helper from
        # zdlg_include_i.nss. Z-dialogs are an entirely script-driven dialog
        # system: only `zdlg_converse.dlg` exists as a real GFF dialog, while
        # the actual conversation logic lives in NWScript handler files. We
        # synthesize a pseudo-dialog per handler so the wiki can surface them
        # alongside conventional .dlg conversations.
        self.script_zdialogs: dict[str, set[str]] = defaultdict(set)
        # z-dialog handler resref → set of dispatcher script resrefs that
        # called StartDlg with that handler. Surfaced on the synthesized
        # conversation page so the reader can trace where the dialog opens.
        self.zdlg_handler_dispatchers: dict[str, set[str]] = defaultdict(set)
        # script resref → set of waypoint/object tags the script jumps to.
        self.script_teleport_tags: dict[str, set[str]] = defaultdict(set)
        # tag (waypoint, door, trigger, placeable, creature) → area resref.
        self.tag_to_area: dict[str, str] = {}
        # Tags added via WP_-prefix stripping fallback (see _build_tag_to_area).
        # Transitions resolved through these tags may not work in-game if the
        # script uses the short form but the actual waypoint tag has the WP_ prefix.
        self.fallback_tags: set[str] = set()
        # dlg resref → list of caller descriptors:
        #   {"kind": "creature"|"placeable"|"door", "resref": ..., "areas": [...]}
        #   {"kind": "module-event", "event": "OnPlayerRest", "script": "..."}
        #   {"kind": "creature-event", "resref": ..., "event": "ScriptDialogue", "script": ...}
        #   {"kind": "placeable-event", "resref": ..., "event": "OnUsed", "script": ...}
        #   {"kind": "trigger-event", "resref": ..., "event": "OnEnter", "script": ...}
        #   {"kind": "area-event", "resref": ..., "event": "OnEnter", "script": ...}
        #   {"kind": "item-script", "resref": ..., "script": ...}  (tag-based scripting)
        self.dialog_callers: dict[str, list[dict]] = defaultdict(list)
        # dlg resref → list of teleport destinations reachable from the dialog:
        #   {"tag": <waypoint/object tag>, "area": <area resref or None>,
        #    "via_script": <script resref>, "node_kind": "entry"|"reply",
        #    "node_index": <int>}
        self.dialog_teleports: dict[str, list[dict]] = defaultdict(list)
        # dlg resref → list of {"resref": script, "kind": "active"|"action",
        #                       "node_kind": "entry"|"reply", "node_index": int}
        self.dialog_scripts: dict[str, list[dict]] = defaultdict(list)
        # script resref → set of store tags the script opens via OpenStore().
        self.script_store_tags: dict[str, set[str]] = defaultdict(set)
        # scripts that call OpenStore but without a literal tag lookup —
        # typically Bedlamson's Dynamic Merchant System (bdm_cnv_opn_stor),
        # which opens whichever store object was stored in a local var at spawn.
        self.script_bdm_open: set[str] = set()
        # store instance tag (lowercase) → list of opener descriptors, same
        # shape as dialog_callers entries plus optional "via_dialog"/"via_script".
        self.store_tag_openers: dict[str, list[dict]] = defaultdict(list)
        # Pseudo-map nodes for conversations triggered "globally" (rest menu etc.)
        # that contain teleports. id → {label, conv_resref, dests: [area resrefs]}
        self.global_convo_pseudo: dict[str, dict] = {}
        # Edges (area-or-pseudo-id → area resref) introduced by conversation
        # teleports, drawn alongside trigger/door transitions on the map.
        self.conv_transitions: list[dict] = []
        # Player-option labels (exact, post-strip, color-tokens removed) whose
        # subtree of teleport scripts should NOT contribute to the area map.
        # Lets a builder hide admin/DM teleport menus (e.g. "[Admin Options]")
        # from cluttering map edges while still surfacing them on the
        # conversation page. Set via --exclude-conv-option on the CLI.
        self.exclude_option_texts: list[str] = []
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
        self.max_character_level: int = 40
        self.max_ability_bonus: int = 12
        self.max_player_level: int = 40
        # Set from main(): the resolved --2da-dir (part of the counter-gear
        # staleness fingerprint) and whether --counter-gear asked for a re-run.
        self.twoda_dir: "Path | None" = None
        self.run_counter_gear: bool = False
        # Replacement dice a module grants on an ordinary critical when it has
        # disabled the engine's save-or-die Devastating Critical (detected from
        # baseitems.2da, not configured). 0 = stock behaviour.
        self.devcrit_bonus_dice: int = 0
        # Areas whose Comments field contains "WIKI_HIDDEN" (case-insensitive).
        # These areas, their stores, and their creature placements are omitted
        # from all wiki output (map, index pages, cross-references).
        self.hidden_areas: set[str] = set()
        # Item source cross-references (built in index())
        # item resref → stores that sell it: [{"area_rr", "slug", "name"}]
        self.item_sold_at: dict[str, list[dict]] = defaultdict(list)
        # item resref → containers that hold it: [{"area_rr", "idx", "pname", "locked", "dc"}]
        self.item_in_container: dict[str, list[dict]] = defaultdict(list)
        # item resref → creatures that carry it: [{"area_rr", "crr", "cname", "dropable"}]
        # dropable=True means Dropable=1 on that item's equip/inventory entry — it will
        # appear in the loot bag when the creature dies.
        self.item_carried_by: dict[str, list[dict]] = defaultdict(list)
        # script resref → list of item resrefs created via CreateItemOnObject
        self.script_creates_items: dict[str, list[str]] = defaultdict(list)
        # scripts that call GenerateTreasure/GenerateHighTreasure/etc. (random loot)
        self.script_generates_treasure: set[str] = set()
        # custom token number → set of script resrefs that call SetCustomToken(N, ...)
        self.token_setters: dict[int, set[str]] = defaultdict(set)
        # quest plot tag (lowercased) → {entry id → set of script resrefs that
        # award that entry via AddJournalQuestEntry("tag", id, ...)}. Surfaced on
        # the per-quest pages so the reader can trace where each step is granted.
        self.quest_grants: dict[str, dict[int, set[str]]] = defaultdict(
            lambda: defaultdict(set))
        # quest plot tag (lowercased) → {entry id → set of dialog resrefs} for quests
        # granted via a dialog node's built-in Quest/QuestEntry fields (no script needed).
        self.quest_dialog_grants: dict[str, dict[int, set[str]]] = defaultdict(
            lambda: defaultdict(set))
        # dialog resref → {quest_tag_lower → set of entry_ids granted in that dialog}
        # (reverse of quest_grants + quest_dialog_grants; built by _build_dialog_quest_index)
        self.dialog_quest_grants_rev: dict[str, dict[str, set[int]]] = defaultdict(
            lambda: defaultdict(set))
        # quest_tag_lower → (display_name, slug) for link generation
        self.quest_tag_to_info: dict[str, tuple[str, str]] = {}
        # item resref → list of script-source dicts for the item page
        # {"kind", "script", "label", "areas", "dlg", "crr", "prr"}
        self.item_from_script: dict[str, list[dict]] = defaultdict(list)
        # item resref → [{"area_rr", "kind" (container|door), "name", "idx" (containers only),
        #                  "required" (bool), "dst_area" (doors only, may be None)}]
        self.item_is_key_for: dict[str, list[dict]] = defaultdict(list)
        # script resref → set of item tags checked via GetItemPossessedBy
        self.script_checks_item_tags: dict[str, set[str]] = defaultdict(set)
        # script resref → set of item tags that a damage-affecting script (OnDamaged)
        # requires the damager to wield/hold (e.g. dunharrowking.nss heals back all
        # damage unless the attacker's weapon Tag == "narsil"). Surfaced on creature
        # pages as a "can only be damaged by" requirement.
        self.script_damage_req_tags: dict[str, set[str]] = defaultdict(set)
        # script resrefs that mitigate the creature's own incoming damage (self-heal,
        # HP restore, or self-applied damage immunity). Used together with
        # script_damage_req_tags to decide whether to warn on the creature page.
        self.script_mitigates_damage: set[str] = set()
        # script resref → retaliation analysis dict (see _analyze_retaliation) for
        # OnDamaged scripts that strike back at the attacker / nearby creatures.
        self.script_retaliation: dict[str, dict] = {}
        # item resref → list of {kind, script, event, area(s), …} for script-based key checks
        self.item_script_checks: dict[str, list[dict]] = defaultdict(list)
        # dialog resref → sorted list of item resrefs whose tags are checked in that dialog
        self.dialog_item_checks: dict[str, list[str]] = defaultdict(list)
        # (area_rr, container_idx) pairs for containers with random-treasure OnOpen
        self.random_treasure_containers: set[tuple[str, int]] = set()
        # Property-variant tracking: inline items with the same TemplateResRef but
        # different PropertiesList get synthetic resrefs (base__v2, base__v3 …).
        # item_variants_of: base_rr → [variant_rr, ...]
        # item_is_variant_of: variant_rr → base_rr
        # _item_prop_registry: base_rr → {prop_key → assigned_rr} (internal)
        self.item_variants_of: dict[str, list[str]] = defaultdict(list)
        self.item_is_variant_of: dict[str, str] = {}
        self._item_prop_registry: dict[str, dict[tuple, str]] = {}

        # Canonical creature tracking (built in index() after encounter_spawns).
        # canonical_rr → {"c": gff_struct, "bp_rr": source_blueprint_rr}
        # For a blueprint-only canonical, "c" == db.creatures[bp_rr].
        # For a variant, "c" is the instance GFF struct that first introduced
        # the differing key, and "bp_rr" is the source blueprint resref.
        self.canonical_creatures: dict[str, dict] = {}
        # blueprint_rr → canonical_rr  (equals rr for non-variants)
        self.canonical_for_bp: dict[str, str] = {}
        # (area_rr, idx) → canonical_rr
        self.canonical_for_inst: dict[tuple, str] = {}
        # canonical_rr → source blueprint_rr (rr → rr for non-variants)
        self.canonical_bp_of: dict[str, str] = {}
        # internal: blueprint_rr → {creature_key_tuple → canonical_rr}
        self._creature_key_registry: dict[str, dict[tuple, str]] = {}
        # canonical_rr → [{"area", "kind": "placed"|"encounter"|"script", "enc_rr", "count"}]
        self.canonical_locations: dict[str, list[dict]] = defaultdict(list)
        # canonical_rr → set of FactionIDs seen across its placed GIT instances
        self.canonical_inst_factions: dict[str, set[int]] = defaultdict(set)
        # Script-spawned creatures (built in index_dialogs() after _build_tag_to_area()).
        # area_rr → [{"bp_rr": str, "can_rr": str, "script": str}]
        self.area_script_spawns: dict[str, list[dict]] = defaultdict(list)

        # Tag-conflict tracking for stores, areas, and conversations.
        # store_tag_groups: tag_lower → [resref, ...]  (populated during load())
        self.store_tag_groups: dict[str, list[str]] = defaultdict(list)
        # area_tag_groups: tag_lower → [resref, ...]  (populated during load())
        self.area_tag_groups: dict[str, list[str]] = defaultdict(list)
        # item_tag_groups: tag_lower → [resref, ...]  (populated during load()).
        # Used to resolve an item Tag (e.g. a damage-gate requirement parsed from a
        # script) back to the item blueprint(s) so the wiki can link to its page.
        self.item_tag_groups: dict[str, list[str]] = defaultdict(list)
        # _dialog_key_registry: conversation_key → [resref, ...]  (populated during load())
        self._dialog_key_registry: dict[tuple, list[str]] = defaultdict(list)
        # script resref → [{"creature_resref": str, "waypoint_tag": str}] raw pairs
        # extracted during _parse_scripts(); resolved to areas after _build_tag_to_area().
        self.script_creature_waypoint_spawns: dict[str, list[dict]] = defaultdict(list)

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
