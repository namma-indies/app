#!/usr/bin/env bash
# What actually happens on the box during a deploy. Fed over SSH via
# `ssh "$HOST" 'bash -s' < deploy/remote.sh` -- see deploy.sh (manual runs)
# and .github/workflows/deploy.yml (CI runs). Single source of truth for
# both so they can't drift.
set -euo pipefail

COMPOSE="docker-compose.prod.yml"

cd ~/app
git pull --ff-only origin main
echo "==> now at $(git rev-parse --short HEAD)"
sudo docker compose -f "$COMPOSE" up -d --build
sudo docker image prune -f >/dev/null
