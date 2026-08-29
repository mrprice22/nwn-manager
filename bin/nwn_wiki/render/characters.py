"""Character index and per-character detail pages.

The character analogue of :mod:`nwn_wiki.render.creatures` /
:mod:`nwn_wiki.render.creature_page`, built from the records
:func:`nwn_wiki.players.model.build_records` assembles out of the servervault,
the kill ledger and the session history.

Rendered only when the build was given a vault (``--vault-dir``). Modules with
no server behind them -- the 2009 and lordoftherings forks -- pass no vault, so
these pages, and the whole Players nav section, simply do not exist for them.

Privacy: these pages show the account (player) name alongside the character
name, which is a deliberate widening of the Top Killers rule for this section.
CD keys and character UUIDs are internal join keys and are never rendered.
"""

from __future__ import annotations

from nwn_wiki.htmlgen.blocks import items_layout, toc_sidebar
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.lookups import feat_name, skill_name

from nwn_wiki import state

ABILITY_ORDER = ("Str", "Dex", "Con", "Int", "Wis", "Cha")


def _date(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


def _player_cell(rec: dict) -> str:
    """The account name, or an em dash when the roster never saw it log in.

    Never falls back to the CD key: that is account credentials, not a name.
    """
    return E(rec["player"]) if rec["player"] else '<span class="muted">—</span>'


def _char_link(rec: dict, ctx: PageCtx) -> str:
    href = ctx.dir_url("characters") + f"{rec['slug']}.html"
    return f'<a href="{E(href)}">{E(rec["name"])}</a>'


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #

def render_character_index(out) -> None:
    """characters/index.html — every character in the servervault."""
    recs = state._CHARACTERS
    if not recs:
        return
    ctx = PageCtx("characters/index.html")

    rows = []
    for r in recs:
        rows.append(
            "<tr>"
            f"<td>{_char_link(r, ctx)}</td>"
            f"<td>{_player_cell(r)}</td>"
            f"<td>{r['level']}</td>"
            f"<td>{E(r['class_line'])}</td>"
            f"<td>{E(r['race'])}</td>"
            f"<td>{r['kills_total']:,}</td>"
            f"<td>{_date(r['last_seen'])}</td>"
            "</tr>"
        )

    n_players = len({r["player"] for r in recs if r["player"]})
    sections = [
        "<h1>Characters</h1>",
        f"<p>{len(recs):,} character{'s' if len(recs) != 1 else ''} in the server "
        f"vault, across {n_players} known player{'s' if n_players != 1 else ''}.</p>",
        '<table class="data"><thead><tr>'
        "<th>Character</th><th>Player</th><th>Level</th><th>Classes</th>"
        "<th>Race</th><th>Kills</th><th>Last Seen</th>"
        "</tr></thead><tbody>",
        "\n".join(rows),
        "</tbody></table>",
    ]
    sidebar = toc_sidebar([
        '<div class="toc-group-heading">Players</div>',
        f'<div><a href="{E(ctx.url("characters/index.html"))}">Characters</a></div>',
        f'<div><a href="{E(ctx.url("characters/leaderboards.html"))}">Leaderboards</a></div>',
    ])
    write_page(out, ctx, "Characters", items_layout(sidebar, "\n".join(sections)))


# --------------------------------------------------------------------------- #
# Who's Online
# --------------------------------------------------------------------------- #

def render_online_page(out) -> None:
    """characters/online.html — the live roster.

    The only page on the site whose content is not baked at build time. The
    markup written here is the *fallback*: a plain "unavailable" message that is
    correct when the endpoint is unreachable, the pusher is dead, or JavaScript
    is off. site.js replaces it on load and every 60s thereafter from the worker
    route in src/index.js.

    Rendered only when --online-api was given, so a realm without a status
    endpoint has no page to link to and no nav entry offering one.
    """
    if not state._ONLINE_API:
        return
    ctx = PageCtx("characters/online.html")

    sidebar_rows = ['<div class="toc-group-heading">Players</div>']
    if state._CHARACTERS:
        sidebar_rows += [
            f'<div><a href="{E(ctx.url("characters/index.html"))}">All Characters</a></div>',
            f'<div><a href="{E(ctx.url("characters/leaderboards.html"))}">Leaderboards</a></div>',
        ]

    body = "\n".join([
        "<h1>Who's Online</h1>",
        f'<div data-online-roster data-online-url="{E(state._ONLINE_API)}">',
        '<p class="muted">Live status is unavailable right now.</p>',
        "</div>",
        '<p class="muted">This page refreshes itself about once a minute. '
        'The roster is published by the game server\u2019s host a few minutes '
        'after players join or leave, so it can lag a little behind the game.</p>',
    ])
    write_page(out, ctx, "Who's Online", items_layout(toc_sidebar(sidebar_rows), body))


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #

def _stat_block(rec: dict) -> str:
    ab = rec["abilities"]
    cells = "".join(
        f"<div><dt>{a}</dt><dd>{ab.get(a, 0)}</dd></div>" for a in ABILITY_ORDER)
    return f'<dl class="kv abilities">{cells}</dl>'


def _equipped_list(rec: dict) -> str:
    """Equipped gear, by the item's own name.

    A saved character carries instantiated items, so LocalizedName is the name
    the player sees; bicreader lowercases it for matching, so it is title-cased
    back here purely for display.
    """
    names = [i["name"] for i in rec["equipped"] if i.get("name")]
    if not names:
        return '<p class="muted">Nothing equipped.</p>'
    return "<ul>" + "".join(f"<li>{E(n.title())}</li>" for n in sorted(names)) + "</ul>"


def render_character_pages(out) -> None:
    """characters/<slug>.html — one page per character."""
    for rec in state._CHARACTERS:
        ctx = PageCtx(f"characters/{rec['slug']}.html")

        head = [f"<h1>{E(rec['name'])}</h1>"]
        if rec["player"]:
            head.append(f'<p class="muted">Played by {E(rec["player"])}</p>')

        facts = [
            ("Level", str(rec["level"])),
            ("Classes", rec["class_line"]),
            ("Race", " ".join(x for x in (rec["subrace"], rec["race"]) if x)),
            ("Alignment", rec["alignment"]),
            ("Hit Points", f"{rec['max_hp']:,}"),
            ("Experience", f"{rec['xp']:,}"),
            ("Gold", f"{rec['gold']:,}"),
            ("Deity", rec["deity"]),
            ("Last seen", _date(rec["last_seen"])),
            ("Play time", f"{rec['play_hours']:g} h" if rec["play_hours"] else ""),
        ]
        fact_html = "".join(
            f"<div><dt>{E(k)}</dt><dd>{E(v)}</dd></div>"
            for k, v in facts if v
        )

        kills = [
            ("Total kills", f"{rec['kills_total']:,}"),
            ("Solo", f"{rec['kills_solo']:,}"),
            ("In party", f"{rec['kills_party']:,}"),
            ("Unique creatures", f"{rec['unique_creatures']:,}"),
            ("Last kill", _date(rec["last_kill"])),
        ]
        if rec["top_kill"]:
            kills.append(("Most killed",
                          f"{rec['top_kill']['name']} ({rec['top_kill']['count']:,})"))
        kill_html = "".join(f"<div><dt>{E(k)}</dt><dd>{E(v)}</dd></div>"
                            for k, v in kills)

        feats = sorted({feat_name(f) for f in rec["feats"] if feat_name(f)})
        skills = sorted(
            ((skill_name(i), rank) for i, rank in enumerate(rec["skills"]) if rank),
            key=lambda p: -p[1])

        body_parts = [
            *head,
            '<div class="card"><h2>Character</h2>'
            f'<dl class="kv">{fact_html}</dl></div>',
            '<div class="card"><h2>Abilities</h2>' + _stat_block(rec) + "</div>",
            '<div class="card"><h2>Bestiary</h2>'
            f'<dl class="kv">{kill_html}</dl></div>',
            "<h2>Equipped</h2>" + _equipped_list(rec),
        ]
        if skills:
            body_parts.append(
                "<h2>Skills</h2><table class=\"data\"><thead><tr>"
                "<th>Skill</th><th>Rank</th></tr></thead><tbody>"
                + "".join(f"<tr><td>{E(n)}</td><td>{r}</td></tr>" for n, r in skills)
                + "</tbody></table>")
        if feats:
            body_parts.append(
                f"<h2>Feats <small class=\"muted\">({len(feats)})</small></h2>"
                + "<ul>" + "".join(f"<li>{E(f)}</li>" for f in feats) + "</ul>")

        sidebar = toc_sidebar([
            '<div class="toc-group-heading">Players</div>',
            f'<div><a href="{E(ctx.url("characters/index.html"))}">All Characters</a></div>',
            f'<div><a href="{E(ctx.url("characters/leaderboards.html"))}">Leaderboards</a></div>',
        ])
        write_page(out, ctx, rec["name"], items_layout(sidebar, "\n".join(body_parts)))
