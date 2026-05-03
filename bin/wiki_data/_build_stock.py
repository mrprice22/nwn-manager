#!/usr/bin/env python3
"""Build the bundled stock NWN1 lookup JSONs in this directory from a
local NWN install. Run when bumping NWN versions (Beamdog EE patches
occasionally tweak labels — Divine Champion → Champion of Torm, etc.)
or to bootstrap a fresh checkout from authoritative sources.

Reads via `nwn_resman_extract`:
  - baseitems.2da, racialtypes.2da, classes.2da, iprp_feats.2da,
    appearance.2da
  - lang/en/data/dialog.tlk (TLK ref → pretty name)

Writes:
  - baseitems.json, racialtypes.json, classes.json, iprp_feats.json,
    appearance.json

Usage:
  ./_build_stock.py [--nwn DIR]

Default --nwn is ~/.local/share/Steam/steamapps/common/Neverwinter Nights.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _2da_lib import (
    col_index, load_tlk_entries, parse_2da, require_tools, resolve_name,
    tlk_to_json,
)

# (2da basename, output json basename, "Name" col, "Label" col)
#
# placeables.2da is intentionally absent: stock placeable rows are rarely
# interesting (player-visible placeables almost always come from a HAK)
# and bundling 1000+ rows of "PLC_*"-style labels would dwarf the rest of
# the file. Custom HAKs supply placeable names through the auto-detected
# CEP overlay or `--2da-dir`.
TARGETS = [
    ("baseitems.2da",   "baseitems",   "Name", "label"),
    ("racialtypes.2da", "racialtypes", "Name", "Label"),
    ("classes.2da",     "classes",     "Name", "Label"),
    ("iprp_feats.2da",  "iprp_feats",  "Name", "Label"),
    ("appearance.2da",  "appearance",  "STRING_REF", "LABEL"),
]


def extract_stock_2da(nwn_root: Path, name: str, dest: Path) -> Path:
    """Pull a stock 2DA out of the NWN install via nwn_resman_extract."""
    subprocess.run(
        ["nwn_resman_extract", "--root", str(nwn_root), name],
        cwd=dest, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    p = dest / name
    if not p.is_file():
        raise FileNotFoundError(f"{name} not produced from {nwn_root}")
    return p


def build(nwn_dir: Path, out_dir: Path) -> None:
    require_tools()
    if shutil.which("nwn_resman_extract") is None:
        sys.exit("error: nwn_resman_extract must be on PATH "
                 "(included in the neverwinter package).")

    dialog_tlk = nwn_dir / "lang" / "en" / "data" / "dialog.tlk"
    if not dialog_tlk.exists():
        sys.exit(f"error: missing {dialog_tlk}")

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        print(f"converting dialog.tlk → json (in {tmp})")
        dialog_strings = load_tlk_entries(tlk_to_json(dialog_tlk, tmp))
        print(f"  dialog.tlk: {len(dialog_strings)} entries")

        for twoda_name, out_name, name_col, label_col in TARGETS:
            print(f"  · stock → {twoda_name}")
            try:
                twoda = extract_stock_2da(nwn_dir, twoda_name, tmp)
            except Exception as e:
                print(f"    warn: could not extract {twoda_name}: {e}",
                      file=sys.stderr)
                continue
            headers, rows = parse_2da(twoda)
            n_idx = col_index(headers, name_col)
            l_idx = col_index(headers, label_col)
            if l_idx is None and n_idx is None:
                print(f"    warn: no '{name_col}' or '{label_col}' column; skipping",
                      file=sys.stderr)
                continue

            mapping: dict[str, str] = {}
            for row in rows:
                if not row:
                    continue
                try:
                    ridx = int(row[0])
                except ValueError:
                    continue
                name_cell = row[n_idx] if (n_idx is not None and n_idx < len(row)) else ""
                label_cell = row[l_idx] if (l_idx is not None and l_idx < len(row)) else ""
                pretty = resolve_name(name_cell, label_cell, dialog_strings, {})
                if pretty:
                    mapping[str(ridx)] = pretty

            out_path = out_dir / f"{out_name}.json"
            out_path.write_text(
                json.dumps({"_source": f"stock NWN :: {twoda_name}",
                            **mapping},
                           indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"    wrote {out_path.name}: {len(mapping)} rows")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    home = Path(os.path.expanduser("~"))
    ap.add_argument("--nwn", type=Path,
                    default=home / ".local" / "share" / "Steam" / "steamapps"
                    / "common" / "Neverwinter Nights",
                    help="NWN install root (containing lang/en/data/dialog.tlk)")
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent
    build(args.nwn, out_dir)
    print("done.")


if __name__ == "__main__":
    main()
