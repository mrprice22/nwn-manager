"""Quest rendering for the wiki.

A browseable, structured view of the module journal: the Quests index
(table of contents, optionally grouped by builder ``@group`` directive)
plus a detail page per quest line.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from nwn_wiki.db.derived import _quest_hidden, _quest_slug
from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E, nwn_html
from nwn_wiki.htmlgen.links import (_area_link, _creature_link, _item_link,
                                    _script_link, link)
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.util import _try_int


# ---------------------------------------------------------------------------
# Quests — a browseable, structured view of the module journal: one overview
# index (table of contents) plus a detail page per quest line.
# ---------------------------------------------------------------------------

# NWN toolset journal "Priority" dropdown values.
_QUEST_PRIORITY_LABELS = {0: "Highest", 1: "High", 2: "Medium", 3: "Low", 4: "Lowest"}


def _quest_priority_label(prio: Any) -> str:
    try:
        p = int(prio)
    except (TypeError, ValueError):
        return ""
    return _QUEST_PRIORITY_LABELS.get(p, str(p))


def _quest_categories(db: Db) -> list[dict]:
    return list_items(db.jrl.get("Categories")) if db.jrl else []


def _quest_slugs(cats: list[dict]) -> list[str]:
    """Slug per category, in category order. Deterministic, so the index and
    the per-quest pages agree on URLs as long as both iterate in this order."""
    used: set[str] = set()
    return [_quest_slug(fld(c, "Tag", ""), loc(c.get("Name")), used) for c in cats]


def _quest_entry_id(e: dict) -> int:
    return _try_int(fld(e, "ID", 0))


# Builder-authored directives parsed from a quest category's Comment field —
# the only per-quest free text the toolset exposes (individual journal entries
# have no Comment field). All optional and module-agnostic:
#   @group 'Name'   place this quest under the "Name" heading on the index
#   @order N        sort position of this quest within its group (integer;
#                   lower = earlier; ties and ungrouped quests fall back to
#                   alphabetical order by name)
#   @group-order N  sort position of this quest's group among all groups
#                   (set on any quest in the group; first found wins)
#   @hidden         retired/inactive quest — omit it from the wiki entirely
#                   (@retired and @inactive are accepted synonyms)
# The group name may be wrapped in single quotes, double quotes, or left bare.
_RE_QUEST_GROUP = re.compile(
    r"@group\s+(?:'([^']*)'|\"([^\"]*)\"|([^\n]+))", re.IGNORECASE)
_RE_QUEST_ORDER = re.compile(r"@order\s+(\d+)", re.IGNORECASE)
_RE_QUEST_GROUP_ORDER = re.compile(r"@group-order\s+(\d+)", re.IGNORECASE)
# Whole directive lines, stripped before the Comment is shown to readers.
_RE_QUEST_DIRECTIVE = re.compile(
    r"^[ \t]*@(?:group-order|group|order|hidden|retired|inactive)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE)


def _quest_group(comment: str) -> str:
    """The @group name declared in a quest's Comment, or '' if none."""
    m = _RE_QUEST_GROUP.search(comment or "")
    if not m:
        return ""
    return (m.group(1) or m.group(2) or m.group(3) or "").strip()


def _quest_sort_order(comment: str) -> int | None:
    """Sort position of this quest within its group declared via @order N.
    Returns None when no @order is present (callers fall back to alphabetical)."""
    m = _RE_QUEST_ORDER.search(comment or "")
    return int(m.group(1)) if m else None


def _quest_group_order(comment: str) -> int | None:
    """Sort position of this quest's group declared via @group-order N.
    Returns None when not present (callers fall back to alphabetical)."""
    m = _RE_QUEST_GROUP_ORDER.search(comment or "")
    return int(m.group(1)) if m else None


def _quest_comment_display(comment: str) -> str:
    """Builder comment with the @group/@order directive lines removed, so the
    machine-readable markers don't surface in the human-facing note."""
    if not comment:
        return ""
    cleaned = _RE_QUEST_DIRECTIVE.sub("", comment)
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n", cleaned).strip()




