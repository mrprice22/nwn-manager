"""Minimal Markdown-to-HTML conversion for the wiki's manual pages.

Supports the subset the module's ``docs.manual`` sources actually use:
ATX headings, fenced code blocks, horizontal rules, unordered/ordered
lists, paragraphs, and inline code/bold/italic/links.

Leaf module: stdlib only -- nothing here may import another nwn_wiki module.
"""

from __future__ import annotations

import re


def md_to_html(text: str) -> str:
    """Convert a subset of Markdown to HTML (no third-party deps)."""
    import html as _html

    lines = text.splitlines()
    out: list[str] = []
    in_ul = False
    in_ol = False
    in_code = False
    code_buf: list[str] = []

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def inline(s: str) -> str:
        s = _html.escape(s, quote=False)
        # inline code
        s = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", s)
        # bold
        s = re.sub(r"\*\*(.+?)\*\*|__(.+?)__",
                   lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", s)
        # italic
        s = re.sub(r"\*([^*]+)\*|_([^_]+)_",
                   lambda m: f"<em>{m.group(1) or m.group(2)}</em>", s)
        # links
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                   lambda m: f'<a href="{_html.escape(m.group(2), quote=True)}">'
                             f"{m.group(1)}</a>", s)
        return s

    i = 0
    while i < len(lines):
        raw = lines[i]

        # fenced code block
        if raw.startswith("```"):
            if in_code:
                out.append(f"<pre><code>{''.join(code_buf)}</code></pre>")
                code_buf.clear()
                in_code = False
            else:
                close_lists()
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(_html.escape(raw) + "\n")
            i += 1
            continue

        stripped = raw.strip()

        # blank line
        if not stripped:
            close_lists()
            i += 1
            continue

        # ATX headings
        m = re.match(r"^(#{1,3})\s+(.*)", stripped)
        if m:
            close_lists()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue

        # horizontal rule
        if re.match(r"^[-*_]{3,}$", stripped):
            close_lists()
            out.append("<hr>")
            i += 1
            continue

        # unordered list
        m = re.match(r"^[-*]\s+(.*)", stripped)
        if m:
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue

        # ordered list
        m = re.match(r"^\d+\.\s+(.*)", stripped)
        if m:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue

        # paragraph: collect consecutive non-blank, non-heading lines
        close_lists()
        para: list[str] = []
        while i < len(lines):
            r = lines[i].strip()
            if (not r or r.startswith("#") or r.startswith("```")
                    or re.match(r"^[-*_]{3,}$", r)
                    or re.match(r"^[-*]\s+", r)
                    or re.match(r"^\d+\.\s+", r)):
                break
            para.append(inline(r))
            i += 1
        out.append(f"<p>{'<br>'.join(para)}</p>")

    close_lists()
    if in_code and code_buf:
        out.append(f"<pre><code>{''.join(code_buf)}</code></pre>")
    return "\n".join(out)
