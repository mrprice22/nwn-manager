"""Conversation rendering for the wiki.

The Conversations index plus the per-dialog page: caller/teleport/script
cross-reference tables, custom-token annotation, the recursive dialog tree
and the flat entry/reply node index.
"""

from __future__ import annotations

import re
from pathlib import Path

from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.blocks import meta_dl
from nwn_wiki.htmlgen.chrome import write_page
from nwn_wiki.htmlgen.escape import E, nwn_html, nwn_text
from nwn_wiki.htmlgen.links import (_area_link, _creature_link, _item_link,
                                    _script_link, link)
from nwn_wiki.htmlgen.pagectx import PageCtx


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def _dialog_node_anchor(kind: str, idx: int) -> str:
    return f"{kind[0]}{idx}"   # e.g. "e3", "r12"


def _dialog_text_preview(node: dict, max_len: int = 80) -> str:
    """First non-empty line of a dialog node's text, truncated. Strips
    NWN colour tokens so the preview is plain readable text."""
    text = nwn_text(loc(node.get("Text"))).strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    if len(line) > max_len:
        line = line[:max_len].rstrip() + "…"
    return line


_RE_CUSTOM_TOKEN = re.compile(r"&lt;CUSTOM(\d+)&gt;")


def _annotate_custom_tokens(escaped_html: str,
                             token_setters: dict[int, set[str]],
                             db: "Db", ctx: PageCtx) -> str:
    """Replace HTML-escaped <CUSTOMn> tokens with annotated spans that link to
    the scripts that call SetCustomToken(n, ...). If no setter is found,
    the token is wrapped in a muted span so it's visually distinct."""
    def replace(m: re.Match) -> str:
        n = int(m.group(1))
        setters = sorted(token_setters.get(n, set()))
        raw = m.group(0)  # &lt;CUSTOMn&gt;
        if setters:
            links = ", ".join(_script_link(db, s, ctx) for s in setters)
            return (f'<span class="custom-token" '
                    f'title="SetCustomToken({n}, ...) called in: '
                    + ", ".join(setters) + f'">'
                    f'{raw}'
                    f'<sup class="token-setters">[{links}]</sup>'
                    f'</span>')
        else:
            return (f'<span class="custom-token custom-token-unknown" '
                    f'title="SetCustomToken({n}, ...) — no setter found in static analysis">'
                    f'{raw}</span>')
    return _RE_CUSTOM_TOKEN.sub(replace, escaped_html)


def _render_dialog_node_refs(db: Db, refs_field: list[dict],
                             tgt_kind: str,
                             tgt_nodes: list[dict] | None = None) -> str:
    """Render a node's child-pointer list ("RepliesList" inside an entry,
    "EntriesList" inside a reply) as inline anchor links to other nodes
    on the same page. tgt_kind is "reply" or "entry". When tgt_nodes is
    supplied, append a short text preview of the target so the reader
    doesn't have to jump to scan each branch."""
    if not refs_field:
        return ""
    cells = []
    for ref in refs_field:
        idx = fld(ref, "Index")
        active = fld(ref, "Active", "")
        is_child = fld(ref, "IsChild", 0)
        anchor = _dialog_node_anchor(tgt_kind, int(idx)) if isinstance(idx, int) else "?"
        label = "→ child" if is_child else f"→ {tgt_kind} #{idx}"
        bits = [f'<a href="#{E(anchor)}">{E(label)}</a>']
        if (tgt_nodes is not None and isinstance(idx, int)
                and 0 <= idx < len(tgt_nodes)):
            preview = _dialog_text_preview(tgt_nodes[idx])
            if preview:
                bits.append(f' <span class="dlg-link-preview">“{nwn_html(preview)}”</span>')
        if active:
            bits.append(f' <small class="muted">(if <code>{E(active)}</code>)</small>')
        cells.append("".join(bits))
    return '<ul class="dlg-links"><li>' + "</li><li>".join(cells) + "</li></ul>"


