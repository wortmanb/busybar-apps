#!/usr/bin/env bash
# /opt/busybar-deploy/deploy.sh
# Pull-based deploy for wortmanb/busybar-apps.
# Fetches the public deploy-lab branch and, if the commit changed,
# atomically publishes the new build under /srv/busybar-apps/current.
# Run via systemd timer (busybar-deploy.timer).
set -euo pipefail

REPO_URL="https://github.com/wortmanb/busybar-apps.git"
BRANCH="deploy-lab"
GIT_DIR="/opt/busybar-deploy/repo.git"
SERVE_ROOT="/srv/busybar-apps"          # real dir, owned by gh-runner
RELEASES_DIR="$SERVE_ROOT/releases"
CURRENT_LINK="$SERVE_ROOT/current"      # symlink Caddy reads
KEEP_RELEASES=2

log() { echo "[busybar-deploy] $*"; }

# ── Initialise bare repo on first run ────────────────────────────────────────
if [ ! -d "$GIT_DIR" ]; then
  log "Initialising bare repo…"
  git clone --bare --depth=1 --branch "$BRANCH" "$REPO_URL" "$GIT_DIR"
fi

# ── Fetch latest commit ───────────────────────────────────────────────────────
log "Fetching $BRANCH…"
git --git-dir="$GIT_DIR" fetch --depth=1 origin "$BRANCH"

NEW_SHA=$(git --git-dir="$GIT_DIR" rev-parse FETCH_HEAD)
log "Remote SHA: $NEW_SHA"

# ── Compare with deployed SHA ────────────────────────────────────────────────
CURRENT_SHA="(none)"
SHA_FILE="$CURRENT_LINK/.deploy-sha"
if [ -L "$CURRENT_LINK" ] && [ -f "$SHA_FILE" ]; then
  CURRENT_SHA=$(cat "$SHA_FILE")
fi

if [ "$NEW_SHA" = "$CURRENT_SHA" ]; then
  log "Already at $NEW_SHA — nothing to do."
  exit 0
fi

log "Deploying $NEW_SHA (was $CURRENT_SHA)…"

# ── Export commit to a new release directory ──────────────────────────────────
mkdir -p "$RELEASES_DIR"
RELEASE_DIR="$RELEASES_DIR/$NEW_SHA"
mkdir -p "$RELEASE_DIR"
git --git-dir="$GIT_DIR" archive FETCH_HEAD | tar -x -C "$RELEASE_DIR"
echo "$NEW_SHA" > "$RELEASE_DIR/.deploy-sha"

# ── Atomic symlink swap ───────────────────────────────────────────────────────
# ln -sfn + mv -Tf is POSIX-atomic on Linux (single rename syscall).
# Both operations are within /srv/busybar-apps which gh-runner owns.
ln -sfn "$RELEASE_DIR" "${CURRENT_LINK}.next"
mv -Tf "${CURRENT_LINK}.next" "$CURRENT_LINK"

log "Deployed $NEW_SHA → $CURRENT_LINK"

# ── Prune old releases ────────────────────────────────────────────────────────
# shellcheck disable=SC2012
OLD_RELEASES=$(ls -1dt "$RELEASES_DIR"/*/  2>/dev/null | tail -n "+$((KEEP_RELEASES + 1))")
if [ -n "$OLD_RELEASES" ]; then
  echo "$OLD_RELEASES" | xargs rm -rf
  log "Pruned old releases."
fi
