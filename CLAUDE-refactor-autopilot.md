# Refactor autopilot — unattended `nwn-wiki` modularisation loop

This is the runbook for **refactor autopilot mode**: an unattended Claude session that
works the `refactor-backlog.yaml` backlog item by item until it runs out of compute (or
the backlog is exhausted). Start it with the `/autopilot` skill, or by telling a session
"follow CLAUDE-refactor-autopilot.md".

It is a sibling of `nwn_homers_lotr/CLAUDE-autopilot.md` and borrows that loop's shape
(files-as-state, fresh subagent per item, hard gate, escape hatches, WIP marker). The
domain is different: this loop refactors one Python program and proves each step by
diffing generated output, rather than building a module and running smoke tests.

## The one thing that makes this safe

`bin/nwn-wiki` generates a static HTML wiki. That output is a **complete behavioural
fingerprint**: if the regenerated tree is byte-identical, the refactor step was correct.
`bin/wiki-golden` turns that into a mechanical gate.

**A backlog item is not done because the code looks right. It is done because
`bin/wiki-golden check` passed.** Never report an item shipped without citing a gate run.

## Where this runs

**Only inside the `refactor/nwn-wiki-modules` worktree** (normally
`~/GIT/nwn_manager-refactor`). The parent checkout `~/GIT/nwn_manager` stays on a stable
commit because `homers-lotr-wiki-publish.service` and `nwn-season-wiki-publish@.service`
put its `bin/` on their `PATH` — a half-migrated package there would break the live wiki
publish. See "Hard rules".

## Context economy

- **Files are the only state.** Each iteration must be executable with zero memory of the
  previous one: `refactor-backlog.yaml` and git are the entire loop state. Never depend on
  conversation history for what is done or in flight.
- **Fresh context per item — delegate implementation to a subagent.** The orchestrating
  session runs the loop bookkeeping (select, gate, backlog edits, commits, pushes) and
  launches a `general-purpose` subagent (synchronous, `run_in_background: false`) per item.
  Each subagent starts clean — the equivalent of `/clear` between items. Its brief must
  include: the item's `id`/`title`/`plan`, an instruction to read this runbook first, the
  moves-only rule, and what to report back. Trivial items (delete a dead function, add one
  helper) may be done inline — don't pay a subagent spin-up for a one-liner.
- **Don't read the monolith whole.** It is ~18k lines. Grep for symbols and read ranges.
  The backlog item names the line ranges it covers.

## The loop

Each iteration works exactly **one** item, end to end.

### 0. Reconcile

Before picking anything, read `autopilot-wip.md` and run `git status --porcelain`.

- **`id: none` and a clean tree** → nothing to reconcile, go to step 1.
- **`stage: shipping` with a `commit:` hash, but the backlog doesn't yet show that item
  `done` with that hash** → the code already shipped; resume at step 5.2 with the recorded
  hash.
- **`stage: implementing`/`gate` for a named item, or a dirty tree** → that item was
  interrupted. Resume *it*, don't pick a new one. Inspect what is on disk and either
  finish it (→ step 4) or take the step 3b escape hatch. Never `reset --hard` or
  `clean -f` without confirming first.

### 1. Select

Re-read `refactor-backlog.yaml` fresh. Pick the lowest-numbered `pending` item whose
`after:` dependencies are all `done`. The backlog is **deliberately ordered** — the
extraction sequence is dependency-driven (leaf modules before the modules that import
them), so do not reorder it on judgment.

If nothing is selectable, go to "Stopping".

### 2. Implement

Set `autopilot-wip.md` to the item's `id`, a `started` timestamp, `stage: implementing`.
Append every file you touch to its `files:` line — that manifest is the only thing the
session-boundary safety net is allowed to stage. Keep `notes:` a current one-line summary.

**The moves-only rule (phase 1 items).** A `gate: zero-diff` extraction item is a *move*:
cut a block of code from the monolith, paste it into a new module, fix up imports. No
renames, no signature changes, no reformatting, no "while I'm here" cleanups, no comment
rewrites. Anything you notice that wants fixing goes into the item's `found:` list for a
later phase — it does **not** get fixed now. Mixing a fix into a move destroys the whole
value of the zero-diff gate, because you can no longer tell which change caused a diff.

**The mutable-state rule — this is the one way to silently break the build.** Several
module globals are written at runtime by loaders and read later by renderers. After the
split they live in `nwn_wiki/state.py` and **must always be accessed through the module
object**:

```python
from nwn_wiki import state          # yes
state.HAS_ACTIVITY_PAGE             # yes — reads the current value

from nwn_wiki.state import HAS_ACTIVITY_PAGE   # NO — snapshots the binding
```

A `from`-import of mutable state produces output that is wrong rather than broken, and
the gate will catch it only if the corpus happens to exercise that path. Never use one.

**Declaring an intended diff (`gate: expected-diff` items).** Write the expectation
*before* implementing, as a JSON file under `gates/<item-id>.json`:

```json
{
  "reason": "gate the Pictures nav entry on there actually being creature pics",
  "paths": ["docs/**/*.html"],
  "line_patterns": ["creatures/pictures\\.html"]
}
```

`paths` is a glob allowlist of files permitted to differ; `line_patterns` (optional) are
regexes every added/removed line must match. If the actual diff exceeds the declaration,
that is a **hard failure** — do not widen the declaration to make it pass. Widening it
after seeing the diff defeats the point: the declaration is a prediction, and a wrong
prediction means you did not understand the change. Take an escape hatch instead.

