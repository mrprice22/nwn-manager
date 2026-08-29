"""Page shell, navigation chrome and theme assets for the wiki.

Everything that wraps a rendered body in the standard site page: the header
brand/banner, the nav bar and its manual-page dropdowns, the footer, plus the
per-module theme loader and the creature-picture index that the nav and page
shell depend on.

Mutable build state (theme assets, manual menus, boss registry, creature pics)
lives in :mod:`nwn_wiki.state` and is always reached through the module object
-- ``state.X`` -- so writes by the loaders here are visible to the renderers.

Once every loader has run, ``cli.main()`` freezes the subset of that state the
page shell actually reads into a single :class:`SiteChrome`, publishes it as
``state.CHROME`` and serialises it to ``<out>/.chrome.json``.  That file is what
``bin/nwn-wiki-activity`` loads when it re-writes ``activity.html`` and the
manual pages in a separate interpreter, so the nav bar it emits cannot drift
from the nav bar the main build emitted.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from nwn_wiki.gff import fld
from nwn_wiki.htmlgen.escape import E, nwn_text
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.util import _try_float

from nwn_wiki import state

CHROME_FILE = ".chrome.json"
_CHROME_FORMAT = 2


def has_creature_pics_page() -> bool:
    """True when creatures/pictures.html will be written.

    The single source of truth for that fact: render_creature_pictures()
    early-returns on ``not has_creature_pics_page()`` and SiteChrome gates the
    Pictures nav entry on the same call, so the nav and the page cannot
    disagree.  Reads state through the module object because
    scan_creature_pics() fills _CREATURE_PIC_GROUPS at load time.
    """
    return bool(state._CREATURE_PIC_GROUPS)


@dataclass
class SiteChrome:
    """The facts the page shell and nav bar depend on, resolved once per build.

    Built by :meth:`from_state` after every loader has run, so it is a snapshot
    of a settled build rather than a live view: any consumer holding one is
    guaranteed to render the same chrome as every other consumer, including the
    ``nwn-wiki-activity`` subprocess running in its own interpreter.

    ``has_inaccessible`` / ``has_creature_pics`` gate the Items > Inaccessible
    and Creatures > Pictures nav entries.  Each is fed by the *same* predicate
    its renderer early-returns on -- ``has_inaccessible_items()`` for
    render_inaccessible_index, ``has_creature_pics()`` for
    render_creature_pictures -- so the nav cannot offer a page that was never
    written.
    """

    has_activity: bool = False
    has_server_firsts: bool = False
    has_characters: bool = False
    has_online: bool = False
    has_bosses: bool = False
    manual_menus: dict[str, list[dict]] = field(default_factory=dict)
    manual_menu_order: dict[str, int] = field(default_factory=dict)
    theme_favicon: str = ""
    theme_header_imgs: list[str] = field(default_factory=list)
    theme_header_dims: list[tuple[int, int] | None] = field(default_factory=list)
    theme_extra_css: str = ""
    # Default True = "assume present", which is what the unconditional nav
    # entries mean today; a build that knows better passes the real value.
    has_inaccessible: bool = True
    has_creature_pics: bool = True

    @classmethod
    def from_state(cls, has_inaccessible: bool = True,
                   has_creature_pics: bool | None = None) -> "SiteChrome":
        """Snapshot the current :mod:`nwn_wiki.state` nav globals."""
        if has_creature_pics is None:
            has_creature_pics = has_creature_pics_page()
        return cls(
            has_activity=bool(state._HAS_ACTIVITY_PAGE),
            has_server_firsts=bool(state._HAS_SERVER_FIRSTS),
            has_characters=bool(state._CHARACTERS),
            has_online=bool(state._ONLINE_API),
            has_bosses=bool(state._BOSS_REGISTRY),
            manual_menus=state._MANUAL_MENUS,
            manual_menu_order=state._MANUAL_MENU_ORDER,
            theme_favicon=state._THEME_FAVICON,
            theme_header_imgs=state._THEME_HEADER_IMGS,
            theme_header_dims=state._THEME_HEADER_DIMS,
            theme_extra_css=state._THEME_EXTRA_CSS,
            has_inaccessible=bool(has_inaccessible),
            has_creature_pics=bool(has_creature_pics),
        )

    def to_dict(self) -> dict:
        return {
            "format": _CHROME_FORMAT,
            "has_activity": self.has_activity,
            "has_server_firsts": self.has_server_firsts,
            "has_characters": self.has_characters,
            "has_online": self.has_online,
            "has_bosses": self.has_bosses,
            "manual_menus": self.manual_menus,
            "manual_menu_order": self.manual_menu_order,
            "theme_favicon": self.theme_favicon,
            "theme_header_imgs": list(self.theme_header_imgs),
            "theme_header_dims": [list(d) if d else None
                                  for d in self.theme_header_dims],
            "theme_extra_css": self.theme_extra_css,
            "has_inaccessible": self.has_inaccessible,
            "has_creature_pics": self.has_creature_pics,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SiteChrome":
        return cls(
            has_activity=bool(d.get("has_activity")),
            has_server_firsts=bool(d.get("has_server_firsts")),
            has_characters=bool(d.get("has_characters")),
            has_online=bool(d.get("has_online")),
            has_bosses=bool(d.get("has_bosses")),
            manual_menus=d.get("manual_menus") or {},
            manual_menu_order=d.get("manual_menu_order") or {},
            theme_favicon=d.get("theme_favicon") or "",
            theme_header_imgs=list(d.get("theme_header_imgs") or []),
            theme_header_dims=[tuple(x) if x else None
                               for x in (d.get("theme_header_dims") or [])],
            theme_extra_css=d.get("theme_extra_css") or "",
            has_inaccessible=bool(d.get("has_inaccessible", True)),
            has_creature_pics=bool(d.get("has_creature_pics", True)),
        )

    def save(self, out: Path) -> Path:
        """Serialise to ``<out>/.chrome.json`` and return the path written."""
        path = out / CHROME_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2,
                       sort_keys=True) + "\n"
        )
        return path

    @classmethod
    def load(cls, out: Path) -> "SiteChrome | None":
        """Read ``<out>/.chrome.json``, or None when it is absent/unreadable.

        None means "this wiki tree predates the chrome file (or it is corrupt)"
        and the caller should fall back to deriving the nav facts itself.
        """
        path = Path(out) / CHROME_FILE
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return cls.from_dict(data)


def active_chrome() -> SiteChrome:
    """The SiteChrome page() renders with.

    ``state.CHROME`` once the build has published one; before that (manual pages
    are rendered in the same pass that discovers them) a snapshot of the live
    globals, which is what the nav used to read directly.
    """
    if state.CHROME is not None:
        return state.CHROME
    return SiteChrome.from_state()


#: Marks the one nav anchor that points at the page being rendered.  Paired
#: with aria-current="page" (which is the part that actually carries meaning --
#: screen readers announce it, and it needs no stylesheet to work).  The class
#: is only a styling hook; nothing in wiki_assets/style.css or site.js claims
#: this name today, so adding it is inert until someone writes a rule for it.
NAV_CURRENT_CLASS = "nav-current"


def _nav_link(ctx: PageCtx, target: str, label: str) -> str:
    """One nav anchor pointing at ``target`` (an output-root-relative path).

    When ``target`` *is* the page being rendered the anchor gains
    ``aria-current="page"`` and :data:`NAV_CURRENT_CLASS`.  The comparison is
    made against ``ctx.rel`` -- the same fact write_page() writes the file from
    -- so "am I the current page?" cannot drift from "where did this page land?"
    the way a hand-passed flag or a title match would.  A page with no nav entry
    of its own (any detail page: an area, an item, a creature) simply matches
    nothing and gets no marked anchor.
    """
    mark = (f' class="{NAV_CURRENT_CLASS}" aria-current="page"'
            if PurePosixPath(target) == ctx.rel else "")
    return f'<a href="{E(ctx.url(target))}"{mark}>{E(label)}</a>'


def _manual_menu_rows(entries: list[dict], ctx: PageCtx) -> list[str]:
    """Return nav row HTML (links / submenus) for a list of manual-page entries."""
    rows: list[str] = []
    for entry in entries:
        if entry["kind"] == "file":
            rows.append(
                _nav_link(ctx, "manual/{}.html".format(entry["stem"]),
                          entry["title"])
            )
        else:
            dirname = entry["dirname"]
            sub_items = "\n".join(
                _nav_link(ctx, f'manual/{dirname}/{it["stem"]}.html',
                          it["title"])
                for it in entry["items"]
            )
            rows.append(
                f'<div class="nav-submenu">'
                f'<span class="nav-submenu-label">{E(entry["title"])}</span>'
                f'<div class="nav-submenu-menu">{sub_items}</div>'
                f'</div>'
            )
    return rows


def _quests_nav(chrome: SiteChrome, ctx: PageCtx) -> str:
    """Return the Quests nav entry: a plain link to the generated quest index,
    or a dropdown when manual pages target @menu 'Quests'.

    Modules that declare no @menu 'Quests' page keep the single "Quests" link
    exactly as before; declaring one turns the entry into a dropdown and the
    generated index becomes "Journal Entries" inside it.
    """
    rows = _manual_menu_rows(chrome.manual_menus.get("Quests", []), ctx)
    if not rows:
        return _nav_link(ctx, "quests/index.html", "Quests")
    rows.append(_nav_link(ctx, "quests/index.html", "Journal Entries"))
    inner = "\n".join(rows)
    return (
        '<div class="nav-dropdown">'
        '<span class="nav-dropdown-label">Quests &#9660;</span>'
        f'<div class="nav-dropdown-menu">{inner}</div>'
        '</div>'
    )


def _activity_dropdown(chrome: SiteChrome, ctx: PageCtx) -> str:
    """Return the Players nav dropdown HTML.

    Everything player-facing lives under one menu: Who's Online (live, and only
    where a status endpoint is configured), the character index and
    leaderboards, then Player Activity and Server Firsts -- which used to be a
    separate 'Activity' menu and moved here when the section was introduced.
    Manual pages still target @menu 'Activity'; the key is the authored one and
    is left alone so existing Comment directives keep working.

    Player Activity / Server Firsts are refreshed more often than the rest of
    the wiki (via nwn-wiki-activity). Every entry is gated on its own data, and
    the whole menu disappears when none of it exists -- which is what keeps the
    archived forks, who have no server vault, free of an empty Players menu.
    """
    manual_rows = _manual_menu_rows(chrome.manual_menus.get("Activity", []), ctx)
    if not (chrome.has_activity or chrome.has_server_firsts
            or chrome.has_characters or chrome.has_online or manual_rows):
        return ""
    rows: list[str] = list(manual_rows)
    if chrome.has_online:
        rows.append(_nav_link(ctx, "characters/online.html", "Who's Online"))
    if chrome.has_characters:
        rows.append(_nav_link(ctx, "players/index.html", "Players"))
        rows.append(_nav_link(ctx, "characters/index.html", "Characters"))
        rows.append(
            _nav_link(ctx, "characters/leaderboards.html", "Leaderboards"))
        rows.append(
            _nav_link(ctx, "players/achievements.html", "Hall of Fame"))
    if chrome.has_activity:
        rows.append(_nav_link(ctx, "activity.html", "Player Activity"))
    if chrome.has_server_firsts:
        rows.append(
            _nav_link(ctx, "manual/ServerFirsts.html", "Server Firsts"))
    inner = "\n".join(rows)
    label = ("Players" if (chrome.has_characters or chrome.has_online)
             else "Activity")
    return (
        '<div class="nav-dropdown">'
        f'<span class="nav-dropdown-label">{label} &#9660;</span>'
        f'<div class="nav-dropdown-menu">{inner}</div>'
        '</div>'
    )


def _docs_dropdown(chrome: SiteChrome, ctx: PageCtx) -> str:
    """Return the Documents nav dropdown HTML, or empty string if none."""
    entries = chrome.manual_menus.get("Documents", [])
    if not entries:
        return ""
    inner = "\n".join(_manual_menu_rows(entries, ctx))
    return (
        '<div class="nav-dropdown">'
        '<span class="nav-dropdown-label">Documents &#9660;</span>'
        f'<div class="nav-dropdown-menu">{inner}</div>'
        '</div>'
    )


def _custom_manual_dropdowns(chrome: SiteChrome, ctx: PageCtx) -> str:
    """Return nav dropdown HTML for any custom @menu names (excluding the
    built-in Documents/Activity/Quests dropdowns), ordered by @menu-order then
    first-seen position."""
    custom_names = [n for n in chrome.manual_menus
                    if n not in ("Documents", "Activity", "Quests")]
    custom_names.sort(key=lambda n: (chrome.manual_menu_order.get(n, 10**9),
                                      list(chrome.manual_menus).index(n)))
    blocks: list[str] = []
    for name in custom_names:
        inner = "\n".join(_manual_menu_rows(chrome.manual_menus[name], ctx))
        blocks.append(
            '<div class="nav-dropdown">'
            f'<span class="nav-dropdown-label">{E(name)} &#9660;</span>'
            f'<div class="nav-dropdown-menu">{inner}</div>'
            '</div>'
        )
    return "\n".join(blocks)


def _md_title(text: str, stem: str) -> str:
    """Extract title from first # heading, or derive from filename stem."""
    for line in text.splitlines():
        m = re.match(r"^#\s+(.*)", line.strip())
        if m:
            return m.group(1).strip()
    return stem.replace("-", " ").replace("_", " ").title()