def _quest_group_anchor(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"group-{slug}" if slug else "group"


def _quest_start_locations(db: Db, c: dict, action_to_dlgs,
                           script_to_module_event=None,
                           script_to_placeable_areas=None,
                           script_to_trigger_areas=None):
    """Best-effort guess at where a quest begins, for the index overview.

    ``action_to_dlgs`` maps a (lowercased) script resref to the dialogs that run
    it as an action node (the reverse of ``db.dialog_scripts``). We take the
    scripts that award the quest's *first known* journal step, find the
    conversations whose nodes run them, and the NPCs that own those
    conversations (``db.dialog_callers``). Returns ``(npcs, areas)``:

      npcs  - ``[(canonical_rr, name), ...]`` quest-giver creatures, deduped by
              blueprint resref. ``canonical_rr`` is "" when the blueprint has no
              creature page of its own.
      areas - ``[(area_rr, name), ...]`` areas those NPCs are placed in, deduped
              by area resref.

    Also detects quests granted by:
    - Module events (OnClientEnter etc.) → area = module entry area
    - Placeable events (OnUsed etc.) → area = placeable placement area(s)
    - Trigger events (OnEnter etc.) → area = trigger placement area(s)
    - Waypoint-spawned NPCs → area from canonical_locations fallback
    """
    tag = fld(c, "Tag", "")
    tag_lower = (tag or "").lower()
    grants = db.quest_grants.get(tag_lower, {})
    dlg_grants = db.quest_dialog_grants.get(tag_lower, {})
    entries = list_items(c.get("EntryList"))
    if not entries or (not grants and not dlg_grants):
        return [], []
    # Opening step = the first entry, in builder/progression order, that we
    # actually know a granting script or dialog-native grant for.
    ordered = sorted(entries, key=_quest_entry_id)
    scripts: set[str] = set()
    dlgs_direct: set[str] = set()
    for e in ordered:
        step = _quest_entry_id(e)
        scripts = grants.get(step) or set()
        dlgs_direct = dlg_grants.get(step) or set()
        if scripts or dlgs_direct:
            break
    if not scripts and not dlgs_direct:
        return [], []
    npcs = {}     # bp_rr -> (canonical_rr, name)
    areas = {}    # area_rr -> area name

    # Priority 1: non-dialog sources fill areas first so they appear
    # prominently (a shrine or trigger zone is more specific than an NPC area).

    # Module-event grants (OnClientEnter, OnModuleLoad, etc.): area = module entry.
    if script_to_module_event:
        entry_area = fld(db.ifo or {}, "Mod_Entry_Area", "") if db.ifo else ""
        for sref in scripts:
            if sref.lower() in script_to_module_event and entry_area:
                areas.setdefault(entry_area, db.area_name(entry_area))

    # Placeable-event grants: quest opened by interacting with a placeable.
    if script_to_placeable_areas:
        for sref in scripts:
            for ar in script_to_placeable_areas.get(sref.lower(), ()):
                areas.setdefault(ar, db.area_name(ar))

    # Trigger-event grants: quest opened by entering a trigger zone.
    if script_to_trigger_areas:
        for sref in scripts:
            for ar in script_to_trigger_areas.get(sref.lower(), ()):
                areas.setdefault(ar, db.area_name(ar))

    def _trace_dlg_callers(dlg_resref: str) -> None:
        """Resolve a dialog resref to NPC quest-givers and their areas."""
        for caller in db.dialog_callers.get(dlg_resref, ()):
            if caller.get("kind") not in ("creature", "creature-instance"):
                continue
            bp = (caller.get("resref") or "").lower()
            if bp:
                can = (bp if bp in db.canonical_creatures
                       else db.canonical_for_bp.get(bp, ""))
                name = (db.canonical_creature_name(can) if can
                        else (db.creature_name(bp) or caller.get("tag") or bp))
                npcs.setdefault(bp, (can, name))
            car = caller.get("areas") or (
                [caller["area"]] if caller.get("area") else [])
            # Waypoint-spawned NPCs have no placed area in dialog_callers;
            # fall back to canonical_locations (includes script-spawn areas).
            if not car and bp:
                can_for_bp = (bp if bp in db.canonical_creatures
                              else db.canonical_for_bp.get(bp, ""))
                if can_for_bp:
                    car = [loc["area"]
                           for loc in db.canonical_locations.get(can_for_bp, [])
                           if loc.get("area")]
            for ar in car:
                if ar:
                    areas.setdefault(ar, db.area_name(ar))

    # Priority 2: dialog-action tracing — scripts that run inside dialog action
    # nodes → find the owning dialog → resolve NPC callers.
    for sref in scripts:
        for dlg in action_to_dlgs.get(sref.lower(), ()):
            _trace_dlg_callers(dlg)

    # Priority 3: dialog-native quest grants (Quest/QuestEntry fields on dialog
    # nodes) — the dialog itself is already known, go straight to callers.
    for dlg in dlgs_direct:
        _trace_dlg_callers(dlg)

    return list(npcs.values()), list(areas.items())


def _quest_loc_cell(items, render):
    """Render an index "begins at" cell: up to three links via ``render(item)``,
    an em dash when empty, a muted +N when more than three are known."""
    if not items:
        return '<span class="muted">&mdash;</span>'
    cell = ", ".join(render(it) for it in items[:3])
    if len(items) > 3:
        cell += f' <span class="muted">+{len(items) - 3}</span>'
    return cell
def render_quests_index(db: Db, out: Path) -> None:
    """Quests landing page: an overview plus a table of contents of every
    journal quest, each row linking to that quest's detail page. Quests whose
    Comment marks them @hidden are omitted entirely. When any visible quest
    declares `@group 'Name'` in its builder Comment, quests are gathered under
    those headings; otherwise every quest sits in one table."""
    ctx = PageCtx("quests/index.html")
    cats = _quest_categories(db)
    visible = [i for i in range(len(cats))
               if not _quest_hidden(fld(cats[i], "Comment", ""))]
    if not visible:
        msg = "(no active quests)" if cats else "(no journal quests)"
        write_page(out, ctx, "Quests", f"<h1>Quests</h1><p>{msg}</p>")
        return

    slugs = _quest_slugs(cats)
    total_steps = sum(len(list_items(cats[i].get("EntryList"))) for i in visible)
    quest_comments = {i: fld(cats[i], "Comment", "") for i in visible}
    quest_groups = {i: _quest_group(quest_comments[i]) for i in visible}
    quest_orders = {i: _quest_sort_order(quest_comments[i]) for i in visible}
    has_groups = any(quest_groups.values())

    # script (lowercased) -> dialogs running it as an action node, i.e. the
    # reverse of db.dialog_scripts. Lets a quest's grant script be traced to
    # the NPC whose conversation awards it (where the quest is picked up).
    action_to_dlgs: dict[str, set[str]] = defaultdict(set)
    for _dlg_rr, _ents in db.dialog_scripts.items():
        for _e in _ents:
            if _e.get("kind") == "action":
                action_to_dlgs[(_e.get("resref") or "").lower()].add(_dlg_rr)

    # Module events: grant script → event label.
    _ql_module_event: dict[str, str] = {}
    if db.ifo:
        for _field, _label in db.MODULE_EVENT_FIELDS.items():
            _s = (fld(db.ifo, _field, "") or "").lower()
            if _s:
                _ql_module_event[_s] = _label

    # Placeable events: grant script → set of area resrefs.
    _ql_plc_bp_areas: dict[str, set[str]] = defaultdict(set)
    for _ar, _pls in db.area_placeables.items():
        for _pl in _pls:
            _rr = (fld(_pl, "TemplateResRef", "") or "").lower()
            if _rr:
                _ql_plc_bp_areas[_rr].add(_ar)
    _ql_plc_areas: dict[str, set[str]] = defaultdict(set)
    for _prr, _p in db.placeables.items():
        for _field in db.PLACEABLE_EVENT_FIELDS:
            _s = (fld(_p, _field, "") or "").lower()
            if _s:
                _ql_plc_areas[_s] |= _ql_plc_bp_areas.get(_prr, set())
    for _ar, _pls in db.area_placeables.items():
        for _pl in _pls:
            for _field in db.PLACEABLE_EVENT_FIELDS:
                _s = (fld(_pl, _field, "") or "").lower()
                if _s:
                    _ql_plc_areas[_s].add(_ar)

    # Trigger events: grant script → set of area resrefs.
    _ql_trg_bp_areas: dict[str, set[str]] = defaultdict(set)
    for _ar, _ts in db.area_triggers.items():
        for _t in _ts:
            _rr = (fld(_t, "TemplateResRef", "") or "").lower()
            if _rr:
                _ql_trg_bp_areas[_rr].add(_ar)
    _ql_trg_areas: dict[str, set[str]] = defaultdict(set)
    for _trr, _t in db.triggers.items():
        for _field in db.TRIGGER_EVENT_FIELDS:
            _s = (fld(_t, _field, "") or "").lower()
            if _s:
                _ql_trg_areas[_s] |= _ql_trg_bp_areas.get(_trr, set())
    for _ar, _ts in db.area_triggers.items():
        for _t in _ts:
            for _field in db.TRIGGER_EVENT_FIELDS:
                _s = (fld(_t, _field, "") or "").lower()
                if _s:
                    _ql_trg_areas[_s].add(_ar)

    def row(i: int) -> str:
        c = cats[i]
        tag = fld(c, "Tag", "")
        qname = loc(c.get("Name")) or tag or "(unnamed quest)"
        entries = list_items(c.get("EntryList"))
        has_end = any(fld(e, "End", 0) for e in entries)
        prio = _quest_priority_label(fld(c, "Priority"))
        xp = fld(c, "XP", 0) or 0
        grants = db.quest_grants.get((tag or "").lower(), {})
        n_grants = sum(1 for e in entries if grants.get(_quest_entry_id(e)))
        npcs, areas = _quest_start_locations(
            db, c, action_to_dlgs,
            script_to_module_event=_ql_module_event,
            script_to_placeable_areas=_ql_plc_areas,
            script_to_trigger_areas=_ql_trg_areas,
        )
        npc_cell = _quest_loc_cell(
            npcs,
            lambda it: (_creature_link(db, it[0], ctx, it[1])
                        if it[0] else nwn_html(it[1])))
        area_cell = _quest_loc_cell(
            areas,
            lambda it: _area_link(db, it[0], ctx, it[1]))
        return (
            f"<tr><td>{link(f'{slugs[i]}.html', qname)}</td>"
            f"<td><code>{E(tag)}</code></td>"
            f"<td>{npc_cell}</td>"
            f"<td>{area_cell}</td>"
            f"<td>{E(prio)}</td>"
            f"<td>{E(xp) if xp else ''}</td>"
            f"<td>{len(entries)}</td>"
            f"<td>{'&#10003;' if has_end else ''}</td>"
            f"<td>{n_grants if n_grants else ''}</td></tr>"
        )

    def table(indices: list[int]) -> str:
        def _sort_key(i: int):
            name = (loc(cats[i].get("Name")) or fld(cats[i], "Tag", "")).lower()
            order = quest_orders[i]
            return (order if order is not None else float("inf"), name)
        ordered = sorted(indices, key=_sort_key)
        return (
            '<table class="data"><thead><tr>'
            "<th>Quest</th><th>Tag</th>"
            "<th>Begins (NPC)</th><th>Begins (area)</th>"
            "<th>Priority</th><th>XP</th>"
            "<th>Steps</th><th>Final step</th><th>Awarded</th>"
            "</tr></thead><tbody>"
            + "\n".join(row(i) for i in ordered)
            + "</tbody></table>"
        )

    parts = [
        "<h1>Quests</h1>",
        f"<p>{len(visible)} quests &middot; {total_steps} journal entries. "
        '<small class="muted">Quests are the module&rsquo;s journal categories; '
        "each row links to that quest&rsquo;s entries. &ldquo;Begins&rdquo; is a "
        "best-effort hint at the NPC and area that grant the opening step, traced "
        "from the scripts that award it. &ldquo;Final step&rdquo; marks "
        "a quest with a completion entry. &ldquo;Awarded&rdquo; counts the entries a "
        "script is known to grant via <code>AddJournalQuestEntry</code>."
        + (" Quests are grouped by the <code>@group</code> label set in their "
           "builder Comment." if has_groups else "")
        + "</small></p>",
    ]

    if not has_groups:
        # No quest declares a group — a single flat table, no section headings.
        parts.append(table(visible))
    else:
        grouped: dict[str, list[int]] = defaultdict(list)
        for i in visible:
            grouped[quest_groups[i]].append(i)  # "" == no @group declared
        # Determine group sort order: @group-order N from the first quest in
        # the group that declares it, then alphabetical for the rest.
        group_sort_order: dict[str, int | None] = {}
        for g, idxs in grouped.items():
            order = None
            for i in idxs:
                order = _quest_group_order(quest_comments[i])
                if order is not None:
                    break
            group_sort_order[g] = order
        def _group_key(g: str):
            o = group_sort_order[g]
            return (o if o is not None else float("inf"), g.lower())
        for g in sorted((g for g in grouped if g), key=_group_key):
            parts.append(
                f'<h2 id="{E(_quest_group_anchor(g))}">{nwn_html(g)} '
                f'<small class="muted">({len(grouped[g])})</small></h2>')
            parts.append(table(grouped[g]))
        if grouped.get(""):
            parts.append(
                f'<h2 id="{E(_quest_group_anchor("Other"))}">Other '
                f'<small class="muted">({len(grouped[""])})</small></h2>')
            parts.append(table(grouped[""]))

    write_page(out, ctx, "Quests", "".join(parts))


def render_quest_page(db: Db, c: dict, slug: str, out: Path) -> None:
    """Detail page for a single quest line: metadata plus its journal entries
    in progression order, cross-referenced to the scripts that award each."""
    ctx = PageCtx(f"quests/{slug}.html")
    tag = fld(c, "Tag", "")
    qname = loc(c.get("Name")) or tag or "(unnamed quest)"
    prio = _quest_priority_label(fld(c, "Priority"))
    xp = fld(c, "XP", 0) or 0
    raw_comment = fld(c, "Comment", "")
    group = _quest_group(raw_comment)
    comment = _quest_comment_display(raw_comment)

    parts = [
        '<p><a href="index.html">&larr; All quests</a></p>',
        f"<h1>{nwn_html(qname)}</h1>",
    ]
    meta = []
    if group:
        meta.append('Group: <a href="index.html#'
                    f'{E(_quest_group_anchor(group))}">{nwn_html(group)}</a>')
    if tag:
        meta.append(f"Tag: <code>{E(tag)}</code>")
    if prio:
        meta.append(f"Priority: {E(prio)}")
    if xp:
        meta.append(f"XP: {E(xp)}")
    if meta:
        parts.append(f"<p>{' &middot; '.join(meta)}</p>")
    if comment:
        parts.append('<p class="muted"><strong>Builder comment:</strong> '
                     f"{nwn_html(comment)}</p>")

    entries = list_items(c.get("EntryList"))
    grants = db.quest_grants.get((tag or "").lower(), {})
    dlg_grants = db.quest_dialog_grants.get((tag or "").lower(), {})

    # Build a reverse tag→resref map for items referenced in granting scripts
    tag_to_item_rr: dict[str, str] = {}
    for rr, it in db.items.items():
        t = (fld(it, "Tag", "") or "").strip().lower()
        if t:
            tag_to_item_rr.setdefault(t, rr)

    def _items_html(item_set: set[str]) -> str:
        if not item_set:
            return '<span class="muted">&mdash;</span>'
        return ", ".join(
            _item_link(db, irr, ctx) if irr in db.items else E(db.item_name(irr))
            for irr in sorted(item_set, key=lambda r: db.item_name(r).lower())
        )

    def _collect_entry_items(granters: list[str], granting_dlgs: list[str]
                             ) -> tuple[set[str], set[str], set[str]]:
        """Return (required, consumed, granted) item resref sets for a quest entry.
        required  — checked in condition (Active) scripts of granting dialogs
        consumed  — checked in action scripts of granting dialogs or granting scripts
        granted   — created by granting action scripts or granting scripts
        """
        required: set[str] = set()
        consumed: set[str] = set()
        granted: set[str] = set()

        for dlg_rr in granting_dlgs:
            for ds in db.dialog_scripts.get(dlg_rr, []):
                s_rr = ds["resref"]
                s_kind = ds["kind"]
                if s_kind == "active":
                    for itag in db.script_checks_item_tags.get(s_rr, set()):
                        irr = tag_to_item_rr.get(itag.lower())
                        if irr:
                            required.add(irr)
                elif s_kind == "action":
                    for itag in db.script_checks_item_tags.get(s_rr, set()):
                        irr = tag_to_item_rr.get(itag.lower())
                        if irr:
                            consumed.add(irr)
                    for irr in db.script_creates_items.get(s_rr, []):
                        if irr in db.items:
                            granted.add(irr)

        for script_rr in granters:
            for itag in db.script_checks_item_tags.get(script_rr, set()):
                irr = tag_to_item_rr.get(itag.lower())
                if irr:
                    consumed.add(irr)
            for irr in db.script_creates_items.get(script_rr, []):
                if irr in db.items:
                    granted.add(irr)

        # Items that are both required and consumed are consumed (they get taken)
        required -= consumed
        return required, consumed, granted

    if not entries:
        parts.append("<p>(no journal entries)</p>")
    else:
        rows = []
        any_req = any_cons = any_granted = False
        for e in sorted(entries, key=_quest_entry_id):
            eid = _quest_entry_id(e)
            end = fld(e, "End", 0)
            txt = loc(e.get("Text"))
            granters = sorted(grants.get(eid, set()))
            granter_parts = [_script_link(db, s, ctx) for s in granters]
            granting_dlgs = sorted(dlg_grants.get(eid, set()))
            for dlg_rr in granting_dlgs:
                dlg_label = db.dialog_label(dlg_rr)
                granter_parts.append(
                    link(f"../conversations/{dlg_rr}.html",
                         f"dialog: {dlg_label}"))
            grant_html = (", ".join(granter_parts)
                          if granter_parts else '<span class="muted">&mdash;</span>')
            req, cons, granted = _collect_entry_items(granters, granting_dlgs)
            if req:
                any_req = True
            if cons:
                any_cons = True
            if granted:
                any_granted = True
            end_badge = '<span class="badge">final</span>' if end else ""
            rows.append((eid, end_badge, txt, grant_html, req, cons, granted))

        # Only show item columns that have data in at least one row
        header = ("<th>ID</th><th></th><th>Journal text</th><th>Awarded by</th>"
                  + ("<th>Items required</th>" if any_req else "")
                  + ("<th>Items consumed</th>" if any_cons else "")
                  + ("<th>Items granted</th>" if any_granted else ""))
        table_rows = []
        for eid, end_badge, txt, grant_html, req, cons, granted in rows:
            r = (f"<tr><td>{eid}</td><td>{end_badge}</td>"
                 f"<td>{nwn_html(txt)}</td><td>{grant_html}</td>"
                 + (f"<td>{_items_html(req)}</td>" if any_req else "")
                 + (f"<td>{_items_html(cons)}</td>" if any_cons else "")
                 + (f"<td>{_items_html(granted)}</td>" if any_granted else "")
                 + "</tr>")
            table_rows.append(r)
        parts.append(
            '<table class="data"><thead><tr>'
            + header +
            "</tr></thead><tbody>" + "\n".join(table_rows) + "</tbody></table>"
        )

    write_page(out, ctx, f"Quest: {qname}", "\n".join(parts))