### 3. Gate

```
bin/wiki-golden check                              # zero-diff items
bin/wiki-golden check --expect gates/<item-id>.json  # expected-diff items
```

Primary corpus (`homers_lotr`) per item. **Before every push**, run `--all` (adds the two
frozen CEP2 archives, which exercise the no-custom-TLK / no-hak_2da / no-`docs.manual`
paths the live module never hits).

**Known gate blind spots** — two things the byte gate does not actually prove:

- **`module-index/` JSON is normalised before comparison.** `_normalise()` rewrites every
  `module-index/*.json` through `json.dumps(sort_keys=True, indent=2)` with `generated_at`
  dropped, so key order, indentation and float repr in that subtree are invisible to the
  gate. An item touching those writers must compensate with a raw double-build comparison
  (build at HEAD and with the change, `_normalise` patched off, diff byte-for-byte) — see
  `module-index-split`'s `impl_notes` for the recipe.
- **Nothing executes the JavaScript.** The gate compares emitted `<script>` text only, and
  no JS runtime is installed on this box. A regression in `site.js` — search, nav, map
  pan/zoom — ships silently. Reason it through by hand and say so in `impl_notes`.

The gate must pass before shipping. On failure: fix and re-run. If the diff is not
understood after two attempts, escape-hatch it — a mysterious diff is exactly the
situation where guessing costs the most.

For an `expected-diff` item that passed, run `bin/wiki-golden rebaseline` (and record a
sample hunk in the item's `impl_notes`) so subsequent items compare against the new truth.

#### 3a. Escape hatch: design question

If an item turns out to need a judgment only the admin should make — a behavioural fork
to resolve, a naming decision with no obvious answer, a change whose diff is legitimate
but larger than declared — set the item's `status: design`, append the blocking question
to its `design_questions` (each `status: open`, `answer: null`, **with your recommended
answer in the question text**), commit whatever partial work still gates green, and move
to the next item. Never silently pick for the admin.

Only resume a `design` item once **every** one of its `design_questions` is `answered`.

#### 3b. Escape hatch: too big

If the item is legitimate but can't be finished this iteration: append a dated progress
note to `notes`, leave it `pending`, commit whatever partial work gates green, and
continue it next iteration. After ~2 stalled iterations, convert it to a design question
(3a) with the scope problem written up — or split it in the backlog into two items.

### 4. Ship

1. **Commit** on `refactor/nwn-wiki-modules`. Message format:
   `nwn-wiki refactor: <what moved/changed> (backlog: <item-id>)`.
   Immediately update `autopilot-wip.md`: `stage: shipping`, `commit:` the hash.
2. **Update the backlog item**: `status: done`, `commit:` the hash, `date:` today's actual
   date, `gate:` the result line from step 3 (e.g. `homers_lotr IDENTICAL (241s)`), plus
   `impl_notes` (what moved where, anything surprising) and any `found:` entries noticed
   along the way. Run `python3 bin/backlog-lint.py` — must be clean.
3. **Commit the backlog** as its own commit, then reset `autopilot-wip.md` to `id: none`
   (it is gitignored — a plain file write, not part of any commit).
4. **Push** to `origin refactor/nwn-wiki-modules` after running the `--all` gate, so
   nothing sits unpushed.

### 5. Loop

Back to step 1. Work is synchronous, so chain iterations directly in one turn. Use
`ScheduleWakeup` only as a long fallback (~1800s) if genuinely waiting on something.

## Session-boundary safety net

`autopilot-wip.md` (repo root, gitignored, machine-maintained) plus
`bin/autopilot-safety-commit`, wired to the `PreCompact` and `SessionEnd` hooks in
`.claude/settings.json`. The script only acts when the marker shows an active item **and**
its `files:` line names paths that actually changed; it stages exactly those and nothing
else. It never runs `git add -A`. It is a no-op in ordinary interactive sessions.

It cannot write handoff notes — that needs judgment — so the live `notes:` line kept
during step 2 is the real handoff.

## Hard rules — never do these

- **Never work outside the worktree.** Do not edit, checkout, or commit in
  `~/GIT/nwn_manager`; do not touch `~/GIT/nwn_homers_lotr` or the two archive repos at
  all. They are read-only corpora. `bin/wiki-golden` already reflink-copies them into
  `.golden/work/` so the build cannot write back — never bypass it by pointing `--src` at
  a live repo, which would rewrite that repo's `module-index/`.
- **Never merge to `main`**, and never switch the parent checkout's branch. The live wiki
  publish services run from it.
- **Never run `bin/refresh-homers-lotr-wiki`** — it writes and can publish the real
  `docs/` tree. `bin/wiki-golden` is the only sanctioned way to build the wiki here.
- **Never touch the systemd units** or mask/stop the publish timers.
- **Never commit `.golden/`** (gitignored — it is hundreds of MB of build output).
- **Never widen an `expected_diff` declaration after seeing the diff** (see step 2).
- **Never use `from nwn_wiki.state import X`** (see step 2).
- **Never mark an item `done` without a passing gate run recorded in its `gate:` field.**
- **Never delete the monolith's code without it landing somewhere** — every phase-1 item
  is a move, and the gate proves nothing was dropped only if the code still runs.

## Stopping

Stop when every item is `done`/`design`, or compute runs out. On stop: make sure the tree
is committed and pushed, then report a session summary (items shipped, items parked in
`design`, gate status, LOC moved) in the final message. If running under `/autopilot`
dynamic pacing, end the loop with `ScheduleWakeup stop: true`.
