# shellcheck shell=bash
# Shared definition of "this build is broken", sourced by nwn-manager and
# repack-homers-lotr so the desktop shortcut, the season deploy path and a bare
# `nwn-manager repack` all fail on exactly the same conditions.
#
# WHY THIS HAS TO BE PATTERN MATCHING ON OUTPUT
#
# nasher is invoked with --yes (force-answer its overwrite/continue prompts) and
# --nssFlags:-y (keep compiling past a bad script, so one failure does not mask
# every later one). Both are wanted. Together they mean nasher will pack a
# module containing scripts that never compiled and still exit 0, so its exit
# status proves nothing and the compiler's own words are the only signal.
#
# A module with failed compiles must never reach a server folder. A missing
# .ncs does not announce itself at runtime -- the script simply does nothing,
# silently, forever.
#
#   ^Compile Error:            nwn_script_comp's per-script failure
#   ^Error:                    nasher's own fatal errors
#   is not a valid             bad resref / target name
#   longer than 16 characters  resref overflow; the script will never load
#   Results: ... N errored     the compiler's own tally. The authoritative one:
#                              it catches failures whose message text matches
#                              none of the patterns above. "0 errored" is
#                              excluded by requiring a leading 1-9.
#   Warning: Compiled only     partial compile
NWNMGR_BUILD_ERROR_RE='^Compile Error:|^Error:|is not a valid|longer than 16 characters|Results:.*[1-9][0-9]* errored|Warning: Compiled only'