def _brand_html(chrome: SiteChrome, ctx: PageCtx) -> str:
    """Return the site-header brand: a banner image when the theme supplies one,
    otherwise plain text.

    The banner is emitted with width/height attributes (read from the file by
    _img_pixel_size) so the browser reserves the right box before the image
    downloads — without them the whole nav bar shifts once the header decodes.
    When a theme ships several banners one is picked at random by a tiny inline
    script that runs the moment the <img> is parsed, so only the chosen image is
    ever fetched and the reserved box matches it.
    """
    home = E(ctx.url("index.html"))
    if not chrome.theme_header_imgs:
        return f'<a class="brand" href="{home}">Module Wiki</a>'

    srcs = [ctx.url(f"assets/{n}") for n in chrome.theme_header_imgs]
    dims = chrome.theme_header_dims or [None] * len(srcs)

    def size_attrs(i: int) -> str:
        d = dims[i] if i < len(dims) else None
        return f' width="{d[0]}" height="{d[1]}"' if d else ""

    if len(srcs) == 1:
        return (f'<a class="brand" href="{home}">'
                f'<img class="site-header-img" src="{E(srcs[0])}"{size_attrs(0)}'
                f' alt="Home"></a>')

    entries = [{"src": s, "w": d[0], "h": d[1]} if d else {"src": s}
               for s, d in zip(srcs, dims)]
    imgs_attr = E(json.dumps(entries, separators=(",", ":")))
    picker = ('(function(){var i=document.currentScript.previousElementSibling,'
              'a=JSON.parse(i.dataset.headerImages),'
              'p=a[Math.floor(Math.random()*a.length)];'
              'if(p.w){i.width=p.w;i.height=p.h;}i.src=p.src;})();')
    # No src on the <img>: the picker assigns it synchronously, so the browser
    # never starts a fetch for a banner it is about to discard.
    return (f'<a class="brand" href="{home}">'
            f'<img class="site-header-img"{size_attrs(0)}'
            f' data-header-images="{imgs_attr}" alt="Home">'
            f'<script>{picker}</script>'
            f'<noscript><img class="site-header-img" src="{E(srcs[0])}"'
            f'{size_attrs(0)} alt="Home"></noscript></a>')


