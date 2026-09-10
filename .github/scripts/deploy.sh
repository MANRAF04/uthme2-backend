#!/usr/bin/env bash
set -euo pipefail

: "${DEPLOY_PATH:?DEPLOY_PATH is not set}"

COMPOSE_DIR="${COMPOSE_DIR:-$DEPLOY_PATH}"
COMPOSE_SERVICES="${COMPOSE_SERVICES:-grades-api grades-worker grades-beat}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
GIT_SHA="${GIT_SHA:-}"

if docker compose version >/dev/null 2>&1; then
  compose() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  compose() { docker-compose "$@"; }
else
  echo "docker compose is not installed on this server" >&2
  exit 1
fi

cd "$DEPLOY_PATH"

if [ ! -f uth.ovpn ]; then
  echo "uth.ovpn is missing from $DEPLOY_PATH, it is gitignored and has to be placed there once" >&2
  exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "$DEPLOY_PATH has uncommitted changes to tracked files, refusing to deploy" >&2
  git status --short --untracked-files=no >&2
  exit 1
fi

git fetch --prune origin
git checkout "$DEPLOY_BRANCH"
git merge --ff-only "${GIT_SHA:-origin/$DEPLOY_BRANCH}"
git log -1 --oneline

if [ -n "$GIT_SHA" ] && [ "$(git rev-parse HEAD)" != "$GIT_SHA" ]; then
  echo "note: HEAD is ahead of the commit that triggered this run, local commits are included" >&2
fi

cd "$COMPOSE_DIR"

# unquoted on purpose so a space separated service list expands to separate args
# shellcheck disable=SC2086
compose build $COMPOSE_SERVICES
# shellcheck disable=SC2086
compose up -d $COMPOSE_SERVICES
# shellcheck disable=SC2086
compose ps $COMPOSE_SERVICES

docker image prune -f
