"""Creature index rendering for the wiki.

The all-creatures index, the boss registry and its Bosses page, the
creature-pictures gallery, and the by-area / by-CR / by-race creature views.
"""

from __future__ import annotations

import re
import urllib.parse
from collections import defaultdict
from pathlib import Path

from nwn_wiki.combat import epic_toughness_hp
from nwn_wiki.db.scripts import _strip_nss_comments
from nwn_wiki.gff import fld, list_items
from nwn_wiki.htmlgen.blocks import items_layout, toc_sidebar
from nwn_wiki.htmlgen.chrome import has_creature_pics_page, write_page
from nwn_wiki.htmlgen.escape import E, nwn_html, nwn_text
from nwn_wiki.htmlgen.links import _area_link, link
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.lookups import class_name, race_name
from nwn_wiki.respawn import (boss_reset_seconds, boss_respawn_seconds,
                               format_respawn)
from nwn_wiki.util import _try_float, _try_int
from nwn_wiki.warn import _warn_once

from nwn_wiki import state


def creature_feat_ids(c: dict | None, bp: dict | None = None) -> list[int]:
    """Feat ids from a creature's FeatList (instance first, blueprint fallback)."""
    feats = list_items((c or {}).get("FeatList")) or (
        list_items(bp.get("FeatList")) if bp else [])
    return [int(fld(f, "Feat")) for f in feats if fld(f, "Feat") is not None]


def creature_max_hp(c: dict | None, bp: dict | None = None) -> int | None:
    """Effective max HP the way the engine reports it: stored MaxHitPoints plus
    Epic Toughness HP applied on spawn (see epic_toughness_hp). Returns None when
    no HP field is set so callers can render an empty cell."""
    raw = fld(c, "MaxHitPoints") or fld(c, "HitPoints")
    if raw in (None, "") and bp is not None:
        raw = fld(bp, "MaxHitPoints") or fld(bp, "HitPoints")
    try:
        base = int(raw)
    except (TypeError, ValueError):
        return None
    return base + epic_toughness_hp(creature_feat_ids(c, bp))


def _bp_field(c: dict | None, bp: dict | None):
    """Field getter for a creature: instance value first, blueprint fallback.

    Every creature table needs the same instance-then-blueprint lookup; the
    returned closure captures the pair so call sites read as ``_f('Race')``."""

    def _f(key, default=None):
        v = fld(c, key)
        if v is None and bp is not None:
            v = fld(bp, key)
        return default if v is None else v

    return _f


def _class_str(c: dict | None, bp: dict | None) -> str:
    """"Class Lvl/Class Lvl" summary of a creature's ClassList (bp fallback)."""
    classes = list_items((c or {}).get("ClassList")) or (
        list_items(bp.get("ClassList")) if bp else [])
    return "/".join(
        f"{class_name(fld(cl, 'Class'))} {fld(cl, 'ClassLevel', '')}"
        for cl in classes
    )


def _kills_head() -> str:
    """<th>s for the bestiary kill columns; empty when no bestiary is loaded."""
    return ("<th>Kills</th><th>Solo</th><th>Party</th>"
            if state._BESTIARY_ACTIVE else "")


def _kills_cells(resrefs: list[str]) -> str:
    """Kill <td>s matching _kills_head(), summed over ``resrefs`` (a boss counts
    its variant blueprints as one). Kept beside the header so the two can't drift."""
    if not state._BESTIARY_ACTIVE:
        return ""
    ks = [state._BESTIARY_KILLS[r] for r in resrefs if r in state._BESTIARY_KILLS]
    if not ks:
        return "<td>—</td><td>—</td><td>—</td>"
    return (f"<td>{sum(k['total'] for k in ks)}</td>"
            f"<td>{sum(k['solo'] for k in ks)}</td>"
            f"<td>{sum(k['party'] for k in ks)}</td>")


def _faction_cell(db: Db, c: dict | None, bp: dict | None) -> str:
    """<td> holding a creature's faction name; empty for a creature-less row."""
    if c is None:
        return "<td></td>"
    return f"<td>{E(db.faction_name(_bp_field(c, bp)('FactionID', '')))}</td>"