def page(title: str, body: str, ctx: PageCtx, page_updated_at: str = "",
         chrome: SiteChrome | None = None) -> str:
    """Wrap body in the standard wiki page shell.

    ``ctx`` is where the page will be written; every link below is built from
    it, so the nav can only ever point at the real output root.  Prefer
    :func:`write_page`, which takes the same ctx and writes to exactly the path
    the links were built for.

    ``chrome`` defaults to the build's published SiteChrome (``state.CHROME``),
    so every page in a run — including the ones the nwn-wiki-activity
    subprocess rewrites — is shelled from the same set of facts.
    """
    if chrome is None:
        chrome = active_chrome()
    u = ctx.url
    extra_css = (f'\n  <link rel="stylesheet" href="{E(u("assets/" + chrome.theme_extra_css))}">'
                 if chrome.theme_extra_css else "")
    favicon = (f'\n  <link rel="icon" href="{E(u("assets/" + chrome.theme_favicon))}">'
               if chrome.theme_favicon else "")
    brand = _brand_html(chrome, ctx)
    # Both entries carry their own leading newline+indent so that switching them
    # off removes the whole line rather than leaving a blank one behind.
    pictures_nav = (
        "\n          " + _nav_link(ctx, "creatures/pictures.html", "Pictures")
        if chrome.has_creature_pics else "")
    inaccessible_nav = (
        "\n          " + _nav_link(ctx, "items/inaccessible/index.html",
                                   "Inaccessible")
        if chrome.has_inaccessible else "")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{E(nwn_text(title))}</title>
  <script>document.documentElement.classList.add('js-nav','nav-pending');</script>
  <link rel="stylesheet" href="{E(u('assets/style.css'))}">{extra_css}{favicon}
  <script src="{E(u('assets/site.js'))}" defer></script>
