"""Area map rendering for the wiki.

The force-directed area-map SVG shared by the home index and the dedicated
``/map`` page, plus the legend hint that keeps the two in sync.
"""

from __future__ import annotations

import math
from pathlib import Path

from nwn_wiki.htmlgen.chrome import page, write
from nwn_wiki.htmlgen.escape import E, nwn_first_color, nwn_text


# Shared between the home index and the dedicated /map page so the legend stays
# in sync. Excludes the <h2> so each caller can title its own heading.
_MAP_HINT_HTML = (
    '<p class="map-hint">Click an area to open its page. Lines: '
    '<span class="legend trigger">trigger</span> '
    '<span class="legend door">door</span> '
    '<span class="legend convo">conversation teleport</span>. '
    '<span class="legend pseudo">★</span> = a global-trigger '
    'conversation node (e.g. Rest Menu).</p>'
)


def render_map_page(db: Db, out: Path,
                    positions: dict[str, tuple[float, float]],
                    sizes: dict[str, tuple[float, float]],
                    base_url: str = "") -> None:
    """Dedicated Map page (map/index.html) rendering the same area-map SVG as the
    home index. Targeted by the header 'Map' nav link so the map remains reachable
    even when the index is replaced by an author-supplied override."""
    body = "\n".join([
        '<h1>Area map</h1>',
        _MAP_HINT_HTML,
        render_map_svg(db, positions, sizes, base_url=base_url),
    ])
    write(out / "map" / "index.html", page("Map", body, root_rel=".."))