def _creature_row(name_cell: str, c: dict | None, bp: dict | None, *,
                  after_name: str = "", show_race: bool = True,
                  cr_default="", tail: str = "") -> str:
    """One <tr> of the shape every creature table shares.

    Columns: Name, ``after_name`` cells, [Race], Class, HP, CR, ``tail`` cells.
    ``c``/``bp`` are the canonical instance and its blueprint; pass ``c=None``
    for a row with no creature behind it (a boss-registry resref with no page),
    which blanks race/class/HP and falls back to ``cr_default``."""
    if c is None:
        race, cls_str, hp, cr = "", "", None, cr_default
    else:
        _f = _bp_field(c, bp)
        race = race_name(_f("Race"))
        cls_str = _class_str(c, bp)
        hp = creature_max_hp(c, bp)
        cr = _f("ChallengeRating", cr_default)
    return (
        f"<tr><td>{name_cell}</td>"
        f"{after_name}"
        f"{f'<td>{E(race)}</td>' if show_race else ''}"
        f"<td>{E(cls_str)}</td>"
        f"<td>{E(hp if hp is not None else '')}</td>"
        f"<td>{E(cr)}</td>"
        f"{tail}</tr>"
    )


# The four creature index pages each open their TOC with a "Views" block
# linking to the other three plus the search page.
_CREATURE_VIEWS = [
    ("index.html", "All Creatures"),
    ("by-area/index.html", "By Area"),
    ("by-cr/index.html", "By Challenge Rating"),
    ("by-race/index.html", "By Race"),
]


def _views_toc(current: str, prefix: str) -> list[str]:
    """The 'Views' sibling nav shared by the four creature index pages.

    ``current`` is the page's own href in :data:`_CREATURE_VIEWS` (omitted from
    the list); ``prefix`` is the relative path back up to ``creatures/``.
    """
    parts = ['<div class="toc-group-heading">Views</div>']
    parts += [f'<div><a href="{prefix}{href}">{label}</a></div>'
              for href, label in _CREATURE_VIEWS if href != current]
    parts.append(f'<div><a href="{prefix}search.html">Search</a></div>')
    return parts


def render_creatures_index(db: Db, out: Path) -> None:
    rows_present = []
    rows_absent = []
    for can_rr in sorted(db.canonical_creatures.keys(),
                         key=lambda r: nwn_text(db.canonical_creature_name(r)).lower()):
        entry = db.canonical_creatures[can_rr]
        c = entry["c"]
        bp_rr = entry["bp_rr"]
        bp = db.creatures.get(bp_rr) if bp_rr != can_rr else None
        is_variant = db.canonical_bp_of.get(can_rr) != can_rr
        if is_variant:
            base_rr = db.canonical_bp_of[can_rr]
            variant_note = (
                f' <small class="muted">(variant of '
                f'{link(f"{base_rr}.html", db.canonical_creature_name(base_rr))})</small>'
            )
        else:
            variant_note = ""
        total_count = sum(l["count"] for l in db.canonical_locations.get(can_rr, []))
        row = _creature_row(
            f"{link(f'{can_rr}.html', db.canonical_creature_name(can_rr))}{variant_note}",
            c, bp,
            after_name=f"<td>{total_count if total_count else '—'}</td>",
            tail=_faction_cell(db, c, bp) + _kills_cells([can_rr]),
        )
        if total_count:
            rows_present.append(row)
        else:
            rows_absent.append(row)
    n_unique = len(db.canonical_creatures)
    n_variants = sum(1 for r in db.canonical_creatures
                     if db.canonical_bp_of.get(r) != r)
    variant_note_str = (
        f" ({n_variants} equipment/class/name variant{'s' if n_variants != 1 else ''})"
        if n_variants else ""
    )
    TABLE_HEAD = (
        '<table class="data"><thead><tr>'
        "<th>Name</th><th>Count</th><th>Race</th><th>Class</th>"
        "<th>HP</th><th>CR</th><th>Faction</th>"
        f"{_kills_head()}"
        "</tr></thead><tbody>"
    )
    toc_parts = [
        *_views_toc("index.html", ""),
        '<div class="toc-group-heading">Sections</div>',
    ]
    if rows_present:
        toc_parts.append(
            f'<div><a href="#present">In Module'
            f' <span class="muted">({len(rows_present)})</span></a></div>'
        )
    if rows_absent:
        toc_parts.append(
            f'<div><a href="#absent">Blueprint Only'
            f' <span class="muted">({len(rows_absent)})</span></a></div>'
        )
    sidebar = toc_sidebar(toc_parts)
    sections: list[str] = [
        "<h1>Creatures</h1>",
        f"<p>{n_unique} unique creature{'s' if n_unique != 1 else ''}{variant_note_str}.</p>",
    ]
    if rows_present:
        sections.append(
            f'<h2 id="present">In Module'
            f' <small class="muted">({len(rows_present)})</small></h2>'
            + TABLE_HEAD + "\n".join(rows_present) + "</tbody></table>"
        )
    if rows_absent:
        sections.append(
            f'<h2 id="absent">Blueprint Only'
            f' <small class="muted">({len(rows_absent)})</small></h2>'
            + TABLE_HEAD + "\n".join(rows_absent) + "</tbody></table>"
        )
    body = items_layout(sidebar, "\n".join(sections))
    write_page(out, PageCtx("creatures/index.html"), "Creatures",
               body)