</head>
<body>
  <header class="site-header">
    {brand}
    <nav>
      {_nav_link(ctx, 'map/index.html', 'Map')}
      {_nav_link(ctx, 'areas/index.html', 'Areas')}
      <div class="nav-dropdown">
        <span class="nav-dropdown-label">Creatures &#9660;</span>
        <div class="nav-dropdown-menu">
          {_nav_link(ctx, 'creatures/index.html', 'All Creatures')}
          {_nav_link(ctx, 'creatures/by-area/index.html', 'By Area')}
          {_nav_link(ctx, 'creatures/by-cr/index.html', 'By Challenge Rating')}
          {_nav_link(ctx, 'creatures/by-race/index.html', 'By Race')}
          {_nav_link(ctx, 'creatures/search.html', 'Search')}
          {_nav_link(ctx, 'creatures/bosses.html', 'Bosses') if chrome.has_bosses else ''}{pictures_nav}
        </div>
      </div>
      <div class="nav-dropdown">
        <span class="nav-dropdown-label">Items &#9660;</span>
        <div class="nav-dropdown-menu">
          {_nav_link(ctx, 'items/index.html', 'Accessible')}{inaccessible_nav}
          {_nav_link(ctx, 'items/properties/index.html', 'By Property')}
          {_nav_link(ctx, 'items/search.html', 'Search')}
        </div>
      </div>
      {_nav_link(ctx, 'stores/index.html', 'Stores')}
      {_nav_link(ctx, 'conversations/index.html', 'Conversations')}
      {_nav_link(ctx, 'factions.html', 'Factions')}
      {_quests_nav(chrome, ctx)}
      {_activity_dropdown(chrome, ctx)}
      {_docs_dropdown(chrome, ctx)}
      {_custom_manual_dropdowns(chrome, ctx)}
    </nav>
  </header>
  <main>
{body}
  </main>
  <footer>generated by <a href="https://github.com/mrprice22/nwn-manager">nwn-manager</a> &mdash; <span id="wiki-generated-at" data-meta-url="{E(u('assets/meta.json'))}">{E(f"last updated {page_updated_at}") if page_updated_at else ""}</span></footer>
