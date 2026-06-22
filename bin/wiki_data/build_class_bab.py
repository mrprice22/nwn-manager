#!/usr/bin/env python3
"""Build class_bab.json from stock classes.2da + cls_atk_*.2da.

Each class in classes.2da names an AttackBonusTable (CLS_ATK_1 = full BAB,
CLS_ATK_2 = 3/4, CLS_ATK_3 = 1/2). Those tables give the base attack bonus per
class level (row index = level-1). nwn-wiki sums these per class to derive a
creature's BAB instead of the old crude "+1 per level for full classes" guess.

Output:
  {"_source": "...", "<class_id>": [bab_at_lvl1, bab_at_lvl2, ...], ...}

Run after an NWN patch.

Usage:
  ./build_class_bab.py [--nwn DIR]
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
from _2da_lib import col_index, parse_2da, require_tools


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


def _bab_list(atk_2da: Path) -> list[int]:
    """cls_atk_N.2da → [bab_at_lvl1, bab_at_lvl2, ...] (row index = level-1)."""
    _hdrs, rows = parse_2da(atk_2da)
    by_level: dict[int, int] = {}
    for row in rows:
        if not row:
            continue
        try:
            ridx = int(row[0])
            bab = int(row[1])
        except (ValueError, IndexError):
            continue
        by_level[ridx] = bab
    if not by_level:
        return []
    return [by_level.get(i, 0) for i in range(max(by_level) + 1)]


def build(nwn: Path, out_dir: Path) -> None:
    require_tools()
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        print("extracting classes.2da")
        chdrs, crows = parse_2da(extract_stock_2da(nwn, "classes.2da", tmp))
        atk_i = col_index(chdrs, "AttackBonusTable")
        if atk_i is None:
            sys.exit("error: classes.2da has no AttackBonusTable column")

        atk_cache: dict[str, list[int]] = {}
        result: dict[str, object] = {
            "_source": "stock NWN :: classes.2da AttackBonusTable + cls_atk_*.2da",
        }
        for row in crows:
            if not row:
                continue
            try:
                cid = int(row[0])
            except ValueError:
                continue
            tbl = (row[atk_i] if atk_i < len(row) else "").strip()
            if not tbl or tbl == "****":
                continue
            tbl_lc = tbl.lower()  # CLS_ATK_1 → cls_atk_1
            if tbl_lc not in atk_cache:
                try:
                    atk_cache[tbl_lc] = _bab_list(
                        extract_stock_2da(nwn, f"{tbl_lc}.2da", tmp))
                except Exception as e:
                    print(f"  warn: {tbl_lc}: {e}", file=sys.stderr)
                    atk_cache[tbl_lc] = []
            if atk_cache[tbl_lc]:
                result[str(cid)] = atk_cache[tbl_lc]
        print(f"  {len(result) - 1} classes, tables: {sorted(atk_cache)}")

        out_path = out_dir / "class_bab.json"
        out_path.write_text(
            json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    home = Path(os.path.expanduser("~"))
    ap.add_argument("--nwn", type=Path,
                    default=home / ".local" / "share" / "Steam" / "steamapps"
                    / "common" / "Neverwinter Nights")
    args = ap.parse_args()
    build(args.nwn, Path(__file__).resolve().parent)
    print("done.")


if __name__ == "__main__":
    main()
