#!/usr/bin/env bash
# What actually happens on the staging box during a deploy. Fed over SSH via
# `ssh "$HOST" "BRANCH=$BRANCH bash -s" < deploy/remote-staging.sh` -- see
# deploy-staging.sh (manual runs) and .github/workflows/deploy-staging.yml
# (CI runs, dispatched against whatever branch you pick).
#
# Deliberately separate from deploy/remote.sh (prod): prod only ever
# fast-forwards `main` -- a hard reset there would be a bug that could nuke
# unpushed history on the box. Staging deploys arbitrary feature branches
# that aren't ancestors of each other, so a plain `git pull --ff-only` would
# fail every time you switch branches. `checkout` + `reset --hard` is correct
# here because the staging box never has local commits worth preserving.
set -euo pipefail

BRANCH="${BRANCH:?set BRANCH to the ref to deploy}"
COMPOSE="docker-compose.prod.yml"

cd ~/app
git fetch -q origin "$BRANCH"
git checkout -q "$BRANCH"
git reset --hard "origin/$BRANCH"
echo "==> now at $BRANCH ($(git rev-parse --short HEAD))"
sudo docker compose -f "$COMPOSE" up -d --build
sudo docker image prune -f >/dev/null