</body>
</html>
"""


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_page(out: Path, ctx: PageCtx, title: str, body: str,
               page_updated_at: str = "",
               chrome: SiteChrome | None = None) -> None:
    """Shell ``body`` and write it to the path ``ctx`` names under ``out``.

    The single ctx feeds both halves, so a page cannot be written to one depth
    while its nav links point at another -- the failure mode the hand-threaded
    ``root_rel`` argument used to make possible.
    """
    write(ctx.path(out),
          page(title, body, ctx, page_updated_at=page_updated_at,
               chrome=chrome))


def _img_pixel_size(path: Path) -> tuple[int, int] | None:
    """Intrinsic (width, height) of a PNG/GIF/JPEG/WebP, or None if unreadable.

    Stdlib-only header parse — Pillow is not a dependency of this script. Used to
    emit width/height on the header banner so the browser reserves its box before
    the bytes arrive; None (SVG, odd encodings) just means no attributes.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                chunk = head[12:16]
                if chunk == b"VP8X":
                    # 24-bit little-endian canvas size minus one, at offset 24.
                    d = head[24:30]
                    return (d[0] | d[1] << 8 | d[2] << 16) + 1, (d[3] | d[4] << 8 | d[5] << 16) + 1
                if chunk == b"VP8 ":
                    d = head[26:30]     # after the 3-byte start code + 0x9d012a
                    return (d[0] | d[1] << 8) & 0x3FFF, (d[2] | d[3] << 8) & 0x3FFF
                if chunk == b"VP8L":
                    b = head[21:25]
                    n = b[0] | b[1] << 8 | b[2] << 16 | b[3] << 24
                    return (n & 0x3FFF) + 1, ((n >> 14) & 0x3FFF) + 1
                return None
            if head[:2] == b"\xff\xd8":
                # Walk the marker chain to the start-of-frame, which carries the size.
                fh.seek(2)
                while True:
                    b = fh.read(1)
                    if not b:
                        return None
                    if b != b"\xff":
                        continue
                    marker = fh.read(1)
                    while marker == b"\xff":     # fill bytes
                        marker = fh.read(1)
                    if not marker:
                        return None
                    m = marker[0]
                    if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                        continue                # standalone markers, no payload
                    seg = fh.read(2)
                    if len(seg) < 2:
                        return None
                    length = struct.unpack(">H", seg)[0]
                    if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        body = fh.read(5)
                        if len(body) < 5:
                            return None
                        h, w = struct.unpack(">HH", body[1:5])
                        return int(w), int(h)
                    fh.seek(length - 2, 1)
    except (OSError, struct.error, IndexError):
        return None
    return None


