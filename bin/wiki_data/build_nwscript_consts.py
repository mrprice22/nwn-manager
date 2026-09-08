#!/usr/bin/env python3
"""Build nwscript_consts.json from a local NWN install's ovr/nwscript.nss.

nwscript.nss is the engine's own constant header — the authoritative source for
names like BASE_ITEM_RAPIER, IP_CONST_DAMAGEBONUS_2d6 and DAMAGE_TYPE_FIRE. It
ships with the game, not with this repo, so the extraction happens here (once,
against an install) and the JSON is committed; the wiki build only reads the JSON
and never needs an NWN install of its own.

Output structure — integer constants grouped by their leading prefix:
  {
    "_source":     "...",
    "_extracted":  "<iso date>",
    "BASE_ITEM":   {"RAPIER": 40, ...},
    "DAMAGE_TYPE": {"FIRE": 256, ...},
    ...
  }

Prefixes are matched against a curated list (PREFIXES below), longest first, so
IP_CONST_DAMAGEBONUS_2d6 groups under "IP_CONST_DAMAGEBONUS" rather than the
shorter "IP_CONST".
Constants whose name matches no known prefix are dropped — they are one-off
engine scalars (ENGINE_NUM_STRUCTURES, TRUE, FALSE) with no table to join to.

Re-run after any Beamdog EE patch that adds constants:
  bin/refresh-nwn-script-consts [--nwn DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

# `int NAME = 123;` / `int NAME=-1;` — the only declaration form nwscript.nss
# uses for integer constants. Float and string constants are deliberately
# ignored: nothing indexes a 2DA by them.
#
# The name is NOT all-caps: the dice constants carry a lowercase d
# (IP_CONST_DAMAGEBONUS_1d4, IP_CONST_MONSTERDAMAGE_1d2), which is exactly the
# family this export exists to serve. Only the first character is required to
# be uppercase, which is enough to skip local variables.
_CONST_RE = re.compile(r"^\s*int\s+([A-Z][A-Za-z0-9_]*)\s*=\s*(-?\d+)\s*;")

# Curated constant families. Matched longest-first, so a name is attributed to
# the most specific family that prefixes it (IP_CONST_ONHIT_CASTSPELL before
# IP_CONST_ONHIT before IP_CONST). Keep this list additive: an unknown family is
# dropped, never guessed at.
PREFIXES: list[str] = [
    # Item properties — the iprp_*.2da row spaces.
    "IP_CONST_ABILITY",
    "IP_CONST_ACMODTYPE",
    "IP_CONST_ALIGNMENTGROUP",
    "IP_CONST_ALIGNMENT",
    "IP_CONST_AMMOTYPE",
    "IP_CONST_ARCANE_SPELL_FAILURE",
    "IP_CONST_CASTSPELL_NUMUSES",
    "IP_CONST_CASTSPELL",
    "IP_CONST_CLASS",
    "IP_CONST_DAMAGEBONUS",
    "IP_CONST_DAMAGEIMMUNITY",
    "IP_CONST_DAMAGEREDUCTION",
    "IP_CONST_DAMAGERESIST",
    "IP_CONST_DAMAGETYPE",
    "IP_CONST_DAMAGEVULNERABILITY",
    "IP_CONST_FEAT",
    "IP_CONST_IMMUNITYMISC",
    "IP_CONST_IMMUNITYSPELL",
    "IP_CONST_LIGHTBRIGHTNESS",
    "IP_CONST_MONSTERDAMAGE",
    "IP_CONST_LIGHTCOLOR",
    "IP_CONST_ONHIT_CASTSPELL",
    "IP_CONST_ONHIT_DURATION",
    "IP_CONST_ONHIT_SAVEDC",
    "IP_CONST_ONHIT",
    "IP_CONST_PROTECTION",
    "IP_CONST_RACIALTYPE",
    "IP_CONST_REDUCEDWEIGHT",
    "IP_CONST_SAVEBASETYPE",
    "IP_CONST_SAVEVS",
    "IP_CONST_SKILL",
    "IP_CONST_SPELLLEVEL",
    "IP_CONST_SPELLRESISTANCEBONUS",
    "IP_CONST_SPELLSHIELD",
    "IP_CONST_TRAPDAMAGE",
    "IP_CONST_TRAPSTRENGTH",
    "IP_CONST_TRAPTYPE",
    "IP_CONST_WEIGHTINCREASE",
    "IP_CONST",
    # Core 2DA row spaces.
    "BASE_ITEM",
    "RACIAL_TYPE",
    "CLASS_TYPE",
    "APPEARANCE_TYPE",
    "SPELL_SCHOOL",
    "SPELLABILITY",
    "SPELL",
    "SKILL",
    "FEAT",
    "INVENTORY_SLOT",
    # Bitmasks and enums with no 2DA behind them — exported for completeness,
    # never joined to a table (see the const-prefix map in reports/enums.py).
    "DAMAGE_TYPE",
    "DAMAGE_POWER",
    "DAMAGE_BONUS",
    "ALIGNMENT",
    "ABILITY",
    "SAVING_THROW_TYPE",
    "SAVING_THROW",
    "IMMUNITY_TYPE",
    "OBJECT_TYPE",
    "CREATURE_SIZE",
    "CREATURE_TYPE",
    "GENDER",
    "STANDARD_FACTION",
    "TALENT_CATEGORY",
    "VFX_DUR",
    "VFX_IMP",
    "VFX_FNF",
    "VFX_BEAM",
    "VFX_COM",
    "VFX_EYES",
    "VFX_HIT",
    "VFX",
]

# Longest first so the most specific family wins.
_PREFIXES_SORTED = sorted(PREFIXES, key=len, reverse=True)


def _family(name: str) -> str | None:
    """The curated family a constant belongs to, or None if it matches none."""
    for p in _PREFIXES_SORTED:
        if name.startswith(p + "_") and len(name) > len(p) + 1:
            return p
    return None


def parse_nwscript(path: Path) -> dict[str, dict[str, int]]:
    """Group `int NAME = N;` declarations in nwscript.nss by constant family."""
    out: dict[str, dict[str, int]] = {}
    for line in path.read_text(encoding="cp1252", errors="replace").splitlines():
        m = _CONST_RE.match(line)
        if not m:
            continue
        name, raw = m.group(1), m.group(2)
        fam = _family(name)
        if fam is None:
            continue
        out.setdefault(fam, {})[name[len(fam) + 1:]] = int(raw)
    return out


def build(nwn_root: Path, out_dir: Path) -> None:
    src = nwn_root / "ovr" / "nwscript.nss"
    if not src.is_file():
        sys.exit(f"error: {src} not found — pass --nwn /path/to/Neverwinter Nights")

    families = parse_nwscript(src)
    if not families:
        sys.exit(f"error: no integer constants parsed from {src}")

    result: dict = {
        "_source": f"{src} (ovr/nwscript.nss)",
        "_extracted": date.today().isoformat(),
        "_doc": "NWScript integer constants grouped by family. Suffixes are the "
                "constant name minus the family prefix and one underscore.",
    }
    for fam in sorted(families):
        result[fam] = dict(sorted(families[fam].items(), key=lambda kv: (kv[1], kv[0])))

    out_path = out_dir / "nwscript_consts.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    total = sum(len(v) for v in families.values())
    print(f"wrote {out_path}: {total} constants in {len(families)} families")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    home = Path(os.path.expanduser("~"))
    ap.add_argument("--nwn", type=Path,
                    default=home / ".local" / "share" / "Steam" / "steamapps"
                    / "common" / "Neverwinter Nights")
    args = ap.parse_args()
    build(args.nwn, Path(__file__).resolve().parent)
    print("done.")


if __name__ == "__main__":
    main()
