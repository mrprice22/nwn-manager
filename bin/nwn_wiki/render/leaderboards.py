"""characters/leaderboards.html — cross-character rankings.

Every board is built from the same character records the index and detail pages
use, so a number here can always be traced to a character page.

**Deduplication.** Kept as a safety net, though it should now find nothing:
every duplicate came from an admin copy of a player's ``.bic`` (the dev vault is
synced from prod for debugging), and admin accounts are excluded from the
records entirely -- see ``model.build_records``. Two entries sharing a UUID also
share the kill-ledger rows, so ranking them raw would count one character's
kills once per copy; if such a pair ever reappears, boards rank by UUID rather
than silently inflating a total.
"""

from __future__ import annotations

from nwn_wiki.htmlgen.blocks import items_layout, toc_sidebar
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.render.players import tracking_note

from nwn_wiki import state

TOP_N = 10


def dedupe(records: list[dict]) -> list[dict]:
    """One record per character UUID; prefer the copy with a known player."""
    best: dict[str, dict] = {}
    for r in records:
        key = r["uuid"] or f"slug:{r['slug']}"
        cur = best.get(key)
        if cur is None or (not cur["player"] and r["player"]):
            best[key] = r
    return list(best.values())


def _board(title: str, blurb: str, rows: list[tuple[dict, str]],
           ctx: PageCtx, anchor: str) -> str:
    if not rows:
        return ""
    body = "".join(
        "<tr>"
        f"<td>{i}</td>"
        f'<td><a href="{E(ctx.dir_url("characters") + rec["slug"] + ".html")}">'
        f"{E(rec['name'])}</a></td>"
        f"<td>{E(rec['player']) if rec['player'] else '<span class=\"muted\">—</span>'}</td>"
        f"<td>{E(val)}</td>"
        "</tr>"
        for i, (rec, val) in enumerate(rows, 1)
    )
    return (
        f'<h2 id="{anchor}">{E(title)}</h2>'
        f'<p class="muted">{E(blurb)}</p>'
        '<table class="data"><thead><tr>'
        "<th>#</th><th>Character</th><th>Player</th><th>Score</th>"
        "</tr></thead><tbody>" + body + "</tbody></table>"
    )


def render_leaderboards(out) -> None:
    recs = dedupe(state._CHARACTERS)
    if not recs:
        return
    ctx = PageCtx("characters/leaderboards.html")

    def top(key, fmt, filter_zero=True):
        rows = sorted(recs, key=lambda r: -(r[key] or 0))
        rows = [r for r in rows if (r[key] or 0) > 0] if filter_zero else rows
        return [(r, fmt(r)) for r in rows[:TOP_N]]

    boards = [
        _board("Most Kills", "Total creatures slain, solo and in party.",
               top("kills_total", lambda r: f"{r['kills_total']:,}"), ctx, "kills"),
        _board("Bestiary Completion", "Distinct creature types killed.",
               top("unique_creatures", lambda r: f"{r['unique_creatures']:,}"),
               ctx, "bestiary"),
        _board("Deepest Pockets", "Gold carried.",
               top("gold", lambda r: f"{r['gold']:,}"), ctx, "gold"),
        # Genuinely per-character, unlike the account figure this board used to
        # show -- which ranked one player's four characters 1-4 on the same
        # number, because the server log names the account and never the
        # character. See ptm_db.nss.
        _board("Most Played", "Hours played on this character.",
               top("char_hours", lambda r: f"{r['char_hours']:g} h"),
               ctx, "played"),
    ]
    boards = [b for b in boards if b]

    sidebar = toc_sidebar([
        '<div class="toc-group-heading">Players</div>',
        f'<div><a href="{E(ctx.url("characters/index.html"))}">All Characters</a></div>',
        '<div class="toc-group-heading">Boards</div>',
        '<div><a href="#kills">Most Kills</a></div>',
        '<div><a href="#bestiary">Bestiary Completion</a></div>',
        '<div><a href="#gold">Deepest Pockets</a></div>',
        '<div><a href="#played">Most Played</a></div>',
    ])
    sections = [
        "<h1>Leaderboards</h1>",
        f'<p>Top {TOP_N} of {len(recs):,} characters. Every board here ranks a '
        'single character; per-player figures live on the '
        f'<a href="{E(ctx.url("players/index.html"))}">players</a> page.</p>',
        *boards,
        tracking_note() if any("Most Played" in b for b in boards) else "",
    ]
    write_page(out, ctx, "Leaderboards", items_layout(sidebar, "\n".join(sections)))
