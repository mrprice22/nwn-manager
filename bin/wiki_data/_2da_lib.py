"""Shared helpers for the wiki_data builders (`_build_stock.py` and
`cep/_build.py`). Pure stdlib + the `nwn_erf` / `nwn_tlk` CLIs from the
neverwinter package on PATH.

These are import-time-cheap utilities; the actual TARGETS lists live in
the per-builder scripts so each can declare its own source HAKs and
column names without coupling to the other.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

# NWN custom-TLK strrefs are biased by 0x01000000 (bit 24 set). Anything
# below this is in the stock dialog.tlk; anything at-or-above resolves
# (after subtracting the offset) into the module's custom TLK.
CUSTOM_TLK_OFFSET = 0x01000000


def _2da_split(line: str) -> list[str]:
    out, i, n = [], 0, len(line)
    while i < n:
        while i < n and line[i] in (" ", "\t"):
            i += 1
        if i >= n:
            break
        if line[i] == '"':
            j = i + 1
            while j < n and line[j] != '"':
                j += 1
            out.append(line[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and line[j] not in (" ", "\t"):
                j += 1
            tok = line[i:j]
            out.append("" if tok == "****" else tok)
            i = j
    return out


def parse_2da(path: Path) -> tuple[list[str], list[list[str]]]:
    """Parse a 2DA V2.0 file. Returns (headers, rows). row[0] is the row
    index as text; subsequent cells correspond to `headers` 1-indexed."""
    text = path.read_text(encoding="cp1252", errors="replace")
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip().startswith("2DA"):
        i += 1
    i += 1
    while i < len(lines) and (not lines[i].strip()
                              or lines[i].lstrip().upper().startswith("DEFAULT")):
        i += 1
    if i >= len(lines):
        return [], []
    headers = _2da_split(lines[i])
    rows = []
    for ln in lines[i + 1:]:
        if not ln.strip():
            continue
        cells = _2da_split(ln)
        if cells:
            rows.append(cells)
    return headers, rows


def col_index(headers: list[str], name: str) -> int | None:
    """Locate a column by case-insensitive name. Returns the index into a
    row produced by `parse_2da` (which is +1 of the header position because
    row[0] is the row index)."""
    lower = [h.lower() for h in headers]
    if name.lower() in lower:
        return lower.index(name.lower()) + 1
    return None


def load_tlk_entries(tlk_json: Path) -> dict[int, str]:
    """Load a TLK previously converted with `nwn_tlk -k json`."""
    d = json.loads(tlk_json.read_text())
    return {int(e["id"]): e.get("text", "") for e in d.get("entries", [])
            if isinstance(e, dict) and "id" in e}


def title_label(s: str) -> str:
    """Best-effort prettify of a 2DA Label cell when no TLK string is
    available. `shortsword` → `Shortsword`."""
    s = s.replace("_", " ").strip()
    return s[:1].upper() + s[1:] if s else s


def resolve_name(name_cell: str, label_cell: str,
                 dialog: dict[int, str], custom: dict[int, str]) -> str:
    """Pick a display string for a 2DA row.

    Order of preference:
      1. Custom TLK lookup (Name >= 0x01000000) — engine-side custom strings.
      2. Stock dialog.tlk lookup (small Name) — most rows.
      3. Title-cased Label cell — last resort when no TLK ref is set.
    """
    s = ""
    try:
        ref = int(name_cell)
    except (TypeError, ValueError):
        ref = -1
    if ref >= CUSTOM_TLK_OFFSET:
        s = custom.get(ref - CUSTOM_TLK_OFFSET, "")
    elif ref >= 0:
        s = dialog.get(ref, "")
    return s or title_label(label_cell)


def require_tools() -> None:
    """Hard-fail if the neverwinter CLIs aren't on PATH."""
    missing = [t for t in ("nwn_erf", "nwn_tlk") if shutil.which(t) is None]
    if missing:
        sys.exit(f"error: {', '.join(missing)} must be on PATH "
                 f"(see project README — `nimble install neverwinter`).")


def extract_2da(hak: Path, name: str, dest: Path) -> Path:
    """Extract a single 2da from an ERF/HAK using nwn_erf into dest dir."""
    subprocess.run(
        ["nwn_erf", "-x", "-f", str(hak), name],
        cwd=dest, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    p = dest / name
    if not p.is_file():
        raise FileNotFoundError(f"{name} not produced by nwn_erf -x from {hak}")
    return p


def tlk_to_json(tlk: Path, dest: Path) -> Path:
    """Convert a .tlk to .json via nwn_tlk; returns the produced .json path."""
    out = dest / (tlk.stem + ".json")
    subprocess.run(
        ["nwn_tlk", "-i", str(tlk), "-o", str(out), "-k", "json"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    return out
