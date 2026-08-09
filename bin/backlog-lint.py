#!/usr/bin/env python3
"""backlog-lint.py — validate refactor-backlog.yaml.

Run after every backlog edit, before committing.  The refactor autopilot loop
carries all of its state in this file, so a malformed entry (a dangling `after:`
dependency, an item marked done with no gate result, an expected-diff item with
no declared expectation) means the loop can no longer be trusted to resume
correctly after a restart.

Exit 0 = clean, 1 = errors.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
BACKLOG = REPO / "refactor-backlog.yaml"
GATES = REPO / "gates"

STATUSES = {"pending", "design", "done"}
GATE_KINDS = {"zero-diff", "expected-diff"}
REQUIRED = {"id", "phase", "title", "status", "gate"}


def main() -> int:
    if not BACKLOG.is_file():
        print(f"error: {BACKLOG} not found")
        return 1

    doc = yaml.safe_load(BACKLOG.read_text(encoding="utf-8")) or {}
    items = doc.get("items") or []
    errs: list[str] = []
    seen: set[str] = set()
    ids = {it.get("id") for it in items if isinstance(it, dict)}

    for i, it in enumerate(items):
        where = f"item[{i}]"
        if not isinstance(it, dict):
            errs.append(f"{where}: not a mapping")
            continue
        iid = it.get("id", "<no id>")
        where = f"{iid}"

        missing = REQUIRED - set(it)
        if missing:
            errs.append(f"{where}: missing required key(s): {', '.join(sorted(missing))}")

        if iid in seen:
            errs.append(f"{where}: duplicate id")
        seen.add(iid)

        status = it.get("status")
        if status not in STATUSES:
            errs.append(f"{where}: status {status!r} not in {sorted(STATUSES)}")

        gate = it.get("gate")
        if gate not in GATE_KINDS:
            errs.append(f"{where}: gate {gate!r} not in {sorted(GATE_KINDS)}")

        # A shipped expected-diff item must have left its declaration behind, so
        # a later reader can see what the diff was allowed to be.  Unstarted
        # items legitimately have no gate file yet — the runbook requires it to
        # be written before implementing, and `wiki-golden check --expect` fails
        # loudly if it is missing at that point.
        if gate == "expected-diff" and status == "done":
            gf = GATES / f"{iid}.json"
            if not gf.is_file():
                errs.append(f"{where}: gate=expected-diff and done, but "
                            f"{gf.relative_to(REPO)} is missing")

        for dep in it.get("after") or []:
            if dep not in ids:
                errs.append(f"{where}: after: unknown item id {dep!r}")

        # a done item must carry proof
        if status == "done":
            if not it.get("commit"):
                errs.append(f"{where}: status done but no commit:")
            if not it.get("date"):
                errs.append(f"{where}: status done but no date:")
            if not it.get("gate_result"):
                errs.append(f"{where}: status done but no gate_result: "
                            f"(record the wiki-golden output line)")

        if status == "design":
            qs = it.get("design_questions") or []
            if not qs:
                errs.append(f"{where}: status design but no design_questions")
            for q in qs:
                if not isinstance(q, dict) or "question" not in q:
                    errs.append(f"{where}: malformed design_question entry")

    # dependency ordering: an item cannot depend on a later one
    order = {it.get("id"): n for n, it in enumerate(items) if isinstance(it, dict)}
    for it in items:
        if not isinstance(it, dict):
            continue
        for dep in it.get("after") or []:
            if dep in order and order[dep] > order.get(it.get("id"), 0):
                errs.append(f"{it.get('id')}: after: {dep!r} appears later in the file")

    if errs:
        for e in errs:
            print(f"  {e}")
        print(f"\nbacklog-lint: {len(errs)} error(s)")
        return 1

    by_status: dict[str, int] = {}
    for it in items:
        by_status[it.get("status", "?")] = by_status.get(it.get("status", "?"), 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(by_status.items()))
    print(f"backlog-lint: clean — {len(items)} items ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
