#!/usr/bin/env bash
# Resolve the season-scoped values a repack wrapper needs, from the target
# repo's own nasher.cfg + server.env. Sourced by repack-homers-lotr and
# repack-homers-lotr-clean so one pair of scripts serves every season.
#
# Before this existed, both wrappers hard-coded six values — the project path,
# the build artifact name (eight occurrences), the OneDrive copy dir, the NWN
# modules dir, the installed .mod filename and the archive prefix — so a new
# season needed a hand-edited clone. That was the single most error-prone step
# in a cutover's Phase 1 (see season-cutover-prereqs.md item 5).
#
# Usage:
#   . "$(dirname "${BASH_SOURCE[0]}")/repack-project.sh"
#   repack_resolve_project "$@"      # consumes nothing; reads REPACK_PROJECT
#
# Project selection, highest precedence first:
#   --project DIR   passed to the wrapper (the wrapper stores it in REPACK_PROJECT)
#   $NWN_PROJECT    environment
#   the default below — the unnumbered repo, which is the permanent DEV realm.
#                       Building dev by default is right: dev is the only place
#                       development happens. Production repos are never built
#                       directly — bin/season-promote.sh drives their repack.
#
# Sets, on success:
#   PROJECT             absolute path to the module repo
#   MODFILE             build artifact, e.g. homers_lotr_v3.mod   (nasher.cfg [target].file)
#   MODBASE             MODFILE without the .mod extension
#   NWN_MOD_DIR         $NWN_HOME_DIR/modules
#   NWN_MOD_DEST        installed module path — MUST match NWN_MODULE exactly
#   ONEDRIVE_ROOT       OneDrive share root    (override: $REPACK_ONEDRIVE_DIR)
#   ONEDRIVE_MOD_DIR    per-environment subdir — $ONEDRIVE_ROOT/Season<N>,
#                       or $ONEDRIVE_ROOT/Test when SEASON_ROLE=dev
#   ONEDRIVE_MOD_DEST   OneDrive copy path
#   plus everything server.env exports (NWN_MODULE, NWN_HOME_DIR, SEASON_*, …)

REPACK_DEFAULT_PROJECT=${REPACK_DEFAULT_PROJECT:-/var/home/james/GIT/nwn_homers_lotr}

repack_resolve_project() {
  PROJECT=${REPACK_PROJECT:-${NWN_PROJECT:-$REPACK_DEFAULT_PROJECT}}
  PROJECT=$(cd "$PROJECT" 2>/dev/null && pwd) || {
    echo "error: project dir not found: ${REPACK_PROJECT:-${NWN_PROJECT:-$REPACK_DEFAULT_PROJECT}}" >&2
    return 1
  }

  [[ -f $PROJECT/nasher.cfg ]] || { echo "error: no nasher.cfg in $PROJECT" >&2; return 1; }
  [[ -f $PROJECT/server.env ]] || { echo "error: no server.env in $PROJECT" >&2; return 1; }

  # shellcheck disable=SC1091
  . "$PROJECT/server.env"

  # [target].file is the build artifact nasher writes into the project root.
  MODFILE=$(sed -n 's/^[[:space:]]*file[[:space:]]*=[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' \
              "$PROJECT/nasher.cfg" | head -1)
  [[ -n $MODFILE ]] || { echo "error: no [target].file in $PROJECT/nasher.cfg" >&2; return 1; }
  MODBASE=${MODFILE%.mod}

  # In NWN the module name IS the installed .mod filename, so NWN_MODULE must
  # equal it minus the extension or nwserver exits at boot with module-not-found.
  [[ -n ${NWN_MODULE:-} ]] || { echo "error: NWN_MODULE unset in $PROJECT/server.env" >&2; return 1; }
  [[ -n ${NWN_HOME_DIR:-} ]] || { echo "error: NWN_HOME_DIR unset in $PROJECT/server.env" >&2; return 1; }
  NWN_MOD_DIR="$NWN_HOME_DIR/modules"
  NWN_MOD_DEST="$NWN_MOD_DIR/$NWN_MODULE.mod"

  # Each season gets its own subfolder under the shared OneDrive root. Builds go
  # out to the Windows toolset from here and come back deliberately renamed as
  # point-in-time backups, so the unpack side can't key on a filename — but with
  # the folder season-scoped, "newest mtime in this dir" is unambiguous.
  #
  # The dev realm gets its OWN folder rather than Season<N>. It is permanent and
  # is never a season, but it carries SEASON_NUM = whichever season it currently
  # feeds — so keying purely on the number would land dev and that season's
  # production repo in the same directory. Two environments sharing one folder
  # breaks the unpack side outright: refresh-homers-lotr takes the newest mtime
  # in the dir, so a dev build would be unpacked into production (and vice
  # versa) with nothing to signal it. Scope by role first, number second.
  ONEDRIVE_ROOT=${REPACK_ONEDRIVE_DIR:-$HOME/OneDrive/Games/NWNHomersLOTR}
  if [[ ${SEASON_ROLE:-} == dev ]]; then
    ONEDRIVE_MOD_DIR="$ONEDRIVE_ROOT/Test"
  elif [[ -n ${SEASON_NUM:-} ]]; then
    ONEDRIVE_MOD_DIR="$ONEDRIVE_ROOT/Season$SEASON_NUM"
  else
    ONEDRIVE_MOD_DIR="$ONEDRIVE_ROOT"   # non-seasoned project: legacy flat layout
  fi
  ONEDRIVE_MOD_DEST="$ONEDRIVE_MOD_DIR/$MODFILE"

  # Directories the UNPACK side scans, in preference order. WRITING stays
  # single-target ($ONEDRIVE_MOD_DEST above) -- only reading widens, so a build
  # still lands in exactly one folder and the round-trip has one canonical home.
  #
  # The dev realm gets a second read location: builds sent out to the Windows
  # toolset have been coming back into $ONEDRIVE_ROOT/Dev as well as Test/, and
  # an unpack that scans only Test/ silently ignores the newer one -- you get a
  # stale unpacked/ with nothing saying so. Seasons deliberately do NOT scan Dev/:
  # pulling a dev build into a production repo is the exact cross-contamination
  # the per-role folder split exists to prevent.
  ONEDRIVE_MOD_DIRS=( "$ONEDRIVE_MOD_DIR" )
  if [[ ${SEASON_ROLE:-} == dev ]]; then
    ONEDRIVE_MOD_DIRS+=( "$ONEDRIVE_ROOT/Dev" )
  fi
}

