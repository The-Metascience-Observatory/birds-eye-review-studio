#!/usr/bin/env bash
# Mark build/dependency directories as ignored by Dropbox (kept locally, never
# synced) when the checkout lives inside a Dropbox folder. Dropbox racing
# Next's atomic writes deletes the .tmp files it writes into .next mid-build:
#
#   ENOENT .next/static/development/_buildManifest.js.tmp.<rand>
#   ENOENT .next/server/app/reviews/[id]/dashboard/page/server-reference-manifest.json
#
# which surfaces as a 500 on every page.
#
# The com.dropbox.ignored xattr is LOST whenever a directory is deleted and
# recreated — so `rm -rf .next` silently un-ignores it. Re-run this after any
# cache wipe; the npm `predev`/`prebuild` hooks do that automatically.
#
# Anywhere else (no Dropbox in the path, no xattr tool) this is a silent no-op
# and always exits 0, so it can never fail a dev or build run.
set -u

cd "$(dirname "$0")/.." || exit 0

case "$(pwd -P)" in
  *Dropbox*) ;;
  *) exit 0 ;;
esac

set_ignored() {
  local dir="$1"
  mkdir -p "$dir" 2>/dev/null || return 0
  if command -v attr >/dev/null 2>&1; then
    attr -s com.dropbox.ignored -V 1 "$dir" >/dev/null 2>&1
  elif command -v setfattr >/dev/null 2>&1; then
    setfattr -n user.com.dropbox.ignored -v 1 "$dir" >/dev/null 2>&1
  elif command -v xattr >/dev/null 2>&1; then
    # macOS
    xattr -w com.dropbox.ignored 1 "$dir" >/dev/null 2>&1
  fi
  return 0
}

set_ignored .next
set_ignored node_modules
exit 0
