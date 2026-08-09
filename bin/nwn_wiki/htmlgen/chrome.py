"""Page shell, navigation chrome and theme assets for the wiki.

Everything that wraps a rendered body in the standard site page: the header
brand/banner, the nav bar and its manual-page dropdowns, the footer, plus the
per-module theme loader and the creature-picture index that the nav and page
shell depend on.

Mutable build state (theme assets, manual menus, boss registry, creature pics)
lives in :mod:`nwn_wiki.state` and is always reached through the module object
-- ``state.X`` -- so writes by the loaders here are visible to the renderers.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import sys
from collections import defaultdict
from pathlib import Path

from nwn_wiki.gff import fld
from nwn_wiki.htmlgen.escape import E, nwn_text

from nwn_wiki import state


def _manual_menu_rows(entries: list[dict], root_rel: str) -> list[str]:
    """Return nav row HTML (links / submenus) for a list of manual-page entries."""
    rows: list[str] = []
    for entry in entries:
        if entry["kind"] == "file":
            rows.append(
                f'<a href="{E(root_rel)}/manual/{E(entry["stem"])}.html">'
                f'{E(entry["title"])}</a>'
            )
        else:
            sub_items = "\n".join(
                f'<a href="{E(root_rel)}/manual/{E(entry["dirname"])}/{E(it["stem"])}.html">'
                f'{E(it["title"])}</a>'
                for it in entry["items"]
            )
            rows.append(
                f'<div class="nav-submenu">'
                f'<span class="nav-submenu-label">{E(entry["title"])}</span>'
                f'<div class="nav-submenu-menu">{sub_items}</div>'
                f'</div>'
            )
    return rows


def _quests_nav(root_rel: str) -> str:
    """Return the Quests nav entry: a plain link to the generated quest index,
    or a dropdown when manual pages target @menu 'Quests'.

    Modules that declare no @menu 'Quests' page keep the single "Quests" link
    exactly as before; declaring one turns the entry into a dropdown and the
    generated index becomes "Journal Entries" inside it.
    """
    index_link = f'{E(root_rel)}/quests/index.html'
    rows = _manual_menu_rows(state._MANUAL_MENUS.get("Quests", []), root_rel)
    if not rows:
        return f'<a href="{index_link}">Quests</a>'
    rows.append(f'<a href="{index_link}">Journal Entries</a>')
    inner = "\n".join(rows)
    return (
        '<div class="nav-dropdown">'
        '<span class="nav-dropdown-label">Quests &#9660;</span>'
        f'<div class="nav-dropdown-menu">{inner}</div>'
        '</div>'
    )


def _activity_dropdown(root_rel: str) -> str:
    """Return the Activity nav dropdown HTML (manual pages targeting @menu
    'Activity', then Player Activity + Server Firsts).

    Player Activity / Server Firsts are refreshed more often than the rest of
    the wiki (via nwn-wiki-activity). Shown only when at least one entry exists.
    """
    manual_rows = _manual_menu_rows(state._MANUAL_MENUS.get("Activity", []), root_rel)
    if not (state._HAS_ACTIVITY_PAGE or state._HAS_SERVER_FIRSTS or manual_rows):
        return ""
    rows: list[str] = list(manual_rows)
    if state._HAS_ACTIVITY_PAGE:
        rows.append(f'<a href="{E(root_rel)}/activity.html">Player Activity</a>')
    if state._HAS_SERVER_FIRSTS:
        rows.append(
            f'<a href="{E(root_rel)}/manual/ServerFirsts.html">Server Firsts</a>'
        )
    inner = "\n".join(rows)
    return (
        '<div class="nav-dropdown">'
        '<span class="nav-dropdown-label">Activity &#9660;</span>'
        f'<div class="nav-dropdown-menu">{inner}</div>'
        '</div>'
    )


def _docs_dropdown(root_rel: str) -> str:
    """Return the Documents nav dropdown HTML, or empty string if none."""
    entries = state._MANUAL_MENUS.get("Documents", [])
    if not entries:
        return ""
    inner = "\n".join(_manual_menu_rows(entries, root_rel))
    return (
        '<div class="nav-dropdown">'
        '<span class="nav-dropdown-label">Documents &#9660;</span>'
        f'<div class="nav-dropdown-menu">{inner}</div>'
        '</div>'
    )


def _custom_manual_dropdowns(root_rel: str) -> str:
    """Return nav dropdown HTML for any custom @menu names (excluding the
    built-in Documents/Activity/Quests dropdowns), ordered by @menu-order then
    first-seen position."""
    custom_names = [n for n in state._MANUAL_MENUS
                    if n not in ("Documents", "Activity", "Quests")]
    custom_names.sort(key=lambda n: (state._MANUAL_MENU_ORDER.get(n, 10**9),
                                      list(state._MANUAL_MENUS).index(n)))
    blocks: list[str] = []
    for name in custom_names:
        inner = "\n".join(_manual_menu_rows(state._MANUAL_MENUS[name], root_rel))
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


def _brand_html(root_rel: str) -> str:
    """Return the site-header brand: a banner image when the theme supplies one,
    otherwise plain text.

    The banner is emitted with width/height attributes (read from the file by
    _img_pixel_size) so the browser reserves the right box before the image
    downloads — without them the whole nav bar shifts once the header decodes.
    When a theme ships several banners one is picked at random by a tiny inline
    script that runs the moment the <img> is parsed, so only the chosen image is
    ever fetched and the reserved box matches it.
    """
    home = f'{E(root_rel)}/index.html'
    if not state._THEME_HEADER_IMGS:
        return f'<a class="brand" href="{home}">Module Wiki</a>'

    srcs = [f"{root_rel}/assets/{n}" for n in state._THEME_HEADER_IMGS]
    dims = state._THEME_HEADER_DIMS or [None] * len(srcs)

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


def page(title: str, body: str, root_rel: str = ".", page_updated_at: str = "") -> str:
    """Wrap body in the standard wiki page shell."""
    extra_css = (f'\n  <link rel="stylesheet" href="{E(root_rel)}/assets/{E(state._THEME_EXTRA_CSS)}">'
                 if state._THEME_EXTRA_CSS else "")
    favicon = (f'\n  <link rel="icon" href="{E(root_rel)}/assets/{E(state._THEME_FAVICON)}">'
               if state._THEME_FAVICON else "")
    brand = _brand_html(root_rel)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{E(nwn_text(title))}</title>
  <script>document.documentElement.classList.add('js-nav','nav-pending');</script>
  <link rel="stylesheet" href="{E(root_rel)}/assets/style.css">{extra_css}{favicon}
  <script src="{E(root_rel)}/assets/site.js" defer></script>
</head>
<body>
  <header class="site-header">
    {brand}
    <nav>
      <a href="{E(root_rel)}/map/index.html">Map</a>
      <a href="{E(root_rel)}/areas/index.html">Areas</a>
      <div class="nav-dropdown">
        <span class="nav-dropdown-label">Creatures &#9660;</span>
        <div class="nav-dropdown-menu">
          <a href="{E(root_rel)}/creatures/index.html">All Creatures</a>
          <a href="{E(root_rel)}/creatures/by-area/index.html">By Area</a>
          <a href="{E(root_rel)}/creatures/by-cr/index.html">By Challenge Rating</a>
          <a href="{E(root_rel)}/creatures/by-race/index.html">By Race</a>
          <a href="{E(root_rel)}/creatures/search.html">Search</a>
          {f'<a href="{E(root_rel)}/creatures/bosses.html">Bosses</a>' if state._BOSS_REGISTRY else ''}
          <a href="{E(root_rel)}/creatures/pictures.html">Pictures</a>
        </div>
      </div>
      <div class="nav-dropdown">
        <span class="nav-dropdown-label">Items &#9660;</span>
        <div class="nav-dropdown-menu">
          <a href="{E(root_rel)}/items/index.html">Accessible</a>
          <a href="{E(root_rel)}/items/inaccessible/index.html">Inaccessible</a>
          <a href="{E(root_rel)}/items/properties/index.html">By Property</a>
          <a href="{E(root_rel)}/items/search.html">Search</a>
        </div>
      </div>
      <a href="{E(root_rel)}/stores/index.html">Stores</a>
      <a href="{E(root_rel)}/conversations/index.html">Conversations</a>
      <a href="{E(root_rel)}/factions.html">Factions</a>
      {_quests_nav(root_rel)}
      {_activity_dropdown(root_rel)}
      {_docs_dropdown(root_rel)}
      {_custom_manual_dropdowns(root_rel)}
    </nav>
  </header>
  <main>
{body}
  </main>
  <footer>generated by <a href="https://github.com/mrprice22/nwn-manager">nwn-manager</a> &mdash; <span id="wiki-generated-at" data-meta-url="{E(root_rel)}/assets/meta.json">{E(f"last updated {page_updated_at}") if page_updated_at else ""}</span></footer>
</body>
</html>
"""


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


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
    try:
        return float(raw)
    except (TypeError, ValueError):
        return -1.0


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
