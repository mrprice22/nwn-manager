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
}

# Print the resolved configuration — used by --show-config and worth echoing in
# any run that targets a non-default project, so a mis-pointed repack is obvious
# before it installs anything.
repack_print_project() {
  echo "  project:   $PROJECT"
  echo "  season:    num='${SEASON_NUM:-unset}' role='${SEASON_ROLE:-unset}'"
  echo "  artifact:  $MODFILE"
  echo "  installed: $NWN_MOD_DEST"
  echo "  onedrive:  $ONEDRIVE_MOD_DEST"
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
