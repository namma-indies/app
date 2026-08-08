#!/usr/bin/env bash
# Deploy the current branch to the staging box by hand.
#
# Usage:  ./deploy/deploy-staging.sh                 # deploy current branch
#         BRANCH=some-feature ./deploy/deploy-staging.sh
#         HOST=my-staging-alias ./deploy/deploy-staging.sh
#
# Unlike deploy.sh (prod), this does not require a clean tree or a
# specific branch -- staging exists to test whatever you're pushing before
# it reaches main. It does still require the branch to be pushed, since the
# box pulls from origin same as prod.
set -euo pipefail

HOST="${HOST:-argos-staging}"
BRANCH="${BRANCH:-$(git branch --show-current)}"
URL="${URL:-https://staging.nammaindies.org}"

if [ -z "$BRANCH" ]; then
  echo "!! detached HEAD -- set BRANCH explicitly" >&2
  exit 1
fi

echo "==> deploying '$BRANCH' to '$HOST'"

git fetch -q origin "$BRANCH"
if [ "$(git rev-parse "$BRANCH")" != "$(git rev-parse "origin/$BRANCH")" ]; then
  echo "!! local '$BRANCH' isn't pushed -- push first (the box pulls from origin)" >&2
  exit 1
fi

ssh "$HOST" "BRANCH=$BRANCH bash -s" < "$(dirname "$0")/remote-staging.sh"

echo "==> waiting for the app to answer"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/" || true)
  if [ "$code" = "200" ]; then echo "==> live (HTTP $code)"; exit 0; fi
  sleep 3
done

echo "!! app did not return 200 after 90s -- check: ssh $HOST 'cd ~/app && sudo docker compose -f docker-compose.prod.yml logs --tail=50 app'" >&2
exit 1
