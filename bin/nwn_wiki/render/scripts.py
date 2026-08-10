"""Script pages for the wiki (NWScript source view).

One page per shipped ``.nss`` file plus the plain index they are linked from.
"""

from __future__ import annotations

from pathlib import Path

from nwn_wiki.htmlgen.chrome import page, write
from nwn_wiki.htmlgen.escape import E
from nwn_wiki.htmlgen.links import _conv_link, link


def render_script_page(db: "Db", resref: str, out: Path) -> None:
    """One page per shipped .nss script — just the source, syntax-untouched
    inside a <pre>. Linked from dialog caller / event-script tables so the
    reader can jump from "this OnUsed opens dialog X" straight to the code."""
    path = db.script_paths.get(resref)
    if not path:
        return
    try:
        source = path.read_text(errors="replace")
    except Exception as e:
        source = f"(failed to read {path.name}: {e})"
    starts = sorted(db.script_dialogs.get(resref, set()))
    zstarts = sorted(db.script_zdialogs.get(resref, set()))
    teleports = sorted(db.script_teleport_tags.get(resref, set()))
    meta_rows = []
    if starts:
        meta_rows.append("<dt>Starts dialogs</dt><dd>" + ", ".join(
            _conv_link(db, d, "..") for d in starts) + "</dd>")
    if zstarts:
        meta_rows.append("<dt>Opens z-dialogs</dt><dd>" + ", ".join(
            _conv_link(db, d, "..") for d in zstarts) + "</dd>")
    if teleports:
        parts = []
        for t in teleports:
            a = db.tag_to_area.get(t)
            if a and a in db.areas:
                parts.append(f"<code>{E(t)}</code> ("
                             + link(f"../areas/{a}.html", db.area_name(a)) + ")")
            else:
                parts.append(f"<code>{E(t)}</code>")
        meta_rows.append("<dt>Teleports to</dt><dd>" + ", ".join(parts) + "</dd>")
    meta = ('<dl class="meta">' + "\n".join(meta_rows) + "</dl>") if meta_rows else ""
    body = (
        f"<h1>Script: <code>{E(resref)}</code></h1>"
        + meta
        + f'<pre class="nss-source"><code>{E(source)}</code></pre>'
    )
    write(out / "scripts" / f"{resref}.html",
          page(f"Script {resref}", body, root_rel=".."))


def render_scripts_index(db: "Db", out: Path) -> None:
    """Plain listing of every shipped .nss script. Mostly used as a target
    for cross-page links (caller tables, z-dialog handler pages); not
    surfaced in the top nav so we don't drown it in 2000+ entries."""
    if not db.script_paths:
        return
    rows = []
    for rr in sorted(db.script_paths):
        starts = db.script_dialogs.get(rr, set())
        zstarts = db.script_zdialogs.get(rr, set())
        flags = []
        if starts:
            flags.append("starts dialog")
        if zstarts:
            flags.append("opens z-dialog")
        if db.script_teleport_tags.get(rr):
            flags.append("teleports")
        rows.append(
            f"<tr><td>{link(f'{rr}.html', rr)}</td>"
            f"<td>{', '.join(flags)}</td></tr>"
        )
    body = (
        "<h1>Scripts</h1>"
        f"<p>{len(rows)} NWScript files.</p>"
        '<table class="data"><thead><tr>'
        "<th>ResRef</th><th>Notes</th>"
        "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
    )
    write(out / "scripts" / "index.html",
          page("Scripts", body, root_rel=".."))
