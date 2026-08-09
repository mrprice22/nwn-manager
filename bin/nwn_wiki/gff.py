"""GFF-as-JSON cell accessors, TLK parsing, and the stock-blueprint caches.

The unpacked module tree stores every GFF field as ``{"type": ..., "value": ...}``;
:func:`gff`, :func:`fld`, :func:`loc` and :func:`list_items` unwrap those cells.
:func:`read_tlk` parses NWN's TLK V3.0 string tables, whose contents land in
``state.BASE_TLK`` / ``state.CUSTOM_TLK`` for :func:`loc` to resolve StrRefs
against.

This is a leaf module: stdlib plus :mod:`nwn_wiki.paths`, :mod:`nwn_wiki.state`
and :mod:`nwn_wiki.warn` only.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from nwn_wiki import state
from nwn_wiki.paths import DATA_DIR
from nwn_wiki.warn import _warn_once


# ---------------------------------------------------------------------------
# GFF helpers
# ---------------------------------------------------------------------------

def gff(node: Any, default: Any = None) -> Any:
    """Unwrap a {'type': ..., 'value': ...} cell to its value."""
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    return default if node is None else node


def fld(struct: dict | None, name: str, default: Any = None) -> Any:
    """Look up a named field's *value* in a GFF struct/dict."""
    if not struct or name not in struct:
        return default
    return gff(struct[name], default)


def read_tlk(path: Path) -> dict[int, str]:
    """Parse an NWN TLK V3.0 file into {strref: text}.
    Empty entries (no TextPresent flag) are omitted."""
    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"TLK " or data[4:8] != b"V3.0":
        raise ValueError(f"not a TLK V3.0 file: {path}")
    _lang_id, count, str_off = struct.unpack_from("<III", data, 8)
    out: dict[int, str] = {}
    base = 20
    for i in range(count):
        off = base + i * 40
        flags = struct.unpack_from("<I", data, off)[0]
        if not (flags & 0x1):  # TextPresent
            continue
        soff, ssize = struct.unpack_from("<II", data, off + 28)
        start = str_off + soff
        out[i] = data[start:start + ssize].decode("cp1252", errors="replace")
    return out


def _load_stock_item_names() -> tuple[dict[str, str], dict[str, int], dict[str, int], dict[str, list]]:
    p = DATA_DIR / "stock_item_names.json"
    if not p.exists():
        return {}, {}, {}, {}
    raw = json.loads(p.read_text())
    names: dict[str, str] = {}
    base_items: dict[str, int] = {}
    costs: dict[str, int] = {}
    props: dict[str, list] = {}
    for k, v in raw.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        if "name" in v:
            names[k] = v["name"]
        if "base_item" in v:
            base_items[k] = int(v["base_item"])
        if "cost" in v and v["cost"]:
            costs[k] = int(v["cost"])
        if "properties" in v and isinstance(v["properties"], list):
            props[k] = v["properties"]
    return names, base_items, costs, props


STOCK_ITEM_NAMES: dict[str, str]
STOCK_ITEM_BASE: dict[str, int]
STOCK_ITEM_COST: dict[str, int]
STOCK_ITEM_PROPS: dict[str, list]
STOCK_ITEM_NAMES, STOCK_ITEM_BASE, STOCK_ITEM_COST, STOCK_ITEM_PROPS = _load_stock_item_names()


def _load_stock_creature_names() -> dict[str, str]:
    """Display names for stock NWN/CEP creatures referenced only by encounter
    pools (no module .utc). Flat resref -> name map; "_"-prefixed keys are
    metadata. Unlisted resrefs fall back to the resref itself."""
    p = DATA_DIR / "stock_creature_names.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    return {k: v for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, str)}


STOCK_CREATURE_NAMES: dict[str, str] = _load_stock_creature_names()


def loc(node: Any, lang: int = 0) -> str:
    """Resolve a cexolocstring. Falls back to TLK tables for ID-only refs;
    if the StrRef can't be resolved, yields a `[TLK#N]` placeholder."""
    val = gff(node)
    if not isinstance(val, dict) or not val:
        return ""
    key = str(lang)
    if key in val and isinstance(val[key], str):
        return val[key]
    if "id" in val:
        sid = val["id"]
        if isinstance(sid, int) and sid >= 0:
            if sid >= state.CUSTOM_TLK_BASE:
                t = state.CUSTOM_TLK.get(sid - state.CUSTOM_TLK_BASE)
                if t is not None:
                    return t
                if not state.CUSTOM_TLK:
                    _warn_once(f"StrRef {sid} (custom TLK) unresolved: no custom TLK loaded — re-run with --custom-tlk")
                else:
                    _warn_once(f"StrRef {sid} (custom TLK row {sid - state.CUSTOM_TLK_BASE}) not found in loaded custom TLK")
            else:
                t = state.BASE_TLK.get(sid)
                if t is not None:
                    return t
                if not state.BASE_TLK:
                    _warn_once(f"StrRef {sid} unresolved: no dialog.tlk loaded — re-run with --dialog-tlk")
                else:
                    _warn_once(f"StrRef {sid} not found in loaded dialog.tlk")
        return f"[TLK#{sid}]"
    for v in val.values():
        if isinstance(v, str):
            return v
    return ""


def list_items(node: Any) -> list[dict]:
    val = gff(node)
    if isinstance(val, list):
        return val
    return []
