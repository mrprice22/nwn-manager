"""Generic helpers with no NWN domain knowledge.

A leaf module: it imports nothing from the package, so anything may import it.

Depends only on stdlib -- nothing here may touch ``Db``, ``E()`` or the
renderers.
"""

from __future__ import annotations

from typing import Any


def _try_int(v: Any, default: int = 0) -> int:
    """int(v) with a fallback — for 2DA cells and GFF fields that may be blank,
    '****' or missing."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default
