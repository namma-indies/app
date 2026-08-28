# Embed photos that predate re-identification, on the box. Fed over SSH by
# prepending the settings to stdin, NOT by an env prefix on the command:
#
#   { echo "DRY_RUN=false"; cat deploy/remote-backfill.sh; } | ssh "$HOST" 'bash -s'
#
# The CI deploy key is restricted in the box's authorized_keys with
# `command="bash -s"`. A forced command discards whatever the client sends, so
# `ssh host "DRY_RUN=false bash -s"` silently loses the assignment and this
# script falls back to its --dry-run default -- a real run that reports success
# and writes nothing. stdin is the one channel a forced command cannot drop.
#
# Which creates the trap below: when the script IS stdin, any command that reads
# stdin eats the rest of the script. `docker compose exec -T` does exactly that,
# so bash reached the health check, lost everything after it, and exited 0 --
# a green run that never invoked the backfill at all. Hence `< /dev/null` on
# every `exec -T` here. It is not decoration; without it this file silently
# truncates itself, and how much it loses depends on bash's read buffering,
# which is why the failure looked intermittent.
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
# urllib, not curl: the runtime image is python:3.12-slim, which ships no curl.
# The previous version failed here on every invocation with "sh: 1: curl: not
# found", so the backfill could never run at all.
health=$(sudo docker compose -f "$COMPOSE" exec -T app \
  python -c 'import urllib.request,sys; sys.stdout.write(urllib.request.urlopen("http://localhost:8000/health", timeout=10).read().decode())' \
  < /dev/null || true)
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
  sh -c "cd /app/backend && uv run python scripts/backfill_embeddings.py $ARGS" \
  < /dev/null
