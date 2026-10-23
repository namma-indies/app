# Link sightings that share a byte-identical photo, on the box. Fed over SSH by
# prepending the settings to stdin, NOT by an env prefix on the command:
#
#   { echo "APPLY=true"; cat deploy/remote-merge-identical.sh; } | ssh "$HOST" 'bash -s'
#
# Sibling of remote-backfill.sh, and it inherits both of that file's traps,
# restated here because the next reader may not open that one:
#
#   1. The CI deploy key is pinned in the box's authorized_keys with a forced
#      command. A forced command discards whatever the client sends, so
#      `ssh host "APPLY=true bash -s"` silently loses the assignment and this
#      script falls back to its dry-run default -- a run that reports success
#      and links nothing. stdin is the one channel a forced command cannot drop.
#
#   2. Because the script IS stdin, any command that reads stdin eats the rest
#      of it. `docker compose exec -T` does exactly that, so `< /dev/null` on
#      the exec below is not decoration: without it this file truncates itself
#      at that line, and how much it loses depends on bash's read buffering.
#
# No /health gate, unlike the backfill: this loads no model. It reads photos
# from S3 and writes only hashes, identity links and proposal deletions, all in
# one transaction, so an interrupted run leaves nothing half-done.
set -euo pipefail

COMPOSE="docker-compose.prod.yml"
cd ~/app

ARGS=""
[ "${APPLY:-false}" = "true" ] && ARGS="--apply"

echo "==> merge_identical_photos.py ${ARGS:-(dry run)}"
sudo docker compose -f "$COMPOSE" exec -T app \
  sh -c "cd /app/backend && uv run python scripts/merge_identical_photos.py $ARGS" \
  < /dev/null
