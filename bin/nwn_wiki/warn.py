"""Deduplicated lookup warnings.

``_warn_once`` records a message once and appends the current rendering context
to it, so a large module reports "this StrRef is unresolved, referenced by these
40 items" instead of 40 near-identical stderr lines.  The registry itself lives
in :mod:`nwn_wiki.state` and is always reached through the module object.
"""

from __future__ import annotations

from nwn_wiki import state


def _warn_once(msg: str) -> None:
    already = msg in state._warned
    if not already:
        state._warned[msg] = []
    if state._current_context and state._current_context not in state._warned[msg]:
        state._warned[msg].append(state._current_context)
