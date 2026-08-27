#!/usr/bin/env python3
"""Patch itemprops.json with the iprp cost/subtype tables that stock NWN uses
for Damage Reduction and Immunity: Specific Spell — and correct those two
property mappings.

Background: the hand-maintained itemprops.json had property 22 (Damage
Reduction) pointed at `iprp_reducestype`/`iprp_softcost` (→ "+9 … 10/+2") and
property 53 (Immunity: Specific Spell) pointed at `iprp_spells` (→ wrong spell
names). The authoritative tables are, per stock `itempropdef.2da`:

  prop 22  SubTypeResRef = IPRP_PROTECTION   CostTableResRef = 6 (IPRP_SOAKCOST)
  prop 53  SubTypeResRef = ****              CostTableResRef = 16 (IPRP_SPELLCOST)

Property 84 (Arcane Spell Failure) had no cost table at all, so
`itemprop_format` dropped the amount silently and every prop-84 line rendered
with a blank Value ("Arcane Spell Failure" with no percentage). Per
itempropdef.2da:

  prop 84  SubTypeResRef = ****              CostTableResRef = 27 (IPRP_ARCSPELL)

This script extracts those four 2DAs, builds display tables, and writes them
(plus the corrected property mappings) into itemprops.json. Re-run after an NWN
patch. Other tables in itemprops.json are left untouched.

Usage:
  ./build_itemprop_tables.py [--nwn DIR]
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
from _2da_lib import (
    col_index, load_tlk_entries, parse_2da, require_tools, resolve_name,
    tlk_to_json,
)


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


def _rows_by_id(hdrs, rows):
    out = {}
    for row in rows:
        if not row:
            continue
        try:
            out[int(row[0])] = row
        except ValueError:
            pass
    return out


def build_soakcost(nwn: Path, tmp: Path) -> dict[str, str]:
    """iprp_soakcost.2da → {row: 'soak <N>'} from the Amount column."""
    hdrs, rows = parse_2da(extract_stock_2da(nwn, "iprp_soakcost.2da", tmp))
    amt_i = col_index(hdrs, "Amount")
    tbl: dict[str, str] = {}
    for rid, row in sorted(_rows_by_id(hdrs, rows).items()):
        # The Random sentinel (usually row 0) has Amount '****' and is skipped
        # by the value check below — no special row-0 handling needed.
        amt = row[amt_i] if amt_i is not None and amt_i < len(row) else ""
        if amt and amt != "****":
            tbl[str(rid)] = f"soak {amt}"
    return tbl


def build_protection(nwn: Path, tmp: Path) -> dict[str, str]:
    """iprp_protection.2da → {row: '+<N>'} (the enhancement needed to bypass DR).
    Labels are 'Bonus+10' etc.; strip the 'Bonus' prefix. Unlike the cost
    tables, row 0 here is a real value ('Bonus+1', i.e. the NWScript constant
    IP_CONST_DAMAGEREDUCTION_1 = 0), not a Random sentinel — keep it."""
    hdrs, rows = parse_2da(extract_stock_2da(nwn, "iprp_protection.2da", tmp))
    lbl_i = col_index(hdrs, "Label")
    tbl: dict[str, str] = {}
    for rid, row in sorted(_rows_by_id(hdrs, rows).items()):
        label = row[lbl_i] if lbl_i is not None and lbl_i < len(row) else ""
        if not label or label == "****":
            continue
        tbl[str(rid)] = label.replace("Bonus", "").strip() or label
    return tbl


def build_spellcost(nwn: Path, tmp: Path, dialog: dict[int, str]) -> dict[str, str]:
    """iprp_spellcost.2da → {row: pretty spell name} (Name strref via TLK)."""
    hdrs, rows = parse_2da(extract_stock_2da(nwn, "iprp_spellcost.2da", tmp))
    name_i = col_index(hdrs, "Name")
    lbl_i = col_index(hdrs, "Label")
    tbl: dict[str, str] = {}
    for rid, row in sorted(_rows_by_id(hdrs, rows).items()):
        # Row 0 is a real spell here (e.g. Acid Fog), not a Random sentinel —
        # include any row that resolves to a name.
        name_cell = row[name_i] if name_i is not None and name_i < len(row) else ""
        lbl_cell = row[lbl_i] if lbl_i is not None and lbl_i < len(row) else ""
        nm = resolve_name(name_cell, lbl_cell, dialog, {})
        if nm:
            tbl[str(rid)] = nm
    return tbl


def build_arcspell(nwn: Path, tmp: Path, dialog: dict[int, str]) -> dict[str, str]:
    """iprp_arcspell.2da → {row: '-30%'} (Name strref via TLK).

    Row-indexed, not value-indexed: row 0 is -50%, row 9 is -5%, row 10 is
    +05%, row 19 is +50%. No `_format` fallback is possible, so every row is
    listed. The TLK strings are the labels the game itself shows on the item,
    leading zero and all ("+05%"), so the wiki reads the same as the tooltip.
    """
    hdrs, rows = parse_2da(extract_stock_2da(nwn, "iprp_arcspell.2da", tmp))
    name_i = col_index(hdrs, "Name")
    val_i = col_index(hdrs, "Value")
    tbl: dict[str, str] = {}
    for rid, row in sorted(_rows_by_id(hdrs, rows).items()):
        label = ""
        if name_i is not None and name_i < len(row) and row[name_i]:
            try:
                label = dialog.get(int(row[name_i]), "")
            except ValueError:
                label = ""
        if not label and val_i is not None and val_i < len(row) and row[val_i]:
            # Fall back to the raw Value column if the strref is missing.
            try:
                label = f"{int(row[val_i]):+d}%"
            except ValueError:
                label = ""
        if label:
            tbl[str(rid)] = label
    return tbl


def build(nwn: Path, out_dir: Path) -> None:
    require_tools()
    ip_path = out_dir / "itemprops.json"
    ip = json.loads(ip_path.read_text())

    dialog_tlk = nwn / "lang" / "en" / "data" / "dialog.tlk"
    if not dialog_tlk.exists():
        sys.exit(f"error: missing {dialog_tlk}")

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        print("converting dialog.tlk → json")
        dialog = load_tlk_entries(tlk_to_json(dialog_tlk, tmp))

        print("building iprp_soakcost / iprp_protection / iprp_spellcost / iprp_arcspell")
        tables = ip.setdefault("tables", {})
        tables["iprp_soakcost"] = build_soakcost(nwn, tmp)
        tables["iprp_protection"] = build_protection(nwn, tmp)
        tables["iprp_spellcost"] = build_spellcost(nwn, tmp, dialog)
        tables["iprp_arcspell"] = build_arcspell(nwn, tmp, dialog)
        print(f"  soakcost={len(tables['iprp_soakcost'])} "
              f"protection={len(tables['iprp_protection'])} "
              f"spellcost={len(tables['iprp_spellcost'])} "
              f"arcspell={len(tables['iprp_arcspell'])}")

    # Correct the mis-pointed / missing property mappings.
    props = ip["properties"]
    props["22"]["subtype"] = "iprp_protection"
    props["22"]["cost_table"] = "iprp_soakcost"
    props["53"]["cost_table"] = "iprp_spellcost"
    props["84"]["cost_table"] = "iprp_arcspell"

    ip_path.write_text(
        json.dumps(ip, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {ip_path}")


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
