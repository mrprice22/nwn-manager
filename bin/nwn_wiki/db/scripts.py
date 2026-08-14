"""Script (.nss) scraping and the dialog cross-references it feeds.

``DbScriptsMixin`` owns the one-shot scan of every script in the module
(:meth:`_parse_scripts`) and the derived passes that turn its output into
area-level facts: the tag->area index, script-spawned creature locations,
direct (non-dialog) teleport transitions, and the z-dialog pseudo-entries
that ``StartDlg`` handlers stand in for.  It also answers the small dialog
queries the renderers and ``index_dialogs()`` share.

One mixin of the stack described in :mod:`nwn_wiki.db`; see that docstring
for the rules it follows.

``_extract_func_body`` and the retaliation analysis (``_analyze_retaliation``
and its helpers) travel with it because this package may not import its
callers.  :mod:`nwn_wiki.render.creatures` imports ``_strip_nss_comments``
back from here for the boss registry.
"""

from __future__ import annotations

import re
from typing import Iterable

from nwn_wiki.gff import fld, list_items, loc
from nwn_wiki.htmlgen.escape import nwn_text
from nwn_wiki.lookups import _DAMAGE_TYPE_LABELS


# ---------------------------------------------------------------------------
# NWScript function-body extractor (brace counting)
# ---------------------------------------------------------------------------

def _extract_func_body(text: str, from_pos: int, max_len: int = 8000) -> str:
    """Return the text of the first balanced-brace block at or after from_pos.
    Returns "" when no opening brace is found within max_len chars."""
    i = text.find('{', from_pos, from_pos + max_len)
    if i < 0:
        return ""
    depth = 0
    end = min(len(text), i + max_len)
    for j in range(i, end):
        if text[j] == '{':
            depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return text[i:end]


# --- Retaliation (OnDamaged strike-back) analysis ------------------------------
_RE_STRIP_LINE_COMMENT = re.compile(r'//[^\n]*')
_RE_STRIP_BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.S)
_RE_RET_APPLY_EFFECT = re.compile(r'\bApplyEffect(?:ToObject|AtLocation)\s*\(')
_RE_RET_HARM_EFFECT = re.compile(r'\bEffect(?:Death|Damage|DamageOverTime)\s*\(')
_RE_RET_DAMAGE_TYPE = re.compile(r'\bDAMAGE_TYPE_([A-Z]+)\b')
_RE_RET_DICE = re.compile(r'\bd(\d+)\s*\(\s*(\d+)\s*\)')          # dX(Y) -> "YdX"
_RE_RET_EFFECT_DAMAGE_ARG = re.compile(r'\bEffectDamage\s*\(\s*([A-Za-z_]\w*|\d+)')


def _strip_nss_comments(text: str) -> str:
    return _RE_STRIP_LINE_COMMENT.sub("", _RE_STRIP_BLOCK_COMMENT.sub("", text))


def _ret_dice_to_text(expr: str) -> str:
    """Translate NWN dice calls dX(Y) -> 'YdX' and tidy whitespace."""
    s = _RE_RET_DICE.sub(lambda m: f"{m.group(2)}d{m.group(1)}", expr)
    return re.sub(r"\s+", " ", s).strip()


