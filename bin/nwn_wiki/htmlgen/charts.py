"""Inline SVG charts, built with plain stdlib string formatting.

The wiki ships no charting library: every chart on the site is an SVG string
assembled here, so a page carries its charts in its own markup with nothing to
load and nothing to run. These began life inside the player-activity page and
moved out when the character and player pages wanted the same charts drawn the
same way -- a chart on a character page and a chart on ``activity.html`` are
the same object, and they stay that way by construction.

Styling is inline on the ``<svg>`` element rather than in ``wiki_assets/style.css``
because these strings are also written into pages by ``bin/nwn-wiki-activity``,
which runs without the full build's stylesheet knowledge.
"""

from __future__ import annotations

import html
import math
from datetime import timedelta
from typing import Any


def weekly_rollup(days: list, value_of, combine) -> tuple[list[str], list]:
    """Roll a per-day series up into Mon-anchored weeks.

    `value_of(day)` yields that day's value; `combine(list)` reduces a week's
    worth of values (sum for hours, max for a peak). Returns (labels, values).
    """
    buckets: dict = {}
    for d in days:
        wk = d - timedelta(days=d.weekday())
        buckets.setdefault(wk, []).append(value_of(d))
    weeks = sorted(buckets)
    return (
        [w.strftime("%b %-d") for w in weeks],
        [combine(buckets[w]) for w in weeks],
    )


def _nice_upper(val: float) -> float:
    """Round val up to a visually nice axis maximum."""
    if val <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(val))
    norm = val / exp
    for nice in (1, 2, 2.5, 5, 10):
        if nice >= norm:
            return nice * exp
    return float(val)


def _fmt_num(val: float) -> str:
    if val == int(val):
        return str(int(val))
    return f"{val:.1f}" if val < 10 else str(int(round(val)))


def _se(s: Any) -> str:
    """html.escape shorthand for SVG text content."""
    return html.escape(str(s))


def svg_vbar_chart(
    labels: list[str], values: list[float], title: str,
    ylabel: str = "", bar_color: str = "#6b3a1c",
    width: int = 700, height: int = 270,
    rotate_labels: bool = False,
) -> str:
    """Vertical bar chart returned as an inline SVG string."""
    mt, mb = 32, (72 if rotate_labels else 50)
    ml, mr = 55, 20
    pw, ph = width - ml - mr, height - mt - mb
    n = len(labels)
    if n == 0:
        return f'<svg width="{width}" height="{height}"><text x="10" y="20">No data</text></svg>'
    y_max = _nice_upper(max(values) if values else 1)
    bar_w = max(2.0, pw / n * 0.65)
    bar_gap = pw / n
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="font-family:Georgia,serif;background:#fff;'
        f'border:1px solid #d6d2c4;border-radius:4px;display:block;">'
    ]
    out.append(
        f'<text x="{width/2:.1f}" y="22" text-anchor="middle" '
        f'font-size="13" fill="#6b3a1c">{_se(title)}</text>'
    )
    if ylabel:
        cy = mt + ph / 2
        out.append(
            f'<text x="13" y="{cy:.1f}" text-anchor="middle" font-size="11" fill="#6b6b6b" '
            f'transform="rotate(-90 13 {cy:.1f})">{_se(ylabel)}</text>'
        )
    for i in range(6):
        tv = y_max * i / 5
        y = mt + ph - (tv / y_max) * ph
        out.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+pw}" y2="{y:.1f}" '
            f'stroke="#d6d2c4" stroke-width="1" stroke-dasharray="3,3"/>'
        )
        out.append(
            f'<text x="{ml-6}" y="{y+4:.1f}" text-anchor="end" '
            f'font-size="10" fill="#6b6b6b">{_fmt_num(tv)}</text>'
        )
    out.append(
        f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ph}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )
    out.append(
        f'<line x1="{ml}" y1="{mt+ph}" x2="{ml+pw}" y2="{mt+ph}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )
    for i, (lbl, val) in enumerate(zip(labels, values)):
        bx = ml + i * bar_gap + (bar_gap - bar_w) / 2
        bh = (val / y_max) * ph
        by = mt + ph - bh
        out.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
            f'fill="{bar_color}" opacity="0.82"/>'
        )
        if val > 0:
            out.append(
                f'<text x="{bx+bar_w/2:.1f}" y="{by-3:.1f}" text-anchor="middle" '
                f'font-size="10" fill="{bar_color}">{_fmt_num(val)}</text>'
            )
        cx = bx + bar_w / 2
        if rotate_labels:
            out.append(
                f'<text x="{cx:.1f}" y="{mt+ph+10}" text-anchor="end" font-size="10" '
                f'fill="#6b6b6b" transform="rotate(-45 {cx:.1f} {mt+ph+10})">'
                f'{_se(lbl)}</text>'
            )
        else:
            out.append(
                f'<text x="{cx:.1f}" y="{mt+ph+16}" text-anchor="middle" '
                f'font-size="11" fill="#6b6b6b">{_se(lbl)}</text>'
            )
    out.append('</svg>')
    return "\n".join(out)


