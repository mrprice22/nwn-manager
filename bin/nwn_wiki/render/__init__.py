"""nwn_wiki.render -- page renderers for the wiki generator.

* :mod:`nwn_wiki.render.index` -- the wiki front page.
* :mod:`nwn_wiki.render.areas` -- the area index, area and container pages and
  the area transition graph.
* :mod:`nwn_wiki.render.map` -- the area-map SVG, its legend hint and the
  dedicated ``/map`` page.
* :mod:`nwn_wiki.render.creatures` -- the creature indexes (all, by area, by
  CR, by race), the boss list and the picture gallery.
* :mod:`nwn_wiki.render.creature_page` -- the per-creature detail page, the
  offence/defence extraction behind it, and the creature search page.
* :mod:`nwn_wiki.render.items` -- the items index and per-item pages.
* :mod:`nwn_wiki.render.itemprops_pages` -- the items-by-property pages and the
  item search page.
* :mod:`nwn_wiki.render.stores` -- the store index, per-store and per-store-
  instance pages.
* :mod:`nwn_wiki.render.conversations` -- the conversation index and the
  dialog-tree pages.
* :mod:`nwn_wiki.render.scripts` -- the script index and per-script pages.
* :mod:`nwn_wiki.render.quests` -- the journal-derived quest index and pages.
* :mod:`nwn_wiki.render.factions` -- the faction reputation page.
* :mod:`nwn_wiki.render.manual` -- the module's own Markdown manual pages.
* :mod:`nwn_wiki.render.activity` -- server-log parsing and the player-activity
  page (also driven standalone by ``bin/nwn-wiki-activity``).

This package deliberately re-exports nothing; import from the submodules.
"""

from __future__ import annotations
