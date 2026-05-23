#!/usr/bin/env python3
"""Build spell_info.json from iprp_spells.2da, iprp_onhitspell.2da, and spells.2da.

Maps each iprp_spells / iprp_onhitspell row to spell level per class and innate
level — used by nwn-wiki for Scrolls and Cast Spell / On Hit Cast Spell properties.

Output structure:
  {
    "iprp_spells":     {"<row>": {"innate_level": N, "class_levels": {...}}, ...},
    "iprp_onhitspell": {"<row>": {"innate_level": N, "class_levels": {...}}, ...}
  }

Run after any NWN patch or new HAK data that changes the spell tables.

Usage:
  ./build_spell_info.py [--nwn DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _2da_lib import col_index, load_tlk_entries, parse_2da, require_tools, tlk_to_json

# Per-class spell level columns in spells.2da.
CLASS_COLS: list[tuple[str, str]] = [
    ("Bard",     "Bard"),
    ("Cleric",   "Cleric"),
    ("Druid",    "Druid"),
    ("Paladin",  "Paladin"),
    ("Ranger",   "Ranger"),
    ("Wiz/Sorc", "Wiz_Sorc"),
]


def extract_stock_2da(nwn_root: Path, name: str, dest: Path) -> Path:
    subprocess.run(
        ["nwn_resman_extract", "--root", str(nwn_root), name],
        cwd=dest, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    p = dest / name
    if not p.is_file():
        raise FileNotFoundError(f"{name} not produced from {nwn_root}")
    return p


def _parse_spells_2da(spells_hdrs: list[str], spells_rows: list[list[str]],
                      dialog_strings: dict[int, str]) -> dict[int, dict]:
    """Build spells.2da row_id → {name, innate_level, levels} index."""
    class_cols_idx = [
        (label, col_index(spells_hdrs, col))
        for label, col in CLASS_COLS
    ]
    name_idx   = col_index(spells_hdrs, "Name")
    innate_idx = col_index(spells_hdrs, "Innate_Level") or col_index(spells_hdrs, "Innate")

    result: dict[int, dict] = {}
    for row in spells_rows:
        if not row:
            continue
        try:
            ridx = int(row[0])
        except ValueError:
            continue

        levels: dict[str, int] = {}
        for label, idx in class_cols_idx:
            if idx is None or idx >= len(row):
                continue
            val = row[idx]
            if val:
                try:
                    lvl = int(val)
                    if 0 <= lvl <= 9:
                        levels[label] = lvl
                except ValueError:
                    pass

        spell_name = ""
        if name_idx is not None and name_idx < len(row):
            try:
                ref = int(row[name_idx])
                spell_name = dialog_strings.get(ref, "")
            except (ValueError, TypeError):
                pass

        innate_lvl: int | None = None
        if innate_idx is not None and innate_idx < len(row):
            try:
                innate_lvl = int(row[innate_idx])
            except (ValueError, TypeError):
                pass

        result[ridx] = {"name": spell_name, "innate_level": innate_lvl, "levels": levels}
    return result


def _get_col(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def _build_iprp_table(iprp_2da: Path, spell_row_data: dict[int, dict]) -> dict[str, dict]:
    """Parse an iprp_spells-style 2DA; return {row_id_str: spell_info_entry}.

    Tries both 'SpellIndex' and 'Spell' as the spell reference column name.
    Falls back to spells.2da innate level when iprp file lacks InnateLvl.
    """
    hdrs, rows = parse_2da(iprp_2da)

    # iprp_spells.2da uses SpellIndex; iprp_onhitspell.2da may use Spell
    spell_idx_col = col_index(hdrs, "SpellIndex") or col_index(hdrs, "Spell")
    innate_col    = col_index(hdrs, "InnateLvl") or col_index(hdrs, "Innate_Level")
    caster_col    = col_index(hdrs, "CasterLvl")

    result: dict[str, dict] = {}
    for row in rows:
        if not row:
            continue
        try:
            ridx = int(row[0])
        except ValueError:
            continue

        spell_idx: int | None = None
        raw = _get_col(row, spell_idx_col)
        if raw:
            try:
                spell_idx = int(raw)
            except ValueError:
                pass

        innate_lvl: int | None = None
        raw = _get_col(row, innate_col)
        if raw:
            try:
                innate_lvl = int(raw)
            except ValueError:
                pass

        caster_lvl: int | None = None
        raw = _get_col(row, caster_col)
        if raw:
            try:
                caster_lvl = int(raw)
            except ValueError:
                pass

        class_levels: dict[str, int] = {}
        if spell_idx is not None and spell_idx in spell_row_data:
            sdata = spell_row_data[spell_idx]
            class_levels = sdata["levels"]
            if innate_lvl is None and sdata.get("innate_level") is not None:
                innate_lvl = sdata["innate_level"]

        entry: dict = {}
        if innate_lvl is not None:
            entry["innate_level"] = innate_lvl
        if caster_lvl is not None:
            entry["caster_level"] = caster_lvl
        if class_levels:
            entry["class_levels"] = class_levels

        result[str(ridx)] = entry

    return result


def build(nwn_dir: Path, out_dir: Path) -> None:
    require_tools()

    dialog_tlk = nwn_dir / "lang" / "en" / "data" / "dialog.tlk"
    if not dialog_tlk.exists():
        sys.exit(f"error: missing {dialog_tlk}")

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        print("converting dialog.tlk → json")
        dialog_strings = load_tlk_entries(tlk_to_json(dialog_tlk, tmp))
        print(f"  {len(dialog_strings)} entries")

        print("extracting spells.2da")
        spells_2da = extract_stock_2da(nwn_dir, "spells.2da", tmp)
        spells_hdrs, spells_rows = parse_2da(spells_2da)
        spell_row_data = _parse_spells_2da(spells_hdrs, spells_rows, dialog_strings)
        print(f"  {len(spell_row_data)} spell rows")

        result: dict[str, object] = {
            "_source": "stock NWN :: iprp_spells.2da + iprp_onhitspell.2da + spells.2da",
        }

        for fname, tbl_key in [
            ("iprp_spells.2da",     "iprp_spells"),
            ("iprp_onhitspell.2da", "iprp_onhitspell"),
        ]:
            print(f"extracting {fname}")
            try:
                iprp_2da = extract_stock_2da(nwn_dir, fname, tmp)
            except Exception as e:
                print(f"  warn: could not extract {fname}: {e}", file=sys.stderr)
                continue
            entries = _build_iprp_table(iprp_2da, spell_row_data)
            result[tbl_key] = entries
            print(f"  {len(entries)} rows")

        out_path = out_dir / "spell_info.json"
        out_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        total = sum(
            len(v) for k, v in result.items()
            if not k.startswith("_") and isinstance(v, dict)
        )
        print(f"wrote {out_path}: {total} total entries")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    home = Path(os.path.expanduser("~"))
    ap.add_argument("--nwn", type=Path,
                    default=home / ".local" / "share" / "Steam" / "steamapps"
                    / "common" / "Neverwinter Nights")
    args = ap.parse_args()
    out_dir = Path(__file__).resolve().parent
    build(args.nwn, out_dir)
    print("done.")


if __name__ == "__main__":
    main()
