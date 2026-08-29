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


def _dpr(v) -> str:
    """Damage per round, or an em dash when this character never hit the dummy."""
    return f"{v:,.1f}" if v else "—"


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

    # Ranked by medals: the table's first question is "who is doing well", and
    # the Hall of Fame is the wiki's own answer to that. Ties fall back to the
    # most recently seen, which keeps active players above dormant ones.
    medals = state._PLAYER_ACHIEVEMENTS
    ranked = sorted(
        players,
        key=lambda p: (-medals.get(p["name"], 0),
                       p["last_seen"] is None,
                       -(p["last_seen"].timestamp() if p["last_seen"] else 0),
                       p["name"].lower()),
    )

    rows = []
    for p in ranked:
        won = medals.get(p["name"], 0)
        dpr = _dpr(p.get("best_dpr"))
        rows.append(
            "<tr>"
            f'<td><a href="{E(p["slug"])}.html">{E(p["name"])}</a></td>'
            f"<td>{p['character_count']}</td>"
            f"<td>{won or '—'}</td>"
            f"<td>{p['kills_total']:,}</td>"
            f"<td>{p['unique_creatures']:,}</td>"
            f"<td>{p['best_bestiary']:,}</td>"
            f"<td>{dpr}</td>"
            f"<td>{p['play_hours']:g}</td>"
            f"<td>{_date(p['last_seen'])}</td>"
            "</tr>"
        )
    n_chars = sum(p["character_count"] for p in players)
    body = [
        "<h1>Players</h1>",
        f"<p>{len(players)} player{'s' if len(players) != 1 else ''} with "
        f"{n_chars:,} character{'s' if n_chars != 1 else ''} between them, "
        "ranked by Hall of Fame awards held.</p>",
        '<table class="data"><thead><tr>'
        "<th>Player</th><th>Characters</th><th>Awards</th><th>Kills</th>"
        '<th title="Distinct creature types killed across all of this '
        'player\u2019s characters">Bestiary (all)</th>'
        '<th title="Distinct creature types killed by this player\u2019s '
        'single best character">Best character</th>'
        '<th title="Best damage per round measured on the combat dummy">'
        "Best DPR</th>"
        "<th>Hours</th><th>Last Seen</th>"
        "</tr></thead><tbody>",
        "\n".join(rows),
        "</tbody></table>",
        '<p class="muted">Bestiary (all) is the union across a player\u2019s '
        "characters, so it is at least as large as their best single character. "
        "Hours are per account: the server log records who logged in, never "
        "which character they chose.</p>",
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
                f"<td>{_dpr(c.get('best_dpr'))}</td>"
                f"<td>{_date(c['last_kill'])}</td>"
                "</tr>"
            )

        facts = [
            ("Characters", f"{p['character_count']:,}"),
            ("Best level", str(p["top_level"])),
            ("Total kills", f"{p['kills_total']:,}"),
            ("Unique creatures", f"{p['unique_creatures']:,}"),
            ("Play time", f"{p['play_hours']:g} h" if p["play_hours"] else ""),
            ("Best damage/round",
             f"{p['best_dpr']:,.1f}" if p.get("best_dpr") else ""),
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
            "<th>Kills</th><th>Bestiary</th><th>Best DPR</th><th>Last Kill</th>"
            "</tr></thead><tbody>",
            "\n".join(rows),
            "</tbody></table>",
        ]
        write_page(out, ctx, p["name"],
                   items_layout(_sidebar(ctx), "\n".join(body)))
