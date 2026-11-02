"""Is a place — or the thing standing in it — actually findable by a player?

The area graph has always known this (``build_area_graph`` +
``bfs_shortest_path``, walked once per area in ``cli._compute_area_paths``), but
the answer only ever reached the Areas index and ``area_paths.json``.  Creatures
were judged by a much weaker rule — "is this blueprint placed or spawned in any
non-hidden area" — so a boss sitting in an area with no inbound transition at
all still advertised itself as ordinary content.  That is the bug this module
closes: one predicate, consulted by the creature indexes, the creature page, the
in-game bestiary seed and the module-index report, so they cannot disagree.

Both functions take a duck-typed ``db`` (``Db`` lives in :mod:`nwn_wiki.cli`,
which imports this module) and read :data:`nwn_wiki.state.UNREACHABLE_AREAS`,
which is empty unless the build was given ``--path-from``.
"""

from __future__ import annotations

from nwn_wiki import state


def unreachable_areas(db, area_paths: "dict[str, list | None]",
                      path_from_resref: str) -> frozenset[str]:
    """Visible areas with no route from ``path_from_resref``.

    Empty when the build had no ``--path-from`` (``area_paths`` is then empty
    and every lookup misses), which is what keeps a graph-less build identical
    to how it rendered before reachability existed.

    Excluded on purpose: WIKI_HIDDEN areas (already absent from every page, so
    calling them unreachable would double-count), WIKI_REACHABLE areas (the
    builder has asserted a runtime-only entrance the graph cannot see), and the
    anchor itself (``area_paths`` never holds a self-entry).
    """
    if not path_from_resref or not area_paths:
        return frozenset()
    return frozenset(
        rr for rr in db.areas
        if rr != path_from_resref
        and rr not in db.hidden_areas
        and rr not in db.forced_reachable
        and area_paths.get(rr) is None
    )


def creature_unreachable_areas(db, can_rr: str) -> list[str]:
    """The canonical creature's spawn areas, when EVERY one is unreachable.

    Empty list means "nothing to report" — either the creature is findable
    somewhere, or it has no locations at all.  A location-less creature is
    blueprint-only (a DM avatar, an unused prefab); it makes no claim about
    where to find it, and the existing "present" rule already keeps it out of
    the creature indexes, so it must not be reported as unreachable on top.
    """
    locs = db.canonical_locations.get(can_rr, [])
    if not locs:
        return []
    areas = {l["area"] for l in locs}
    if areas - state.UNREACHABLE_AREAS:
        return []
    return sorted(areas, key=lambda rr: db.area_name(rr).lower())


def creature_is_reachable(db, can_rr: str) -> bool:
    """False only when the creature has locations and all of them are cut off."""
    return not creature_unreachable_areas(db, can_rr)
