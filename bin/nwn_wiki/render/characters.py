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
from nwn_wiki.itemprops import _prop_value_num, itemprop_format
from nwn_wiki.lookups import baseitem_name, feat_name, skill_name
from nwn_wiki.render.players import player_link

from nwn_wiki import state

ABILITY_ORDER = ("Str", "Dex", "Con", "Int", "Wis", "Cha")


def _date(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


def _dpr(v) -> str:
    """Damage per round, or an em dash when this character never hit the dummy."""
    return f"{v:,.1f}" if v else "—"


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
            f"<td>{_dpr(r.get('best_dpr'))}</td>"
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
        "<th>Race</th><th>Kills</th>"
        '<th title="Best damage per round measured on the combat dummy">'
        "Best DPR</th>"
        "<th>Last Seen</th>"
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

    # Link index: character/player name -> this wiki's own page for them.
    #
    # The roster is fetched at read time and may describe a DIFFERENT realm --
    # the TEST wiki deliberately shows who is on the live server. So a name is
    # linked only when THIS wiki actually has a page for it; on the TEST wiki
    # the live season's players simply render as plain text, which is why their
    # names are not clickable there and are on the live wiki.
    import json as _json
    index = {
        "characters": {r["name"]: f"{r['slug']}.html" for r in state._CHARACTERS},
        "players": {p["name"]: ctx.dir_url("players") + f"{p['slug']}.html"
                    for p in state._PLAYERS},
    }

    body = "\n".join([
        "<h1>Who's Online</h1>",
        f'<div data-online-roster data-online-url="{E(state._ONLINE_API)}">',
        '<p class="muted">Live status is unavailable right now.</p>',
        "</div>",
        '<script type="application/json" id="online-link-index">'
        + _json.dumps(index).replace("<", "\\u003c") + "</script>",
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


def _is_gear(item: dict) -> bool:
    """False for the engine's invisible creature-slot items.

    Every PC wears an "x3_it_pchide" ("PC Properties") in a creature-item slot,
    and monsters carry creature weapons the same way. They are engine plumbing,
    not equipment, and listing them as gear is simply wrong. Matched on the base
    item's own label so a HAK adding creature slots is covered too.
    """
    return not baseitem_name(item.get("base")).lower().startswith("creature")


def _item_name(item: dict) -> str:
    """Display name: the instance's own, else the blueprint's, else its type.

    A stock item carries no LocalizedName on the instance -- its name is a TLK
    StrRef resolved through the blueprint -- so the .bic alone yields "".
    """
    name = item.get("display") or ""
    if not name:
        name = state._ITEM_NAMES.get(item.get("resref") or "", "")
    if not name:
        name = baseitem_name(item.get("base")) or "(unnamed item)"
    return name


def _item_cell(item: dict, ctx: PageCtx) -> str:
    """An equipped item, linked to its wiki page when that page exists.

    A character carries an *instantiated* item, whose TemplateResRef names the
    blueprint the item pages are keyed by. The link is emitted only when that
    page was actually written (state._ITEM_PAGES): a character can be wearing
    something crafted, renamed, or from a build the wiki no longer ships, and a
    link to a 404 is worse than plain text.
    """
    name = _item_name(item)
    rr = item.get("resref") or ""
    if rr and rr in state._ITEM_PAGES:
        href = ctx.dir_url("items") + f"{rr}.html"
        return f'<a href="{E(href)}">{E(name)}</a>'
    return E(name)


def _equipped_list(rec: dict, ctx: PageCtx) -> str:
    """Equipped gear, each linked to its item page where one exists."""
    items = [i for i in rec["equipped"] if _is_gear(i)]
    if not items:
        return '<p class="muted">Nothing equipped.</p>'
    items.sort(key=lambda i: _item_name(i).lower())
    return ("<ul>" + "".join(f"<li>{_item_cell(i, ctx)}</li>" for i in items)
            + "</ul>")


# Ability Bonus is item property 0; its subtype names the ability.
_ABILITY_PROP = 0
_ABILITY_BY_SUBTYPE = {"Strength": "Str", "Dexterity": "Dex",
                       "Constitution": "Con", "Intelligence": "Int",
                       "Wisdom": "Wis", "Charisma": "Cha"}


def _ability_totals(rec: dict) -> dict[str, tuple[int, int]]:
    """Ability -> (raw total from items, total after the module's cap).

    Ability bonuses from DIFFERENT items stack, and the engine then clamps the
    total per ability to settings.tml's max-ability-bonus -- NWN's default is
    +12, this module runs +24. So two +12 rings reach a +24 cap where one
    cannot, which is why this sums and then clamps rather than taking the
    largest single item. Same rule, and the same reasoning, as nwn_wiki/sim/pc.py.
    """
    raw: dict[str, int] = {}
    for item in rec["equipped"]:
        if not _is_gear(item):
            continue
        for prop in item.get("properties") or []:
            f = itemprop_format(prop)
            if f.get("property") != "Ability Bonus":
                continue
            ab = _ABILITY_BY_SUBTYPE.get(f.get("subtype", ""))
            if not ab:
                continue
            raw[ab] = raw.get(ab, 0) + (_prop_value_num(f["cost"]) if f["cost"] else 0)

    cap = state._MAX_ABILITY_BONUS
    return {ab: (v, min(v, cap) if cap and cap > 0 else v)
            for ab, v in raw.items()}


def _ability_summary(rec: dict) -> str:
    totals = _ability_totals(rec)
    if not totals:
        return ""
    cap = state._MAX_ABILITY_BONUS
    cells, capped = [], False
    for ab in ABILITY_ORDER:
        if ab not in totals:
            continue
        rawv, eff = totals[ab]
        note = ""
        if rawv != eff:
            capped = True
            note = f' <span class="muted">(+{rawv} before the cap)</span>'
        cells.append(f"<div><dt>{ab}</dt><dd>+{eff}{note}</dd></div>")
    foot = (f"Bonuses from different items stack, then cap at +{cap} per "
            "ability on this module.") if cap and cap > 0 else (
            "Bonuses from different items stack; this module sets no cap.")
    if capped:
        foot += " Values above the cap are shown as granted and as applied."
    return ('<div class="card"><h2>Ability Bonuses from Gear</h2>'
            f'<dl class="kv abilities">{"".join(cells)}</dl>'
            f'<p class="muted">{E(foot)}</p></div>')


def _combined_properties(rec: dict, ctx: PageCtx) -> str:
    """Every item property across the character's equipped gear, in one table.

    This is the question a player actually asks -- "what is all my gear doing
    for me?" -- which no single item page can answer. Identical properties from
    different items are grouped so the source of each is visible, because two
    +2 rings are a very different thing from one +4 one.

    Deliberately NOT summed. Stacking in NWN is per-property and full of special
    cases (same-type bonuses do not stack, damage bonuses do, immunities cap),
    so a single headline number would be confidently wrong. Listing every
    property with its sources is honest and still answers the question.
    """
    rows: dict[tuple, list[str]] = {}
    for item in rec["equipped"]:
        if not _is_gear(item):
            continue
        name = _item_name(item)
        for prop in item.get("properties") or []:
            f = itemprop_format(prop)
            key = (f.get("property", ""), f.get("subtype", ""),
                   f.get("cost", ""), f.get("param", ""))
            if not key[0]:
                continue
            rows.setdefault(key, []).append(name)

    if not rows:
        return ""

    out = []
    for (prop, subtype, cost, param), sources in sorted(
            rows.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1].lower())):
        detail = " ".join(x for x in (subtype, cost, param) if x)
        srcs = ", ".join(E(s) for s in sorted(sources))
        count = f' <span class="muted">x{len(sources)}</span>' if len(sources) > 1 else ""
        out.append(
            f"<tr><td>{E(prop)}{count}</td><td>{E(detail)}</td>"
            f'<td class="muted">{srcs}</td></tr>'
        )

    return (
        '<h2>Combined Gear Properties '
        f'<small class="muted">({len(out)})</small></h2>'
        '<p class="muted">Every property on the equipped gear above, and which '
        "item it comes from. Not summed \u2014 NWN stacking rules are "
        "per-property, so a single total would be misleading.</p>"
        '<table class="data"><thead><tr>'
        "<th>Property</th><th>Value</th><th>From</th>"
        "</tr></thead><tbody>" + "\n".join(out) + "</tbody></table>"
    )


def render_character_pages(out) -> None:
    """characters/<slug>.html — one page per character."""
    for rec in state._CHARACTERS:
        ctx = PageCtx(f"characters/{rec['slug']}.html")

        head = [f"<h1>{E(rec['name'])}</h1>"]
        if rec["player"]:
            head.append('<p class="muted">Played by '
                        f'{player_link(ctx, rec["player"])}</p>')

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

        dummy = rec.get("best_dpr")
        dummy_html = ""
        if dummy:
            dummy_html = (
                '<div class="card"><h2>Combat Dummy</h2>'
                f'<dl class="kv"><div><dt>Best damage/round</dt>'
                f"<dd>{dummy:,.1f}</dd></div></dl>"
                '<p class="muted">The best of this character\u2019s 10-round '
                "trials on the combat dummy, not an average: a trial can be cut "
                "short or spent testing a gear swap.</p></div>")

        body_parts = [
            *head,
            '<div class="card"><h2>Character</h2>'
            f'<dl class="kv">{fact_html}</dl></div>',
            '<div class="card"><h2>Abilities</h2>' + _stat_block(rec) + "</div>",
            '<div class="card"><h2>Bestiary</h2>'
            f'<dl class="kv">{kill_html}</dl></div>',
            dummy_html,
            "<h2>Equipped</h2>" + _equipped_list(rec, ctx),
        ]
        abilities = _ability_summary(rec)
        if abilities:
            body_parts.append(abilities)
        combined = _combined_properties(rec, ctx)
        if combined:
            body_parts.append(combined)
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
