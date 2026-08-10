"""Manual-page rendering helpers for the wiki.

Title extraction and the builder-authored ``@menu``/``@order``/``@menu-order``
nav directives that decide where a ``docs.manual/`` page lands in the site nav,
plus ``render_manual_pages()`` itself and the one generated (data-driven) manual
page, the Server-First kill leaderboard.

Mutable build state (the manual menus and the server-firsts flag) lives in
:mod:`nwn_wiki.state` and is always reached through the module object --
``state.X`` -- so writes here are visible to the nav builders in
:mod:`nwn_wiki.htmlgen.chrome`.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from nwn_wiki.bestiary import _utc_to_local
from nwn_wiki.htmlgen.chrome import _md_title, page, write
from nwn_wiki.htmlgen.escape import E
from nwn_wiki.htmlgen.links import link
from nwn_wiki.htmlgen.markdown import md_to_html
from nwn_wiki.util import _tz_label_from_env

from nwn_wiki import state


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


# ---------------------------------------------------------------------------
# Bestiary kill stats (read from / seeded into the live NWNX:EE campaign DB)
# ---------------------------------------------------------------------------


def _render_server_first_body() -> str:
    """Inner HTML for the generated Server-First leaderboard manual page."""
    tz_label = _tz_label_from_env()
    parts = [
        "<h1>Server First Kills</h1>",
        f"<p>The first adventurer (or party) to slay each fearsome creature of "
        f"Challenge Rating {state.BST_SF_CR} or higher, recorded server-wide.</p>",
        "<p class=\"note\"><strong>How the credited player is chosen:</strong> "
        "the server-first record goes to the player who landed the "
        "<em>killing blow</em>. When a creature is slain by a party, only that "
        "finisher is named here — every contributing party member still gets "
        "the kill counted on the creature's own page (under Party). The "
        "<strong>Player</strong> column is the player’s account name and the "
        "<strong>Character</strong> column the character they were playing at the "
        "time.</p>",
    ]
    if not state._SERVER_FIRSTS:
        parts.append("<p><em>No server-first kills have been recorded yet — "
                     "the legends are still unwritten.</em></p>")
        return "\n".join(parts)
    rows = []
    for sf in state._SERVER_FIRSTS:
        rr = sf["resref"]
        cname = link(f"../creatures/{rr}.html", sf["cname"])
        cr = int(round(sf["cr"]))
        player_display = sf["player_name"] or sf["name"]
        rows.append(
            f"<tr><td>{cname}</td><td>{cr}</td>"
            f"<td>{E(player_display)}</td><td>{E(sf['name'])}</td>"
            f"<td>{E(_utc_to_local(sf['at']))}</td></tr>"
        )
    parts.append(
        '<table class="data"><thead><tr>'
        f"<th>Creature</th><th>CR</th><th>Player</th><th>Character</th><th>When ({tz_label})</th>"
        "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
    )
    return "\n".join(parts)


def render_manual_pages(project_root: Path, out: Path) -> None:
    """Scan <project_root>/docs.manual/ for .md/.html files and subdirs, render each."""
    state._MANUAL_MENUS = {}
    state._MANUAL_MENU_ORDER = {}
    manual_dir = project_root / "docs.manual"
    if not manual_dir.is_dir():
        return

    # Pass 1: collect all page metadata and content so state._MANUAL_MENUS is complete
    # before any page HTML is written (the dropdowns on every page must list all docs).
    # (out_path, title, body, root_rel, page_updated_at)
    pages_to_write: list[tuple[Path, str, str, str, str]] = []

    def note_menu_order(menu_name: str, menu_order: int | None) -> None:
        if menu_order is not None and menu_name not in state._MANUAL_MENU_ORDER:
            state._MANUAL_MENU_ORDER[menu_name] = menu_order

    top_files = sorted(
        p for p in manual_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".md", ".html")
    )
    for doc_path in top_files:
        raw_text = doc_path.read_text(encoding="utf-8")
        menu_name = _manual_menu(raw_text)
        order = _manual_sort_order(raw_text)
        note_menu_order(menu_name, _manual_menu_order(raw_text))
        title, body = _manual_doc_body(doc_path, text=raw_text)
        stem = doc_path.stem
        state._MANUAL_MENUS.setdefault(menu_name, []).append(
            {"kind": "file", "title": title, "stem": stem, "_order": order})
        pages_to_write.append((out / "manual" / f"{stem}.html", title, body, "..", ""))

    # Generated (data-driven) page: Server-First kill leaderboard. Surfaced via the
    # Activity nav dropdown (see _activity_dropdown), not Documents. Its content is
    # (re)generated only when the bestiary DB was loaded this run; otherwise, if a
    # prior full build already produced the page, keep it in the nav without
    # rewriting it — this keeps the nav consistent when nwn-wiki-activity re-renders
    # manual pages without DB access.
    sf_path = out / "manual" / "ServerFirsts.html"
    if state._BESTIARY_ACTIVE:
        sf_now = datetime.now().strftime("%b %-d, %Y %H:%M")
        state._HAS_SERVER_FIRSTS = True
        pages_to_write.append((sf_path, "Server Firsts",
                               _render_server_first_body(), "..", sf_now))
    elif sf_path.exists():
        state._HAS_SERVER_FIRSTS = True

    for sub_dir in sorted(d for d in manual_dir.iterdir() if d.is_dir()):
        doc_files = sorted(
            p for p in sub_dir.iterdir()
            if p.is_file() and p.suffix.lower() in (".md", ".html")
        )
        if not doc_files:
            continue
        dirname = sub_dir.name
        folder_title = dirname.replace("-", " ").replace("_", " ")
        items: list[dict] = []
        # Folder-level @menu/@order/@menu-order are taken from the first file
        # (in sorted order) that declares them — first found wins, mirroring
        # the quest @group-order rule.
        folder_menu: str | None = None
        folder_order: int | None = None
        for doc_path in doc_files:
            raw_text = doc_path.read_text(encoding="utf-8")
            if folder_menu is None and _RE_MANUAL_MENU.search(raw_text):
                folder_menu = _manual_menu(raw_text)
            if folder_order is None:
                folder_order = _manual_sort_order(raw_text)
            note_menu_order(_manual_menu(raw_text), _manual_menu_order(raw_text))
            title, body = _manual_doc_body(doc_path, text=raw_text)
            stem = doc_path.stem
            items.append({"title": title, "stem": stem})
            pages_to_write.append((
                out / "manual" / dirname / f"{stem}.html", title, body, "../..", "",
            ))
        state._MANUAL_MENUS.setdefault(folder_menu or "Documents", []).append(
            {"kind": "dir", "title": folder_title, "dirname": dirname,
             "items": items, "_order": folder_order})

    # Sort each menu's entries by @order (list.sort is stable, so entries
    # without @order keep their original alphabetical/insertion order,
    # trailing after any explicitly-ordered ones).
    for entries in state._MANUAL_MENUS.values():
        entries.sort(key=lambda e: e["_order"] if e["_order"] is not None else 10**9)

    # Pass 2: write all pages now that state._MANUAL_MENUS is fully populated.
    for out_path, title, body, root_rel, page_ts in pages_to_write:
        write(out_path, page(title, body, root_rel=root_rel, page_updated_at=page_ts))

    total = len(pages_to_write)
    if total:
        print(f"[nwn-wiki] rendered {total} manual page(s) from {manual_dir}")