def svg_hbar_chart(
    labels: list[str], values: list[float], title: str,
    xlabel: str = "", bar_color: str = "#6b3a1c",
) -> str:
    """Horizontal bar chart returned as an inline SVG string."""
    bar_h, bar_gap = 22, 30
    n = len(labels)
    width = 700
    ml, mr, mt, mb = 130, 65, 34, 38
    height = mt + n * bar_gap + mb
    pw = width - ml - mr
    x_max = _nice_upper(max(values) if values else 1)
    y_bot = mt + n * bar_gap
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="font-family:Georgia,serif;background:#fff;'
        f'border:1px solid #d6d2c4;border-radius:4px;display:block;">'
    ]
    out.append(
        f'<text x="{width/2:.1f}" y="24" text-anchor="middle" '
        f'font-size="13" fill="#6b3a1c">{_se(title)}</text>'
    )
    for i in range(6):
        tv = x_max * i / 5
        x = ml + (tv / x_max) * pw
        out.append(
            f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{y_bot}" '
            f'stroke="#d6d2c4" stroke-width="1" stroke-dasharray="3,3"/>'
        )
        out.append(
            f'<text x="{x:.1f}" y="{y_bot+14}" text-anchor="middle" '
            f'font-size="10" fill="#6b6b6b">{_fmt_num(tv)}</text>'
        )
    if xlabel:
        out.append(
            f'<text x="{ml+pw/2:.1f}" y="{y_bot+30}" text-anchor="middle" '
            f'font-size="11" fill="#6b6b6b">{_se(xlabel)}</text>'
        )
    out.append(
        f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{y_bot}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )
    out.append(
        f'<line x1="{ml}" y1="{y_bot}" x2="{ml+pw}" y2="{y_bot}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )
    for i, (lbl, val) in enumerate(zip(labels, values)):
        by = mt + i * bar_gap + (bar_gap - bar_h) / 2
        bw = (val / x_max) * pw
        out.append(
            f'<rect x="{ml}" y="{by:.1f}" width="{bw:.1f}" height="{bar_h}" '
            f'fill="{bar_color}" opacity="0.82"/>'
        )
        if val > 0:
            out.append(
                f'<text x="{ml+bw+5:.1f}" y="{by+bar_h/2+4:.1f}" '
                f'font-size="10" fill="{bar_color}">{_fmt_num(val)}</text>'
            )
        out.append(
            f'<text x="{ml-8}" y="{by+bar_h/2+4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#1f1f1f">{_se(lbl)}</text>'
        )
    out.append('</svg>')
    return "\n".join(out)


def svg_line_chart(
    labels: list[str], values: list[float], title: str,
    ylabel: str = "", line_color: str = "#5a2b78",
    width: int = 700, height: int = 270,
    rotate_labels: bool = False,
) -> str:
    """Line chart returned as an inline SVG string.

    Same axes, gridlines, margins and label handling as :func:`svg_vbar_chart`,
    so the two read as one chart family when a page shows both.

    A day with no play is plotted as zero, not as a break in the line: the
    series is a continuous calendar, and a gap would read as "not measured"
    when what actually happened is "measured, and nobody logged in". Labels are
    thinned on a crowded axis -- every point still gets its own vertex, only
    the tick text is dropped, so the shape of the line is never approximated.
    """
    mt, mb = 32, (72 if rotate_labels else 50)
    ml, mr = 55, 20
    pw, ph = width - ml - mr, height - mt - mb
    n = len(labels)
    if n == 0:
        return f'<svg width="{width}" height="{height}"><text x="10" y="20">No data</text></svg>'
    y_max = _nice_upper(max(values) if values else 1)
    # One point sits mid-plot rather than hard against the y-axis, which would
    # read as an axis tick rather than a measurement.
    step = pw / (n - 1) if n > 1 else 0.0
    x_of = (lambda i: ml + i * step) if n > 1 else (lambda i: ml + pw / 2)
    y_of = lambda v: mt + ph - (v / y_max) * ph

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="font-family:Georgia,serif;background:#fff;'
        f'border:1px solid #d6d2c4;border-radius:4px;display:block;">'
    ]
    out.append(
        f'<text x="{width/2:.1f}" y="22" text-anchor="middle" '
        f'font-size="13" fill="#6b3a1c">{_se(title)}</text>'
    )
    if ylabel:
        cy = mt + ph / 2
        out.append(
            f'<text x="13" y="{cy:.1f}" text-anchor="middle" font-size="11" fill="#6b6b6b" '
            f'transform="rotate(-90 13 {cy:.1f})">{_se(ylabel)}</text>'
        )
    for i in range(6):
        tv = y_max * i / 5
        y = mt + ph - (tv / y_max) * ph
        out.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+pw}" y2="{y:.1f}" '
            f'stroke="#d6d2c4" stroke-width="1" stroke-dasharray="3,3"/>'
        )
        out.append(
            f'<text x="{ml-6}" y="{y+4:.1f}" text-anchor="end" '
            f'font-size="10" fill="#6b6b6b">{_fmt_num(tv)}</text>'
        )
    out.append(
        f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ph}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )
    out.append(
        f'<line x1="{ml}" y1="{mt+ph}" x2="{ml+pw}" y2="{mt+ph}" '
        f'stroke="#9a9a9a" stroke-width="1.5"/>'
    )

    pts = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in enumerate(values))
    out.append(
        f'<polyline points="{pts}" fill="none" stroke="{line_color}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
    )
    # Point markers stay legible up to a few dozen days; past that they merge
    # into the line and are dropped rather than drawn as a smear.
    if n <= 60:
        for i, v in enumerate(values):
            out.append(
                f'<circle cx="{x_of(i):.1f}" cy="{y_of(v):.1f}" r="2.5" '
                f'fill="{line_color}"/>'
            )

    # Thin the tick text so labels never overlap: rotated labels need ~14px of
    # horizontal room, upright ones ~45px.
    min_px = 14 if rotate_labels else 45
    every = max(1, math.ceil(min_px / step)) if step else 1
    for i, lbl in enumerate(labels):
        if i % every and i != n - 1:
            continue
        cx = x_of(i)
        if rotate_labels:
            out.append(
                f'<text x="{cx:.1f}" y="{mt+ph+10}" text-anchor="end" font-size="10" '
                f'fill="#6b6b6b" transform="rotate(-45 {cx:.1f} {mt+ph+10})">'
                f'{_se(lbl)}</text>'
            )
        else:
            out.append(
                f'<text x="{cx:.1f}" y="{mt+ph+16}" text-anchor="middle" '
                f'font-size="11" fill="#6b6b6b">{_se(lbl)}</text>'
            )
    out.append('</svg>')
    return "\n".join(out)