def load_wiki_theme(project_root: Path, out: Path) -> None:
    """Copy files from <project_root>/wiki-theme/ into output assets and set theme globals."""
    theme_dir = project_root / "wiki-theme"
    if not theme_dir.is_dir():
        return
    assets_out = out / "assets"
    assets_out.mkdir(parents=True, exist_ok=True)
    favicon_exts = {".ico", ".png", ".svg", ".gif"}
    img_exts = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
    loaded: list[str] = []
    for f in sorted(theme_dir.iterdir()):
        if not f.is_file():
            continue
        shutil.copy2(f, assets_out / f.name)
        stem, ext = f.stem.lower(), f.suffix.lower()
        if stem == "favicon" and ext in favicon_exts:
            state._THEME_FAVICON = f.name
            loaded.append(f"favicon={f.name}")
        elif stem.startswith("header") and ext in img_exts:
            state._THEME_HEADER_IMGS.append(f.name)
            state._THEME_HEADER_DIMS.append(_img_pixel_size(f))
            loaded.append(f"header={f.name}")
        elif f.name == "theme.css":
            state._THEME_EXTRA_CSS = f.name
            loaded.append("css=theme.css")
    if loaded:
        print(f"[nwn-wiki] loaded wiki-theme ({', '.join(loaded)}) from {theme_dir}")


