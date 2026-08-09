"""NWN 2DA parsing, plus the CEP-hak sniff that decides whether to layer the
bundled CEP overlay onto the stock lookups.

:func:`parse_2da` / :func:`_2da_split` read the V2.0 whitespace-delimited 2DA
format used by the files extracted from a module's HAKs (``--2da-dir``).

This is a leaf module: stdlib plus :mod:`nwn_wiki.gff` only.  The override
loaders that consume these parsers (``load_2da_overrides``, ``load_json_overlay``)
still live in :mod:`nwn_wiki.cli` because they mutate the id→name lookup tables
defined there; they move once those tables do.
"""

from __future__ import annotations

from pathlib import Path

from nwn_wiki.gff import fld, list_items


def parse_2da(path: Path) -> tuple[list[str], list[list[str]]]:
    """Parse an NWN 2DA (V2.0) file. Returns (column_headers, rows).
    Each row's first cell is the row index as text. Quoted strings (with
    embedded spaces) are honoured. `****` cells are returned as empty
    strings."""
    text = path.read_text(encoding="cp1252", errors="replace")
    lines = [ln for ln in text.splitlines()]
    # Skip blank/header lines until we find the first whitespace-delimited
    # row. The first non-blank line after the magic is normally the
    # column-header row; some files include a `DEFAULT: ""` directive.
    i = 0
    while i < len(lines) and not lines[i].strip().startswith("2DA"):
        i += 1
    i += 1  # past "2DA V2.0"
    # Optional DEFAULT line
    while i < len(lines) and (not lines[i].strip() or
                              lines[i].lstrip().upper().startswith("DEFAULT")):
        i += 1
    # Column headers
    if i >= len(lines):
        return [], []
    headers = _2da_split(lines[i])
    i += 1
    rows: list[list[str]] = []
    for ln in lines[i:]:
        if not ln.strip():
            continue
        cells = _2da_split(ln)
        if not cells:
            continue
        rows.append(cells)
    return headers, rows


def _2da_split(line: str) -> list[str]:
    """Split a 2DA line on whitespace, keeping double-quoted spans together
    and decoding `****` as the empty string."""
    out: list[str] = []
    i = 0
    n = len(line)
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


def detect_cep_haks(ifo: dict | None) -> list[str]:
    """Return the subset of `Mod_HakList` entries that look like CEP haks.
    Empty list when the module is vanilla. Used to decide whether to layer
    the bundled CEP overlay onto the stock lookups."""
    if not ifo:
        return []
    haks = []
    for h in list_items(ifo.get("Mod_HakList")):
        name = (fld(h, "Mod_Hak", "") or "").lower()
        if name.startswith("cep"):
            haks.append(name)
    return haks
