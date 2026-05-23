#!/usr/bin/env python3
"""Build stock_item_names.json from a local NWN install.

Scans every UTI blueprint in the NWN KEY/BIF archives and extracts:
  - name    — resolved from dialog.tlk (or inline CExoLocString text)
  - base_item — BaseItem row id (baseitems.2da)
  - tag     — blueprint Tag field

Output: stock_item_names.json alongside this script, committed to the repo.
Run this script once per NWN install, and re-run after a Beamdog patch that
changes item blueprints or the base TLK.

Usage:
  ./build_stock_item_names.py [--nwn DIR] [--tlk PATH]

Defaults:
  --nwn  ~/.local/share/Steam/steamapps/common/Neverwinter Nights
  --tlk  <nwn>/lang/en/data/dialog.tlk
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
from pathlib import Path

_NWN_UTI_RESTYPE = 2025

_DEFAULT_NWN = (
    Path.home() / ".local/share/Steam/steamapps/common/Neverwinter Nights"
)


# ---------------------------------------------------------------------------
# TLK reader
# ---------------------------------------------------------------------------

def read_tlk(path: Path) -> dict[int, str]:
    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"TLK " or data[4:8] != b"V3.0":
        raise ValueError(f"not a TLK V3.0 file: {path}")
    _lang_id, count, str_off = struct.unpack_from("<III", data, 8)
    out: dict[int, str] = {}
    base = 20
    for i in range(count):
        off = base + i * 40
        flags = struct.unpack_from("<I", data, off)[0]
        if not (flags & 0x1):
            continue
        soff, ssize = struct.unpack_from("<II", data, off + 28)
        start = str_off + soff
        out[i] = data[start:start + ssize].decode("cp1252", errors="replace")
    return out


# ---------------------------------------------------------------------------
# KEY / BIF parsers
# ---------------------------------------------------------------------------

def _parse_nwn_key(key_path: Path) -> dict[str, tuple[Path, int]]:
    """Return {resref: (bif_path, var_resource_id)} for all UTI entries."""
    key_dir = key_path.parent
    try:
        data = key_path.read_bytes()
    except OSError:
        return {}
    if data[:4] != b"KEY ":
        return {}

    n_bif, n_key = struct.unpack_from("<II", data, 8)
    ofs_bif, ofs_key = struct.unpack_from("<II", data, 16)

    game_root = key_dir.parent
    bif_paths: list[Path] = []
    for i in range(n_bif):
        e = ofs_bif + i * 12
        _fsize, name_off, name_len, _drives = struct.unpack_from("<IIHH", data, e)
        raw = data[name_off:name_off + name_len].rstrip(b"\x00")
        rel = raw.decode("latin-1").replace("\\", os.sep)
        for cand in (game_root / rel, key_dir / rel, key_dir / Path(rel).name):
            if cand.is_file():
                bif_paths.append(cand)
                break
        else:
            bif_paths.append(game_root / rel)

    result: dict[str, tuple[Path, int]] = {}
    for i in range(n_key):
        e = ofs_key + i * 22
        resref = data[e:e + 16].rstrip(b"\x00").decode("latin-1").lower()
        res_type = struct.unpack_from("<H", data, e + 16)[0]
        res_id = struct.unpack_from("<I", data, e + 18)[0]
        if res_type != _NWN_UTI_RESTYPE:
            continue
        bif_idx = (res_id >> 20) & 0xFFF
        var_idx = res_id & 0xFFFFF
        if bif_idx < len(bif_paths):
            result[resref] = (bif_paths[bif_idx], var_idx)
    return result


def _read_bif_entry(bif_path: Path, var_idx: int) -> bytes:
    try:
        data = bif_path.read_bytes()
    except OSError:
        return b""
    if data[:4] != b"BIFF":
        return b""
    n_var, _n_fixed, ofs_var = struct.unpack_from("<III", data, 8)
    for i in range(n_var):
        e = ofs_var + i * 16
        entry_id, offset, size, _res_type = struct.unpack_from("<IIII", data, e)
        if (entry_id & 0xFFFFF) == var_idx:
            return data[offset:offset + size]
    return b""


# ---------------------------------------------------------------------------
# Minimal GFF V3.2 parser — extracts LocalizedName, BaseItem, Tag, PropertiesList
# ---------------------------------------------------------------------------

# GFF field types
_DWORD = 4
_INT = 5
_CExoString = 10
_CExoLocString = 12
_LIST = 15

_INLINE_TYPES = {0, 1, 2, 3, 4, 5, 8, 11}  # types stored inline in field data slot


def _read_prop_struct(gff_bytes: bytes, s_idx: int,
                      struct_offset: int, field_offset: int, field_indices_offset: int,
                      pn_idx: int | None, st_idx: int | None,
                      ct_idx: int | None, cv_idx: int | None,
                      p1_idx: int | None, p1v_idx: int | None,
                      ca_idx: int | None) -> dict | None:
    """Extract one PropertiesList entry struct as a plain dict."""
    s_base = struct_offset + s_idx * 12
    if s_base + 12 > len(gff_bytes):
        return None
    _s_type, s_data, s_field_count = struct.unpack_from("<III", gff_bytes, s_base)
    if s_field_count == 0:
        return None
    if s_field_count == 1:
        s_field_indices = [s_data]
    else:
        fi_base = field_indices_offset + s_data
        s_field_indices = [
            struct.unpack_from("<I", gff_bytes, fi_base + j * 4)[0]
            for j in range(s_field_count)
            if fi_base + (j + 1) * 4 <= len(gff_bytes)
        ]
    prop: dict = {}
    for fi in s_field_indices:
        f_base = field_offset + fi * 12
        if f_base + 12 > len(gff_bytes):
            continue
        f_type, f_label, f_data = struct.unpack_from("<III", gff_bytes, f_base)
        if f_label == pn_idx:
            prop["PropertyName"] = f_data & 0xFFFF
        elif f_label == st_idx:
            prop["Subtype"] = f_data & 0xFFFF
        elif f_label == ct_idx:
            prop["CostTable"] = f_data & 0xFF
        elif f_label == cv_idx:
            prop["CostValue"] = f_data & 0xFFFF
        elif f_label == p1_idx:
            prop["Param1"] = f_data & 0xFF
        elif f_label == p1v_idx:
            prop["Param1Value"] = f_data & 0xFF
        elif f_label == ca_idx:
            prop["ChanceAppear"] = f_data & 0xFF
    return prop if prop else None


def _gff_parse_uti(gff_bytes: bytes, tlk: dict[int, str]) -> dict:
    """Parse a UTI GFF binary and return {name, base_item, tag, cost, properties}."""
    if len(gff_bytes) < 56:
        return {}
    (struct_offset, _sc, field_offset, _fc,
     label_offset, label_count, field_data_offset, _fdc,
     field_indices_offset, _fic, list_indices_offset, _lic) = struct.unpack_from("<12I", gff_bytes, 8)

    # Map label text → label index
    labels: dict[bytes, int] = {}
    for i in range(label_count):
        base = label_offset + i * 16
        lab = gff_bytes[base:base + 16].rstrip(b"\x00")
        labels[lab] = i
    ln_idx  = labels.get(b"LocalizedName")
    bi_idx  = labels.get(b"BaseItem")
    tg_idx  = labels.get(b"Tag")
    co_idx  = labels.get(b"Cost")
    pl_idx  = labels.get(b"PropertiesList")
    pn_idx  = labels.get(b"PropertyName")
    st_idx  = labels.get(b"Subtype")
    ct_idx  = labels.get(b"CostTable")
    cv_idx  = labels.get(b"CostValue")
    p1_idx  = labels.get(b"Param1")
    p1v_idx = labels.get(b"Param1Value")
    ca_idx  = labels.get(b"ChanceAppear")

    # Top-level struct (struct 0)
    s_data, s_field_count = struct.unpack_from("<II", gff_bytes, struct_offset + 4)
    if s_field_count == 0:
        return {}
    if s_field_count == 1:
        field_indices = [s_data]
    else:
        fi_base = field_indices_offset + s_data
        field_indices = [
            struct.unpack_from("<I", gff_bytes, fi_base + j * 4)[0]
            for j in range(s_field_count)
        ]

    result: dict = {}
    for fi in field_indices:
        f_type, f_label, f_data = struct.unpack_from("<III", gff_bytes,
                                                      field_offset + fi * 12)

        if f_label == ln_idx and f_type == _CExoLocString:
            fd = field_data_offset + f_data
            if fd + 12 > len(gff_bytes):
                continue
            total_size = struct.unpack_from("<I", gff_bytes, fd)[0]
            strref = struct.unpack_from("<I", gff_bytes, fd + 4)[0]
            str_count = struct.unpack_from("<I", gff_bytes, fd + 8)[0]
            if strref != 0xFFFFFFFF:
                name = tlk.get(strref)
                if name:
                    result["name"] = name
            elif str_count > 0:
                # inline string: lang_id(4) + len(4) + text(len)
                p = fd + 12
                if p + 8 <= len(gff_bytes):
                    _lang = struct.unpack_from("<I", gff_bytes, p)[0]
                    slen = struct.unpack_from("<I", gff_bytes, p + 4)[0]
                    if p + 8 + slen <= len(gff_bytes):
                        result["name"] = gff_bytes[p + 8:p + 8 + slen].decode(
                            "cp1252", errors="replace")

        elif f_label == bi_idx and f_type in (_DWORD, _INT):
            result["base_item"] = f_data  # inline value for 4-byte types

        elif f_label == co_idx and f_type in (_DWORD, _INT):
            result["cost"] = f_data  # inline value for 4-byte types

        elif f_label == tg_idx and f_type == _CExoString:
            fd = field_data_offset + f_data
            if fd + 4 <= len(gff_bytes):
                slen = struct.unpack_from("<I", gff_bytes, fd)[0]
                if fd + 4 + slen <= len(gff_bytes):
                    result["tag"] = gff_bytes[fd + 4:fd + 4 + slen].decode(
                        "cp1252", errors="replace")

        elif f_label == pl_idx and f_type == _LIST:
            # f_data is a byte offset into the list indices array.
            li_off = list_indices_offset + f_data
            if li_off + 4 > len(gff_bytes):
                continue
            n_structs = struct.unpack_from("<I", gff_bytes, li_off)[0]
            props = []
            for si in range(n_structs):
                if li_off + 4 + (si + 1) * 4 > len(gff_bytes):
                    break
                s_idx = struct.unpack_from("<I", gff_bytes, li_off + 4 + si * 4)[0]
                prop = _read_prop_struct(
                    gff_bytes, s_idx, struct_offset, field_offset, field_indices_offset,
                    pn_idx, st_idx, ct_idx, cv_idx, p1_idx, p1v_idx, ca_idx,
                )
                if prop:
                    props.append(prop)
            if props:
                result["properties"] = props

    return result


# ---------------------------------------------------------------------------
# Main build logic
# ---------------------------------------------------------------------------

def build(nwn_dir: Path, tlk_path: Path, out_path: Path) -> None:
    key_path = nwn_dir / "data" / "nwn_base.key"
    if not key_path.is_file():
        sys.exit(f"error: KEY file not found: {key_path}")
    if not tlk_path.is_file():
        sys.exit(f"error: TLK not found: {tlk_path}")

    t0 = time.time()
    print(f"loading dialog.tlk from {tlk_path}")
    tlk = read_tlk(tlk_path)
    print(f"  {len(tlk)} entries")

    print(f"parsing KEY index from {key_path}")
    index = _parse_nwn_key(key_path)
    if not index:
        sys.exit("error: no UTI entries found in KEY file")
    print(f"  {len(index)} UTI blueprints")

    missing = sum(1 for _, (bp, _) in index.items() if not bp.is_file())
    if missing:
        print(f"  warn: {missing} BIF(s) not found, those entries will be skipped",
              file=sys.stderr)

    data: dict[str, dict] = {}
    skipped = 0
    for resref, (bif_path, var_idx) in index.items():
        if not bif_path.is_file():
            skipped += 1
            continue
        raw = _read_bif_entry(bif_path, var_idx)
        if not raw:
            skipped += 1
            continue
        props = _gff_parse_uti(raw, tlk)
        if props:
            data[resref] = props

    elapsed = time.time() - t0
    print(f"  extracted {len(data)} blueprints, {skipped} skipped ({elapsed:.1f}s)")

    out: dict = {
        "_source": f"stock NWN :: nwn_base.key UTI blueprints + dialog.tlk",
        "_fields": ["name (str)", "base_item (int, baseitems.2da row)", "tag (str)",
                    "cost (int, GP value, omitted when 0)",
                    "properties (list of PropertiesList dicts, omitted when empty)"],
        **data,
    }
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"wrote {out_path}  ({len(data)} entries)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nwn", type=Path, default=_DEFAULT_NWN,
                    help="NWN install root (default: %(default)s)")
    ap.add_argument("--tlk", type=Path, default=None,
                    help="path to dialog.tlk (default: <nwn>/lang/en/data/dialog.tlk)")
    args = ap.parse_args()

    nwn_dir = args.nwn.resolve()
    tlk_path = (args.tlk or nwn_dir / "lang" / "en" / "data" / "dialog.tlk").resolve()
    out_path = Path(__file__).resolve().parent / "stock_item_names.json"
    build(nwn_dir, tlk_path, out_path)


if __name__ == "__main__":
    main()
