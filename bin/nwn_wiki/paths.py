"""Filesystem anchors for the wiki generator.

SCRIPT_DIR is bin/, one level *up* from this package: the bundled lookup tables
(wiki_data/), the CSS and JS (wiki_assets/) and the sibling nwn-wiki-activity
executable all live beside the CLI shim, not inside the package.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SCRIPT_DIR / "wiki_data"
ASSETS_DIR = SCRIPT_DIR / "wiki_assets"
