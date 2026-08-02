#!/usr/bin/env bash
# Deploy the current origin/main to the production box.
#
# Usage:  ./deploy/deploy.sh            # deploy origin/main
#         HOST=seoul ./deploy/deploy.sh # deploy somewhere else
#
# The box is a git checkout of this repo. Secrets live only in the box's
# .env, which this script never touches. Host details are an ssh config
# alias, not values in this public repo -- see docs/OPERATIONS.md.
set -euo pipefail

HOST="${HOST:-argos}"

echo "==> deploying to '$HOST'"

# Refuse to deploy a dirty or unpushed tree: the box pulls from origin, so
# anything not pushed simply would not ship, silently.
if [ -n "$(git status --porcelain)" ]; then
  echo "!! working tree is dirty -- commit or stash first" >&2
  exit 1
fi
git fetch -q origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "!! HEAD is not origin/main -- push first (or you'll deploy something else)" >&2
  exit 1
fi

ssh "$HOST" 'bash -s' < "$(dirname "$0")/remote.sh"

echo "==> waiting for the app to answer"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' https://app.nammaindies.org/ || true)
  if [ "$code" = "200" ]; then echo "==> live (HTTP $code)"; exit 0; fi
  sleep 3
done

echo "!! app did not return 200 after 90s -- check: ssh $HOST 'cd ~/app && sudo docker compose -f docker-compose.prod.yml logs --tail=50 app'" >&2
exit 1
