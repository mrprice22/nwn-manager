"""Player (account) index and per-player pages.

A player is an account; a character is one of their .bic files. The wiki has
always shown characters -- Top Killers names them, and the Players section gives
each one a page -- but nothing tied the several characters one person plays back
together. These pages are that link.

Only accounts the roster could name appear here. A character whose owner could
not be resolved from the session log still gets its own page; it simply belongs
to no player page, which is the honest representation of not knowing who played
it. CD keys are never rendered -- see nwn_wiki.players.model.
"""

from __future__ import annotations

from nwn_wiki.htmlgen.blocks import items_layout, toc_sidebar
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E
from nwn_wiki.htmlgen.pagectx import PageCtx

from nwn_wiki import state


def _date(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


def player_href(ctx: PageCtx, slug: str) -> str:
    return ctx.dir_url("players") + f"{slug}.html"


def player_link(ctx: PageCtx, name: str) -> str:
    """Link to a player's page, or their bare name when they have none."""
    slug = state._PLAYER_SLUGS.get(name)
    if not slug:
        return E(name)
    return f'<a href="{E(player_href(ctx, slug))}">{E(name)}</a>'


def _sidebar(ctx: PageCtx) -> str:
    rows = ['<div class="toc-group-heading">Players</div>']
    if state._ONLINE_API:
        rows.append(f'<div><a href="{E(ctx.url("characters/online.html"))}">'
                    "Who&#x27;s Online</a></div>")
    rows += [
        f'<div><a href="{E(ctx.url("players/index.html"))}">Players</a></div>',
        f'<div><a href="{E(ctx.url("characters/index.html"))}">Characters</a></div>',
        f'<div><a href="{E(ctx.url("characters/leaderboards.html"))}">Leaderboards</a></div>',
    ]
    if state._CHARACTERS:
        rows.append(f'<div><a href="{E(ctx.url("players/achievements.html"))}">'
                    "Hall of Fame</a></div>")
    return toc_sidebar(rows)


def render_players_index(out) -> None:
    """players/index.html — every account with at least one published character."""
    players = state._PLAYERS
    if not players:
        return
    ctx = PageCtx("players/index.html")

    rows = []
    for p in players:
        rows.append(
            "<tr>"
            f'<td><a href="{E(p["slug"])}.html">{E(p["name"])}</a></td>'
            f"<td>{p['character_count']}</td>"
            f"<td>{p['top_level']}</td>"
            f"<td>{p['kills_total']:,}</td>"
            f"<td>{p['unique_creatures']:,}</td>"
            f"<td>{p['play_hours']:g}</td>"
            f"<td>{_date(p['last_seen'])}</td>"
            "</tr>"
        )
    n_chars = sum(p["character_count"] for p in players)
    body = [
        "<h1>Players</h1>",
        f"<p>{len(players)} player{'s' if len(players) != 1 else ''} with "
        f"{n_chars:,} character{'s' if n_chars != 1 else ''} between them.</p>",
        '<table class="data"><thead><tr>'
        "<th>Player</th><th>Characters</th><th>Best Level</th><th>Kills</th>"
        "<th>Bestiary</th><th>Hours</th><th>Last Seen</th>"
        "</tr></thead><tbody>",
        "\n".join(rows),
        "</tbody></table>",
    ]
    write_page(out, ctx, "Players", items_layout(_sidebar(ctx), "\n".join(body)))


def render_player_pages(out) -> None:
    """players/<slug>.html — one page per account, listing their characters."""
    for p in state._PLAYERS:
        ctx = PageCtx(f"players/{p['slug']}.html")
        rows = []
        for c in p["characters"]:
            href = ctx.dir_url("characters") + f"{c['slug']}.html"
            rows.append(
                "<tr>"
                f'<td><a href="{E(href)}">{E(c["name"])}</a></td>'
                f"<td>{c['level']}</td>"
                f"<td>{E(c['class_line'])}</td>"
                f"<td>{E(c['race'])}</td>"
                f"<td>{c['kills_total']:,}</td>"
                f"<td>{c['unique_creatures']:,}</td>"
                f"<td>{_date(c['last_kill'])}</td>"
                "</tr>"
            )

        facts = [
            ("Characters", f"{p['character_count']:,}"),
            ("Best level", str(p["top_level"])),
            ("Total kills", f"{p['kills_total']:,}"),
            ("Unique creatures", f"{p['unique_creatures']:,}"),
            ("Play time", f"{p['play_hours']:g} h" if p["play_hours"] else ""),
            ("Last seen", _date(p["last_seen"])),
        ]
        fact_html = "".join(f"<div><dt>{E(k)}</dt><dd>{E(v)}</dd></div>"
                            for k, v in facts if v)

        body = [
            f"<h1>{E(p['name'])}</h1>",
            '<div class="card"><h2>Player</h2>'
            f'<dl class="kv">{fact_html}</dl>'
            '<p class="muted">Play time and last-seen are per account: the '
            "server log records who logged in, never which character they "
            "chose.</p></div>",
            f"<h2>Characters <small class=\"muted\">({p['character_count']})</small></h2>",
            '<table class="data"><thead><tr>'
            "<th>Character</th><th>Level</th><th>Classes</th><th>Race</th>"
            "<th>Kills</th><th>Bestiary</th><th>Last Kill</th>"
            "</tr></thead><tbody>",
            "\n".join(rows),
            "</tbody></table>",
        ]
        write_page(out, ctx, p["name"],
                   items_layout(_sidebar(ctx), "\n".join(body)))
