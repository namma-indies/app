# Rescore every photo with the current detector, on the box. Fed over SSH by
# prepending the settings to stdin, NOT by an env prefix on the command:
#
#   { echo "MODE=run"; cat deploy/remote-rescore.sh; } | ssh "$HOST" 'bash -s'
#
# This is `remote-backfill.sh`'s sibling and inherits both of its traps, which
# are worth restating rather than assuming the next reader finds that file:
#
#   1. The CI deploy key is pinned in the box's authorized_keys with a forced
#      command. A forced command discards whatever the client sends, so
#      `ssh host "MODE=run bash -s"` silently loses the assignment and this
#      script falls back to its dry-run default -- a real run that reports
#      success and writes nothing. stdin is the one channel a forced command
#      cannot drop.
#
#   2. Because the script IS stdin, any command that reads stdin eats the rest
#      of the script. `docker compose exec -T` does exactly that. Hence
#      `< /dev/null` on every `exec -T` below. It is not decoration; without it
#      this file silently truncates itself, and how much it loses depends on
#      bash's read buffering, which is why that failure looked intermittent.
#
# WHY THIS EXISTS
# ---------------
# `sightings.dog_confidence` was scored by two detectors that disagree badly --
# YOLOv8n gave 0.021 to a clearly visible dog where YOLO26x gives 0.800 -- and
# nothing recorded which one ran. Until this has scored the whole corpus under
# one model, `animal_confidence` is NULL for every pre-existing sighting, the
# moderator queue at /moderation/animals is empty, and no threshold can be
# chosen honestly. Running this is step one of that; see #67.
#
# Safe to run repeatedly and safe to interrupt: pending is "no row for THIS
# model", each photo is written in its own statement, `save_detection` upserts,
# and the sighting's number is recomputed per photo rather than at the end.
set -euo pipefail

COMPOSE="docker-compose.prod.yml"
cd ~/app

# Refuse rather than write nothing. Without the models loaded the detector
# raises for every photo, the script dutifully logs a failure on each, and you
# get a run that finished having scored exactly zero things.
# urllib, not curl: the runtime image is python:3.12-slim, which ships no curl.
health=$(sudo docker compose -f "$COMPOSE" exec -T app \
  python -c 'import urllib.request,sys; sys.stdout.write(urllib.request.urlopen("http://localhost:8000/health", timeout=10).read().decode())' \
  < /dev/null || true)
case "$health" in
  *'"reid":"ready"'*) ;;
  *) echo "!! /health does not report reid: ready -- models are not loaded" >&2
     echo "   $health" >&2
     exit 1 ;;
esac

case "${MODE:-dry-run}" in
  run)
      # --embed fills a missing MiewID vector from the same detection pass.
      # Rescoring and then running backfill_embeddings would be two forward
      # passes over identical bytes, which is the waste #49 removed from the
      # capture path.
      ARGS="--embed ${EXTRA_ARGS:-}"
      ;;
  histogram)
      # Reads what is already stored and scores nothing. This is the input to
      # choosing a threshold -- only the input. A histogram says where the
      # scores cluster, not where the detector starts being wrong; for that,
      # walk /moderation/animals from the bottom.
      ARGS="--histogram"
      ;;
  *)
      ARGS="--dry-run"
      ;;
esac

echo "==> rescore_photos.py $ARGS"
# Serial by design: inference is CPU-bound ONNX on the same box that serves
# requests. -T because there is no TTY on the far side of an SSH pipe.
sudo docker compose -f "$COMPOSE" exec -T app \
  sh -c "cd /app/backend && uv run python scripts/rescore_photos.py $ARGS" \
  < /dev/null
