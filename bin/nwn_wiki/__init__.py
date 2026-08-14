"""nwn_wiki — generate a static HTML wiki from an unpacked NWN1 module.

The command-line entry point is bin/nwn-wiki, a thin shim over
:func:`nwn_wiki.cli.main`.

Note for callers that poke module state (bin/nwn-wiki-activity,
bin/recover-activity-gap): import the *module* that owns the value and set the
attribute there, e.g. ``from nwn_wiki import cli`` then ``cli.X = ...``.  Never
``from nwn_wiki.cli import X`` — a from-import snapshots the binding, so later
writes are invisible to the reader and the wrong value is rendered silently.
"""

from __future__ import annotations
