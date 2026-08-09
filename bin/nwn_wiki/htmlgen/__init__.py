"""nwn_wiki.htmlgen -- HTML emission helpers for the wiki generator.

Split into leaf modules so the renderers in :mod:`nwn_wiki.cli` can import
just the piece they need:

* :mod:`nwn_wiki.htmlgen.escape` -- HTML escaping and NWN colour-token
  rendering (stdlib only).
* :mod:`nwn_wiki.htmlgen.links` -- anchor/label builders for scripts,
  conversations, factions, races and tilesets.
* :mod:`nwn_wiki.htmlgen.markdown` -- the minimal Markdown-to-HTML
  converter used for the module's manual pages (stdlib only).
* :mod:`nwn_wiki.htmlgen.chrome` -- the page shell, nav dropdowns, theme
  asset loader and creature-picture index.

This package deliberately re-exports nothing; import from the submodules.
"""

from __future__ import annotations
