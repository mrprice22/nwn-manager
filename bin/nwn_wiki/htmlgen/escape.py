"""HTML escaping and NWN colour-token rendering.

``E()`` is the escape used by every page builder; the ``nwn_*`` helpers turn
the ``<cRGB>``/``</c>`` tokens embedded in module text into either coloured
HTML spans or plain text.

Leaf module: stdlib only -- nothing here may import another nwn_wiki module.
"""

from __future__ import annotations

import html
import re
from typing import Any


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def E(s: Any) -> str:
    """HTML-escape a value, treating None as ''."""
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


# ---------------------------------------------------------------------------
# NWN colour token rendering
# ---------------------------------------------------------------------------
#
# NWN strings can embed colour codes in two related forms:
#   <cRGB>...</c>      – the normal in-game form (R,G,B are raw bytes 0–255).
#   <<cRGB>...<</c>    – the doubled-bracket form used inside conversation,
#                        journal, and description fields so the toolset
#                        editors don't strip the angle brackets.
# The byte values arrive here as Unicode code points after JSON decoding,
# usually 1:1 (Latin-1 style); a few values in 0x80–0x9F may have been
# remapped through CP-1252 (e.g. 0x80 → U+20AC '€'), which we reverse so
# the original byte can be recovered.
#
# Module text is loaded straight from GFF and may contain unbalanced tokens
# (e.g. <c…> with no </c>); we auto-close any open spans at the end.

def _colour_byte(ch: str) -> int:
    """Recover the 0–255 NWN channel byte from a single decoded character."""
    o = ord(ch)
    if o <= 0xFF:
        return o
    try:
        return ch.encode("cp1252")[0]
    except (UnicodeEncodeError, IndexError):
        return min(o, 0xFF)


def nwn_html(s: Any) -> str:
    """Render an NWN string with <cRGB>/</c> tokens to HTML-safe markup.

    Plain text is HTML-escaped; colour tokens become <span style="color:#rrggbb">
    so the intended colour is shown and the raw bytes are hidden."""
    if s is None:
        return ""
    s = str(s)
    if not s:
        return ""
    out: list[str] = []
    buf: list[str] = []
    depth = 0

    def flush() -> None:
        if buf:
            out.append(html.escape("".join(buf), quote=True))
            buf.clear()

    i = 0
    n = len(s)
    while i < n:
        # Doubled-bracket open: <<cRGB>
        if (i + 7 <= n and s[i] == "<" and s[i + 1] == "<"
                and s[i + 2] == "c" and s[i + 6] == ">"):
            flush()
            r, g, b = (_colour_byte(s[i + 3]), _colour_byte(s[i + 4]),
                       _colour_byte(s[i + 5]))
            out.append(f'<span style="color:#{r:02x}{g:02x}{b:02x}">')
            depth += 1
            i += 7
            continue
        # Doubled-bracket close: <</c>
        if i + 5 <= n and s[i:i + 5] == "<</c>":
            flush()
            if depth > 0:
                out.append("</span>")
                depth -= 1
            i += 5
            continue
        # Standard open: <cRGB>
        if (i + 6 <= n and s[i] == "<" and s[i + 1] == "c"
                and s[i + 5] == ">"):
            flush()
            r, g, b = (_colour_byte(s[i + 2]), _colour_byte(s[i + 3]),
                       _colour_byte(s[i + 4]))
            out.append(f'<span style="color:#{r:02x}{g:02x}{b:02x}">')
            depth += 1
            i += 6
            continue
        # Standard close: </c>
        if i + 4 <= n and s[i:i + 4] == "</c>":
            flush()
            if depth > 0:
                out.append("</span>")
                depth -= 1
            i += 4
            continue
        buf.append(s[i])
        i += 1

    flush()
    out.extend(["</span>"] * depth)
    return "".join(out)


_C_TOKEN_RE = re.compile(r"<<?/c>|<<?c.{3}>|</?[A-Za-z][^>]*>", re.DOTALL)
_C_OPEN_RE = re.compile(r"<<?c(.)(.)(.)>", re.DOTALL)


# Canonical NWN1 in-game text colours for damage / effect / save types.
# Source: https://nwn.wiki/spaces/NWN1/pages/38177018/Colour+Tokens
# Applied to property subtypes, weapon damage extras, and creature feat /
# spell labels so the wiki echoes the colour cues a player sees in the game.
NWN_DAMAGE_COLORS: dict[str, str] = {
    "Acid":       "#01ff01",
    "Cold":       "#99ffff",
    "Divine":     "#ffff01",
    "Electrical": "#0166ff",
    "Fire":       "#ff0101",
    "Magical":    "#cc77ff",
    "Negative":   "#999999",
    "Positive":   "#ffffff",
    "Sonic":      "#ff9901",
}

_DMG_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in NWN_DAMAGE_COLORS) + r")\b"
)


def colorize_damage_words(s: str) -> str:
    """Wrap NWN damage / effect type words in spans with their canonical
    in-game hex colour. Safe to call on either plain text or already-escaped
    HTML — the substitution only matches bare words, never markup. The CSS
    class `nwn-dmg` adds a text-shadow so the lighter NWN colours (Cold,
    Divine, Positive) stay legible on the wiki's light background."""
    if not s:
        return s
    return _DMG_WORD_RE.sub(
        lambda m: (
            f'<span class="nwn-dmg nwn-dmg-{m.group(1).lower()}" '
            f'style="color:{NWN_DAMAGE_COLORS[m.group(1)]}">'
            f"{m.group(1)}</span>"
        ),
        s,
    )


def nwn_text(s: Any) -> str:
    """Strip NWN colour tokens, returning plain text (no HTML escaping).

    Use for places that can't render markup: HTML <title>, alt attributes,
    sort keys."""
    if s is None:
        return ""
    return _C_TOKEN_RE.sub("", str(s))


def nwn_first_color(s: Any) -> str | None:
    """Return '#rrggbb' for the first <cRGB>/<<cRGB> token in s, or None.

    Useful for elements that can carry a single colour (SVG <text> fill)
    but not inline runs."""
    if s is None:
        return None
    m = _C_OPEN_RE.search(str(s))
    if not m:
        return None
    r, g, b = (_colour_byte(m.group(1)), _colour_byte(m.group(2)),
               _colour_byte(m.group(3)))
    return f"#{r:02x}{g:02x}{b:02x}"
