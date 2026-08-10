"""Manual-page rendering helpers for the wiki.

Title extraction and the builder-authored ``@menu``/``@order``/``@menu-order``
nav directives that decide where a ``docs.manual/`` page lands in the site nav.
"""

from __future__ import annotations

import re
from pathlib import Path

from nwn_wiki.htmlgen.chrome import _md_title
from nwn_wiki.htmlgen.markdown import md_to_html


def _html_title(text: str, stem: str) -> str:
    """Extract title from <title> or first <h1> tag, or derive from filename stem."""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.IGNORECASE | re.DOTALL)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return stem.replace("-", " ").replace("_", " ").title()


# Builder-authored directives that override where a docs.manual/ page (or an
# entire subfolder) lands in the site nav, mirroring the quest @group/@order
# scheme (see _RE_QUEST_GROUP et al. above). Found by regex search anywhere in
# the raw file text:
#   @menu 'Name'     place this page/folder under the "Name" top-level
#                    dropdown instead of "Documents". 'Documents', 'Activity'
#                    and 'Quests' fold into those existing built-in nav
#                    entries; any other name creates a new top-level dropdown.
#                    ('Quests' is a plain link to the generated quest index
#                    until at least one page targets it — see _quests_nav.)
#   @order N         sort position of this entry within its target menu
#                    (integer; lower = earlier; ties fall back to alphabetical)
#   @menu-order N    sort position of a custom menu among other custom menus
#                    in the nav (set on any entry targeting that menu; first
#                    found wins) — meaningless for 'Documents'/'Activity',
#                    which keep their fixed nav position.
# In .md files, write the directive as its own bare line — it is stripped
# before Markdown conversion (md_to_html HTML-escapes plain text, so an
# unstripped line would otherwise render as literal visible text). In .html
# files, wrap it in an HTML comment (e.g. <!-- @menu 'Activity' -->) — the
# body is inserted verbatim, so a comment is already invisible in the browser
# and needs no stripping.
_RE_MANUAL_MENU = re.compile(
    r"@menu\s+(?:'([^']*)'|\"([^\"]*)\"|([^\n]+))", re.IGNORECASE)
_RE_MANUAL_ORDER = re.compile(r"@order\s+(\d+)", re.IGNORECASE)
_RE_MANUAL_MENU_ORDER = re.compile(r"@menu-order\s+(\d+)", re.IGNORECASE)
# Whole directive lines, stripped from .md source before Markdown conversion.
_RE_MANUAL_DIRECTIVE = re.compile(
    r"^[ \t]*@(?:menu-order|menu|order)\b[^\n]*$", re.IGNORECASE | re.MULTILINE)


def _manual_menu(text: str) -> str:
    """The @menu name declared in a manual page's source, or 'Documents' if none."""
    m = _RE_MANUAL_MENU.search(text or "")
    if not m:
        return "Documents"
    return (m.group(1) or m.group(2) or m.group(3) or "").strip() or "Documents"


def _manual_sort_order(text: str) -> int | None:
    """Sort position of this entry within its target menu, via @order N.
    Returns None when not present (callers fall back to alphabetical)."""
    m = _RE_MANUAL_ORDER.search(text or "")
    return int(m.group(1)) if m else None


def _manual_menu_order(text: str) -> int | None:
    """Sort position of this entry's target menu among other custom menus,
    via @menu-order N. Returns None when not present."""
    m = _RE_MANUAL_MENU_ORDER.search(text or "")
    return int(m.group(1)) if m else None


def _manual_doc_body(path: Path, text: str | None = None) -> tuple[str, str]:
    """Return (title, body_html) for a .md or .html manual doc."""
    if text is None:
        text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".html":
        # Extract <body> content if present, otherwise use full text as body
        m = re.search(r"<body[^>]*>(.*?)</body>", text, re.IGNORECASE | re.DOTALL)
        body = m.group(1) if m else text
        return _html_title(text, path.stem), body
    cleaned = _RE_MANUAL_DIRECTIVE.sub("", text)
    return _md_title(cleaned, path.stem), md_to_html(cleaned)
