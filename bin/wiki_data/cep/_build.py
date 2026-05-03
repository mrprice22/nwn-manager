#!/usr/bin/env python3
"""Build CEP overlay JSONs from extracted CEP HAKs + TLKs.

Run once when bumping CEP versions. Reads:
  - cep2da.hak           (baseitems, racialtypes, appearance, placeables, portraits)
  - cep.tlk              (CEP custom string table, for added rows)
  - dialog.tlk           (stock NWN, for stock-pointing rows in CEP 2DAs)

Writes JSON pairs into the script's directory:
  - baseitems.json, racialtypes.json, appearance.json, placeables.json, portraits.json

Each JSON is {"<row>": "<pretty name>"}, matching the bundled stock layout in
../*.json. The wiki tool merges these on top of the stock dicts when it sees
a `cep*` HAK in the module's IFO (see nwn-wiki).

Usage:
  ./_build.py [--cep DIR] [--nwn DIR]

Defaults assume the layout used during initial CEP 2.71 ingest:
  --cep ~/Downloads/CEP_2.71_full_with_hotfix1
  --nwn ~/.local/share/Steam/steamapps/common/Neverwinter Nights
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _2da_lib import (
    col_index, extract_2da, load_tlk_entries, parse_2da, require_tools,
    resolve_name, tlk_to_json,
)

# (hak filename, 2da filename, output json basename, "Name" col, "Label" col)
#
# feat.2da is intentionally skipped: the wiki only renders a *count* of
# creature feats, never their names, and CEP's feat.2da reserves ~24k rows
# for PRC compatibility — bundling it would add ~650 KB of data nothing
# reads. Re-add it here if/when the wiki starts surfacing feat names.
TARGETS = [
    ("cep2da.hak", "baseitems.2da",   "baseitems",   "Name",       "label"),
    ("cep2da.hak", "racialtypes.2da", "racialtypes", "Name",       "Label"),
    ("cep2da.hak", "appearance.2da",  "appearance",  "STRING_REF", "LABEL"),
    ("cep2da.hak", "placeables.2da",  "placeables",  "StrRef",     "Label"),
    ("cep2da.hak", "portraits.2da",   "portraits",   "BaseResRef", "BaseResRef"),
]


def build(cep_dir: Path, nwn_dir: Path, out_dir: Path) -> None:
    require_tools()

    hak_dir = cep_dir / "hak"
    dialog_tlk = nwn_dir / "lang" / "en" / "data" / "dialog.tlk"
    cep_tlk = cep_dir / "tlk" / "cep.tlk"

    for p in (hak_dir, dialog_tlk, cep_tlk):
        if not p.exists():
            sys.exit(f"error: missing {p}")

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        print(f"converting TLKs → json (in {tmp})")
        dialog_strings = load_tlk_entries(tlk_to_json(dialog_tlk, tmp))
        cep_strings = load_tlk_entries(tlk_to_json(cep_tlk, tmp))
        print(f"  dialog.tlk: {len(dialog_strings)} entries")
        print(f"  cep.tlk:    {len(cep_strings)} entries")

        for hak_name, twoda_name, out_name, name_col, label_col in TARGETS:
            hak = hak_dir / hak_name
            if not hak.exists():
                print(f"warn: {hak} missing — skipping {twoda_name}", file=sys.stderr)
                continue
            print(f"  · {hak_name} → {twoda_name}")
            twoda = extract_2da(hak, twoda_name, tmp)
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
                pretty = resolve_name(name_cell, label_cell, dialog_strings, cep_strings)
                if pretty:
                    mapping[str(ridx)] = pretty

            out_path = out_dir / f"{out_name}.json"
            out_path.write_text(
                json.dumps({"_source": f"cep2 v2.71 :: {hak_name}::{twoda_name}",
                            **mapping},
                           indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"    wrote {out_path.name}: {len(mapping)} rows")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    home = Path(os.path.expanduser("~"))
    ap.add_argument("--cep", type=Path,
                    default=home / "Downloads" / "CEP_2.71_full_with_hotfix1",
                    help="CEP install root (containing hak/ and tlk/)")
    ap.add_argument("--nwn", type=Path,
                    default=home / ".local" / "share" / "Steam" / "steamapps"
                    / "common" / "Neverwinter Nights",
                    help="NWN install root (for lang/en/data/dialog.tlk)")
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent
    build(args.cep, args.nwn, out_dir)
    print("done.")


if __name__ == "__main__":
    main()
