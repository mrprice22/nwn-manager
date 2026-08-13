"""Site index (home page) rendering for the wiki.

``render_index()`` writes ``docs/index.html``: either the author-supplied
landing page from ``index.html``/``index.md`` at the project root, or the
generated module overview (blueprint counts, HAK/TLK metadata, the
global-trigger conversation table) followed by the area map.
"""

from __future__ import annotations

from pathlib import Path

from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.blocks import meta_dl
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E, nwn_html
from nwn_wiki.htmlgen.links import link
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.render.manual import _manual_doc_body
from nwn_wiki.render.map import _MAP_HINT_HTML, render_map_svg


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_index(db: Db, out: Path, module_title: str,
                 positions: dict[str, tuple[float, float]],
                 sizes: dict[str, tuple[float, float]],
                 base_url: str = "", project_root: Path | None = None) -> None:
    ctx = PageCtx("index.html")
    # Author-supplied landing page replaces the generated overview/map index.
    # .html takes precedence over .md. Body fragment only (same handling as
    # docs.manual pages) — page() adds the nav header/footer. The map still
    # lives on its own dedicated /map page (see render_map_page).
    if project_root is not None:
        override = next((project_root / f"index.{ext}" for ext in ("html", "md")
                         if (project_root / f"index.{ext}").is_file()), None)
        if override is not None:
            _, body_html = _manual_doc_body(override)
            write_page(out, ctx, module_title, body_html)
            print(f"[nwn-wiki] index: using override {override}")
            return

    # Module overview block
    ifo = db.ifo or {}
    start_area = fld(ifo, "Mod_Entry_Area", "")
    haks = list_items(ifo.get("Mod_HakList"))
    hak_names = [fld(h, "Mod_Hak", "") for h in haks]
    tlk = fld(ifo, "Mod_CustomTlk", "")
    xp = fld(ifo, "Mod_XPScale")
    desc = loc(ifo.get("Mod_Description")) if ifo else ""

    meta_rows = [
        f'<dt>Areas</dt><dd>{len(db.areas)}</dd>',
        f'<dt>Creatures</dt><dd>{len(db.creatures)}</dd>',
        f'<dt>Items</dt><dd>{len(db.items)}</dd>',
        f'<dt>Stores</dt><dd>{len(db.stores)}</dd>',
        f'<dt>Dialogues</dt><dd>{len(db.dialogs)}</dd>',
        f'<dt>Scripts</dt><dd>{len(db.scripts)}</dd>',
    ]
    if start_area:
        meta_rows.append(f'<dt>Entry area</dt><dd>{link(f"areas/{start_area}.html", db.area_name(start_area))}</dd>')
    if tlk:
        meta_rows.append(f'<dt>Custom TLK</dt><dd>{E(tlk)}</dd>')
    if xp is not None:
        meta_rows.append(f'<dt>XP scale</dt><dd>{E(xp)}%</dd>')
    if hak_names:
        meta_rows.append(f'<dt>HAKs</dt><dd>{E(", ".join(hak_names))}</dd>')
    overview = [
        f'<h1>{nwn_html(module_title)}</h1>',
        meta_dl(meta_rows, "\n"),
    ]
    if desc:
        overview.append(f'<p class="desc">{nwn_html(desc)}</p>')

    # Global-triggered conversations (rest menu, item activators, …) get
    # called out above the map: a player can fire them from anywhere, and
    # they often hide teleport destinations the map otherwise can't show.
    global_dlgs = sorted(
        db.global_convo_pseudo.values(),
        key=lambda info: info["conv_resref"],
    )
    if global_dlgs:
        overview.append("<h2>Global-trigger conversations</h2>")
        overview.append('<p class="muted">Reachable from anywhere via a '
                        'module-level event (rest, level-up, etc.) or a '
                        'tag-based item activator. Each contains at least '
                        'one teleport.</p>')
        rows = []
        for info in global_dlgs:
            rr = info["conv_resref"]
            callers = db.dialog_callers.get(rr, [])
            kinds = sorted({(c["kind"], c.get("event") or c.get("script", ""))
                            for c in callers
                            if c["kind"] in ("module-event", "item-script")})
            via = ", ".join(
                f"<code>{E(ev)}</code>" if k == "module-event"
                else f"item <code>{E(ev)}</code>"
                for k, ev in kinds
            )
            dests = ", ".join(
                link(f"areas/{a}.html", db.area_name(a))
                for a in info["dests"] if a in db.areas and a not in db.hidden_areas)
            rows.append(
                f"<tr><td>{link(f'conversations/{rr}.html', db.dialog_label(rr))}</td>"
                f"<td><code>{E(rr)}</code></td>"
                f"<td>{via}</td>"
                f"<td>{dests}</td></tr>"
            )
        overview.append(
            '<table class="data"><thead><tr>'
            "<th>Conversation</th><th>ResRef</th><th>Triggered via</th>"
            "<th>Teleports to</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    overview.append('<h2>Area map</h2>')
    overview.append(_MAP_HINT_HTML)
    overview.append(render_map_svg(db, positions, sizes, base_url=base_url))

    body = "\n".join(overview)
    write_page(out, ctx, module_title, body)
