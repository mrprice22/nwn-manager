#!/usr/bin/env python3
"""Build cr_info.json: the stock 2DA numbers the toolset's challenge-rating
formula reads (nwn_wiki/cr.py, bin/nwn-cr).

The bundled name lookups (classes.json, feat.json, ...) carry labels only. The
CR formula needs numeric columns from six tables, so they get their own file:

  classes.2da      HitDie, SavingThrowTable (-> cls_savthr_*.2da per level)
  racialtypes.2da  CRModifier
  feat.2da         CRValue        (only rows with a non-zero value)
  spells.2da       Innate         (spell innate level)
  creaturespeed.2da  WALKRATE, 2DAName
  appearance.2da   MOVERATE       (used when a creature's WalkRate is 7 = Default)

A module's own 2DAs (``nwn-cr --2da-dir``) override these row by row at run
time, the same way ``nwn-wiki --2da-dir`` overrides the name tables.

Output:
  {"_source": "...",
   "classes": {"<id>": {"hit_die": 10, "save_table": "cls_savthr_fight"}},
   "save_tables": {"cls_savthr_fight": [[fort, ref, will] at level 1, ...]},
   "races": {"<id>": 1.0}, "feats": {"<id>": 0.5}, "spells": {"<id>": 3},
   "speeds": {"<id>": {"walk": 1.75, "name": "NORM"}}, "appearance_move": {"<id>": "NORM"}}

Run after an NWN patch.

Usage:
  ./build_cr_info.py [--nwn DIR]
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
from _2da_lib import col_index, parse_2da  # noqa: E402


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


def _rows(path: Path):
    """[(row id, {column: cell})] with '****' cells dropped."""
    hdrs, rows = parse_2da(path)
    out = []
    for row in rows:
        if not row:
            continue
        try:
            rid = int(row[0])
        except ValueError:
            continue
        cells = {h: row[i + 1] for i, h in enumerate(hdrs)
                 if i + 1 < len(row) and row[i + 1] != "****"}
        out.append((rid, cells))
    return out


def _num(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def saves_table(path: Path) -> list[list[int]]:
    """cls_savthr_*.2da -> [[fort, ref, will] at level 1, at level 2, ...]."""
    by_level = {}
    for _rid, c in _rows(path):
        lvl = int(_num(c.get("Level"), 0) or 0)
        if lvl > 0:
            by_level[lvl] = [int(_num(c.get(k), 0) or 0)
                             for k in ("FortSave", "RefSave", "WillSave")]
    return [by_level.get(i, [0, 0, 0]) for i in range(1, max(by_level, default=0) + 1)]


def build(nwn: Path, out_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        get = lambda n: extract_stock_2da(nwn, n, tmp)  # noqa: E731
        result: dict = {"_source": "stock NWN :: classes, cls_savthr_*, racialtypes, feat, "
                                   "spells, creaturespeed, appearance (build_cr_info.py)"}

        sav_cache: dict[str, list] = {}
        classes = {}
        for rid, c in _rows(get("classes.2da")):
            ent = {"hit_die": int(_num(c.get("HitDie"), 8) or 8)}
            tbl = (c.get("SavingThrowTable") or "").lower()
            if tbl:
                if tbl not in sav_cache:
                    try:
                        sav_cache[tbl] = saves_table(get(tbl + ".2da"))
                    except Exception as e:  # a hak-only table name
                        print(f"  warn: {tbl}: {e}", file=sys.stderr)
                        sav_cache[tbl] = []
                ent["save_table"] = tbl
            classes[str(rid)] = ent
        result["classes"] = classes
        result["save_tables"] = {k: v for k, v in sav_cache.items() if v}
        result["races"] = {str(r): _num(c.get("CRModifier"), 1.0)
                           for r, c in _rows(get("racialtypes.2da"))}
        result["feats"] = {str(r): v for r, c in _rows(get("feat.2da"))
                           if (v := _num(c.get("CRValue"), 0.0))}
        result["spells"] = {str(r): v for r, c in _rows(get("spells.2da"))
                            if (v := _num(c.get("Innate"))) is not None}
        result["speeds"] = {str(r): {"walk": _num(c.get("WALKRATE"), 0.0),
                                     "name": (c.get("2DAName") or "").upper()}
                            for r, c in _rows(get("creaturespeed.2da"))}
        result["appearance_move"] = {str(r): c["MOVERATE"].upper()
                                     for r, c in _rows(get("appearance.2da"))
                                     if c.get("MOVERATE")}
        print(f"  {len(classes)} classes ({len(sav_cache)} save tables), "
              f"{len(result['races'])} races, {len(result['feats'])} feats with a CR value, "
              f"{len(result['spells'])} spells, {len(result['appearance_move'])} appearances")
        out_path = out_dir / "cr_info.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n",
                            encoding="utf-8")
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
