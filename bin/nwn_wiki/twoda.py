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

import re
import sys
from pathlib import Path

from nwn_wiki.gff import fld, list_items
from nwn_wiki.items import BASEITEM_SLOTS
from nwn_wiki.lookups import (
    APPEARANCE,
    BASEITEM_COLUMNS_SEEN,
    BASEITEMS,
    CLASSES,
    FEATS,
    IPROP_TABLES,
    IPRP_FEATS,
    PLACEABLES,
    RACE_ABILITY_ADJ,
    RACES,
    SKILLS,
    SPELLS,
    WEAPONS,
    _WEAPON_2DA_COLS,
)


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


# ---------------------------------------------------------------------------
# 2DA override loader
# ---------------------------------------------------------------------------
#
# CEP and other HAKs ship custom baseitems.2da / iprp_*.2da files that
# override or extend the stock tables. The bundled JSON lookups can't know
# what's in those HAKs, so we let the user point us at a directory of
# extracted 2DAs (`--2da-dir`) and override the relevant lookups in place.
#
# Extracting from a HAK with the neverwinter.nim toolchain:
#
#     mkdir -p hak_2da
#     nwn_erf -x -f /path/to/cepbaseitem.hak -d hak_2da
#
# Then point the wiki at it:
#
#     nwn-wiki --src unpacked --out docs --2da-dir hak_2da


