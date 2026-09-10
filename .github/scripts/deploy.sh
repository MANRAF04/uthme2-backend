#!/usr/bin/env bash
set -euo pipefail

: "${DEPLOY_PATH:?DEPLOY_PATH is not set}"

COMPOSE_DIR="${COMPOSE_DIR:-$DEPLOY_PATH}"
COMPOSE_SERVICES="${COMPOSE_SERVICES:-}"
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
  echo "uth.ovpn is missing from $DEPLOY_PATH, copy it to the server once (it is gitignored)" >&2
  exit 1
fi

git fetch --prune origin
git checkout -B "$DEPLOY_BRANCH" "${GIT_SHA:-origin/$DEPLOY_BRANCH}"
git log -1 --oneline

cd "$COMPOSE_DIR"

# COMPOSE_SERVICES is unquoted so a space separated list expands to multiple args
# shellcheck disable=SC2086
compose build $COMPOSE_SERVICES
# shellcheck disable=SC2086
compose up -d $COMPOSE_SERVICES

compose ps
docker image prune -f
