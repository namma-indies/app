#!/usr/bin/env bash
# Run the daily @nammaindies post inside the app container. Called by
# deploy/social-post.service; safe to run by hand (`sudo systemctl start
# social-post.service`), since a day that has already posted posts nothing.
#
# `< /dev/null` on the exec: `docker compose exec -T` reads stdin, and when a
# script is piped to `bash -s` that stdin is the rest of the script -- see the
# trap described in deploy/remote-backfill.sh. Harmless under systemd, needed
# if this is ever run the other way.
set -euo pipefail
COMPOSE="docker-compose.prod.yml"
cd ~/app

sudo docker compose -f "$COMPOSE" exec -T app \
  sh -c 'cd /app/backend && uv run python scripts/post_daily.py' \
  < /dev/null