# Print the resolved configuration — used by --show-config and worth echoing in
# any run that targets a non-default project, so a mis-pointed repack is obvious
# before it installs anything.
repack_print_project() {
  echo "  project:   $PROJECT"
  echo "  season:    num='${SEASON_NUM:-unset}' role='${SEASON_ROLE:-unset}'"
  echo "  artifact:  $MODFILE"
  echo "  installed: $NWN_MOD_DEST"
  # The plain ONEDRIVE_MOD_DEST name is no longer written -- each build gets its
  # own timestamped file -- so report the folder and the pattern instead of a
  # path that will not exist.
  echo "  onedrive:  $ONEDRIVE_MOD_DIR/${MODBASE}_<YYYYmmdd_HHMMSS>.mod"
  echo "  retention: ${REPACK_KEEP_BUILDS:-5} builds in the OneDrive folder and in the repo root"
}

# ---------------------------------------------------------------------------
# Push the OneDrive copy to the cloud NOW, instead of waiting for the next boot.
#
# The copy under $ONEDRIVE_MOD_DIR is only a LOCAL mirror. Nothing uploads it on
# its own: the only `onedrive --sync` on this host is the one at the end of
# bin/backup-homers-lotr, which runs once per boot behind a 24h sentinel. So a
# build made at midday did not reach the cloud (or any other machine) until the
# 03:00 reboot -- 12+ hours later, and not at all on a day with no reboot. From
# the OneDrive web view or a second PC that looks exactly like "the repack never
# copied anything", which is the confusion this exists to end (2026-08-22).
#
# Detached on purpose: a full sync walks the whole synced tree and takes minutes,
# and repack must not sit there holding the build window open. The upload keeps
# running after the terminal closes; check $REPACK_ONEDRIVE_LOG for the outcome.
#
# flock -n: the boot backup runs the same command, and two concurrent syncs on
# one item DB is the way to corrupt it. If the lock is held we simply skip --
# the run that holds it scans the whole tree, so it picks this file up anyway.
#
#   REPACK_ONEDRIVE_SYNC=0   skip the upload (leave it to the daily backup)
#   ONEDRIVE_BIN=/path       override binary discovery
: "${REPACK_ONEDRIVE_SYNC:=1}"
: "${REPACK_ONEDRIVE_LOG:=$HOME/.cache/repack-onedrive-sync.log}"
: "${REPACK_ONEDRIVE_LOCK:=$HOME/.cache/onedrive-sync.lock}"

repack_onedrive_upload() {
  [[ ${REPACK_ONEDRIVE_SYNC:-1} -eq 1 ]] || { echo "[onedrive] (REPACK_ONEDRIVE_SYNC=0: skipped)"; return 0; }

  local od=${ONEDRIVE_BIN:-}
  if [[ -z $od ]]; then
    od=$(command -v onedrive || true)
    [[ -z $od && -x /home/linuxbrew/.linuxbrew/opt/onedrive-cli/bin/onedrive ]] \
      && od=/home/linuxbrew/.linuxbrew/opt/onedrive-cli/bin/onedrive
  fi
  [[ -n $od ]] || { echo "[onedrive] WARNING: onedrive binary not found — cloud copy waits for the daily backup"; return 0; }

  mkdir -p "$(dirname "$REPACK_ONEDRIVE_LOG")" "$(dirname "$REPACK_ONEDRIVE_LOCK")"
  setsid nohup flock -n "$REPACK_ONEDRIVE_LOCK" \
    "$od" --sync --threads 1 >>"$REPACK_ONEDRIVE_LOG" 2>&1 &
  disown 2>/dev/null || true
  echo "[onedrive] upload started in the background → $REPACK_ONEDRIVE_LOG"
  echo "[onedrive] (a full sync takes a few minutes; it survives closing this window)"
}

