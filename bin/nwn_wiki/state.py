"""Mutable module state shared across the wiki generator.

Everything here is written at build time by a loader and read later by a
renderer.  ALWAYS reach it through the module object::

    from nwn_wiki import state
    state._HAS_ACTIVITY_PAGE   # reads the current value

NEVER ``from nwn_wiki.state import X``.  A from-import binds the value at import
time, so a later write is invisible to the importer and the wrong value is
rendered -- output that is wrong rather than broken, which no traceback will
tell you about.  The two constants defined here (BST_SF_CR, CREATURE_PIC_EXTS)
live alongside the state they document; reach them the same way, so there is no
exception to remember.
"""

from __future__ import annotations

# TLK lookup tables, populated from --dialog-tlk / --custom-tlk if given.
# StrRefs >= CUSTOM_TLK_BASE are custom-TLK rows (id - CUSTOM_TLK_BASE);
# anything below resolves against the base game's dialog.tlk.
CUSTOM_TLK_BASE = 0x01000000  # 16777216
BASE_TLK: dict[int, str] = {}
CUSTOM_TLK: dict[int, str] = {}

# Populated by render_manual_pages(); used by page() to build the Documents dropdown.
# Each entry is one of:
#   {"kind": "file", "title": str, "stem": str}
#   {"kind": "dir",  "title": str, "dirname": str,
#    "items": [{"title": str, "stem": str}, ...]}
_MANUAL_MENUS: dict[str, list[dict]] = {}  # menu name -> ordered manual-page entries
_MANUAL_MENU_ORDER: dict[str, int] = {}  # custom menu name -> first @menu-order seen
_HAS_ACTIVITY_PAGE: bool = False
_HAS_SERVER_FIRSTS: bool = False  # set in render_manual_pages(); drives Activity nav
_GENERATED_AT: str = ""  # set in main(); included in every page footer

# The build's resolved nav/page-shell facts (htmlgen.chrome.SiteChrome), frozen
# once every loader has run and read by page() for the rest of the run. None
# until then, which makes page() snapshot the live globals above instead --
# manual pages are rendered by the same pass that discovers them, so they are
# shelled before the chrome can exist. Also set by bin/nwn-wiki-activity, from
# the <out>/.chrome.json the main build wrote, so that subprocess renders the
# same nav rather than re-deriving it.
CHROME = None  # type: "nwn_wiki.htmlgen.chrome.SiteChrome | None"

# Bestiary kill-tracking stats, read from the live NWNX:EE campaign DB
# (<db-dir>/bestiarydb.sqlite3) when the module uses the bestiary system. The
# filename must match the module's campaign DB name (BST_DB="bestiarydb" in
# bst_db.nss) — that is the file the in-game scripts read and write.
BST_SF_CR = 60  # Server-First Challenge Rating threshold (mirror bst_db.nss)
_BESTIARY_ACTIVE: bool = False
_BESTIARY_KILLS: dict[str, dict] = {}  # canonical resref -> {"total","solo","party"}
# canonical resref -> per-character kill rows, sorted by total desc:
#   [{"uuid","name","solo","party","total","last"}]  ("uuid" is internal only —
#   never rendered into HTML; pages show char_name.)
_BESTIARY_TOP: dict[str, list[dict]] = {}
_SERVER_FIRSTS: list[dict] = []        # [{"resref","cr","name","cname","at"}]

# Player characters, from the servervault + kill ledger + session history (see
# nwn_wiki.players.model.build_records). Empty unless the build was given a
# --vault-dir, which is what keeps the Players section out of the archived
# forks that never had a server behind them.
#   [{"name","player","slug","level","class_line","kills_total", ...}]
# Records carry a "_cdkey" join key that is INTERNAL ONLY -- like uuid above, it
# is account credentials and must never be rendered into HTML.
_CHARACTERS: list[dict] = []

# Accounts with at least one published character (players/model.build_players),
# and a name -> slug map for linking one from a character or award.
#   [{"name","slug","characters",...}]
_PLAYERS: list[dict] = []
_PLAYER_SLUGS: dict[str, str] = {}

# Hall of Fame results, resolved once by render.achievements.compute_achievements
# so the players index can rank by medals without recomputing them (and so the
# two can never disagree about who won what).
_ACHIEVEMENTS: list[dict] = []
_PLAYER_ACHIEVEMENTS: dict[str, int] = {}   # player name -> awards held

# Blueprint resrefs that actually have an item page in this build, so a
# character's equipped gear links only where the target exists -- crafted or
# renamed gear can name a blueprint the wiki does not ship.
_ITEM_PAGES: set[str] = set()

# Blueprint resref -> display name, for gear whose .bic carries no
# LocalizedName. A stock item's name lives in the TLK, not on the instance, so
# without this a character's stock ring renders as "(unnamed item)". Written to
# <out>/.item-names.json by the full build and re-read by nwn-wiki-activity, the
# same way .chrome.json shares the nav -- so both paths render the same page.
_ITEM_NAMES: dict[str, str] = {}

# The module's cap on the TOTAL ability bonus an character may take from items
# (settings.tml max-ability-bonus; NWN's default is 12, this module runs 24).
# Bonuses from different items stack and are then clamped to this -- see
# nwn_wiki/sim/pc.py, which documents the same rule.
_MAX_ABILITY_BONUS: int = 12

# URL the Who's Online page polls for the live roster, from --online-api. Empty
# on every realm that does not run the status pusher (archived seasons, the
# forks, and the dev realm until it is switched on for testing), which is what
# keeps the page and its nav entry from existing there at all.
_ONLINE_API: str = ""

# Boss respawn tracker ("Roll of the Fallen") registry, parsed from the module's
# brd_db.nss BRD_SeedBoss(...) seed rows by load_boss_registry(). Single source
# of truth shared with the in-game Well of Eru board — the wiki Bosses page is
# generated from the same rows the game seeds into respawndb on module load, so
# the two can never drift. Empty for modules without the tracker (no nav link).
_BOSS_REGISTRY: list[dict] = []   # [{"resref","name","tag","area","area_name","cr"}]
_BOSS_ALIASES: dict[str, str] = {}  # variant blueprint resref -> canonical resref

# Creature artwork loaded from <project-root>/creature-pics/ by scan_creature_pics().
# Filenames are matched to creature display names; a trailing "_NN" sets order.
CREATURE_PIC_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_CREATURE_PICS: dict[str, list[str]] = {}     # canonical resref -> [image filename, ...]
_CREATURE_PIC_GROUPS: list[dict] = []         # [{"name","matches","images","cr"}], sorted

# Per-module theme assets loaded from <project-root>/wiki-theme/ by load_wiki_theme().
_THEME_FAVICON: str = ""           # filename in assets/, e.g. "favicon.png"
_THEME_HEADER_IMGS: list[str] = [] # filenames in assets/, e.g. ["header.png", "header_02.png"]
_THEME_HEADER_DIMS: list[tuple[int, int] | None] = []  # intrinsic (w, h) per header image
_THEME_EXTRA_CSS: str = ""         # filename in assets/, e.g. "theme.css"

# Lookup warning registry: message → sorted list of referencing items/creatures.
# Populated by _warn_once(); avoids flooding stderr on large modules.
_warned: dict[str, list[str]] = {}
# Set by rendering functions before processing each entity so warnings can
# record which item/creature triggered them.
_current_context: str = ""

# Module-index summary collector: (severity, message) tuples accumulated during
# generation and printed as a grouped, colour-coded block at the end of the run.
# severity ∈ {"info", "warn", "issue"}
_module_index_summary: list[tuple[str, str]] = []
