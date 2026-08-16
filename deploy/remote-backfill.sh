# Embed photos that predate re-identification, on the box. Fed over SSH via
# `ssh "$HOST" "DRY_RUN=$DRY_RUN bash -s" < deploy/remote-backfill.sh` -- the
# same shape as remote.sh and remote-seed-models.sh.
#
# Nothing embeds existing photos automatically, and find_candidates filters on
# vec_miew IS NOT NULL, so until this runs the whole existing corpus is
# invisible to matching. That matters more than it sounds: measured accuracy
# tracks how many photos of a dog are already stored (one ~37% top-1, eight
# ~83%), so an unembedded corpus leaves the system at its worst point.
set -euo pipefail

COMPOSE="docker-compose.prod.yml"
cd ~/app

# Refuse rather than write nothing. Without the models the embedder raises for
# every photo, the script dutifully reports "no animal detected" on all of them,
# and you get a clean-looking run that accomplished nothing.
health=$(sudo docker compose -f "$COMPOSE" exec -T app \
  sh -c 'curl -sf http://localhost:8000/health' || true)
case "$health" in
  *'"reid":"ready"'*) ;;
  *) echo "!! /health does not report reid: ready -- models are not loaded" >&2
     echo "   $health" >&2
     exit 1 ;;
esac

ARGS="--dry-run"
[ "${DRY_RUN:-true}" = "false" ] && ARGS="--resolve ${EXTRA_ARGS:-}"

echo "==> backfill_embeddings.py $ARGS"
# Serial by design: embedding is CPU-bound ONNX on the same box that serves
# requests. -T because there is no TTY on the far side of an SSH pipe.
sudo docker compose -f "$COMPOSE" exec -T app \
  sh -c "cd /app/backend && uv run python scripts/backfill_embeddings.py $ARGS"