def _render_dialog_tree(db: Db, entries: list[dict], replies: list[dict],
                        starts: list[dict], ctx: PageCtx) -> str:
    """Render the dialog as nested <details> blocks, recursing from each
    starting entry. Cycles and shared continuations are broken with a
    backref link to the node's first occurrence so the tree can't
    explode (NWN dialog graphs frequently cycle)."""
    if not starts:
        return ""

    visited_e: set[int] = set()
    visited_r: set[int] = set()
    parts: list[str] = []
    # Recursion depth guard — defensive; visited-sets should already cap it,
    # but protects against pathological refs (negative indices, etc.).
    MAX_DEPTH = 200

    def cond_html(active: str) -> str:
        return (f' <small class="muted">(if <code>{E(active)}</code>)</small>'
                if active else "")

    def render_entry(idx: int, ref_active: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            parts.append('<div class="dlg-tree-broken">⚠ max depth</div>')
            return
        if not isinstance(idx, int) or idx < 0 or idx >= len(entries):
            parts.append(f'<div class="dlg-tree-broken">⚠ entry #{E(idx)} (out of range)</div>')
            return
        anchor = _dialog_node_anchor("entry", idx)
        if idx in visited_e:
            preview = _dialog_text_preview(entries[idx])
            parts.append(
                f'<div class="dlg-tree-ref entry-ref">'
                f'↩ <a href="#{E(anchor)}">entry #{idx}</a>'
                + (f' <span class="dlg-link-preview">“{nwn_html(preview)}”</span>'
                   if preview else "")
                + cond_html(ref_active) + '</div>'
            )
            return
        visited_e.add(idx)
        e = entries[idx]
        speaker = fld(e, "Speaker", "")
        action = fld(e, "Script", "")
        text_html = _annotate_custom_tokens(
            nwn_html(loc(e.get("Text"))), db.token_setters, db, ctx) or "<em>(no text)</em>"
        head = (f'<span class="dlg-id">entry #{idx}</span>'
                + (f' <span class="dlg-speaker">[{E(speaker)}]</span>' if speaker else "")
                + (f' <span class="dlg-action">→ <code>{E(action)}</code></span>' if action else "")
                + cond_html(ref_active))
        parts.append('<details class="dlg-tree-node entry" open>')
        parts.append(f'<summary>{head} '
                     f'<a class="dlg-anchor" href="#{E(anchor)}" '
                     f'title="jump to flat entry #{idx}">¶</a></summary>')
        parts.append(f'<div class="dlg-text">{text_html}</div>')
        children = list_items(e.get("RepliesList"))
        if children:
            parts.append('<div class="dlg-tree-children">')
            for ref in children:
                render_reply(fld(ref, "Index"),
                             fld(ref, "Active", ""), depth + 1)
            parts.append('</div>')
        parts.append('</details>')

    def render_reply(idx: int, ref_active: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            parts.append('<div class="dlg-tree-broken">⚠ max depth</div>')
            return
        if not isinstance(idx, int) or idx < 0 or idx >= len(replies):
            parts.append(f'<div class="dlg-tree-broken">⚠ reply #{E(idx)} (out of range)</div>')
            return
        anchor = _dialog_node_anchor("reply", idx)
        if idx in visited_r:
            preview = _dialog_text_preview(replies[idx])
            parts.append(
                f'<div class="dlg-tree-ref reply-ref">'
                f'↩ <a href="#{E(anchor)}">reply #{idx}</a>'
                + (f' <span class="dlg-link-preview">“{nwn_html(preview)}”</span>'
                   if preview else "")
                + cond_html(ref_active) + '</div>'
            )
            return
        visited_r.add(idx)
        r = replies[idx]
        action = fld(r, "Script", "")
        text_html = _annotate_custom_tokens(
            nwn_html(loc(r.get("Text"))), db.token_setters, db, ctx) or "<em>(no text)</em>"
        head = (f'<span class="dlg-id">reply #{idx}</span>'
                + (f' <span class="dlg-action">→ <code>{E(action)}</code></span>' if action else "")
                + cond_html(ref_active))
        parts.append('<details class="dlg-tree-node reply" open>')
        parts.append(f'<summary>{head} '
                     f'<a class="dlg-anchor" href="#{E(anchor)}" '
                     f'title="jump to flat reply #{idx}">¶</a></summary>')
        parts.append(f'<div class="dlg-text">{text_html}</div>')
        children = list_items(r.get("EntriesList"))
        if children:
            parts.append('<div class="dlg-tree-children">')
            for ref in children:
                render_entry(fld(ref, "Index"),
                             fld(ref, "Active", ""), depth + 1)
            parts.append('</div>')
        parts.append('</details>')

    parts.append('<div class="dlg-tree">')
    for s in starts:
        render_entry(fld(s, "Index"), fld(s, "Active", ""), 0)
    parts.append('</div>')
    return "\n".join(parts)


def render_conversations_index(db: Db, out: Path) -> None:
    ctx = PageCtx("conversations/index.html")
    if not db.dialogs:
        write_page(out, ctx, "Conversations",
                   "<h1>Conversations</h1><p>(none)</p>")
        return
    rows = []
    for rr in sorted(db.dialogs.keys(),
                     key=lambda r: db.dialog_label(r).lower()):
        dlg = db.dialogs[rr]
        n_entries = len(list_items(dlg.get("EntryList")))
        n_replies = len(list_items(dlg.get("ReplyList")))
        n_callers = len(db.dialog_callers.get(rr, []))
        n_teleports = len(db.dialog_teleports.get(rr, []))
        global_ = any(c["kind"] in ("module-event", "item-script")
                      for c in db.dialog_callers.get(rr, []))
        is_zdlg = bool(dlg.get("__zdlg_handler__"))
        flags = []
        if is_zdlg:
            flags.append('<span class="badge">z-dialog</span>')
        if global_:
            flags.append('<span class="badge global">global</span>')
        if n_teleports:
            flags.append('<span class="badge teleport">teleport</span>')
        rows.append(
            f"<tr><td>{link(f'{rr}.html', db.dialog_label(rr))}</td>"
            f"<td><code>{E(rr)}</code></td>"
            f"<td>{n_entries}</td>"
            f"<td>{n_replies}</td>"
            f"<td>{n_callers}</td>"
            f"<td>{n_teleports}</td>"
            f"<td>{' '.join(flags)}</td></tr>"
        )
    n_zdlg = sum(1 for d in db.dialogs.values() if d.get("__zdlg_handler__"))
    body = (
        "<h1>Conversations</h1>"
        f"<p>{len(db.dialogs) - n_zdlg} dialog files"
        + (f", plus {n_zdlg} script-driven z-dialog handlers" if n_zdlg else "")
        + ". "
        '<small class="muted">"global" = also reachable from a module-level '
        "event script (rest, level-up, etc.) or a tag-based item activator. "
        '"teleport" = at least one entry/reply runs an action script that '
        "calls JumpToLocation/JumpToObject. "
        '"z-dialog" = HoMERs script-driven dialog (see zdlg_include_i.nss); '
        "entry/reply text is generated by NWScript at runtime so isn't "
        "rendered here.</small></p>"
        '<table class="data"><thead><tr>'
        "<th>Opening line</th><th>ResRef</th><th>NPC lines</th><th>PC lines</th>"
        "<th>Callers</th><th>Teleports</th><th>Flags</th>"
        "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
    )
    write_page(out, ctx, "Conversations", body)


def render_conversation_page(db: Db, resref: str, out: Path) -> None:
    """Per-dialog page. Renders the dialog as a flat outline of Entry and
    Reply nodes with anchor links between them, since real NWN dialog
    graphs cycle and a tree expansion is impractical (and noisy)."""
    dlg = db.dialogs.get(resref)
    if not dlg:
        return
    ctx = PageCtx(f"conversations/{resref}.html")
    zdlg_handler = dlg.get("__zdlg_handler__")
    entries = list_items(dlg.get("EntryList"))
    replies = list_items(dlg.get("ReplyList"))
    starts = list_items(dlg.get("StartingList"))

    if zdlg_handler:
        title_html = (f"<h1>Z-dialog: <code>{E(resref)}</code> "
                      '<span class="badge">z-dialog</span></h1>')
        meta_rows = [
            f"<dt>Handler script</dt><dd>{_script_link(db, zdlg_handler, ctx)}</dd>",
        ]
        dispatchers = sorted(db.zdlg_handler_dispatchers.get(zdlg_handler, set()))
        if dispatchers:
            disp_html = ", ".join(_script_link(db, d, ctx) for d in dispatchers)
            meta_rows.append(f"<dt>Opened from</dt><dd>{disp_html}</dd>")
        meta = [meta_dl(meta_rows, "\n")]
        meta.append(
            '<p class="muted">This is a HoMERs z-dialog: the conversation is '
            "generated entirely by NWScript via the <code>StartDlg(...)</code> "
            "helper in <code>zdlg_include_i.nss</code>. There is no GFF "
            ".dlg resource for it, so entry/reply text isn't rendered — "
            "follow the handler script link above to read its logic.</p>"
        )
        sections: list[str] = [title_html, "\n".join(meta)]
    else:
        sections: list[str] = [
            f"<h1>Conversation: <code>{E(resref)}</code></h1>",
            meta_dl([
                f"<dt>NPC lines</dt><dd>{len(entries)}</dd>",
                f"<dt>PC replies</dt><dd>{len(replies)}</dd>",
                f"<dt>NumWords</dt><dd>{E(fld(dlg, 'NumWords', ''))}</dd>",
                f"<dt>OnEnd</dt><dd><code>{E(fld(dlg, 'EndConversation', ''))}</code></dd>",
                f"<dt>OnAbort</dt><dd><code>{E(fld(dlg, 'EndConverAbort', ''))}</code></dd>",
            ], "\n"),
        ]

    # ---- Triggered by ----
    callers = db.dialog_callers.get(resref, [])
    if callers:
        sections.append("<h2>Triggered by</h2><ul class=\"dlg-callers\">")
        for c in callers:
            sections.append("<li>" + _caller_html(db, c, ctx) + "</li>")
        sections.append("</ul>")
    else:
        sections.append("<h2>Triggered by</h2>"
                        "<p class=\"muted\">No caller found in static analysis. "
                        "May be invoked from runtime-only code paths "
                        "(string-built resrefs, etc.).</p>")

    # ---- Teleport destinations ----
    teleports = db.dialog_teleports.get(resref, [])
    if teleports:
        sections.append("<h2>Teleport destinations</h2>")
        rows = []
        for t in teleports:
            anchor = _dialog_node_anchor(t["node_kind"], t["node_index"])
            dst_area = t.get("area")
            dst_cell = (_area_link(db, dst_area, ctx)
                        if dst_area else f'<em class="muted">unresolved</em>')
            rows.append(
                f"<tr><td><code>{E(t['tag'])}</code></td>"
                f"<td>{dst_cell}</td>"
                f"<td><code>{E(t['via_script'])}</code></td>"
                f"<td><a href=\"#{E(anchor)}\">{E(t['node_kind'])} #{t['node_index']}</a></td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Waypoint / object tag</th><th>Destination area</th>"
            "<th>Via script</th><th>From node</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # ---- Action / condition scripts ----
    scripts = db.dialog_scripts.get(resref, [])
    if scripts:
        sections.append("<h2>Scripts referenced</h2>")
        seen: set[tuple[str, str]] = set()
        rows = []
        for s in scripts:
            key = (s["resref"], s["kind"])
            if key in seen:
                continue
            seen.add(key)
            existence = "✓" if s["resref"] in db.scripts else "?"
            extras = []
            if s["resref"] in db.script_dialogs:
                outs = sorted(db.script_dialogs[s["resref"]])
                extras.append("starts: " + ", ".join(
                    link(f"{o}.html", o) if o in db.dialogs else E(o) for o in outs))
            if s["resref"] in db.script_teleport_tags:
                tp_parts = []
                for t in sorted(db.script_teleport_tags[s["resref"]]):
                    dst_area = db.tag_to_area.get(t)
                    if dst_area:
                        tp_parts.append(
                            f"<code>{E(t)}</code> ("
                            + _area_link(db, dst_area, ctx)
                            + ")"
                        )
                    else:
                        tp_parts.append(f"<code>{E(t)}</code>")
                extras.append("teleports to: " + ", ".join(tp_parts))
            rows.append(
                f"<tr><td><code>{E(s['resref'])}</code></td>"
                f"<td>{E(s['kind'])}</td>"
                f"<td>{existence}</td>"
                f"<td>{' · '.join(extras)}</td></tr>"
            )
        sections.append(
            '<table class="data"><thead><tr>'
            "<th>Script</th><th>Slot</th><th>In module</th><th>Notes</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # ---- Items checked ----
    checked_items = db.dialog_item_checks.get(resref, [])
    if checked_items:
        item_links = []
        for item_rr in sorted(checked_items, key=lambda r: db.item_name(r).lower()):
            name = db.item_name(item_rr)
            item_links.append(
                _item_link(db, item_rr, ctx, name)
                if item_rr in db.items else E(name)
            )
        sections.append(
            "<h2>Items checked</h2>"
            '<p class="muted">These items\' tags are checked via '
            "<code>GetItemPossessedBy</code> in this conversation's scripts:</p>"
            "<ul>" + "".join(f"<li>{h}</li>" for h in item_links) + "</ul>"
        )

    # ---- Quest entries granted ----
    dlg_quests = db.dialog_quest_grants_rev.get(resref, {})
    if dlg_quests:
        rows = []
        for q_tag in sorted(dlg_quests, key=lambda t: db.quest_tag_to_info.get(t, (t, ""))[0].lower()):
            q_name, q_slug = db.quest_tag_to_info.get(q_tag, (q_tag, ""))
            eids = sorted(dlg_quests[q_tag])
            step_str = ", ".join(str(e) for e in eids)
            quest_link = (link(f"../quests/{q_slug}.html", q_name)
                          if q_slug else E(q_name))
            rows.append(f"<tr><td>{quest_link}</td><td><code>{E(q_tag)}</code></td>"
                        f"<td>{E(step_str)}</td></tr>")
        sections.append(
            "<h2>Quest entries granted</h2>"
            '<table class="data"><thead><tr>'
            "<th>Quest</th><th>Tag</th><th>Step(s)</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table>"
        )

    # ---- Custom tokens summary (non-z-dialog only) ----
    if not zdlg_handler:
        all_tokens: set[int] = set()
        for node in entries + replies:
            text = loc(node.get("Text"))
            if text:
                for m in re.finditer(r'<CUSTOM(\d+)>', text):
                    all_tokens.add(int(m.group(1)))

        if all_tokens:
            token_rows = []
            for n in sorted(all_tokens):
                setters = sorted(db.token_setters.get(n, set()))
                setter_html = (", ".join(_script_link(db, s, ctx) for s in setters)
                               if setters else '<em class="muted">not found in static analysis</em>')
                token_rows.append(
                    f"<tr><td><code>&lt;CUSTOM{n}&gt;</code></td>"
                    f"<td>{setter_html}</td></tr>"
                )
            sections.append(
                "<h2>Custom tokens</h2>"
                '<p class="muted">These runtime tokens are filled by NWScript via '
                '<code>SetCustomToken(N, text)</code>. The scripts listed set each token; '
                'the actual value depends on game state at runtime.</p>'
                '<table class="data"><thead><tr>'
                "<th>Token</th><th>Set by script(s)</th>"
                "</tr></thead><tbody>" + "\n".join(token_rows) + "</tbody></table>"
            )

    # ---- Starting entries ----
    if starts:
        sections.append("<h2>Starting entries</h2><ul>")
        for s in starts:
            i = fld(s, "Index")
            active = fld(s, "Active", "")
            anchor = _dialog_node_anchor("entry", int(i)) if isinstance(i, int) else "?"
            preview = ""
            if isinstance(i, int) and 0 <= i < len(entries):
                preview = nwn_text(loc(entries[i].get("Text"))).strip()
                preview = preview.splitlines()[0] if preview else ""
                if len(preview) > 80:
                    preview = preview[:80] + "…"
            cond = (f' <small class="muted">(if <code>{E(active)}</code>)</small>'
                    if active else "")
            sections.append(
                f'<li><a href="#{E(anchor)}">entry #{E(i)}</a>{cond}'
                + (f' — {nwn_html(preview)}' if preview else "") + '</li>'
            )
        sections.append("</ul>")

    # ---- Conversation flow (recursive tree from each starting entry) ----
    tree_html = _render_dialog_tree(db, entries, replies, starts, ctx)
    if tree_html:
        sections.append("<h2>Conversation flow</h2>")
        sections.append(
            '<p class="muted">Each starting entry is expanded recursively. '
            "Repeated nodes (cycles, shared continuations) appear inline as "
            "<code>↩ link</code> backrefs to the first occurrence.</p>"
        )
        sections.append(tree_html)

    # ---- Flat node index (preserves anchors used by Triggered-by /
    # Scripts tables; useful for direct lookup by node number).
    sections.append("<h2>NPC lines (entries) — flat index</h2>")
    for i, e in enumerate(entries):
        anchor = _dialog_node_anchor("entry", i)
        text = loc(e.get("Text"))
        speaker = fld(e, "Speaker", "")
        action = fld(e, "Script", "")
        sections.append(
            f'<div class="dlg-node entry" id="{E(anchor)}">'
            f'<div class="dlg-head"><span class="dlg-id">entry #{i}</span>'
            + (f' <span class="dlg-speaker">[{E(speaker)}]</span>' if speaker else "")
            + (f' <span class="dlg-action">→ <code>{E(action)}</code></span>' if action else "")
            + '</div>'
            f'<div class="dlg-text">{_annotate_custom_tokens(nwn_html(text), db.token_setters, db, ctx) or "<em>(no text)</em>"}</div>'
            + _render_dialog_node_refs(db, list_items(e.get("RepliesList")),
                                       "reply", tgt_nodes=replies)
            + '</div>'
        )

    # ---- Replies ----
    sections.append("<h2>PC replies — flat index</h2>")
    for i, r in enumerate(replies):
        anchor = _dialog_node_anchor("reply", i)
        text = loc(r.get("Text"))
        action = fld(r, "Script", "")
        sections.append(
            f'<div class="dlg-node reply" id="{E(anchor)}">'
            f'<div class="dlg-head"><span class="dlg-id">reply #{i}</span>'
            + (f' <span class="dlg-action">→ <code>{E(action)}</code></span>' if action else "")
            + '</div>'
            f'<div class="dlg-text">{_annotate_custom_tokens(nwn_html(text), db.token_setters, db, ctx) or "<em>(no text)</em>"}</div>'
            + _render_dialog_node_refs(db, list_items(r.get("EntriesList")),
                                       "entry", tgt_nodes=entries)
            + '</div>'
        )

    write_page(out, ctx, f"Conversation {resref}", "\n".join(sections))


def _caller_html(db: Db, c: dict, ctx: PageCtx) -> str:
    """One-line description of a dialog caller, with cross-page links."""
    kind = c.get("kind")
    rr = c.get("resref", "")
    if kind == "creature":
        can_rr = db.canonical_for_bp.get(rr, rr) if rr else rr
        cname = db.canonical_creature_name(can_rr) if can_rr in db.canonical_creatures else rr
        cell = (_creature_link(db, can_rr, ctx, cname)
                if can_rr in db.canonical_creatures else nwn_html(rr))
        areas = ", ".join(_area_link(db, a, ctx)
                          for a in c.get("areas", []) if a in db.areas)
        return (f'<span class="badge">creature</span> NPC {cell} '
                f"<code>{E(rr)}</code>" + (f" — in {areas}" if areas else ""))
    if kind == "creature-instance":
        area_rr = c.get("area", "")
        idx = c.get("idx", 0)
        can_rr = db.canonical_for_inst.get((area_rr, idx), rr)
        inst_label = db.canonical_creature_name(can_rr) if can_rr else (db.creature_instance_name(area_rr, idx) or rr or "(unnamed)")
        inst_url = ctx.url(f"creatures/{can_rr}.html") if can_rr else "#"
        area_link = (_area_link(db, area_rr, ctx)
                     if area_rr in db.areas else nwn_html(area_rr))
        return (f'<span class="badge">creature</span> NPC '
                f'<a href="{E(inst_url)}">{nwn_html(inst_label)}</a>'
                f" — in {area_link}")
    if kind == "placeable":
        # Placeables don't have their own page; we'll link to one of their
        # area pages instead, which is where the placement lives.
        areas = ", ".join(_area_link(db, a, ctx)
                          for a in c.get("areas", []) if a in db.areas)
        return (f"Placeable <code>{E(rr)}</code>" +
                (f" — in {areas}" if areas else ""))
    if kind == "door":
        areas = ", ".join(_area_link(db, a, ctx)
                          for a in c.get("areas", []) if a in db.areas)
        return (f"Door <code>{E(rr)}</code>" +
                (f" — in {areas}" if areas else ""))
    if kind in ("placeable-instance", "door-instance"):
        area_rr = c.get("area", "")
        tag = c.get("tag") or ""
        area_link = (_area_link(db, area_rr, ctx)
                     if area_rr in db.areas else nwn_html(area_rr))
        bp = f" <small class=\"muted\">(blueprint <code>{E(rr)}</code>)</small>" if rr else ""
        label = "Placeable" if kind == "placeable-instance" else "Door"
        ident = f"<code>{E(tag)}</code>" if tag else "(untagged)"
        return (f'<span class="badge">instance</span> {label} {ident}'
                f"{bp} — in {area_link}")
    if kind in ("creature-event", "placeable-event", "door-event",
                "trigger-event", "area-event",
                "placeable-event-instance", "door-event-instance",
                "trigger-event-instance"):
        ev = c.get("event", "")
        s = c.get("script", "")
        is_instance = kind.endswith("-instance")
        if kind == "creature-event":
            can_rr = db.canonical_for_bp.get(rr, rr) if rr else rr
            cname = db.canonical_creature_name(can_rr) if can_rr in db.canonical_creatures else rr
            ent = (_creature_link(db, can_rr, ctx, cname)
                   if can_rr in db.canonical_creatures else nwn_html(rr))
            label = f"NPC {ent} <code>{E(rr)}</code>"
        elif kind == "area-event":
            ent = (_area_link(db, rr, ctx)
                   if rr in db.areas else nwn_html(rr))
            label = f"Area {ent} <code>{E(rr)}</code>"
        elif is_instance:
            tag = c.get("tag") or ""
            ident = f"<code>{E(tag)}</code>" if tag else "(untagged)"
            bp = (f" <small class=\"muted\">(blueprint <code>{E(rr)}</code>)</small>"
                  if rr else "")
            entity = kind.split("-")[0].title()  # Placeable / Door / Trigger
            label = (f'<span class="badge">instance</span> {entity} {ident}{bp}')
        else:
            label = f"{kind.split('-')[0].title()} <code>{E(rr)}</code>"
        areas = ", ".join(_area_link(db, a, ctx)
                          for a in c.get("areas", []) if a in db.areas)
        return (f"{label} via <code>{E(ev)}</code> script {_script_link(db, s, ctx)}" +
                (f" — in {areas}" if areas else ""))
    if kind == "module-event":
        ev = c.get("event", "")
        s = c.get("script", "")
        return (f'<span class="badge global">global</span> '
                f"Module <code>{E(ev)}</code> via script {_script_link(db, s, ctx)}")
    if kind == "item-script":
        ent = (_item_link(db, rr, ctx)
               if rr in db.items else nwn_html(rr))
        s = c.get("script", "")
        return (f"Item {ent} <code>{E(rr)}</code> via tag-script "
                f"{_script_link(db, s, ctx)}")
    return E(repr(c))