def render_map_svg(db: Db, positions: dict[str, tuple[float, float]],
                   sizes: dict[str, tuple[float, float]],
                   base_url: str = "") -> str:
    if not positions:
        return "<p>(no areas)</p>"

    # When a publish URL is given, rewrite node anchor hrefs as absolute URLs
    # so the downloaded standalone SVG keeps clickable links.
    url_prefix = base_url.rstrip("/") + "/" if base_url else ""

    # Box sizes are in "tile units"; positions share those units. We scale
    # everything up to give text room — bigger labels, bigger boxes — so the
    # map is readable instead of cramming tiny rects on the borders.
    scale = 14.0  # px per tile unit
    inset = 0.5  # leave a small visual gap inside each AABB

    # Compute extents from rect bounds (not just centres) so labels never
    # get clipped at the edge.
    min_x = min(x - sizes[r][0] / 2 for r, (x, y) in positions.items())
    max_x = max(x + sizes[r][0] / 2 for r, (x, y) in positions.items())
    min_y = min(y - sizes[r][1] / 2 for r, (x, y) in positions.items())
    max_y = max(y + sizes[r][1] / 2 for r, (x, y) in positions.items())
    pad = 6.0
    min_x -= pad; max_x += pad
    min_y -= pad; max_y += pad

    vx = min_x * scale
    vy = min_y * scale
    vw = (max_x - min_x) * scale
    vh = (max_y - min_y) * scale

    # Edges connect rect *boundaries* — not centres — so lines visibly
    # terminate at the nearest edge of each box.
    def rect_edge_point(r: str, towards: tuple[float, float]) -> tuple[float, float]:
        cx, cy = positions[r]
        w, h = sizes[r]
        dx = towards[0] - cx
        dy = towards[1] - cy
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return cx, cy
        # Scale to land on rect boundary.
        sx = (w / 2 - inset) / abs(dx) if dx else float('inf')
        sy = (h / 2 - inset) / abs(dy) if dy else float('inf')
        s = min(sx, sy)
        return cx + dx * s, cy + dy * s

    def node_label(nid: str) -> str:
        if nid in db.global_convo_pseudo:
            return db.global_convo_pseudo[nid]["label"]
        return nwn_text(db.area_name(nid))

    edge_lines = []
    drawn = set()
    for tr in db.transitions:
        a = tr["src_area"]
        b = tr["dst_area"]
        if a not in positions or not b or b not in positions:
            continue
        key = (min(a, b), max(a, b), tr["kind"])
        if key in drawn:
            continue
        drawn.add(key)
        ax, ay = rect_edge_point(a, positions[b])
        bx, by = rect_edge_point(b, positions[a])
        cls = "edge-trigger" if tr["kind"] == "trigger" else "edge-door"
        names = sorted([node_label(a), node_label(b)], key=str.lower)
        title = f"{names[0]} connected to {names[1]}"
        # Pair each visible thin line with a transparent thick hit-area line
        # so the <title> tooltip is easy to trigger by hovering the edge.
        edge_lines.append(
            f'<g class="edge-group"><title>{E(title)}</title>'
            f'<line x1="{ax * scale:.1f}" y1="{ay * scale:.1f}" '
            f'x2="{bx * scale:.1f}" y2="{by * scale:.1f}" class="edge-hit"/>'
            f'<line x1="{ax * scale:.1f}" y1="{ay * scale:.1f}" '
            f'x2="{bx * scale:.1f}" y2="{by * scale:.1f}" class="{cls}"/>'
            f'</g>'
        )
        # Draw dashed edges for alt destinations of dup-tag transitions.
        if tr.get("is_dup_tag"):
            for alt in tr.get("dst_area_alts", []):
                if alt not in positions:
                    continue
                alt_key = (min(a, alt), max(a, alt), tr["kind"])
                if alt_key in drawn:
                    continue
                drawn.add(alt_key)
                ax2, ay2 = rect_edge_point(a, positions[alt])
                bx2, by2 = rect_edge_point(alt, positions[a])
                title2 = f"{node_label(a)} ↔ {node_label(alt)} (duplicate tag: may not work)"
                edge_lines.append(
                    f'<g class="edge-group"><title>{E(title2)}</title>'
                    f'<line x1="{ax2 * scale:.1f}" y1="{ay2 * scale:.1f}" '
                    f'x2="{bx2 * scale:.1f}" y2="{by2 * scale:.1f}" class="edge-hit"/>'
                    f'<line x1="{ax2 * scale:.1f}" y1="{ay2 * scale:.1f}" '
                    f'x2="{bx2 * scale:.1f}" y2="{by2 * scale:.1f}" class="edge-dup-tag"/>'
                    f'</g>'
                )

    # Conversation-teleport edges. Drawn as a separate, dashed style so a
    # quest hub's "rest menu can teleport you to X" doesn't get confused
    # with a real walkable transition. Use an arrowhead because these
    # edges are directional (src → dst); regular transitions are drawn
    # bidirectionally already.
    conv_drawn = set()
    for tr in db.conv_transitions:
        s = tr["src"]
        d = tr["dst_area"]
        if s not in positions or d not in positions:
            continue
        key = (s, d)
        if key in conv_drawn:
            continue
        conv_drawn.add(key)
        ax, ay = rect_edge_point(s, positions[d])
        bx, by = rect_edge_point(d, positions[s])
        title = f"From: {node_label(s)} To: {node_label(d)}"
        edge_lines.append(
            f'<g class="edge-group"><title>{E(title)}</title>'
            f'<line x1="{ax * scale:.1f}" y1="{ay * scale:.1f}" '
            f'x2="{bx * scale:.1f}" y2="{by * scale:.1f}" class="edge-hit"/>'
            f'<line x1="{ax * scale:.1f}" y1="{ay * scale:.1f}" '
            f'x2="{bx * scale:.1f}" y2="{by * scale:.1f}" '
            f'class="edge-convo" marker-end="url(#convo-arrow)"/>'
            f'</g>'
        )

    # Nodes — rendered in three ordered layers so edges always appear over box
    # fills but text labels appear over edges.
    # layer_boxes : the rect/polygon fills + hover hit-area (behind edges)
    # layer_labels: text labels + click anchors             (in front of edges)
    font_size = 44.0

    def _label_lines(name: str, pw: float, ph: float) -> list[str]:
        """Word-wrap name to fit pw, returning 1-3 lines.  Single words that
        exceed pw are allowed to overflow rather than being mangled."""
        char_w = font_size * 0.55
        line_h = font_size * 1.3
        max_ln = max(1, int(ph / line_h))
        cpl = max(3, int(pw / char_w))  # chars per line
        if len(name) <= cpl:
            return [name]
        words = name.split()
        lines: list[str] = []
        cur = ""
        for word in words:
            cand = (cur + " " + word).strip() if cur else word
            if len(cand) <= cpl:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = word
                if len(lines) >= max_ln - 1 and cur:
                    # Last allowed line — truncate if still too long
                    if len(cur) > cpl:
                        cur = cur[: cpl - 1] + "…"
                    break
        if cur:
            lines.append(cur)
        return lines[:max_ln] or [name]

    layer_boxes: list[str] = []
    layer_labels: list[str] = []
    for nid, (x, y) in positions.items():
        w, h = sizes[nid]
        px = (x - w / 2) * scale
        py = (y - h / 2) * scale
        pw = w * scale
        ph = h * scale
        cx = x * scale
        cy = y * scale
        if nid in db.global_convo_pseudo:
            info = db.global_convo_pseudo[nid]
            label = info["label"]
            href = url_prefix + f"conversations/{info['conv_resref']}.html"
            r = min(pw, ph) * 0.45
            pts = []
            for k in range(8):
                ang = math.pi / 8 + k * math.pi / 4
                pts.append(f"{cx + r * math.cos(ang):.1f},{cy + r * math.sin(ang):.1f}")
            layer_boxes.append(
                f'<a href="{E(href)}"><g class="convo-node">'
                f'<polygon points="{" ".join(pts)}" />'
                f'<title>Conversation: {E(info["conv_resref"])} '
                f'({E(label)} — global trigger)</title>'
                f'</g></a>'
            )
            layer_labels.append(
                f'<a href="{E(href)}">'
                f'<text x="{cx:.1f}" y="{cy:.1f}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'font-size="{font_size * 0.7:.0f}" class="convo-label">{E(label)}</text>'
                f'</a>'
            )
            continue
        # SVG <text> can't render mid-string colour changes without <tspan>s,
        # so collapse to the first colour token and strip the rest.
        raw = db.area_name(nid)
        name = nwn_text(raw)
        fill = nwn_first_color(raw)
        fill_attr = f' style="fill:{fill}"' if fill else ""
        href = url_prefix + f"areas/{nid}.html"
        # Box fill + hover area
        layer_boxes.append(
            f'<a href="{E(href)}"><g class="area-node">'
            f'<rect x="{px:.1f}" y="{py:.1f}" '
            f'width="{pw:.1f}" height="{ph:.1f}" rx="6"/>'
            f'<title>{E(name)} ({E(nid)}, {int(w)}×{int(h)})</title>'
            f'</g></a>'
        )
        # Text label: word-wrapped, allowed to overflow the box horizontally
        lines = _label_lines(name, pw, ph)
        if len(lines) == 1:
            text_el = (
                f'<text x="{cx:.1f}" y="{cy:.1f}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'font-size="{font_size:.0f}" class="area-label"{fill_attr}>{E(lines[0])}</text>'
            )
        else:
            lh = font_size * 1.3
            ty0 = cy - lh * (len(lines) - 1) / 2
            tspans = "".join(
                f'<tspan x="{cx:.1f}" y="{ty0 + i * lh:.1f}">{E(ln)}</tspan>'
                for i, ln in enumerate(lines)
            )
            text_el = (
                f'<text text-anchor="middle" '
                f'font-size="{font_size:.0f}" class="area-label"{fill_attr}>{tspans}</text>'
            )
        layer_labels.append(f'<a href="{E(href)}">{text_el}</a>')

    # Cap the rendered *width* so the map fits a normal screen horizontally
    # at default browser zoom; height scales proportionally so aspect ratio
    # is preserved. We deliberately don't cap height — for tall modules that
    # makes display_w shrink and leaves a wide whitespace gap inside the
    # 96vw wrap, plus it kills vertical panning. Letting height grow means
    # .map-wrap (overflow:auto) scrolls vertically for tall layouts, and
    # browser zoom magnifies these CSS pixels so the user can pan in both
    # axes once the map exceeds the wrap.
    # The .map-wrap has a fixed viewport (~96vw). Draw the SVG at 4× the size
    # that would fit in that viewport so the user pans via scrollbars / middle
    # mouse instead of seeing the entire map shrunk to fit.
    target_w = 1400.0
    fit = min(target_w / vw, 1.0) if vw > 0 else 1.0
    zoom = 4.0
    display_w = vw * fit * zoom
    display_h = vh * fit * zoom
    # Inline styles so the SVG renders correctly when downloaded and opened
    # standalone (the page's external stylesheet won't be available there).
    # Kept in sync with wiki_assets/style.css's svg .* rules.
    embedded_css = (
        'svg{background:#fff;font-family:"Iowan Old Style",Georgia,serif}'
        '.area-node rect{fill:#fff;stroke:#3a5a8a;stroke-width:2.4}'
        '.area-node:hover rect{fill:#e6efff}'
        '.area-label{fill:#222;pointer-events:none}'
        '.edge-trigger{stroke:#6b3a1c;stroke-width:3;opacity:0.8;fill:none}'
        '.edge-door{stroke:#2d6a3a;stroke-width:3;opacity:0.8;fill:none}'
        '.edge-convo{stroke:#9b4dca;stroke-width:3;opacity:0.85;fill:none;'
        'stroke-dasharray:8,5}'
        '.edge-dup-tag{stroke:#cc6600;stroke-width:2;opacity:0.75;fill:none;'
        'stroke-dasharray:5,4}'
        '.edge-hit{stroke:transparent;stroke-width:30;fill:none;'
        'pointer-events:stroke;cursor:help}'
        '.convo-node polygon{fill:#f3e8ff;stroke:#9b4dca;stroke-width:2.4}'
        '.convo-node:hover polygon{fill:#e3c9ff}'
        '.convo-label{fill:#5a2b78;pointer-events:none}'
    )
    defs = (
        '<defs>'
        f'<style>{embedded_css}</style>'
        '<marker id="convo-arrow" viewBox="0 0 12 12" refX="10" refY="6" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M0,0 L12,6 L0,12 Z" fill="#9b4dca"/>'
        '</marker>'
        '</defs>'
    )
    # Three-layer SVG: box fills → edges → text labels.
    # Edges are drawn over box fills so connections are always visible even at
    # box boundaries, and text is drawn last so it's never obscured by edges.
    svg_body = (
        "\n".join(layer_boxes)   # layer 1: rect/polygon fills
        + "\n"
        + "\n".join(edge_lines)  # layer 2: edges on top of fills
        + "\n"
        + "\n".join(layer_labels)  # layer 3: text labels on top of edges
    )
    return (
        '<div class="map-toolbar">'
        '<span class="map-zoom-controls">'
        '<button type="button" class="map-zoom-btn" id="area-map-zoom-out" '
        'title="Zoom out">−</button>'
        '<button type="button" class="map-zoom-btn" id="area-map-zoom-reset" '
        'title="Reset zoom">1:1</button>'
        '<button type="button" class="map-zoom-btn" id="area-map-zoom-in" '
        'title="Zoom in">+</button>'
        '</span>'
        '<button type="button" id="area-map-download" '
        'onclick="downloadAreaMap()">Download SVG</button>'
        '</div>'
        + f'<div class="map-wrap" id="area-map-wrap">'
        + f'<svg class="area-map" id="area-map-svg" '
        + 'xmlns="http://www.w3.org/2000/svg" '
        + 'xmlns:xlink="http://www.w3.org/1999/xlink" '
        + f'width="{display_w:.0f}" height="{display_h:.0f}" '
        + f'viewBox="{vx:.1f} {vy:.1f} {vw:.1f} {vh:.1f}" '
        + f'preserveAspectRatio="xMidYMid meet">'
        + defs
        + svg_body
        + '</svg></div>'
        + '<script>(function(){'
          'var wrap=document.getElementById("area-map-wrap");'
          'var svg=document.getElementById("area-map-svg");'
          'if(!wrap||!svg)return;'
          # Zoom. Capture the 1:1 baseline size before any resize, then default
          # the view to 20% out (zoom=0.8); the "1:1" reset returns to baseline.
          'var baseW=parseFloat(svg.getAttribute("width"));'
          'var baseH=parseFloat(svg.getAttribute("height"));'
          'var zoom=0.8;'
          'svg.setAttribute("width",Math.round(baseW*zoom));'
          'svg.setAttribute("height",Math.round(baseH*zoom));'
          # Center now that the SVG is at its initial (20%-out) size.
          'wrap.scrollLeft=(wrap.scrollWidth-wrap.clientWidth)/2;'
          'wrap.scrollTop=(wrap.scrollHeight-wrap.clientHeight)/2;'
          'function applyZoom(z,px,py){'
            'z=Math.max(0.2,Math.min(5,z));'
            'if(z===zoom)return;'
            'px=px!==undefined?px:wrap.clientWidth/2;'
            'py=py!==undefined?py:wrap.clientHeight/2;'
            'var fx=(wrap.scrollLeft+px)/(baseW*zoom);'
            'var fy=(wrap.scrollTop+py)/(baseH*zoom);'
            'zoom=z;'
            'svg.setAttribute("width",Math.round(baseW*zoom));'
            'svg.setAttribute("height",Math.round(baseH*zoom));'
            'wrap.scrollLeft=fx*baseW*zoom-px;'
            'wrap.scrollTop=fy*baseH*zoom-py;'
          '}'
          'document.getElementById("area-map-zoom-in").onclick=function(){applyZoom(zoom*1.25);};'
          'document.getElementById("area-map-zoom-out").onclick=function(){applyZoom(zoom/1.25);};'
          'document.getElementById("area-map-zoom-reset").onclick=function(){applyZoom(1);};'
          'wrap.addEventListener("wheel",function(e){'
            'e.preventDefault();'
            'var r=wrap.getBoundingClientRect();'
            'applyZoom(e.deltaY<0?zoom*1.12:zoom/1.12,e.clientX-r.left,e.clientY-r.top);'
          '},{passive:false});'
          # Download
          'window.downloadAreaMap=function(){'
          'var src=new XMLSerializer().serializeToString(svg);'
          'if(!src.match(/^<\\?xml/))src=\'<?xml version="1.0" encoding="UTF-8"?>\\n\'+src;'
          'var blob=new Blob([src],{type:"image/svg+xml;charset=utf-8"});'
          'var url=URL.createObjectURL(blob);'
          'var a=document.createElement("a");'
          'a.href=url;a.download="area-map.svg";'
          'document.body.appendChild(a);a.click();'
          'document.body.removeChild(a);'
          'setTimeout(function(){URL.revokeObjectURL(url);},100);'
          '};'
          '})();</script>'
    )