def _analyze_retaliation(raw: str) -> dict | None:
    """Inspect an OnDamaged script and, if it strikes back at the attacker or
    nearby creatures (rather than just running AI), return a structured summary:
    {slay, aoe, dtype, amount, align, chance_pct, pc_only}. Returns None for
    scripts that do not apply a harmful effect to someone other than themselves."""
    text = _strip_nss_comments(raw)
    if not (_RE_RET_APPLY_EFFECT.search(text) and _RE_RET_HARM_EFFECT.search(text)):
        return None
    aoe = "GetFirstObjectInShape" in text
    targets_attacker = "GetLastDamager" in text or "GetLastHostileActor" in text
    if not (aoe or targets_attacker):
        return None
    info: dict = {"slay": "EffectDeath" in text, "aoe": aoe,
                  "pc_only": "GetIsPC" in text}
    dt = _RE_RET_DAMAGE_TYPE.search(text)
    if dt:
        info["dtype"] = _DAMAGE_TYPE_LABELS.get(dt.group(1), dt.group(1).title())
    # Damage amount: resolve EffectDamage's first argument (often a local variable).
    m = _RE_RET_EFFECT_DAMAGE_ARG.search(text)
    if m:
        arg = m.group(1)
        if arg.isdigit():
            info["amount"] = arg
        else:
            am = re.search(r'\b' + re.escape(arg) + r'\s*=\s*([^;]+);', text)
            if am:
                info["amount"] = _ret_dice_to_text(am.group(1))
    if "ALIGNMENT_GOOD" in text:
        info["align"] = "Good-aligned"
    elif "ALIGNMENT_EVIL" in text:
        info["align"] = "Evil-aligned"
    # Chance gate: a dN() roll compared against a threshold, e.g. d10() >= 9, or
    # the same via a local variable: `int roll = d10(); if (roll >= 9)`.
    nN = nK = None
    cm = re.search(r'\bd(\d+)\s*\(\s*\)\s*>=\s*(\d+)', text)
    if cm:
        nN, nK = int(cm.group(1)), int(cm.group(2))
    else:
        rv = re.search(r'(\w+)\s*=\s*d(\d+)\s*\(\s*\)\s*;', text)
        if rv:
            kk = re.search(r'\b' + re.escape(rv.group(1)) + r'\s*>=\s*(\d+)', text)
            if kk:
                nN, nK = int(rv.group(2)), int(kk.group(1))
    if nN and nK is not None and 0 < nK <= nN:
        info["chance_pct"] = round((nN - nK + 1) / nN * 100)
    return info