def _creature_cr_value(db: Db, can_rr: str) -> float:
    """Numeric ChallengeRating for a canonical creature, or -1 if absent/blank."""
    entry = db.canonical_creatures.get(can_rr)
    if not entry:
        return -1.0
    c = entry["c"]
    bp_rr = entry["bp_rr"]
    bp = db.creatures.get(bp_rr) if bp_rr != can_rr else None
    raw = fld(c, "ChallengeRating")
    if raw is None and bp is not None:
        raw = fld(bp, "ChallengeRating")
    return _try_float(raw, -1.0)


def scan_creature_pics(project_root: Path, db: Db) -> None:
    """Index <project_root>/creature-pics/ artwork against creature names.

    Populates the module-level globals state._CREATURE_PICS (canonical resref ->
    [image filename]) and state._CREATURE_PIC_GROUPS (display groups for the Pictures
    page, sorted by descending CR with unmatched groups last).

    Filename rules: the stem (minus extension) is matched case-insensitively to
    a creature's display name. A trailing "_NN" sets the display order of
    multiple images for the same creature (e.g. "Gimli_01.png", "Gimli_02.png").
    """
    state._CREATURE_PICS = {}
    state._CREATURE_PIC_GROUPS = []
    pics_dir = project_root / "creature-pics"
    if not pics_dir.is_dir():
        return

    # Build a case-insensitive name -> [canonical resref] index once.
    name_index: dict[str, list[str]] = defaultdict(list)
    for can_rr in db.canonical_creatures:
        name_index[db.canonical_creature_name(can_rr).strip().lower()].append(can_rr)

    suffix_re = re.compile(r"^(?P<base>.*?)_(?P<num>\d+)$")
    # base_name (original case) -> {"matches": [...], "images": [(order, filename)]}
    groups: dict[str, dict] = {}
    for f in sorted(pics_dir.iterdir(), key=lambda p: p.name.lower()):
        if not f.is_file() or f.suffix.lower() not in state.CREATURE_PIC_EXTS:
            continue
        stem = f.stem
        m = suffix_re.match(stem)
        if m:
            base_name = m.group("base")
            order = int(m.group("num"))
        else:
            base_name = stem
            order = 0
        key = base_name.strip().lower()
        g = groups.setdefault(base_name, {"matches": name_index.get(key, []),
                                          "images": []})
        g["images"].append((order, f.name))

    for base_name, g in groups.items():
        if not g["matches"]:
            print(f"warn: creature-pics: '{base_name}' matches no creature; "
                  f"shown under 'Unmatched'", file=sys.stderr)
        images = [fn for _o, fn in sorted(g["images"], key=lambda t: (t[0], t[1].lower()))]
        if g["matches"]:
            cr = max(_creature_cr_value(db, rr) for rr in g["matches"])
        else:
            cr = -1.0
        state._CREATURE_PIC_GROUPS.append({
            "name": base_name,
            "matches": g["matches"],
            "images": images,
            "cr": cr,
        })
        for rr in g["matches"]:
            state._CREATURE_PICS.setdefault(rr, []).extend(images)

    # Descending CR; unmatched (cr < 0) sink to the bottom; tie-break by name.
    state._CREATURE_PIC_GROUPS.sort(
        key=lambda grp: (grp["matches"] == [], -grp["cr"], grp["name"].lower())
    )
    if state._CREATURE_PIC_GROUPS:
        print(f"[nwn-wiki] indexed creature-pics: {len(state._CREATURE_PIC_GROUPS)} "
              f"group(s) from {pics_dir}")
