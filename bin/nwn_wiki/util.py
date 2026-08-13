"""Generic helpers with no NWN domain knowledge.

A leaf module: it imports nothing from the package, so anything may import it.

Depends only on stdlib -- nothing here may touch ``Db``, ``E()`` or the
renderers.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def _try_int(v: Any, default: int = 0) -> int:
    """int(v) with a fallback — for 2DA cells and GFF fields that may be blank,
    '****' or missing."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default


def _fmt_commas(val: Any, blank_zero: bool = False) -> str | None:
    """Comma-grouped integer, or None when ``val`` isn't int-like.

    Shared numeric core of the display formatters (``_fmt_hp``, ``_fmt_cost``):
    each of those handles its own non-numeric fallback, this handles the one
    case they agree on. With ``blank_zero`` a zero renders as '' rather than
    '0', which is what a cost column wants and an HP column does not."""
    try:
        v = int(val)
    except (TypeError, ValueError):
        return None
    if blank_zero and not v:
        return ""
    return f"{v:,}"


def _tz_label_from_env() -> str:
    """Derive a GMT±N label from the TZ environment variable.

    Uses the zoneinfo module (Python 3.9+) so DST is handled correctly
    (e.g. America/Chicago → 'GMT-5' in winter, 'GMT-4' in summer).
    Falls back to 'GMT+0' when TZ is unset or the name is unrecognised.
    """
    tz_name = os.environ.get("TZ", "")
    if not tz_name:
        return "GMT+0"
    try:
        from zoneinfo import ZoneInfo
        zi = ZoneInfo(tz_name)
        offset = datetime.now(zi).utcoffset()
        total_sec = int(offset.total_seconds())
        h, m = divmod(abs(total_sec) // 60, 60)
        sign = "+" if total_sec >= 0 else "-"
        return f"GMT{sign}{h}" if m == 0 else f"GMT{sign}{h}:{m:02d}"
    except Exception:
        return tz_name


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