# --- Boss respawn tracker ("Roll of the Fallen") ---------------------------
# The module's brd_db.nss seeds a curated boss registry into the respawndb
# campaign DB on every module load; the same seed rows are the wiki's boss
# list. Regexes mirror tests/check_boss_registry.py in the module repo.

_BOSS_SEED_RE = re.compile(
    r'BRD_SeedBoss\(\s*"(?P<resref>[^"]*)"\s*,\s*"(?P<name>[^"]*)"\s*,'
    r'\s*"(?P<tag>[^"]*)"\s*,\s*"(?P<area>[^"]*)"\s*,\s*"(?P<area_name>[^"]*)"\s*,'
    r'\s*(?P<cr>[0-9.]+)f?\s*,\s*(?P<respawn>\d+)\s*,\s*"(?P<type>[^"]*)"\s*\)'
)
_BOSS_ALIAS_RE = re.compile(
    r'BRD_SeedAlias\(\s*"(?P<resref>[^"]*)"\s*,\s*"(?P<canonical>[^"]*)"\s*\)'
)


def _load_boss_registry_from_path(p: Path | None) -> None:
    """Populate state._BOSS_REGISTRY/state._BOSS_ALIASES from a brd_db.nss path.
    No-op (empty registry, no Bosses page/nav link) when the path is missing —
    i.e. the module doesn't use the boss respawn tracker."""
    state._BOSS_REGISTRY = []
    state._BOSS_ALIASES = {}
    if not p or not p.is_file():
        return
    text = _strip_nss_comments(p.read_text())
    for m in _BOSS_SEED_RE.finditer(text):
        d = m.groupdict()
        d["cr"] = float(d["cr"])
        state._BOSS_REGISTRY.append(d)
    for m in _BOSS_ALIAS_RE.finditer(text):
        state._BOSS_ALIASES[m.group("resref")] = m.group("canonical")
    if state._BOSS_REGISTRY:
        print(f"[nwn-wiki] boss registry: {len(state._BOSS_REGISTRY)} bosses"
              f" ({len(state._BOSS_ALIASES)} variant aliases) from brd_db.nss")


def load_boss_registry(db: Db) -> None:
    """Populate state._BOSS_REGISTRY/state._BOSS_ALIASES from the module's brd_db.nss."""
    _load_boss_registry_from_path(db.script_paths.get("brd_db"))


def load_boss_registry_from_src(src: Path) -> None:
    """Populate state._BOSS_REGISTRY/state._BOSS_ALIASES from <src>/brd_db.nss.

    For callers (e.g. nwn-wiki-activity) that render pages without building a
    full Db. Mirrors how Db.load maps the 'brd_db' resref to <src>/brd_db.nss."""
    _load_boss_registry_from_path(Path(src) / "brd_db.nss")


