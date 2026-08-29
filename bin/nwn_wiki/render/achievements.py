"""players/achievements.html — the Hall of Fame.

The season-end Hall of Fame in nwn_homers_lotr_s1 was a trophy case built once,
by hand, after the season closed. This is the live version: awards computed from
data the build already has and recomputed on every refresh, so the board is
current while the season is still being played.

Two kinds of award, because two different questions are being asked:

  CHARACTER awards go to one build -- most kills, deepest pockets, hardest
  hitter. The winner is a character, and the player is named beside it.

  PLAYER awards are about the person, not a single build, so they are the
  AVERAGE across every character that player owns. A player with one
  min-maxed level 60 and nine level 1 rerolls should not beat someone whose
  whole stable is strong; averaging is what makes the award mean "this player
  builds well" rather than "this player has one good character".

Ties list every winner: one award, but everyone level with the leader holds it.
Admin and DM characters are excluded upstream and cannot appear here.
"""

from __future__ import annotations

from nwn_wiki.htmlgen.blocks import items_layout, toc_sidebar
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.render.leaderboards import dedupe
from nwn_wiki.render.players import player_link

from nwn_wiki import state

# (id, title, blurb, record key, formatter)
CHARACTER_AWARDS: list[tuple] = [
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
    ("dummy", "Hardest Hitter",
     "Best damage per round measured on the combat dummy.",
     "best_dpr", lambda v: f"{v:,.1f} dmg/round"),
]

PLAYER_AWARDS: list[tuple] = [
    ("well-rounded", "Well-Rounded",
     "Highest ability scores, averaged across all of this player's characters.",
     "avg_ability_total", lambda v: f"{v:,.1f} avg. total ability score"),
    ("dedicated", "Dedicated",
     "Most hours logged on the server. This is an account figure -- the server "
     "log records who logged in, never which character they chose.",
     "play_hours", lambda v: f"{v:g} hours"),
    ("polymath", "Polymath",
     "Most skill ranks, averaged across all of this player's characters.",
     "avg_skill_total", lambda v: f"{v:,.1f} avg. skill ranks"),
]


def _winners(rows: list[dict], key: str) -> list[dict]:
    """Every row tied for the highest value, or [] when nobody scores."""
    best = max((r.get(key) or 0 for r in rows), default=0)
    if best <= 0:
        return []
    return sorted((r for r in rows if (r.get(key) or 0) == best),
                  key=lambda r: r["name"].lower())


def compute_achievements() -> list[dict]:
    """Resolve every award to its winners, once.

    Kept separate from rendering because the players index needs the per-player
    medal count, and recomputing it there would let the two disagree.
    """
    characters = dedupe(state._CHARACTERS)
    players = state._PLAYERS
    out: list[dict] = []

    for aid, title, blurb, key, fmt in CHARACTER_AWARDS:
        winners = _winners(characters, key)
        if winners:
            out.append({"id": aid, "title": title, "blurb": blurb,
                        "kind": "character", "key": key, "fmt": fmt,
                        "winners": winners})

    for aid, title, blurb, key, fmt in PLAYER_AWARDS:
        winners = _winners(players, key)
        if winners:
            out.append({"id": aid, "title": title, "blurb": blurb,
                        "kind": "player", "key": key, "fmt": fmt,
                        "winners": winners})
    return out


def achievement_counts(awards: list[dict]) -> dict[str, int]:
    """Player name -> how many awards they hold.

    A character award counts for the player who owns that character, so the
    medal tally on the players index is "awards this person holds", however
    they were won.
    """
    counts: dict[str, int] = {}
    for award in awards:
        for w in award["winners"]:
            name = w["name"] if award["kind"] == "player" else w["player"]
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts


def render_achievements(out) -> None:
    awards = state._ACHIEVEMENTS
    if not awards:
        return
    ctx = PageCtx("players/achievements.html")

    cards, toc = [], []
    for award in awards:
        toc.append(f'<div><a href="#{award["id"]}">{E(award["title"])}</a></div>')
        lines = []
        for w in award["winners"]:
            if award["kind"] == "player":
                who = player_link(ctx, w["name"])
                by = ""
            else:
                href = ctx.dir_url("characters") + f"{w['slug']}.html"
                who = f'<a href="{E(href)}">{E(w["name"])}</a>'
                by = (f' <span class="muted">— {player_link(ctx, w["player"])}</span>'
                      if w["player"] else "")
            lines.append(
                f"<li>{who}{by} <b>{E(award['fmt'](w[award['key']]))}</b></li>")

        tie = (f' <small class="muted">({len(award["winners"])}-way tie)</small>'
               if len(award["winners"]) > 1 else "")
        kind = ("player" if award["kind"] == "player" else "character")
        cards.append(
            f'<div class="card" id="{award["id"]}">'
            f"<h2>{E(award['title'])}{tie}</h2>"
            f'<p class="muted">{E(award["blurb"])} '
            f'<span class="tag">{kind} award</span></p>'
            f"<ul>{''.join(lines)}</ul></div>"
        )

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

    n_players = sum(1 for a in awards if a["kind"] == "player")
    body = [
        "<h1>Hall of Fame</h1>",
        f"<p>{len(awards)} awards, recomputed every time the wiki is rebuilt. "
        f"{len(awards) - n_players} go to a single character; {n_players} are "
        "averaged across everything a player has built. Admin and DM characters "
        "are not eligible.</p>",
        *cards,
    ]
    write_page(out, ctx, "Hall of Fame",
               items_layout(toc_sidebar(sidebar_rows), "\n".join(body)))
