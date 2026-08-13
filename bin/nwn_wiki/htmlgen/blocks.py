"""nwn_wiki.htmlgen.blocks -- the wiki's repeated page-body wrappers.

Three structural fragments are emitted by nearly every renderer:

* ``<aside class="items-toc">`` -- the floating table-of-contents sidebar.
* ``<div class="items-layout">`` -- the sidebar + content two-column shell.
* ``<dl class="meta">`` -- the key/value box at the top of a detail page.

They are collected here so the markup lives in one place. Each helper is a
pure string builder: stdlib only, no wiki state, no escaping (callers pass
already-escaped HTML).
"""

from __future__ import annotations

from collections.abc import Iterable


def toc_sidebar(parts: Iterable[str], *, wide: bool = False) -> str:
    """The floating TOC sidebar. ``parts`` are concatenated verbatim.

    ``wide=True`` adds the ``items-toc--wide`` modifier used by the pages
    whose TOC entries are long (item properties, creature pictures).
    """
    cls = "items-toc items-toc--wide" if wide else "items-toc"
    return f'<aside class="{cls}">' + "".join(parts) + "</aside>"


def items_layout(sidebar: str, content: str) -> str:
    """The two-column shell: a TOC ``sidebar`` beside ``content``."""
    return (f'<div class="items-layout">{sidebar}'
            f'<div class="items-content">{content}</div></div>')


def meta_dl(rows: Iterable[str], sep: str = "") -> str:
    """The ``<dl class="meta">`` key/value box wrapping ``rows``.

    ``rows`` are ``<dt>…</dt><dd>…</dd>`` fragments. ``sep`` is placed
    between every piece *including* the open and close tags, so a caller
    that used to push the tags and rows into a newline-joined ``sections``
    list passes ``sep="\\n"`` and gets byte-identical output.
    """
    return sep.join(['<dl class="meta">', *rows, "</dl>"])