def _boss_timing_note(db: Db) -> str:
    """The "every boss respawns in N" paragraph that replaces the per-row
    Respawn column — or "" when the module cannot make that promise.

    Returned empty (and the column kept) unless the module has a boss_tune.nss
    naming BOSS_RESPAWN_SECONDS *and* every seeded registry row agrees with it.
    The archive forks have neither; a module mid-retune has the constant but
    stale rows, and would otherwise advertise a number its board contradicts.
    """
    want = boss_respawn_seconds(db)
    if not want:
        return ""
    seeded = {_try_int(b.get("respawn")) for b in state._BOSS_REGISTRY}
    if seeded != {want}:
        return ""

    reset = boss_reset_seconds(db)
    reset_txt = ""
    if reset:
        reset_txt = (
            " Damage a boss and then walk away and it does not simply stay "
            f"wounded: after <strong>{E(format_respawn(reset))}</strong> with "
            "nobody fighting it, it fully resets — every "
            '<a href="../manual/Customizations.html#boss-enrage">enrage</a> '
            "stack stripped, back to full health, back at its lair, and "
            "anything taken from it restored. Landing a hit restarts that "
            "clock."
        )
    return (
        f"<p><strong>Every boss on this list respawns "
        f"{E(format_respawn(want))} after it is killed</strong> — the same "
        "timer for all of them, shown as a live countdown on the in-game "
        "board. (A boss whose lair is an encounter becomes eligible then and "
        f"returns when the lair next triggers.){reset_txt}</p>"
    )


def render_bosses_index(db: Db, out: Path) -> None:
    """creatures/bosses.html — every boss tracked by the in-game Roll of the
    Fallen board, same columns as the creatures index, sorted by CR desc.

    Respawn is stated once above the table rather than per row: the module puts
    every boss on one timer (boss_tune.nss). See _boss_timing_note."""
    if not state._BOSS_REGISTRY:
        return
    ctx = PageCtx("creatures/bosses.html")
    timing_note = _boss_timing_note(db)
    per_boss_respawn = not timing_note   # one shared timer -> no Respawn column
    rows = []
    for boss in sorted(state._BOSS_REGISTRY, key=lambda b: (-b["cr"], b["name"].lower())):
        rr = boss["resref"]
        entry = db.canonical_creatures.get(rr)
        if entry:
            name_cell = link(f"{rr}.html", db.canonical_creature_name(rr))
            c = entry["c"]
            bp_rr = entry["bp_rr"]
            bp = db.creatures.get(bp_rr) if bp_rr != rr else None
        else:
            # Registry row without a matching creature page — registry/module
            # drift; render unlinked and surface it in lookup_warnings.json.
            _warn_once(f"boss registry resref '{rr}' has no creature page "
                       f"(check BRD_SeedBoss rows in brd_db.nss)")
            name_cell = E(boss["name"])
            c = bp = None
        area_rr = boss["area"]
        if area_rr in db.areas and area_rr not in db.hidden_areas:
            lair_cell = _area_link(db, area_rr, ctx, boss["area_name"])
        else:
            lair_cell = E(boss["area_name"])
        # Sum kills across the boss's variant blueprints (e.g. the five
        # leveled Xanith .utcs all count as one boss on the board).
        variant_rrs = [rr] + [v for v, canon in state._BOSS_ALIASES.items()
                              if canon == rr]
        # No per-boss Respawn column: every tracked boss shares one timer, so
        # it is stated once above the table (see _boss_timing_note). A module
        # whose bosses DON'T all share a timer keeps the column.
        tail = _faction_cell(db, c, bp)
        if per_boss_respawn:
            respawn_txt = format_respawn(_try_int(boss.get("respawn")))
            # spawn_type decides what the number means: a placed boss returns
            # exactly this long after death, an encounter boss only becomes
            # eligible then and returns when its lair encounter next triggers.
            if boss.get("type") == "encounter" and respawn_txt != "—":
                respawn_txt += " (then on next trigger)"
            tail += f"<td>{E(respawn_txt)}</td>"
        rows.append(_creature_row(
            name_cell, c, bp,
            after_name=f"<td>{lair_cell}</td>",
            cr_default=boss["cr"],
            tail=tail + _kills_cells(variant_rrs),
        ))
    n = len(state._BOSS_REGISTRY)
    body = (
        "<h1>Bosses</h1>"
        f"<p>The {n} great powers of Middle-earth tracked by the "
        '<a href="../manual/Customizations.html#boss-respawn-tracker">Roll of the '
        "Fallen</a> board in the Well of Eru. This list is generated from the same "
        "registry the game loads, so it always matches the in-game board.</p>"
        + timing_note +
        "<p>Think a boss is missing and should be tracked? Suggest it on Discord "
        "or via the roadmap so it can be considered for the list.</p>"
        '<table class="data"><thead><tr>'
        "<th>Name</th><th>Lair</th><th>Race</th><th>Class</th>"
        "<th>HP</th><th>CR</th><th>Faction</th>"
        + ("<th>Respawn</th>" if per_boss_respawn else "")
        + f"{_kills_head()}"
        "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
    )
    write_page(out, ctx, "Bosses", body)


