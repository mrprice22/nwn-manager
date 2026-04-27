# nwn-manager

Unpack and repack Neverwinter Nights 1 modules (`.mod`) into a git-friendly,
LLM-editable source tree.

A `.mod` is a binary ERF archive of GFF blobs, compiled scripts (`.ncs`), 2DAs
and other resources — opaque to git, opaque to LLMs. `nwn-manager` is a thin
CLI around [nasher][] (which itself uses [neverwinter.nim][]) that turns it
into a tree of JSON, plain-text scripts, and 2DAs that diff cleanly and that
an LLM can read and edit directly.

[nasher]: https://github.com/squattingmonk/nasher.nim
[neverwinter.nim]: https://github.com/niv/neverwinter.nim

## Layout

```
bin/nwn-manager        the wrapper CLI
nasher.cfg             generated on first unpack — edit to rename outputs etc.
src/                   source tree (areas/, creatures/, dialogs/, scripts/, …)
                       GFFs are stored as JSON; scripts as .nss source only
dist/                  packed .mod output (gitignored)
.nasher/               nasher's working cache (gitignored)
```

## Prerequisites

A Nim toolchain plus two Nim packages must be on `PATH`:

- `nim`, `nimble`
- `nasher`
- `nwn_gff`, `nwn_script_comp` (from the `neverwinter` package)

### Install on an immutable Fedora distro (Bazzite, Silverblue, etc.)

`dnf install nim` is blocked by `rpm-ostree`; use [choosenim][] for a
per-user install instead:

```sh
curl -sSfL https://nim-lang.org/choosenim/init.sh | sh -s -- -y
export PATH="$HOME/.nimble/bin:$PATH"   # add to ~/.bashrc to persist
```

[choosenim]: https://github.com/dom96/choosenim

### Install the NWN tooling

```sh
nimble install -y --solver:legacy nasher neverwinter
```

The `--solver:legacy` flag is required: `neverwinter.nimble` declares its
binaries via a dynamic `listFiles(...).mapIt(...)` expression, which the
default SAT-solver's strict declarative parser rejects with `'bin' must be
assigned a sequence with @ prefix`. The legacy solver executes `.nimble`
files as Nim code and accepts the dynamic form.

## Usage

### First-time unpack

From the project root:

```sh
bin/nwn-manager unpack /path/to/your_module.mod
```

This runs `nasher init` under the hood, which creates `nasher.cfg` and the
`src/` tree from the module. Commit `src/` and `nasher.cfg`.

### Re-unpack from an updated `.mod`

```sh
bin/nwn-manager unpack /path/to/your_module.mod
```

If `nasher.cfg` already exists, the wrapper delegates to `nasher unpack` and
refreshes `src/` from the new `.mod`.

### Repack

```sh
bin/nwn-manager repack
```

Runs `nasher pack`, which converts JSON → GFF, compiles `.nss` → `.ncs`, and
writes the packed `.mod` to `dist/`. Extra arguments are passed through to
`nasher` (e.g. a target name when `nasher.cfg` defines several).

### Help

```sh
bin/nwn-manager --help
```

## Notes

- **Scripts** are committed as `.nss` source only. `.ncs` binaries are
  regenerated on every repack and are gitignored.
- **GFF format** is JSON, not YAML — chosen for lossless round-trip and clean
  diffs. nasher and `nwn_gff` agree on this representation.
- **Custom layout** is possible by editing `nasher.cfg` after the initial
  unpack — group sources by area, change the output filename, add HAK or TLK
  targets, etc. The wrapper does not constrain this.
- **Output filename**: `nasher init` defaults the build target's `file` to
  `demo.mod`. After unpack, edit `[target] file = "..."` in `nasher.cfg` to
  match your module name before your first `repack`.

## Known issues

- **Apostrophes in `.mod` filenames** (e.g. `Homer's LOTR.mod`) cause
  `nwn_erf` to fail with `cannot open file stream` because the path is shell-
  re-escaped before being opened. Workaround: symlink the module to a clean
  path and pass the symlink to `unpack`:

  ```sh
  ln -s "/path/with/Homer's mod.mod" /tmp/clean_name.mod
  bin/nwn-manager unpack /tmp/clean_name.mod
  ```

## Out of scope (today)

- Watch mode and auto-repack on save.
- Decompiling existing `.ncs` when source `.nss` is missing.
- TLK / HAK packing — supported by nasher via `nasher.cfg`, but no wrapper
  command in v1.
- Git hooks and CI helpers.