# ---------------------------------------------------------------------------
# Timestamped build names + retention.
#
# The OneDrive copy used to reuse ONE filename per season and be overwritten on
# every build. That overwrite stopped propagating to the Windows PC: it showed
# homers_lotr_test.mod at 8/14 and homers_lotr_s2.mod at 7/25 while this host's
# copies were current and the sync client reported them uploaded (2026-08-23).
# The Windows-side cause is not diagnosable from here, so we stop overwriting:
# a per-build filename is a CREATE, not a MODIFY, and the build date is legible
# in the name. Retention then keeps the folder from growing without bound.
#
#   REPACK_KEEP_BUILDS=N   how many builds to keep per location (default 5)
: "${REPACK_KEEP_BUILDS:=5}"

# One stamp per build, shared by the OneDrive copy and the repo-root archive so
# the two are correlatable by name. Idempotent: later calls reuse the first.
repack_build_stamp() {
  [[ -n ${BUILD_STAMP:-} ]] || BUILD_STAMP=$(date +%Y%m%d_%H%M%S)
  printf '%s\n' "$BUILD_STAMP"
}

# Where THIS build's OneDrive copy goes. ONEDRIVE_MOD_DEST (the plain artifact
# name) stays as the reporting/reference value in repack_print_project -- it is
# no longer written.
repack_onedrive_dest() {
  printf '%s\n' "$ONEDRIVE_MOD_DIR/${MODBASE}_$(repack_build_stamp).mod"
}

# repack_prune_builds <dir> <keep> <mode>
#
# Delete tool-generated builds in <dir> past the <keep> newest. What counts as
# tool-generated is deliberately DIFFERENT per location:
#
#   onedrive  <MODBASE>_YYYYmmdd_HHMMSS.mod, or exactly <MODFILE>. Anchored to
#             the CURRENT nasher.cfg artifact name, because these folders also
#             hold builds the admin renamed by hand as point-in-time backups
#             ("Homer's LOTR TEST ruins scribe update.mod") and those must never
#             be touched. Including the plain <MODFILE> is what retires the
#             stale single-name file now that nothing writes it any more.
#
#   archive   any *_YYYYmmdd_HHMMSS.mod, whatever the prefix; <MODFILE> itself
#             is the live nasher build output and is never pruned. The repo root
#             only ever receives wrapper-written archives, and MODBASE has
#             changed across seasons (homers_lotr_v3 -> _s2 / _test), so
#             anchoring to the current one would strand ~33 GB of older-named
#             archives.
#
# The trailing '$' in both patterns is load-bearing: it protects every
# hand-annotated milestone, which carries a description after the timestamp
# (homers_lotr_v3_20260616_082050_aragorn quest.mod).
repack_prune_builds() {
  local dir=$1 keep=${2:-$REPACK_KEEP_BUILDS} mode=${3:-archive}

  [[ -d $dir ]] || return 0

  # Defence in depth. $ONEDRIVE_ROOT/Dev is the Windows -> Linux direction: it
  # holds builds modified in the toolset and waiting to be unpacked, which exist
  # nowhere else. Nothing here should ever pass it, but ONEDRIVE_MOD_DIRS lists
  # it for READING and one careless edit could wire it in.
  if [[ $(basename "$dir") == Dev ]]; then
    echo "[prune] REFUSING to prune $dir — the Dev folder is never pruned" >&2
    return 1
  fi

  local re
  case "$mode" in
    onedrive) re="^(${MODBASE}_[0-9]{8}_[0-9]{6}|${MODBASE})\.mod$" ;;
    archive)  re="^.*_[0-9]{8}_[0-9]{6}\.mod$" ;;
    *) echo "[prune] unknown mode: $mode" >&2; return 2 ;;
  esac

  # Filter on the BASENAME (%f), not the path: the patterns are ^-anchored and a
  # full path would never match them. Newest mtime first, so tail -n +keep+1 is
  # everything past the retained window.
  local -a victims=()
  mapfile -t victims < <(
    find "$dir" -maxdepth 1 -name '*.mod' -type f -printf '%T@ %f\n' 2>/dev/null \
      | sort -rn \
      | sed 's/^[^ ]* //' \
      | grep -E "$re" \
      | tail -n +$((keep + 1))
  )

  [[ ${#victims[@]} -gt 0 ]] || return 0

  local f
  for f in "${victims[@]}"; do
    rm -f -- "$dir/$f" && echo "[prune] removed $f"
  done
  echo "[prune] $dir: kept the $keep newest build(s), removed ${#victims[@]}"
}