def _pic_src(filename: str, prefix: str) -> str:
    """URL for a creature-pics image; filenames may contain spaces/apostrophes.

    ``prefix`` is the way to ``creatures/pics/`` from the page holding the
    image, and already ends in "/" (see PageCtx.dir_url)."""
    return f"{prefix}{urllib.parse.quote(filename)}"


def _pic_figures(images: list[str], alt: str, ctx: PageCtx) -> str:
    """Render a list of image filenames as full-width <figure><img> blocks.

    ``ctx`` is the page the figures are going into; the image URLs are resolved
    against it, so the same block works from any depth."""
    prefix = ctx.dir_url("creatures/pics")
    return "".join(
        f'<figure><img class="creature-pic" src="{E(_pic_src(fn, prefix))}" '
        f'alt="{E(alt)}" loading="lazy"></figure>'
        for fn in images
    )


def render_creature_pictures(db: Db, out: Path) -> None:
    """Gallery page grouping creature artwork (from creature-pics/) by NPC,
    sorted by descending CR, with a floating left-hand name navigation.

    The early return reads has_creature_pics_page() -- the same predicate
    SiteChrome gates the Pictures nav entry on -- so the nav links to this page
    exactly when it is written."""
    if not has_creature_pics_page():
        return
    ctx = PageCtx("creatures/pictures.html")

    def _slug(name: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return s or "pic"

    # Disambiguate any colliding slugs.
    used: dict[str, int] = {}
    for grp in state._CREATURE_PIC_GROUPS:
        base = _slug(grp["name"])
        n = used.get(base, 0)
        used[base] = n + 1
        grp["_slug"] = base if n == 0 else f"{base}-{n + 1}"

    toc_parts = [
        '<div><a href="index.html">All Creatures</a></div>',
        '<div class="toc-group-heading">Pictures</div>',
    ]
    for grp in state._CREATURE_PIC_GROUPS:
        toc_parts.append(f'<div><a href="#{E(grp["_slug"])}">{nwn_html(grp["name"])}</a></div>')
    sidebar = toc_sidebar(toc_parts, wide=True)

    sections = [
        "<h1>Creature Pictures</h1>",
        f"<p>{len(state._CREATURE_PIC_GROUPS)} creature"
        f"{'s' if len(state._CREATURE_PIC_GROUPS) != 1 else ''} with artwork, "
        f"sorted by descending Challenge Rating.</p>",
    ]
    for grp in state._CREATURE_PIC_GROUPS:
        name = grp["name"]
        sections.append(f'<section id="{E(grp["_slug"])}">')
        sections.append(f"<h2>{nwn_html(name)}</h2>")
        if grp["matches"]:
            links = ", ".join(
                link(f"{rr}.html", db.canonical_creature_name(rr))
                for rr in sorted(grp["matches"],
                                 key=lambda r: db.canonical_creature_name(r).lower())
            )
            sections.append(f'<p class="muted">Appears as: {links}</p>')
        else:
            sections.append('<p class="muted">No matching creature in the module.</p>')
        sections.append('<div class="creature-pics">'
                        + _pic_figures(grp["images"], name, ctx) + "</div>")
        sections.append("</section>")

    body = items_layout(sidebar, "\n".join(sections))
    write_page(out, ctx, "Creature Pictures",
               body)


def render_creatures_by_area(db: Db, out: Path) -> None:
    """Creatures grouped by area. One row per unique creature per area, with
    spawn-method badges and a count of total appearances."""
    ctx = PageCtx("creatures/by-area/index.html")
    # Build area → {can_rr → {"placed": N, "enc": N, "script": N, "enc_rrs": set}}
    area_map: dict[str, dict[str, dict]] = defaultdict(dict)
    for can_rr, locs in db.canonical_locations.items():
        for loc_entry in locs:
            area_rr = loc_entry["area"]
            if area_rr not in area_map[can_rr]:
                area_map[can_rr][area_rr] = {"placed": 0, "enc": 0, "script": 0, "enc_rrs": set()}
            if loc_entry["kind"] == "placed":
                area_map[can_rr][area_rr]["placed"] += loc_entry["count"]
            elif loc_entry["kind"] == "script":
                area_map[can_rr][area_rr]["script"] += loc_entry["count"]
            else:
                area_map[can_rr][area_rr]["enc"] += loc_entry["count"]
                if loc_entry["enc_rr"]:
                    area_map[can_rr][area_rr]["enc_rrs"].add(loc_entry["enc_rr"])

    # Invert to area → [can_rr, ...]
    by_area: dict[str, list[str]] = defaultdict(list)
    for can_rr, areas in area_map.items():
        for area_rr in areas:
            by_area[area_rr].append(can_rr)

    visible_areas = [a for a in by_area if a not in db.hidden_areas]

    # Build sidebar TOC
    toc_parts = [
        *_views_toc("by-area/index.html", "../"),
        '<div class="toc-group-heading">Areas</div>',
    ]
    for area_rr in sorted(visible_areas, key=lambda r: db.area_name(r).lower()):
        cnt = len(by_area[area_rr])
        slug = f"area-{area_rr}"
        toc_parts.append(
            f'<div><a href="#{E(slug)}">{E(db.area_name(area_rr))}'
            f' <span class="muted">({cnt})</span></a></div>'
        )
    sidebar = toc_sidebar(toc_parts)

    sections: list[str] = [
        "<h1>Creatures by Area</h1>",
        f"<p>{len(visible_areas)} areas with creatures. "
        f'See <a href="../index.html">All Creatures</a> for a flat list.</p>',
    ]
    for area_rr in sorted(visible_areas, key=lambda r: db.area_name(r).lower()):
        can_rrs = by_area[area_rr]
        slug = f"area-{area_rr}"
        sections.append(
            f'<h2 id="{E(slug)}">'
            f'{_area_link(db, area_rr, ctx)} '
            f'<small class="muted">({len(can_rrs)})</small></h2>'
        )
        rows = []
        for can_rr in sorted(can_rrs,
                              key=lambda r: nwn_text(db.canonical_creature_name(r)).lower()):
            entry = db.canonical_creatures[can_rr]
            c2 = entry["c"]
            bp2_rr = entry["bp_rr"]
            bp2 = db.creatures.get(bp2_rr) if bp2_rr != can_rr else None
            info = area_map[can_rr][area_rr]
            placed = info["placed"]
            enc = info["enc"]
            script = info["script"]
            badges = []
            if placed:
                badges.append('<span class="badge">placed</span>')
            if enc:
                badges.append('<span class="badge">encounter</span>')
            if script:
                badges.append('<span class="badge">script</span>')
            method = " + ".join(badges) if badges else "—"
            count = placed + enc + script
            rows.append(_creature_row(
                link(f"../{can_rr}.html", db.canonical_creature_name(can_rr)),
                c2, bp2,
                tail=f"<td>{method}</td><td>{count}</td>",
            ))
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Name</th><th>Race</th><th>Class</th>"
            "<th>HP</th><th>CR</th><th>Spawn Method</th><th>Count</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    body = items_layout(sidebar, "\n".join(sections))
    write_page(out, ctx, "Creatures by Area",
               body)


def render_creatures_by_cr(db: Db, out: Path, *, cr_bucket_size: int = 10) -> None:
    """Creatures grouped by Challenge Rating range, with a left-sidebar TOC.

    Each bucket spans at least cr_bucket_size CR levels AND contains at least
    cr_bucket_size creatures; adjacent natural buckets are merged until both
    conditions are met.
    """

    def _cr_val(can_rr: str) -> float | None:
        entry = db.canonical_creatures[can_rr]
        c2 = entry["c"]
        bp2_rr = entry["bp_rr"]
        bp2 = db.creatures.get(bp2_rr) if bp2_rr != can_rr else None
        raw = _bp_field(c2, bp2)("ChallengeRating")
        if raw is None:
            return None
        return _try_float(raw, None)

    # Separate known-CR from unknown-CR canonicals
    known: list[tuple[float, str]] = []
    unknown: list[str] = []
    for can_rr in db.canonical_creatures:
        cr = _cr_val(can_rr)
        if cr is None:
            unknown.append(can_rr)
        else:
            known.append((cr, can_rr))
    known.sort(key=lambda x: x[0])

    # Build natural fixed-width buckets
    natural: dict[int, list[str]] = {}
    for cr, can_rr in known:
        b = int(cr // cr_bucket_size) * cr_bucket_size
        natural.setdefault(b, []).append(can_rr)

    # Merge adjacent natural buckets until each has >= cr_bucket_size creatures
    all_starts = sorted(natural.keys())
    merged: list[tuple[int, int, list[str]]] = []  # (start, end_inclusive, creatures)
    i = 0
    while i < len(all_starts):
        start = all_starts[i]
        end = start + cr_bucket_size - 1
        creatures = list(natural[start])
        i += 1
        while len(creatures) < cr_bucket_size and i < len(all_starts):
            next_start = all_starts[i]
            end = next_start + cr_bucket_size - 1
            creatures.extend(natural[next_start])
            i += 1
        merged.append((start, end, creatures))

    # Build sidebar TOC
    toc_parts = [
        *_views_toc("by-cr/index.html", "../"),
        '<div class="toc-group-heading">Challenge Rating</div>',
    ]
    for start, end, creatures in merged:
        anchor = f"cr-{start}"
        label = f"CR {start}–{end}"
        toc_parts.append(
            f'<div><a href="#{E(anchor)}">{E(label)}'
            f' <span class="muted">({len(creatures)})</span></a></div>'
        )
    if unknown:
        toc_parts.append(
            f'<div><a href="#cr-unknown">Unknown CR'
            f' <span class="muted">({len(unknown)})</span></a></div>'
        )
    sidebar = toc_sidebar(toc_parts)

    sections: list[str] = [
        "<h1>Creatures by Challenge Rating</h1>",
        f"<p>Each group spans at least {cr_bucket_size} CR levels and contains at least "
        f"{cr_bucket_size} creatures (adjacent ranges are merged as needed). "
        f'See <a href="../index.html">All Creatures</a> for a flat list.</p>',
    ]

    def _render_cr_section(anchor: str, label: str, can_rrs: list[str]) -> str:
        sorted_rrs = sorted(
            can_rrs,
            key=lambda r: nwn_text(db.canonical_creature_name(r)).lower(),
        )
        rows = []
        for can_rr in sorted_rrs:
            entry = db.canonical_creatures[can_rr]
            c2 = entry["c"]
            bp2_rr = entry["bp_rr"]
            bp2 = db.creatures.get(bp2_rr) if bp2_rr != can_rr else None
            rows.append(_creature_row(
                link(f"../{can_rr}.html", db.canonical_creature_name(can_rr)),
                c2, bp2,
                tail=_faction_cell(db, c2, bp2),
            ))
        return (
            f'<h2 id="{E(anchor)}">{E(label)}'
            f' <small class="muted">({len(rows)})</small></h2>'
            '<table class="data"><thead><tr>'
            "<th>Name</th><th>Race</th><th>Class</th>"
            "<th>HP</th><th>CR</th><th>Faction</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    for start, end, creatures in merged:
        sections.append(_render_cr_section(f"cr-{start}", f"CR {start}–{end}", creatures))
    if unknown:
        sections.append(_render_cr_section("cr-unknown", "Unknown CR", unknown))

    body = items_layout(sidebar, "\n".join(sections))
    write_page(out, PageCtx("creatures/by-cr/index.html"),
               "Creatures by Challenge Rating",
               body)


def render_creatures_by_race(db: Db, out: Path) -> None:
    """Creatures grouped by Race, with a left-sidebar TOC."""

    def _race_id(can_rr: str) -> "int | None":
        entry = db.canonical_creatures[can_rr]
        c2 = entry["c"]
        bp2_rr = entry["bp_rr"]
        bp2 = db.creatures.get(bp2_rr) if bp2_rr != can_rr else None
        raw = _bp_field(c2, bp2)("Race")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    by_race: dict = defaultdict(list)
    for can_rr in db.canonical_creatures:
        by_race[_race_id(can_rr)].append(can_rr)

    sorted_races = sorted(
        by_race.keys(),
        key=lambda r: race_name(r).lower() if r is not None else "\xff",
    )

    def _race_anchor(rid) -> str:
        return "race-unset" if rid is None else f"race-{rid}"

    toc_parts = [
        *_views_toc("by-race/index.html", "../"),
        '<div class="toc-group-heading">Races</div>',
    ]
    for rid in sorted_races:
        rname = race_name(rid) if rid is not None else "(unset)"
        toc_parts.append(
            f'<div><a href="#{E(_race_anchor(rid))}">{E(rname)}'
            f' <span class="muted">({len(by_race[rid])})</span></a></div>'
        )
    sidebar = toc_sidebar(toc_parts)

    TABLE_HEAD = (
        '<table class="data"><thead><tr>'
        "<th>Name</th><th>Count</th><th>Class</th>"
        "<th>HP</th><th>CR</th><th>Faction</th>"
        "</tr></thead><tbody>"
    )

    sections: list[str] = [
        "<h1>Creatures by Race</h1>",
        f"<p>{len(db.canonical_creatures)} unique creature{'s' if len(db.canonical_creatures) != 1 else ''}"
        f" across {len(sorted_races)} race{'s' if len(sorted_races) != 1 else ''}. "
        f'See <a href="../index.html">All Creatures</a> for a flat list.</p>',
    ]

    for rid in sorted_races:
        rname = race_name(rid) if rid is not None else "(unset)"
        anchor = _race_anchor(rid)
        def _race_sort_key(r):
            total = sum(l["count"] for l in db.canonical_locations.get(r, []))
            has_any = 0 if total > 0 else 1
            _e2 = db.canonical_creatures[r]
            _c2 = _e2["c"]
            _bp2 = db.creatures.get(_e2["bp_rr"]) if _e2["bp_rr"] != r else None
            raw_cr = fld(_c2, "ChallengeRating") or (_bp2 and fld(_bp2, "ChallengeRating"))
            cr_val = _try_float(raw_cr, float("-inf")) if raw_cr else float("-inf")
            return (has_any, -cr_val, nwn_text(db.canonical_creature_name(r)).lower())
        can_rrs = sorted(by_race[rid], key=_race_sort_key)
        rows = []
        for can_rr in can_rrs:
            entry = db.canonical_creatures[can_rr]
            c2 = entry["c"]
            bp2_rr = entry["bp_rr"]
            bp2 = db.creatures.get(bp2_rr) if bp2_rr != can_rr else None
            total_count = sum(l["count"] for l in db.canonical_locations.get(can_rr, []))
            rows.append(_creature_row(
                link(f"../{can_rr}.html", db.canonical_creature_name(can_rr)),
                c2, bp2,
                after_name=f"<td>{total_count if total_count else '&#x2014;'}</td>",
                show_race=False,
                tail=_faction_cell(db, c2, bp2),
            ))
        sections.append(
            f'<h2 id="{E(anchor)}">{E(rname)}'
            f' <small class="muted">({len(rows)})</small></h2>'
            + TABLE_HEAD + "\n".join(rows) + "</tbody></table>"
        )

    body = items_layout(sidebar, "\n".join(sections))
    write_page(out, PageCtx("creatures/by-race/index.html"), "Creatures by Race",
               body)
