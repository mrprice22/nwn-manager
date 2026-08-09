---
name: autopilot
description: Run the unattended nwn-wiki refactor loop — pick refactor-backlog.yaml items, implement, gate with wiki-golden, commit, update the backlog, and repeat until the backlog is done or compute runs out. Use when the user says "autopilot", "run the refactor loop", or "work the refactor backlog unattended".
---

# Refactor autopilot

Read **CLAUDE-refactor-autopilot.md** (repo root) and execute its loop exactly. Summary of
what you're signing up for (the runbook is authoritative — read it in full before
starting):

0. **Reconcile first**: check `autopilot-wip.md` + `git status` for a previous run cut off
   mid-item before picking anything new (runbook step 0).
1. **Confirm you are in the worktree** on branch `refactor/nwn-wiki-modules`. If you are in
   `~/GIT/nwn_manager`, **stop** — that checkout serves the live wiki-publish services.
2. Pick the lowest-numbered `pending` item in `refactor-backlog.yaml` whose `after:` deps
   are `done`. The order is dependency-driven; don't reorder it.
3. Implement it **in a fresh `general-purpose` subagent** (fresh context per item; inline
   only for trivial one-liners). Two rules dominate:
   - **Moves only** on `gate: zero-diff` items — no renames, no signature changes, no
     reformatting, no opportunistic fixes. Anything you notice goes in the item's `found:`
     list for a later phase.
   - **Never `from nwn_wiki.state import X`** — always `from nwn_wiki import state` then
     `state.X`. A `from`-import snapshots the binding and silently produces wrong output.
4. Gate with `bin/wiki-golden check` (add `--expect gates/<item-id>.json` for
   `expected-diff` items; `--all` before every push). **An item is done because the gate
   passed, not because the code looks right.** Never widen an expectation after seeing the
   diff — that's an escape-hatch situation, not a declaration edit.
5. Ship: code commit → backlog item to `done` with `commit:`, `date:`, and the `gate:`
   result line → `python3 bin/backlog-lint.py` (must be clean) → backlog commit → reset
   `autopilot-wip.md` → push to `origin refactor/nwn-wiki-modules`.
6. Repeat until the backlog is exhausted or compute runs out, then report a session
   summary in your final message.

Honor every **hard rule** in the runbook: never work outside the worktree, never merge to
`main`, never run `bin/refresh-homers-lotr-wiki`, never point `--src` at a live module
repo, never commit `.golden/`.

Pacing: work is synchronous, so chain iterations directly in one turn where possible. Use
`ScheduleWakeup` only as a long fallback (~1800s) if genuinely waiting on something, and
end the loop with `stop: true` when the runbook's stop condition is met.
