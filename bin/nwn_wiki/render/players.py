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

from datetime import timedelta

from nwn_wiki.htmlgen.blocks import items_layout, toc_sidebar
from nwn_wiki.htmlgen.charts import svg_line_chart, svg_vbar_chart, weekly_rollup
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E
from nwn_wiki.htmlgen.pagectx import PageCtx
from nwn_wiki.render.activity import (DAILY_CUTOFF, DAILY_WINDOW, DOW_ORDER,
                                      session_hours)

from nwn_wiki import state

# The purple the activity page uses for play-time, so a chart means the same
# thing wherever it appears on the site.
PLAY_COLOR = "#5a2b78"


def _date(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


def tracking_note(prefix: str = "Play time per character is counted from") -> str:
    """The "counted since" caveat, for any page showing per-character hours.

    Tracking began when ptm_db shipped, long after the season did, so a
    character's hours are not its history -- they are its history *since then*.
    Every page showing the figure carries this, because the number is
    misleading without it.
    """
    if not state._PLAYTIME_SINCE:
        return ""
    return (f'<p class="muted">{E(prefix)} '
            f"<b>{E(state._PLAYTIME_SINCE)}</b>, when tracking was switched on. "
            "Time played before that date was never recorded and is not "
            "included, so these totals are lower than a character\u2019s real "
            "history.</p>")


def _dpr(v) -> str:
    """Damage per round, or an em dash when this character never hit the dummy."""
    return f"{v:,.1f}" if v else "—"


def daily_hours_charts(date_hours: dict, title: str,
                       weekly_title: str = "") -> str:
    """A play-hours-per-day line, plus a weekly line once history outgrows it.

    ``date_hours`` maps :class:`datetime.date` to hours. Absent days are drawn
    as zero rather than skipped -- the x-axis is a calendar, and a line that
    hops over an empty week would misrepresent a quiet fortnight as continuous
    play.

    Returns "" when there is nothing to draw, so a caller can concatenate it
    unconditionally.
    """
    days = sorted(d for d in date_hours if d > DAILY_CUTOFF)
    if not days:
        return ""
    # Fill the calendar between the first and last day with actual data, so
    # gaps read as gaps.
    span = [days[0] + timedelta(days=i) for i in range((days[-1] - days[0]).days + 1)]
    recent = span[-DAILY_WINDOW:]

    out = ['<div style="overflow-x:auto;">' + svg_line_chart(
        [d.strftime("%b %-d") for d in recent],
        [round(date_hours.get(d, 0.0), 2) for d in recent],
        title, ylabel="hours",
        width=max(700, len(recent) * 20 + 80), height=270,
        rotate_labels=True, line_color=PLAY_COLOR,
    ) + "</div>"]

    if len(span) > DAILY_WINDOW:
        wk_labels, wk_values = weekly_rollup(
            span, lambda d: date_hours.get(d, 0.0), lambda vs: round(sum(vs), 2))
        out.append('<div style="overflow-x:auto;">' + svg_line_chart(
            wk_labels, wk_values,
            weekly_title or (title + " (weekly, week beginning)"),
            ylabel="hours",
            width=max(700, len(wk_labels) * 20 + 80), height=270,
            rotate_labels=True, line_color=PLAY_COLOR,
        ) + "</div>")
    return "\n".join(out)


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


# =============================================================================
# Ideas and testing (from the project's roadmap credit sidecar)
# =============================================================================

# Roadmap idea types, in the words this wiki uses for them. The roadmap calls
# them Defect/Enhancement/Exploit; players call them bugs and features, and the
# player pages are written for players.
IDEA_LABELS = {
    "Defect": ("Bugs", "Bugs this player reported"),
    "Enhancement": ("Features", "Features and improvements this player suggested"),
    "Exploit": ("Exploits", "Exploits this player reported"),
    "Testing": ("Testing", "Shipped changes this player helped test"),
}
IDEA_COLUMNS = ("Defect", "Enhancement", "Exploit", "Testing")


def _idea_types() -> list[str]:
    """The type keys the sidecar declares, in its own order."""
    meta = state._ROADMAP or {}
    return [t["key"] for t in (meta.get("types") or []) if t.get("key")]


def _roadmap_url(ctx: PageCtx) -> str:
    return ctx.url((state._ROADMAP or {}).get("page") or "manual/Roadmap.html")


def _idea_link(ctx: PageCtx, idea: dict) -> str:
    """An idea title linking to its own entry on the roadmap page.

    Deliberately just the title: the roadmap page holds the notes, the
    discussion and the implementation write-up, and duplicating any of that
    here would leave two copies to keep in step. This is an index into it, the
    same way the roadmap's own first half indexes its second.
    """
    return (f'<a href="{E(_roadmap_url(ctx))}#{E(idea["anchor"])}">'
            f'{E(idea["title"])}</a>')


def _idea_pivot(rec: dict) -> str:
    """The roadmap's type x lifecycle grid, for one player.

    Same shape and same numbers as ``render_summary_pivot`` on the roadmap page
    -- the buckets come from the sidecar, which the roadmap generator fills from
    the same function it renders its own pivot with. Rendered as a plain
    ``table.data`` because the roadmap's ``.rm-pivot`` styling lives in that
    page's own inline stylesheet and is not part of the site's CSS.
    """
    meta = state._ROADMAP or {}
    cols = list(meta.get("columns") or [])
    # "" is the untyped bucket -- kept last, and only when this player has any.
    types = [t for t in list(_idea_types()) + [""]
             if any(rec["pivot"].get((t, c)) for c in cols)]
    if not types or not cols:
        return ""

    head = "".join(f"<th>{E(c)}</th>" for c in cols)
    rows, col_tot = [], dict.fromkeys(cols, 0)
    for t in types:
        cells, rtot = [], 0
        for c in cols:
            n = rec["pivot"].get((t, c), 0)
            rtot += n
            col_tot[c] += n
            cells.append(f"<td>{n or '&mdash;'}</td>")
        label = IDEA_LABELS.get(t, (t or "Unclassified", ""))[0]
        rows.append(f'<tr><th scope="row">{E(label)}</th>'
                    f'{"".join(cells)}<td><b>{rtot}</b></td></tr>')
    grand = sum(col_tot.values())
    rows.append('<tr><th scope="row">Total</th>'
                + "".join(f"<td><b>{col_tot[c]}</b></td>" for c in cols)
                + f"<td><b>{grand}</b></td></tr>")
    return ('<table class="data"><thead><tr><th></th>'
            f"{head}<th>Total</th></tr></thead>"
            f'<tbody>{"".join(rows)}</tbody></table>')


def _idea_group_list(ctx: PageCtx, ideas: list[dict]) -> str:
    """Ideas grouped under their status heading, in the roadmap's own order."""
    meta = (state._ROADMAP or {}).get("statuses") or {}
    groups: dict[str, list[dict]] = {}
    for i in ideas:
        groups.setdefault(i.get("status") or "", []).append(i)
    order = sorted(groups, key=lambda st: (meta.get(st, {}).get("rank", 99), st))

    out = []
    for st in order:
        label = (meta.get(st) or {}).get("label") or st
        items = "".join(
            f"<li>{_idea_link(ctx, i)}"
            + (f' <span class="muted">&mdash; '
               f'{E(IDEA_LABELS.get(i.get("type"), (i.get("type") or "", ""))[0])}'
               "</span>" if i.get("type") else "")
            + "</li>"
            for i in groups[st])
        out.append(f'<h3>{E(label)} <small class="muted">({len(groups[st])})</small></h3>'
                   f"<ul>{items}</ul>")
    return "\n".join(out)


def _ideas_section(ctx: PageCtx, name: str) -> str:
    """The whole "Ideas &amp; Testing" block for one player, or ""."""
    rec = (state._PLAYER_IDEAS or {}).get(name)
    if not rec or not (rec["submitted"] or rec["tested"]):
        return ""

    parts = [f'<h2>Ideas &amp; Testing <small class="muted">'
             f'({len(rec["submitted"])} submitted)</small></h2>']
    pivot = _idea_pivot(rec)
    if pivot:
        parts.append(pivot)
    if rec["submitted"]:
        parts.append(_idea_group_list(ctx, rec["submitted"]))
    if rec["tested"]:
        parts.append(f'<h3>Tested <small class="muted">({len(rec["tested"])})</small></h3>'
                     "<ul>"
                     + "".join(f"<li>{_idea_link(ctx, i)}</li>" for i in rec["tested"])
                     + "</ul>")
    parts.append(f'<p class="muted">Every idea here is listed in full, with '
                 f'notes and progress, on the <a href="{E(_roadmap_url(ctx))}">'
                 "roadmap</a>.</p>")
    return "\n".join(parts)


def _activity_section(name: str, sessions: list[dict]) -> str:
    """One player's own copy of the activity charts, minus the crowd ones.

    Peak-concurrency has no meaning for a single player -- it would be a chart
    of 1s -- so the activity page keeps those and these do not.
    """
    if not sessions:
        return ""
    date_hours, hour_hours, dow_hours = session_hours(sessions)
    if not date_hours:
        return ""

    parts = ["<h2>Activity</h2>",
             daily_hours_charts(date_hours, "Play-hours per day",
                                "Play-hours per week (week beginning)")]
    parts.append('<div style="overflow-x:auto;">' + svg_vbar_chart(
        [str(h) for h in range(24)],
        [round(hour_hours.get(h, 0.0), 2) for h in range(24)],
        f"Play-hours by hour of day ({state._TZ_LABEL})",
        ylabel="hours", width=700, height=260, bar_color=PLAY_COLOR,
    ) + "</div>")
    parts.append('<div style="overflow-x:auto;">' + svg_vbar_chart(
        list(DOW_ORDER),
        [round(dow_hours.get(d, 0.0), 2) for d in DOW_ORDER],
        "Play-hours by day of week",
        ylabel="hours", width=500, height=240, bar_color=PLAY_COLOR,
    ) + "</div>")
    parts.append('<p class="muted">Hours are per account, from the server log: '
                 "it records who logged in, never which character they chose.</p>")
    return "\n".join(parts)


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

    # The idea columns only exist when the project handed us a roadmap credit
    # sidecar (--roadmap-credits); every other module renders the table it
    # always did.
    ideas = state._PLAYER_IDEAS or {}

    rows = []
    for p in ranked:
        won = medals.get(p["name"], 0)
        dpr = _dpr(p.get("best_dpr"))
        idea_cells = ""
        if ideas:
            counts = (ideas.get(p["name"]) or {}).get("counts") or {}
            idea_cells = "".join(f"<td>{counts.get(k) or '—'}</td>"
                                 for k in IDEA_COLUMNS)
        rows.append(
            "<tr>"
            f'<td><a href="{E(p["slug"])}.html">{E(p["name"])}</a></td>'
            f"<td>{p['character_count']}</td>"
            f"<td>{won or '—'}</td>"
            f"<td>{p['kills_total']:,}</td>"
            f"<td>{p['unique_creatures']:,}</td>"
            f"<td>{p['best_bestiary']:,}</td>"
            f"<td>{dpr}</td>"
            f"{idea_cells}"
            f"<td>{p['play_hours']:g}</td>"
            f"<td>{_date(p['last_seen'])}</td>"
            "</tr>"
        )
    idea_heads = "".join(
        f'<th title="{E(IDEA_LABELS[k][1])}">{E(IDEA_LABELS[k][0])}</th>'
        for k in IDEA_COLUMNS) if ideas else ""
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
        + idea_heads +
        "<th>Hours</th><th>Last Seen</th>"
        "</tr></thead><tbody>",
        "\n".join(rows),
        "</tbody></table>",
        '<p class="muted">Bestiary (all) is the union across a player\u2019s '
        "characters, so it is at least as large as their best single character. "
        "Hours are per account: the server log records who logged in, never "
        "which character they chose.</p>",
        ('<p class="muted">Bugs, features and exploits count every idea the '
         "player has submitted, whatever became of it; Testing counts the "
         "changes they helped verify. Each player\u2019s page breaks their "
         "ideas down by status, and the "
         f'<a href="{E(ctx.url("manual/Roadmap.html"))}">roadmap</a> has them '
         "in full.</p>") if ideas else "",
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
            # Ideas first, then the charts: what a player has put into the
            # server reads better before how long they have been on it.
            _ideas_section(ctx, p["name"]),
            _activity_section(p["name"],
                              (state._PLAYER_SESSIONS or {}).get(p["name"]) or []),
        ]
        write_page(out, ctx, p["name"],
                   items_layout(_sidebar(ctx), "\n".join(x for x in body if x)))
