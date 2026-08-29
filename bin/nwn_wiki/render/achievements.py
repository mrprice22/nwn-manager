"""players/achievements.html — the Hall of Fame.

The season-end Hall of Fame in nwn_homers_lotr_s1 was a trophy case built once,
by hand, from a 650-line award file with curated slayer and collector tables.
This is the live, mid-season version: awards that can be computed from data the
build already has, recomputed on every wiki refresh, so the board is current
while the season is still being played rather than only after it ends.

Every award names a character and, where the roster could resolve one, links to
the player behind it -- which is the point of the page: it ties the several
characters one person plays back to the person.

Ties are listed in full: the admin's rule from season 1 is one winner per award,
but everyone tied for first is a winner.
"""

from __future__ import annotations

from nwn_wiki.htmlgen.blocks import items_layout, toc_sidebar
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.render.leaderboards import dedupe
from nwn_wiki.render.players import player_link

from nwn_wiki import state

# (id, title, blurb, record key, value formatter)
# Every award is "highest wins" over a single record field, which keeps the board
# honest: each number on it is the same number the character's own page shows.
AWARDS: list[tuple] = [
    ("slayer", "Slayer of Middle-earth", "Most creatures killed.",
     "kills_total", lambda v: f"{v:,} kills"),
    ("naturalist", "Naturalist", "Most distinct creature types killed.",
     "unique_creatures", lambda v: f"{v:,} species"),
    ("lone-wolf", "Lone Wolf", "Most kills taken solo, without a party.",
     "kills_solo", lambda v: f"{v:,} solo kills"),
    ("comrade", "Shoulder to Shoulder", "Most kills made as part of a party.",
     "kills_party", lambda v: f"{v:,} party kills"),
    ("magnate", "Magnate", "Most gold carried.",
     "gold", lambda v: f"{v:,} gp"),
    ("veteran", "Veteran", "Most experience earned.",
     "xp", lambda v: f"{v:,} xp"),
    ("ironhide", "Ironhide", "Highest maximum hit points.",
     "max_hp", lambda v: f"{v:,} hp"),
]


def _winners(records: list[dict], key: str) -> list[dict]:
    """Every record tied for the highest value, or [] when nobody scores."""
    best = max((r.get(key) or 0 for r in records), default=0)
    if best <= 0:
        return []
    return sorted((r for r in records if (r.get(key) or 0) == best),
                  key=lambda r: r["name"].lower())


def render_achievements(out) -> None:
    records = dedupe(state._CHARACTERS)
    if not records:
        return
    ctx = PageCtx("players/achievements.html")

    cards, toc = [], []
    for aid, title, blurb, key, fmt in AWARDS:
        winners = _winners(records, key)
        if not winners:
            continue
        toc.append(f'<div><a href="#{aid}">{E(title)}</a></div>')

        lines = []
        for rec in winners:
            href = ctx.dir_url("characters") + f"{rec['slug']}.html"
            who = f'<a href="{E(href)}">{E(rec["name"])}</a>'
            by = (f' <span class="muted">— {player_link(ctx, rec["player"])}</span>'
                  if rec["player"] else "")
            lines.append(f"<li>{who}{by} <b>{E(fmt(rec[key]))}</b></li>")

        tie = (f' <small class="muted">({len(winners)}-way tie)</small>'
               if len(winners) > 1 else "")
        cards.append(
            f'<div class="card" id="{aid}">'
            f"<h2>{E(title)}{tie}</h2>"
            f'<p class="muted">{E(blurb)}</p>'
            f"<ul>{''.join(lines)}</ul></div>"
        )

    if not cards:
        return

    sidebar_rows = ['<div class="toc-group-heading">Players</div>']
    if state._ONLINE_API:
        sidebar_rows.append(
            f'<div><a href="{E(ctx.url("characters/online.html"))}">'
            "Who&#x27;s Online</a></div>")
    sidebar_rows += [
        f'<div><a href="{E(ctx.url("players/index.html"))}">Players</a></div>',
        f'<div><a href="{E(ctx.url("characters/index.html"))}">Characters</a></div>',
        f'<div><a href="{E(ctx.url("characters/leaderboards.html"))}">Leaderboards</a></div>',
        '<div class="toc-group-heading">Awards</div>',
        *toc,
    ]

    body = [
        "<h1>Hall of Fame</h1>",
        f"<p>One winner per award across {len(records):,} characters, "
        "recomputed every time the wiki is rebuilt. Admin and DM characters are "
        "not eligible.</p>",
        *cards,
    ]
    write_page(out, ctx, "Hall of Fame",
               items_layout(toc_sidebar(sidebar_rows), "\n".join(body)))
