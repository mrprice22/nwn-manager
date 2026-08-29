"""Display-name lookups: resref/id -> the string the wiki shows.

``DbNamesMixin`` owns the small convenience getters every renderer calls to
turn an internal identifier into human-readable text: area, creature, item
and store names, faction id -> faction name (plus the friendly/hostile test),
the two instance-aware creature name variants, and a dialog's short label.

One mixin of the stack described in :mod:`nwn_wiki.db`; see that docstring
for the rules it follows.  Everything here reads only ``self`` state that the
loader/index passes have already populated, plus the stock-name tables from
:mod:`nwn_wiki.gff`.
"""

from __future__ import annotations

from nwn_wiki.gff import (
    STOCK_CREATURE_NAMES,
    STOCK_ITEM_NAMES,
    fld,
    list_items,
    loc,
)
from nwn_wiki.htmlgen.escape import nwn_text


class DbNamesMixin:
    def area_name(self, resref: str) -> str:
        a = self.areas.get(resref)
        if not a:
            return resref
        return loc(a.get("Name")) or resref

    def creature_name(self, resref: str) -> str:
        c = self.creatures.get(resref)
        if not c:
            return STOCK_CREATURE_NAMES.get(resref, resref)
        first = loc(c.get("FirstName"))
        last = loc(c.get("LastName"))
        full = (first + " " + last).strip()
        return full or STOCK_CREATURE_NAMES.get(resref, resref)

    def item_name(self, resref: str) -> str:
        i = self.items.get(resref)
        if not i:
            return STOCK_ITEM_NAMES.get(resref, resref)
        resolved = loc(i.get("LocalizedName"))
        if not resolved or resolved.startswith("[TLK#"):
            base = self.item_is_variant_of.get(resref, resref)
            return (STOCK_ITEM_NAMES.get(base)
                    or STOCK_ITEM_NAMES.get(resref)
                    or resolved or resref)
        return resolved

    def store_name(self, resref: str) -> str:
        s = self.stores.get(resref)
        if not s:
            return resref
        return loc(s.get("LocName")) or resref

    def is_friendly(self, faction_id: int | None) -> bool:
        if faction_id is None:
            return True
        return self.faction_friendly.get(int(faction_id), True)

    def faction_name(self, faction_id) -> str:
        """Human-readable faction name from repute.fac.json's FactionList.
        Falls back to the numeric id if the faction can't be resolved."""
        if faction_id is None or faction_id == "":
            return ""
        try:
            i = int(faction_id)
        except (TypeError, ValueError):
            return str(faction_id)
        if i == 65535:
            return "(None)"
        if not self.fac:
            return str(i)
        flist = list_items(self.fac.get("FactionList"))
        if 0 <= i < len(flist):
            name = fld(flist[i], "FactionName", "") or ""
            return nwn_text(name) if name else str(i)
        return str(i)

    def encounter_trigger_audience(self, faction_id) -> str:
        """Which players an encounter with this faction actually spawns for.

        A NWN encounter only fires for creatures its own faction treats as
        enemies, so the encounter's FactionID — not any script — decides who
        sees it. Most encounters are faction Hostile, which is hostile to every
        player, so they fire for everyone. The module also tags encounters with
        its Good and Evil allegiance factions: those start hostile to everyone
        too, but taking that side at the Well of Eru orbs makes them friendly to
        you (faction_db.nss :: Faction_ApplyLive), so they stop spawning for
        their own side. Returns "" when the faction can't be resolved.
        """
        if faction_id is None or faction_id == "":
            return ""
        try:
            fid = int(faction_id)
        except (TypeError, ValueError):
            return ""
        # Reputation band: 0-10 hostile (an encounter fires), 11+ not an enemy.
        rep = self.faction_rep_toward_pc.get(fid)
        hostile_by_default = (rep is not None and rep <= 10) or fid == 1
        side = self.allegiance_sides.get(fid)
        if side and fid in self.allegiance_anchored and hostile_by_default:
            return f"Everyone except {side}-allegiance players"
        if hostile_by_default:
            return "Everyone"
        return f"No one ({self.faction_name(fid)} is not hostile to players)"

    def creature_instance_name(self, area: str, idx: int) -> str:
        """Display name for a creature INSTANCE (uses overridden FirstName/
        LastName on the placement, falling back to the blueprint's name)."""
        insts = self.area_creature_instances.get(area, [])
        if not (0 <= idx < len(insts)):
            return ""
        c = insts[idx]["c"]
        first = loc(c.get("FirstName"))
        last = loc(c.get("LastName"))
        full = (first + " " + last).strip()
        if full:
            return full
        rr = fld(c, "TemplateResRef", "") or ""
        return self.creature_name(rr) if rr else "(unnamed)"

    def canonical_creature_name(self, canonical_rr: str) -> str:
        """Display name for a canonical creature entry.
        Uses FirstName/LastName from the canonical struct (which may be a
        GIT instance override), falling back to the source blueprint's name.
        """
        entry = self.canonical_creatures.get(canonical_rr)
        if not entry:
            return canonical_rr
        c = entry["c"]
        first = loc(c.get("FirstName"))
        last = loc(c.get("LastName"))
        full = (first + " " + last).strip()
        if full:
            return full
        bp_rr = entry["bp_rr"]
        if bp_rr and bp_rr != canonical_rr and bp_rr in self.creatures:
            return self.creature_name(bp_rr)
        return self.creature_name(canonical_rr) or canonical_rr

    def dialog_label(self, resref: str) -> str:
        """A short human label for a dialog: the first line of its first
        Starting entry, truncated. Falls back to the resref."""
        dlg = self.dialogs.get(resref)
        if not dlg:
            return resref
        starts = list_items(dlg.get("StartingList"))
        entries = list_items(dlg.get("EntryList"))
        for s in starts:
            i = fld(s, "Index")
            if isinstance(i, int) and 0 <= i < len(entries):
                txt = nwn_text(loc(entries[i].get("Text")))
                txt = txt.strip().splitlines()[0] if txt else ""
                if txt:
                    return (txt[:60] + "…") if len(txt) > 63 else txt
        # Fall back to the first non-empty entry text.
        for e in entries:
            txt = nwn_text(loc(e.get("Text")))
            txt = txt.strip().splitlines()[0] if txt else ""
            if txt:
                return (txt[:60] + "…") if len(txt) > 63 else txt
        return resref
