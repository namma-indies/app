# Report sightings that look like one capture stored twice. Read-only.
#
# Fed over SSH by prepending the settings to stdin, NOT by an env prefix on the
# command:
#
#   { echo "DETAIL=--detail"; cat deploy/remote-find-duplicates.sh; } \
#     | ssh "$HOST" 'bash -s'
#
# The CI deploy key is restricted in the box's authorized_keys with
# `command="bash -s"`. A forced command discards whatever the client sends, so
# `ssh host "DETAIL=--detail bash -s"` silently loses the assignment and this
# script runs its default -- a green run that reports success and answers a
# different question than the one asked. stdin is the one channel a forced
# command cannot drop. See remote-backfill.sh, which documented this first and
# which this file should have been modelled on from the start.
#
# Which creates the second trap: when the script IS stdin, any command that
# reads stdin eats the rest of it. `docker compose exec -T` does exactly that,
# so `< /dev/null` below is not decoration -- without it this file truncates
# itself at that line, and how much it loses depends on bash's read buffering.
# It happens to be harmless here only because the exec is the last statement;
# that is luck, not design, and the next line added below it would vanish.
#
# No health gate, unlike the backfill: this touches no model and writes nothing,
# so there is no state it could leave half-done.
set -euo pipefail

COMPOSE="docker-compose.prod.yml"
cd ~/app

sudo docker compose -f "$COMPOSE" exec -T app \
  sh -c "cd /app/backend && uv run python scripts/find_duplicate_sightings.py ${DETAIL:-}" \
  < /dev/null
