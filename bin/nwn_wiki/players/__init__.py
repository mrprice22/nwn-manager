"""Player/character data layer — the servervault and the campaign DBs.

Ported from ``nwn_homers_lotr_s1/bin/halloffame/``, where it was written for the
season-end Hall of Fame and only ever existed in that frozen archive. The three
modules here are the season-agnostic half of it; the award logic
(``awards.py`` / ``categories.py``) stays behind until the leaderboards need it,
because it encodes season-*end* trophy semantics that need re-tuning for a live
mid-season page.

Module map:

    bicreader.py   servervault/<CDKEY>/*.bic -> parsed character dicts (mtime-cached)
    sources.py     read-only opens of the campaign SQLite DBs + module-index JSON
    identity.py    the CD-key <-> "playerid string" bridge, and the player roster

One deliberate change from the original: the curated identity tables
(ACCOUNT_MERGES, ROADMAP_ALIASES) used to be hardcoded in a season-local
``categories.py``. This package is shared by every realm's build, so they now
start empty and each project installs its own via ``identity.configure()``.

Everything here is read-only. Nothing in this package writes to a campaign DB,
the vault, or the running server.
"""
