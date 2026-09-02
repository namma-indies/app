# Report sightings that look like one capture stored twice. Read-only.
#
# Fed over SSH via `ssh "$HOST" 'bash -s' < deploy/remote-find-duplicates.sh`,
# the same shape as remote-backfill.sh and remote.sh.
#
# No health gate, unlike the backfill: this touches no model and writes nothing,
# so there is no state it could leave half-done. The worst outcome is a query
# that returns nothing useful.
set -euo pipefail

COMPOSE="docker-compose.prod.yml"
cd ~/app

sudo docker compose -f "$COMPOSE" exec -T app \
  sh -c "cd /app/backend && uv run python scripts/find_duplicate_sightings.py"
