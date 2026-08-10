"""Link and label builders shared by the page renderers.

Each helper turns an id or resref into the HTML fragment the wiki uses for it:
a link to the script / conversation / factions / by-race page when the target
exists, and escaped plain text when it does not.  The ``db`` arguments are
duck-typed (``Db`` lives in :mod:`nwn_wiki.cli`) so this stays a leaf module
importing only :mod:`nwn_wiki.htmlgen.escape`, :mod:`nwn_wiki.htmlgen.pagectx`
and :mod:`nwn_wiki.lookups`.

Every helper that emits a site-internal URL takes the :class:`PageCtx` of the
page it is being rendered into, so the link depth comes from that page's output
path rather than from a ``root_rel`` string re-stated at each call site.
"""

from __future__ import annotations

from nwn_wiki.htmlgen.escape import E, nwn_html
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.lookups import TILESETS, race_name


def link(href: str, text: str) -> str:
    return f'<a href="{E(href)}">{nwn_html(text)}</a>'


def _script_link(db: "Db", resref: str | None, ctx: PageCtx) -> str:
    """Render a script resref as a link to its source-view page when the
    script is shipped with the module; otherwise render it as plain code."""
    if not resref:
        return ""
    if resref in db.script_paths:
        return link(ctx.url(f"scripts/{resref}.html"), resref)
    return f"<code>{E(resref)}</code>"


def _conv_link(db: "Db", resref: str | None, ctx: PageCtx) -> str:
    """Render a dialog resref as a link to its conversation page when the
    dialog is in the module; otherwise render it as plain code (lets the
    user see resrefs that point at engine-default dialogs that don't ship
    in the module's dlg files)."""
    if not resref:
        return ""
    if resref.lower() in db.dialogs:
        return link(ctx.url(f"conversations/{resref.lower()}.html"), resref)
    return f"<code>{E(resref)}</code>"


def _faction_dd(db: Db, faction_id, ctx: PageCtx) -> str:
    """Render a faction id as <name> (<id>) linking to the factions page,
    falling back to just the raw id when no name is resolvable."""
    if faction_id is None or faction_id == "":
        return ""
    name = db.faction_name(faction_id)
    try:
        _fid_int = int(faction_id)
    except (TypeError, ValueError):
        _fid_int = None
    if name and str(name) != str(faction_id):
        id_suffix = (
            "" if _fid_int == 65535
            else f' <small class="muted">(id {E(faction_id)})</small>'
        )
        return f'<a href="{E(ctx.url("factions.html"))}">{nwn_html(name)}</a>{id_suffix}'
    return (f'<a href="{E(ctx.url("factions.html"))}">{E(faction_id)}</a>')


def _faction_cell(db: Db, canonical_rr: str, bp_faction_id, ctx: PageCtx) -> str:
    """Render faction for a creature detail page.

    Prefers FactionIDs from placed GIT instances (canonical_inst_factions) over
    the blueprint's stored value, since instances override the blueprint in-game.
    Falls back to bp_faction_id when no placed instances exist.
    When instances span multiple factions, renders each one separated by " / ".
    """
    inst_fids = sorted(db.canonical_inst_factions.get(canonical_rr, set()))
    if inst_fids:
        return " / ".join(_faction_dd(db, fid, ctx) for fid in inst_fids)
    return _faction_dd(db, bp_faction_id, ctx)


def _race_link(race_raw, ctx: PageCtx) -> str:
    """Render a race value as its name, linking to the by-race page anchor."""
    if race_raw is None or race_raw == "":
        return ""
    try:
        rid = int(race_raw)
    except (TypeError, ValueError):
        return E(str(race_raw))
    anchor = f"race-{rid}"
    href = ctx.url("creatures/by-race/index.html")
    return f'<a href="{E(href)}#{anchor}">{E(race_name(rid))}</a>'


def tileset_label(resref: str) -> str:
    """Friendly tileset name with the resref preserved as a muted suffix."""
    if not resref:
        return ""
    pretty = TILESETS.get(resref.lower())
    if pretty:
        return f'{E(pretty)} <small class="muted">({E(resref)})</small>'
    return E(resref)
