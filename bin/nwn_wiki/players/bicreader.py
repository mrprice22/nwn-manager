"""Read the server vault: ``servervault/<CDKEY>/<name>.bic`` -> character dicts.

A ``.bic`` is a GFF blob, so we shell out to ``nwn_gff`` (the same binary the repack
uses) and pull the handful of fields the awards need. Parsing 188 files takes a few
seconds, so results are cached to JSON keyed by path + mtime + size; a re-run after
tuning the pages that consume it is then instant.

Two traps this module exists to absorb:

* **The vault path contains a space** ("Neverwinter Nights"). Every path here stays a
  ``Path``/list argument to ``subprocess`` — never a shell string, never word-split.
* **A ``.bic`` filename is not the character's name** (see ``BIC-EDITING.md``:
  ``mherderous.bic`` is not "Mherderer"). Always read ``FirstName`` from inside the file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

# `nwn_gff` is the same binary bin/nwn-manager preflights for unpack/repack. Prefer
# whatever is on PATH (systemd units set an explicit PATH that includes ~/.nimble/bin)
# and fall back to the default nimble install location for a bare interactive run.
_NIMBLE_GFF = Path.home() / ".nimble" / "bin" / "nwn_gff"
NWN_GFF = Path(shutil.which("nwn_gff") or _NIMBLE_GFF)

# Ability fields, in the order the page prints them.
ABILITIES = ("Str", "Dex", "Con", "Int", "Wis", "Cha")


def _val(node):
    """Unwrap one nwn_gff JSON node to its plain value.

    Every field is ``{"type": ..., "value": ...}``; a CExoLocString's value is a dict
    of language-id -> text, from which we want language 0.
    """
    if not isinstance(node, dict):
        return node
    v = node.get("value")
    if isinstance(v, dict):
        # CExoLocString: {"id": <strref>, "0": "text", ...}. Language 0 is English;
        # a string that only carries a StrRef has no text at all, hence the "".
        if "0" in v:
            return v["0"]
        for key, text in v.items():
            if key != "id" and isinstance(text, str):
                return text
        return ""
    return v


def _get(d: dict, key: str, default=None):
    if key not in d:
        return default
    v = _val(d[key])
    return default if v is None else v


def parse_bic(path: Path, cdkey: str) -> dict | None:
    """Run one .bic through nwn_gff and reduce it to the fields awards care about."""
    try:
        raw = subprocess.run(
            [str(NWN_GFF), "-i", str(path), "-l", "gff", "-k", "json"],
            capture_output=True, check=True,
        ).stdout
        d = json.loads(raw)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        print(f"[warn] unreadable .bic {path.name}: {exc}", file=sys.stderr)
        return None

    first = _get(d, "FirstName", "") or ""
    last = _get(d, "LastName", "") or ""
    name = (f"{first} {last}".strip()) or path.stem

    classes = [
        (int(_val(c["Class"])), int(_val(c["ClassLevel"])))
        for c in (_get(d, "ClassList", []) or [])
        if "Class" in c and "ClassLevel" in c
    ]

    # SkillList is positional: the Nth entry is skill id N. Rank is the only field.
    skills = [int(_val(s.get("Rank", 0)) or 0) for s in (_get(d, "SkillList", []) or [])]
    feats = [int(_val(f["Feat"])) for f in (_get(d, "FeatList", []) or []) if "Feat" in f]

    # A saved character carries *instantiated* items, so the resref field is
    # TemplateResRef (an .utc blueprint's inventory would use InventoryRes instead).
    # Keep tag and name too: the collector awards match on all three, because a
    # resref alone rarely says "this is a gem" or "this is a bottle of wine".
    def _item(i: dict) -> dict:
        return {
            "resref": (_val(i.get("TemplateResRef")) or "").lower(),
            "tag": (_val(i.get("Tag")) or "").lower(),
            "name": (_val(i.get("LocalizedName")) or "").lower(),
            "base": int(_val(i.get("BaseItem", 0)) or 0),
            "stack": int(_val(i.get("StackSize", 1)) or 1),
            "cost": int(_val(i.get("Cost", 0)) or 0),
        }

    items = [_item(i) for i in (_get(d, "ItemList", []) or [])]
    equipped = [_item(i) for i in (_get(d, "Equip_ItemList", []) or [])]

    return {
        "cdkey": cdkey,
        "file": path.name,
        "uuid": _get(d, "UUID", ""),
        "name": name,
        "xp": int(_get(d, "Experience", 0) or 0),
        "gold": int(_get(d, "Gold", 0) or 0),
        "classes": classes,
        "level": sum(lvl for _, lvl in classes),
        "feats": feats,
        "skills": skills,
        "abilities": {a: int(_get(d, a, 0) or 0) for a in ABILITIES},
        "good_evil": int(_get(d, "GoodEvil", 50) or 50),
        "law_chaos": int(_get(d, "LawfulChaotic", 50) or 50),
        "race": int(_get(d, "Race", -1) if _get(d, "Race") is not None else -1),
        "subrace": _get(d, "Subrace", "") or "",
        "deity": _get(d, "Deity", "") or "",
        "max_hp": int(_get(d, "MaxHitPoints", 0) or 0),
        "items": items,
        "equipped": equipped,
        "familiar_type": _get(d, "FamiliarType"),
        "companion_type": _get(d, "CompanionType"),
    }


def _stamp(p: Path) -> str:
    st = p.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def load_vault(vault: Path, cache_path: Path | None = None) -> list[dict]:
    """Parse every character in the vault, using (and refreshing) the JSON cache.

    Returns one dict per character. Directory name is the CD key — that is the only
    place the account<->character link is recorded on disk.
    """
    cache: dict[str, dict] = {}
    if cache_path and cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8")).get("chars", {})
        except (OSError, json.JSONDecodeError):
            cache = {}

    chars: list[dict] = []
    fresh: dict[str, dict] = {}
    parsed = 0

    for keydir in sorted(p for p in vault.iterdir() if p.is_dir()):
        for bic in sorted(keydir.glob("*.bic")):
            ck = f"{keydir.name}/{bic.name}"
            stamp = _stamp(bic)
            hit = cache.get(ck)
            if hit and hit.get("_stamp") == stamp:
                rec = hit
            else:
                got = parse_bic(bic, keydir.name)
                if got is None:
                    continue
                got["_stamp"] = stamp
                rec = got
                parsed += 1
            fresh[ck] = rec
            chars.append(rec)

    if cache_path is not None and parsed:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"chars": fresh}, indent=1), encoding="utf-8"
            )
        except OSError as exc:
            print(f"[warn] could not write bic cache: {exc}", file=sys.stderr)

    print(f"[vault] {len(chars)} characters ({parsed} freshly parsed)", file=sys.stderr)
    return chars