def load_2da_overrides(d: Path) -> None:
    """Patch the in-memory lookup dicts from extracted 2DA files in `d`.
    Updates BASEITEMS, IPRP_FEATS, RACES, CLASSES, APPEARANCE, FEATS, and
    IPROP_TABLES in place; keys that aren't present in the override are left
    at their stock values."""
    files = {
        "baseitems.2da":     ("Label", BASEITEMS),
        "iprp_feats.2da":    ("Label", IPRP_FEATS),
        "racialtypes.2da":   ("Label", RACES),
        "classes.2da":       ("Label", CLASSES),
        "appearance.2da":    ("Label", APPEARANCE),
        "placeables.2da":    ("Label", PLACEABLES),
        "feat.2da":          ("LABEL", FEATS),
        "skills.2da":        ("Label", SKILLS),
        "spells.2da":        ("Label", SPELLS),
    }
    for fname, (col, target) in files.items():
        p = d / fname
        if not p.is_file():
            continue
        try:
            headers, rows = parse_2da(p)
        except Exception as e:
            print(f"  warn: could not parse {p}: {e}", file=sys.stderr)
            continue
        try:
            col_idx = [h.lower() for h in headers].index(col.lower()) + 1
        except ValueError:
            print(f"  warn: {p} has no '{col}' column; skipping", file=sys.stderr)
            continue
        n_loaded = 0
        for row in rows:
            if not row:
                continue
            try:
                ridx = int(row[0])
            except ValueError:
                continue
            label = row[col_idx] if col_idx < len(row) else ""
            if label:
                target[ridx] = label
                n_loaded += 1
        print(f"  override: {fname} → {n_loaded} rows")

    # Equippable slots and weapon stats from a baseitems.2da override.
    #
    # EquipableSlots is a hex bitmask over the SLOT_* constants, so it is the
    # authoritative slot map for CEP/custom base items as well as stock ones.
    #
    # The weapon columns matter just as much: the bundled weapons.json only
    # covers stock rows 0-112, so without this merge every CEP/custom weapon has
    # no damage dice, no crit stats and no weapon type at all — it would deal
    # flat bonuses only in the combat simulation. Merging here also brings in the
    # per-base-item feat columns (Weapon Focus, Improved Critical, Overwhelming /
    # Devastating Critical), which is how the simulator knows which feats a
    # wielder of that weapon could even take.
    bp = d / "baseitems.2da"
    if bp.is_file():
        try:
            headers, rows = parse_2da(bp)
            hl = [h.lower() for h in headers]
            slot_idx = hl.index("equipableslots") + 1 if "equipableslots" in hl else None
            wcol_idx = {c: hl.index(c.lower()) + 1 for c in _WEAPON_2DA_COLS
                        if c.lower() in hl}
            if slot_idx is None:
                print(f"  warn: {bp} has no 'EquipableSlots' column; "
                      "keeping the stock slot map", file=sys.stderr)
            n_slots = n_weap = 0
            for row in rows:
                if not row:
                    continue
                try:
                    ridx = int(row[0])
                except ValueError:
                    continue

                def _cell(idx: int) -> str:
                    v = row[idx] if idx < len(row) else ""
                    return "" if v in ("", "****") else v

                if slot_idx is not None and _cell(slot_idx):
                    cell = _cell(slot_idx)
                    try:
                        BASEITEM_SLOTS[ridx] = (int(cell, 16) if cell.lower().startswith("0x")
                                                else int(cell))
                        n_slots += 1
                    except ValueError:
                        pass

                BASEITEM_COLUMNS_SEEN.update(wcol_idx)
                stats = {c: _cell(i) for c, i in wcol_idx.items() if _cell(i)}
                if stats:
                    # Merge rather than replace: keep any bundled stock value
                    # whose column this 2DA happens not to carry.
                    WEAPONS.setdefault(ridx, {}).update(stats)
                    n_weap += 1
            print(f"  override: baseitems.2da → {n_slots} slot rows, "
                  f"{n_weap} weapon-stat rows")
        except Exception as e:
            print(f"  warn: could not parse {bp} weapon columns: {e}", file=sys.stderr)

    # Racial ability-score adjustments from a racialtypes.2da override (so
    # module-custom races get correct +/- ability mods). Mirrors the stock
    # race_adjust.json; a present override row replaces the stock entry.
    rp = d / "racialtypes.2da"
    if rp.is_file():
        try:
            headers, rows = parse_2da(rp)
            hl = [h.lower() for h in headers]
            adj_idx = {
                ab: (hl.index(f"{ab.lower()}adjust") + 1
                     if f"{ab.lower()}adjust" in hl else None)
                for ab in ("Str", "Dex", "Con", "Int", "Wis", "Cha")
            }
            if any(v is not None for v in adj_idx.values()):
                for row in rows:
                    if not row:
                        continue
                    try:
                        ridx = int(row[0])
                    except ValueError:
                        continue
                    vals: dict[str, int] = {}
                    for ab, idx in adj_idx.items():
                        if idx is None or idx >= len(row):
                            continue
                        cell = row[idx]
                        if not cell or cell == "****":
                            continue
                        try:
                            n = int(cell)
                        except ValueError:
                            continue
                        if n:
                            vals[ab] = n
                    if vals or ridx in RACE_ABILITY_ADJ:
                        RACE_ABILITY_ADJ[ridx] = vals
        except Exception as e:
            print(f"  warn: could not parse {rp} adjusts: {e}", file=sys.stderr)

    # iprp cost/subtype tables: merge from any iprp_*.2da that matches a known
    # IPROP_TABLES key. Custom 2das fully override stock entries for existing
    # rows (e.g. a hak that remaps iprp_immuncost row 3 from 20% to 25%).
    # Row 0 (always the "Random" sentinel) is skipped.
    # Column preference: if a "Value" column exists and the Label looks like a
    # decimal fraction (e.g. "0.25"), use Value formatted as "X%" instead —
    # this handles CEP2-style immuncost tables whose Label is a float (0.25)
    # rather than a human string (25%).
    for p in sorted(d.glob("iprp_*.2da")):
        tbl_name = p.stem
        if tbl_name not in IPROP_TABLES or tbl_name == "iprp_feats":
            continue  # iprp_feats is handled above via IPRP_FEATS dict
        try:
            headers, rows = parse_2da(p)
        except Exception as e:
            print(f"  warn: could not parse {p}: {e}", file=sys.stderr)
            continue
        hdrs_lower = [h.lower() for h in headers]
        try:
            col_idx = hdrs_lower.index("label") + 1
        except ValueError:
            continue  # no Label column — not an iprp cost table
        val_idx: int | None = None
        try:
            val_idx = hdrs_lower.index("value") + 1
        except ValueError:
            pass
        tbl = IPROP_TABLES[tbl_name]
        n_updated = 0
        for row in rows:
            if not row:
                continue
            try:
                ridx = int(row[0])
            except ValueError:
                continue
            if ridx == 0:
                continue  # sentinel row
            label = row[col_idx] if col_idx < len(row) else ""
            if not label:
                continue
            # If the label looks like a bare decimal fraction (e.g. "0.25"),
            # prefer the Value column (e.g. "25") formatted as "25%".
            display = label
            if val_idx is not None and re.fullmatch(r"0?\.\d+|1\.?0*", label):
                raw_val = row[val_idx] if val_idx < len(row) else ""
                try:
                    display = f"{int(float(raw_val))}%"
                except (ValueError, TypeError):
                    pass
            key = str(ridx)
            if tbl.get(key) != display:
                tbl[key] = display
                n_updated += 1
        if n_updated:
            print(f"  override: {p.name} → {n_updated} row(s) updated → {tbl_name}")