class DbScriptsMixin:
    # ---- Dialog & script cross-referencing ----

    # Regex set used to scrape every .nss file once.
    # Keep them line-oriented and forgiving on whitespace; the goal is
    # "good enough to surface the obvious cross-references" not full
    # NWScript parsing.
    _RE_START_CONV = re.compile(
        r"\bActionStartConversation\s*\(\s*[^,)]+,\s*\"([A-Za-z0-9_]+)\""
    )
    # HoMERs z-dialog dispatcher: StartDlg(pc, target, "handler", ...).
    # The third argument names a handler script, not a .dlg file, but it's
    # the player-visible "conversation" identity, so we treat it the same.
    _RE_START_DLG = re.compile(
        r"\bStartDlg\s*\(\s*[^,)]+,\s*[^,)]+,\s*\"([A-Za-z0-9_]+)\""
    )
    _RE_GET_TAG = re.compile(
        r"\b(?:GetWaypointByTag|GetObjectByTag|GetNearestObjectByTag)"
        r"\s*\(\s*\"([A-Za-z0-9_]+)\"\s*\)"
    )
    _RE_JUMP = re.compile(
        r"\b(?:Action)?(?:JumpToLocation|JumpToObject)\s*\("
    )
    _RE_OPEN_STORE = re.compile(
        r"\b(?:OpenStore|gplotAppraiseOpenStore)\s*\("
    )
    _RE_CREATE_ITEM = re.compile(
        r'\bCreateItemOnObject\s*\(\s*"([A-Za-z0-9_]+)"'
    )
    _RE_GENERATE_TREASURE = re.compile(
        r"\bGenerate(?:High|Medium|Low|Boss|Book)?Treasure\s*\("
    )
    _RE_SET_TOKEN = re.compile(r"\bSetCustomToken\s*\(\s*(\d+)\s*,")
    # AddJournalQuestEntry("plotID", nState, oCreature, ...) — quest progression.
    # The plot id matches a journal category Tag; nState matches an entry ID.
    # Tags may contain spaces, so accept anything but a closing quote.
    _RE_ADD_JOURNAL = re.compile(
        r'\bAddJournalQuestEntry\s*\(\s*"([^"]*)"\s*,\s*(\d+)'
    )
    # Variable-form: AddJournalQuestEntry(CONST_IDENTIFIER, step, ...) where
    # the first arg is an identifier (not a function call), resolved via
    # const string declarations found in the script + its includes.
    _RE_ADD_JOURNAL_VAR = re.compile(
        r'\bAddJournalQuestEntry\s*\(\s*([A-Za-z_]\w*)\s*,\s*(\d+)'
    )
    _RE_CONST_STRING = re.compile(
        r'\bconst\s+string\s+([A-Za-z_]\w*)\s*=\s*"([^"]*)"'
    )
    _RE_INCLUDE = re.compile(r'^\s*#\s*include\s+"([A-Za-z0-9_]+)"', re.MULTILINE)
    # String-returning function signature: string FNAME(param-list)
    _RE_STR_FUNC_SIG = re.compile(r'\bstring\s+(\w+)\s*\(([^)]*)\)')
    # Any function signature (any return type): for finding grant-function bodies
    _RE_FUNC_ANY_SIG = re.compile(
        r'\b(?:void|string|int|float|object|vector|location|'
        r'effect|talent|itemproperty|action|command)\s+(\w+)\s*\(([^)]*)\)'
    )
    # if (PARAM == "X") return "Y"; — dispatch table entry
    _RE_IF_EQ_RETURN_STR = re.compile(
        r'\bif\s*\(\s*(\w+)\s*==\s*"([^"]*)"\s*\)\s*return\s*"([^"]*)"\s*;'
    )
    # return "PREFIX" + INNER_FUNC(PARAM); — prefix-chain composition
    _RE_RETURN_STR_PREFIX = re.compile(
        r'\breturn\s+"([^"]*)"\s*\+\s*(\w+)\s*\(\s*(\w+)\s*\)\s*;'
    )
    # AddJournalQuestEntry(DISPATCH_FUNC(PARAM), step, ...) inside function bodies
    _RE_JOURNAL_DISPATCH_CALL = re.compile(
        r'\bAddJournalQuestEntry\s*\(\s*(\w+)\s*\(\s*(\w+)\s*\)\s*,\s*(\d+)'
    )
    _RE_ITEM_POSSESSED_BY = re.compile(
        r'\bGetItemPossessedBy\s*\((?:[^,()]+|\([^)]*\))+,\s*"([A-Za-z0-9_]+)"'
    )
    # Damage-gate by equipped item tag: a script (typically OnDamaged) that compares
    # the tag of an item in an equip slot against a literal, e.g.
    #   GetTag(GetItemInSlot(INVENTORY_SLOT_RIGHTHAND, oPC)) != "narsil"
    # Either operand order is accepted. The captured literal is the required item tag.
    _RE_DAMAGE_SLOT_TAG = re.compile(
        r'GetTag\s*\(\s*GetItemInSlot\s*\([^()]*\)\s*\)\s*(?:==|!=)\s*"([A-Za-z0-9_]+)"'
        r'|"([A-Za-z0-9_]+)"\s*(?:==|!=)\s*GetTag\s*\(\s*GetItemInSlot\s*\([^()]*\)\s*\)'
    )
    # Self-mitigation signals: a damage script that restores the creature's own HP
    # or grants it damage immunity/reduction in response to being hit. These (and a
    # weapon/equip gate, _RE_DAMAGE_SLOT_TAG) distinguish a damage-altering handler
    # from a plain retaliation/AI OnDamaged script.
    _RE_SELF_HEAL = re.compile(r'\bEffectHeal\s*\(')
    _RE_SET_HP_SELF = re.compile(r'\bSetCurrentHitPoints\s*\(\s*OBJECT_SELF')
    _RE_SELF_DMG_IMMUNITY = re.compile(
        r'\bEffectDamage(?:ImmunityIncrease|Reduction|Resistance)\s*\('
    )
    _RE_CREATE_CREATURE = re.compile(
        r'\bCreateObject\s*\(\s*OBJECT_TYPE_CREATURE\s*,\s*"([A-Za-z0-9_]+)"'
    )
    _RE_ANY_LITERAL_STR = re.compile(r'"([A-Za-z0-9_]+)"')

    # Stock OnDamaged handlers that do NOT alter how a creature takes damage.
    # Anything else that ships in the module (i.e. present in script_paths) and is
    # wired as ScriptDamaged is treated as a custom damage-affecting handler.
    _STOCK_DAMAGE_SCRIPTS = frozenset({
        "", "x2_def_ondamage", "nw_c2_default6", "nw_c2_defaultb",
    })

    def is_custom_damage_script(self, resref: str) -> bool:
        """True when `resref` is a non-stock OnDamaged handler that ships in this
        module (so it may change how the creature takes damage)."""
        if not resref:
            return False
        rr = resref.strip()
        if rr.lower() in self._STOCK_DAMAGE_SCRIPTS:
            return False
        return rr in self.script_paths

    def _build_nwstr_dispatch(self, texts: list[str]) -> dict[str, dict[str, str]]:
        """Build {func_name -> {arg_val -> return_val}} for string functions
        whose return is fully determined by a single string argument:

        Pass 1 — if-chain dispatch:
          string F(string P) { if (P=="a") return "X"; if (P=="b") return "Y"; }
          → dispatch["F"] = {"a": "X", "b": "Y"}

        Pass 2 — prefix-chain composition (iterated until stable):
          string G(string P) { return "PFX" + F(P); }
          → dispatch["G"] = {"a": "PFXa", "b": "PFXB"} (using dispatch["F"])
        """
        dispatch: dict[str, dict[str, str]] = {}

        # Pass 1: extract if-chain dispatch tables
        for text in texts:
            for m in self._RE_STR_FUNC_SIG.finditer(text):
                fname = m.group(1)
                str_params = re.findall(r'\bstring\s+(\w+)', m.group(2))
                if not str_params:
                    continue
                param = str_params[0]
                body = _extract_func_body(text, m.end())
                if not body:
                    continue
                tbl: dict[str, str] = {}
                for im in self._RE_IF_EQ_RETURN_STR.finditer(body):
                    if im.group(1) == param:
                        tbl[im.group(2)] = im.group(3)
                if tbl:
                    dispatch.setdefault(fname, {}).update(tbl)

        # Pass 2: compose prefix-chain entries (iterate until stable)
        changed = True
        while changed:
            changed = False
            for text in texts:
                for m in self._RE_STR_FUNC_SIG.finditer(text):
                    fname = m.group(1)
                    if fname in dispatch:
                        continue
                    str_params = re.findall(r'\bstring\s+(\w+)', m.group(2))
                    if not str_params:
                        continue
                    param = str_params[0]
                    body = _extract_func_body(text, m.end())
                    if not body:
                        continue
                    cm = self._RE_RETURN_STR_PREFIX.search(body)
                    if cm and cm.group(3) == param and cm.group(2) in dispatch:
                        prefix = cm.group(1)
                        inner = cm.group(2)
                        tbl = {k: prefix + v for k, v in dispatch[inner].items()}
                        if tbl:
                            dispatch[fname] = tbl
                            changed = True

        return dispatch

    def _parse_scripts(self) -> None:
        """One-shot scan of every .nss file. Populates script_dialogs and
        script_teleport_tags. Cheap enough (~2300 files in well under 1s on
        a hot disk) and avoids per-renderer file I/O."""
        # Pre-read all scripts into cache (avoid re-reading for includes + dispatch build).
        _include_text_cache: dict[str, str] = {}
        for _rr, _path in self.script_paths.items():
            try:
                _include_text_cache[_rr] = _path.read_text(errors="replace")
            except Exception:
                _include_text_cache[_rr] = ""

        def _include_text(inc_rr: str) -> str:
            if inc_rr not in _include_text_cache:
                p = self.script_paths.get(inc_rr)
                try:
                    _include_text_cache[inc_rr] = p.read_text(errors="replace") if p else ""
                except Exception:
                    _include_text_cache[inc_rr] = ""
            return _include_text_cache[inc_rr]

        # Build string dispatch table from all script texts. Used to resolve
        # dynamic tag construction like AddJournalQuestEntry(TagFunc(sParam), ...).
        _nwstr_dispatch = self._build_nwstr_dispatch(
            list(_include_text_cache.values())
        )

        for resref, path in self.script_paths.items():
            text = _include_text_cache.get(resref, "")
            if not text:
                continue
            # ActionStartConversation("dlgresref", ...)
            for m in self._RE_START_CONV.finditer(text):
                self.script_dialogs[resref].add(m.group(1).lower())
            # StartDlg(_, _, "handler", ...) — HoMERs z-dialog dispatcher.
            for m in self._RE_START_DLG.finditer(text):
                handler = m.group(1).lower()
                self.script_zdialogs[resref].add(handler)
                self.zdlg_handler_dispatchers[handler].add(resref)
            # Tag-based teleport heuristic: a script that resolves an object
            # by tag *and* calls JumpTo[Location|Object] is treated as
            # teleporting to that tag. Imperfect (a script may resolve a
            # tag for some other reason), but the false-positive rate on
            # real modules is low and the player-visible value is high.
            if self._RE_JUMP.search(text):
                for m in self._RE_GET_TAG.finditer(text):
                    self.script_teleport_tags[resref].add(m.group(1))
            # Store-opening heuristic: a script that calls OpenStore (or the
            # HoMERs appraise variant) and resolves an object by tag is assumed
            # to be opening the store with that tag.  Scripts that call
            # OpenStore without any tag lookup (e.g. Bedlamson's Dynamic
            # Merchant System bdm_cnv_opn_stor, which uses a runtime local
            # object variable set by proximity) are recorded separately.
            if self._RE_OPEN_STORE.search(text):
                tag_matches = list(self._RE_GET_TAG.finditer(text))
                if tag_matches:
                    for m in tag_matches:
                        self.script_store_tags[resref].add(m.group(1))
                else:
                    self.script_bdm_open.add(resref)
            # CreateItemOnObject with a literal resref string.
            for m in self._RE_CREATE_ITEM.finditer(text):
                irr = m.group(1).lower()
                if irr not in self.script_creates_items[resref]:
                    self.script_creates_items[resref].append(irr)
            # Random/procedural treasure generation.
            if self._RE_GENERATE_TREASURE.search(text):
                self.script_generates_treasure.add(resref)
            # SetCustomToken(N, ...) — track which scripts set which custom tokens.
            for m in self._RE_SET_TOKEN.finditer(text):
                self.token_setters[int(m.group(1))].add(resref)
            # AddJournalQuestEntry("tag", id, ...) — which script awards which step.
            for m in self._RE_ADD_JOURNAL.finditer(text):
                self.quest_grants[m.group(1).strip().lower()][int(m.group(2))].add(resref)
            # Variable-form: AddJournalQuestEntry(CONST_VAR, id, ...) where
            # the tag is a const string identifier, possibly from an include.
            # Merge this script's direct includes so calls inside included
            # function bodies are attributed to the script that includes them.
            if "#include" in text or "AddJournalQuestEntry" in text:
                consts: dict[str, str] = {}
                for mc in self._RE_CONST_STRING.finditer(text):
                    consts[mc.group(1)] = mc.group(2)
                inc_resrefs = [mi.group(1).lower()
                               for mi in self._RE_INCLUDE.finditer(text)]
                bodies = text
                for inc_rr in inc_resrefs:
                    inc_txt = _include_text(inc_rr)
                    if inc_txt:
                        bodies += "\n" + inc_txt
                        for mc in self._RE_CONST_STRING.finditer(inc_txt):
                            consts.setdefault(mc.group(1), mc.group(2))
                if consts and self._RE_ADD_JOURNAL_VAR.search(bodies):
                    for m in self._RE_ADD_JOURNAL_VAR.finditer(bodies):
                        var = m.group(1)
                        if var in consts:
                            tag_val = consts[var].strip().lower()
                            self.quest_grants[tag_val][int(m.group(2))].add(resref)
                # Dynamic tag resolution: find functions in merged text whose body
                # contains AddJournalQuestEntry(DISPATCH_FUNC(PARAM), step, ...) where
                # DISPATCH_FUNC is in _nwstr_dispatch, then find call sites to those
                # functions in the main (non-include) text with a literal string arg.
                if _nwstr_dispatch and inc_resrefs:
                    for sig_m in self._RE_FUNC_ANY_SIG.finditer(bodies):
                        outer_fname = sig_m.group(1)
                        str_params = re.findall(r'\bstring\s+(\w+)', sig_m.group(2))
                        if not str_params:
                            continue
                        body = _extract_func_body(bodies, sig_m.end())
                        if not body:
                            continue
                        jm = self._RE_JOURNAL_DISPATCH_CALL.search(body)
                        if not jm:
                            continue
                        dispatch_func = jm.group(1)
                        inner_param = jm.group(2)
                        step = int(jm.group(3))
                        if dispatch_func not in _nwstr_dispatch:
                            continue
                        if inner_param not in str_params:
                            continue
                        # Find call sites in main text with exactly one string literal
                        for call_m in re.finditer(
                            r'\b' + re.escape(outer_fname)
                            + r'\s*\(((?:[^()]*|\([^()]*\))*)\)',
                            text,
                        ):
                            args_str = call_m.group(1)
                            literals = re.findall(r'"([^"]*)"', args_str)
                            if len(literals) != 1:
                                continue
                            tag = _nwstr_dispatch[dispatch_func].get(literals[0])
                            if tag:
                                self.quest_grants[tag.strip().lower()][step].add(resref)
            # GetItemPossessedBy(obj, "tag") — item-tag checks for key/quest logic.
            for m in self._RE_ITEM_POSSESSED_BY.finditer(text):
                self.script_checks_item_tags[resref].add(m.group(1))
            # GetTag(GetItemInSlot(...)) compared to a literal — a weapon/equip gate
            # that changes how a creature takes damage (see _RE_DAMAGE_SLOT_TAG).
            for m in self._RE_DAMAGE_SLOT_TAG.finditer(text):
                self.script_damage_req_tags[resref].add(m.group(1) or m.group(2))
            # Self-mitigation: restores own HP or grants self damage immunity on hit.
            if self._RE_SET_HP_SELF.search(text) or (
                "OBJECT_SELF" in text
                and (self._RE_SELF_HEAL.search(text)
                     or self._RE_SELF_DMG_IMMUNITY.search(text))
            ):
                self.script_mitigates_damage.add(resref)
            # Retaliation: strikes back at the attacker / nearby creatures on hit.
            _ret = _analyze_retaliation(text)
            if _ret is not None:
                self.script_retaliation[resref] = _ret
            # Detect creature spawns at waypoints (two strategies):
            # 1. Window scan: GetWaypointByTag("tag")/GetObjectByTag("tag") within
            #    10 lines before CreateObject(OBJECT_TYPE_CREATURE, "resref", ...).
            # 2. Co-location: a script that uses GetWaypointByTag+CreateObject with
            #    variables (e.g. a helper function) may call that helper with literal
            #    string arguments on the same line — scan for pairs where one literal
            #    matches a known waypoint tag and the other a known creature blueprint.
            if ("CreateObject" in text and "OBJECT_TYPE_CREATURE" in text
                    and "GetWaypointByTag" in text):
                lines = text.splitlines()
                # Window scan
                recent_tags: list[tuple[str, int]] = []
                for lineno, line in enumerate(lines):
                    for m in self._RE_GET_TAG.finditer(line):
                        recent_tags.append((m.group(1), lineno))
                    for m in self._RE_CREATE_CREATURE.finditer(line):
                        crr = m.group(1).lower()
                        for tag, tag_ln in reversed(recent_tags):
                            if lineno - tag_ln <= 10:
                                entry = {"creature_resref": crr, "waypoint_tag": tag}
                                if entry not in self.script_creature_waypoint_spawns[resref]:
                                    self.script_creature_waypoint_spawns[resref].append(entry)
                                break
                # Co-location scan: pairs of literal strings on the same line
                # where one is a known waypoint tag and the other is a creature resref.
                for line in lines:
                    strs = [m.group(1) for m in self._RE_ANY_LITERAL_STR.finditer(line)]
                    if len(strs) < 2:
                        continue
                    wp_strs = [s for s in strs
                               if s in self.waypoint_area or s.upper() in self.waypoint_area]
                    cr_strs = [s.lower() for s in strs if s.lower() in self.creatures]
                    for wp_tag in wp_strs:
                        actual_tag = wp_tag if wp_tag in self.waypoint_area else wp_tag.upper()
                        for crr in cr_strs:
                            entry = {"creature_resref": crr, "waypoint_tag": actual_tag}
                            if entry not in self.script_creature_waypoint_spawns[resref]:
                                self.script_creature_waypoint_spawns[resref].append(entry)

    def _build_tag_to_area(self) -> None:
        """Tag → area resref index spanning every placement (waypoints,
        doors, triggers, placeables, creatures). Used to resolve the
        destination area of a teleport script's GetWaypointByTag call."""
        for area_resref, git in self.gits.items():
            for wp in list_items(git.get("WaypointList")):
                tag = fld(wp, "Tag")
                if tag and tag not in self.tag_to_area:
                    self.tag_to_area[tag] = area_resref
                # Also expose tag without the conventional WP_ prefix so
                # GetWaypointByTag("foo") can match a waypoint tagged "WP_foo".
                if tag and tag.upper().startswith("WP_"):
                    short = tag[3:]
                    if short and short not in self.tag_to_area:
                        self.tag_to_area[short] = area_resref
                        self.fallback_tags.add(short)
            for d in list_items(git.get("Door List")):
                tag = fld(d, "Tag")
                if tag and tag not in self.tag_to_area:
                    self.tag_to_area[tag] = area_resref
            for t in list_items(git.get("TriggerList")):
                tag = fld(t, "Tag")
                if tag and tag not in self.tag_to_area:
                    self.tag_to_area[tag] = area_resref
            for p in list_items(git.get("Placeable List")):
                tag = fld(p, "Tag")
                if tag and tag not in self.tag_to_area:
                    self.tag_to_area[tag] = area_resref
            for c in list_items(git.get("Creature List")):
                tag = fld(c, "Tag")
                if tag and tag not in self.tag_to_area:
                    self.tag_to_area[tag] = area_resref

    def _resolve_script_creature_spawns(self) -> None:
        """Resolve script_creature_waypoint_spawns to area associations and
        populate canonical_locations (kind='script') and area_script_spawns.

        Must run after _parse_scripts and _build_tag_to_area."""
        seen: set[tuple[str, str]] = set()  # (can_rr, area_rr) already recorded
        for script_rr, pairs in self.script_creature_waypoint_spawns.items():
            for pair in pairs:
                bp_rr = pair["creature_resref"]
                wp_tag = pair["waypoint_tag"]
                if bp_rr not in self.creatures:
                    continue
                area_rr = (self.waypoint_area.get(wp_tag)
                           or self.tag_to_area.get(wp_tag))
                if not area_rr or area_rr not in self.areas:
                    continue
                if area_rr in self.hidden_areas:
                    continue
                can_rr = self.canonical_for_bp.get(bp_rr)
                if not can_rr:
                    continue
                key = (can_rr, area_rr)
                if key in seen:
                    continue
                seen.add(key)
                # Only add if not already covered by a placed or encounter entry
                # for this (creature, area) pair — script spawn is a fallback.
                existing = self.canonical_locations.get(can_rr, [])
                already = any(e["area"] == area_rr and e["kind"] in ("placed", "encounter")
                              for e in existing)
                if not already:
                    self.canonical_locations[can_rr].append(
                        {"area": area_rr, "kind": "script",
                         "enc_rr": None, "count": 1}
                    )
                self.area_script_spawns[area_rr].append(
                    {"bp_rr": bp_rr, "can_rr": can_rr, "script": script_rr}
                )

    def _build_direct_teleport_transitions(self) -> None:
        """Detect placeables whose OnUsed script directly calls ActionJumpToLocation
        (not via a dialog) and populate self.script_transitions.

        Must run after _parse_scripts (for script_teleport_tags) and
        _build_tag_to_area (for tag_to_area) are complete."""
        for area_resref, placeables in self.area_placeables.items():
            for p in placeables:
                bp_rr = (fld(p, "TemplateResRef", "") or "").lower()
                # Instance-level script takes priority; fall back to blueprint.
                script = ((fld(p, "OnUsed", "") or "").lower()
                          or (fld(self.placeables.get(bp_rr, {}), "OnUsed", "") or "").lower())
                if not script:
                    continue
                # Skip dialog-launching scripts — those are in conv_transitions.
                if self._script_dialog_targets(script):
                    continue
                tags = self.script_teleport_tags.get(script)
                if not tags:
                    continue
                p_tag = fld(p, "Tag", "") or bp_rr
                for tag in tags:
                    area = self.tag_to_area.get(tag)
                    if area and area != area_resref and area in self.areas:
                        self.script_transitions.append({
                            "src_area": area_resref,
                            "dst_area": area,
                            "dst_tag": tag,
                            "kind": "use",
                            "label": p_tag,
                            "fallback": tag in self.fallback_tags,
                        })

    def _walk_dialog_nodes(self, dlg: dict) -> Iterable[tuple[str, int, dict]]:
        """Yield (kind, index, node) for every Entry and Reply in a dlg."""
        for i, e in enumerate(list_items(dlg.get("EntryList"))):
            yield ("entry", i, e)
        for i, r in enumerate(list_items(dlg.get("ReplyList"))):
            yield ("reply", i, r)

    def _excluded_dialog_nodes(self, dlg: dict) -> set[tuple[str, int]]:
        """Return the set of (kind, idx) dialog nodes whose teleport scripts
        should be ignored for area-map purposes. A node is excluded if any
        ancestor entry/reply's text exact-matches (after stripping NWN colour
        tokens and surrounding whitespace) one of self.exclude_option_texts —
        we then walk RepliesList/EntriesList from that ancestor to mark the
        whole subtree."""
        if not self.exclude_option_texts:
            return set()
        targets = {t.strip() for t in self.exclude_option_texts if t.strip()}
        if not targets:
            return set()
        entries = list_items(dlg.get("EntryList"))
        replies = list_items(dlg.get("ReplyList"))
        excluded: set[tuple[str, int]] = set()

        def walk(kind: str, idx: int) -> None:
            if not isinstance(idx, int) or idx < 0:
                return
            if (kind, idx) in excluded:
                return
            excluded.add((kind, idx))
            if kind == "entry":
                if idx >= len(entries):
                    return
                for ref in list_items(entries[idx].get("RepliesList")):
                    walk("reply", fld(ref, "Index"))
            else:
                if idx >= len(replies):
                    return
                for ref in list_items(replies[idx].get("EntriesList")):
                    walk("entry", fld(ref, "Index"))

        for i, r in enumerate(replies):
            if nwn_text(loc(r.get("Text"))).strip() in targets:
                walk("reply", i)
        for i, e in enumerate(entries):
            if nwn_text(loc(e.get("Text"))).strip() in targets:
                walk("entry", i)
        return excluded

    def _synthesize_zdialogs(self) -> None:
        """Inject a pseudo-dialog entry into self.dialogs for every z-dialog
        handler that we saw via StartDlg(...). Z-dialogs aren't real .dlg
        files — their content is generated by NWScript at runtime — so the
        synthesized entry has no EntryList/ReplyList. The handler script's
        resref is stored in `__zdlg_handler__` so the conversation page can
        link to its source."""
        for handler in self.zdlg_handler_dispatchers:
            if handler in self.dialogs:
                # An actual .dlg with the same resref already exists; leave
                # it alone (the generic zdlg_converse.dlg goes here too).
                continue
            self.dialogs[handler] = {"__zdlg_handler__": handler}

    def _script_dialog_targets(self, script_resref: str) -> set[str]:
        """All dialog/z-dialog resrefs that a given event script opens.
        Unifies the regular ActionStartConversation index with the z-dialog
        StartDlg index so callers don't have to consult both."""
        out: set[str] = set()
        out.update(self.script_dialogs.get(script_resref, ()))
        out.update(self.script_zdialogs.get(script_resref, ()))
        return out
