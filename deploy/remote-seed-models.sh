# What happens on the box when seeding model weights. Fed over SSH via
# `{ echo "UPLOAD=$UPLOAD"; cat deploy/remote-seed-models.sh; } | ssh "$HOST" 'bash -s'`
# -- settings via stdin, since a forced command drops an env prefix.
# see .github/workflows/seed-models.yml. Same shape as remote.sh and
# remote-staging.sh, and the same compose file and path as both.
#
# The weights are gitignored and ~429 MB, so they never arrive with a deploy.
# Without them both ML background tasks catch a missing model and every upload
# saves with no embedding -- re-ID looks deployed and does nothing, which
# /health reports as "reid": "degraded".
#
# Expects miewid_msv3.onnx and yolo26x.onnx already in /tmp on this box.
set -euo pipefail

COMPOSE="docker-compose.prod.yml"
cd ~/app

# Into the container, which mounts a named volume at app/ml -- so the weights
# survive the container being replaced on the next deploy.
for f in miewid_msv3.onnx yolo26x.onnx; do
  test -f "/tmp/$f" || { echo "!! /tmp/$f missing" >&2; exit 1; }
  sudo docker compose -f "$COMPOSE" cp "/tmp/$f" "app:/app/backend/app/ml/$f"
  rm -f "/tmp/$f"
done

if [ "${UPLOAD:-true}" = "true" ]; then
  # Uploaded BY the box, using the S3 credentials already in its .env, so those
  # never have to exist as GitHub secrets. Once this has run, any future box
  # fetches the weights on boot by itself and this job is not needed again.
  # `< /dev/null` because this script is itself piped over stdin, and
  # `exec -T` without it consumes the remainder -- silently skipping the
  # `restart app` below. See remote-backfill.sh's header.
  sudo docker compose -f "$COMPOSE" exec -T app \
    sh -c 'cd /app/backend && uv run python scripts/fetch_models.py --upload' \
    < /dev/null
fi

# The fetch only runs at boot, so a running container will not pick up files
# that appeared underneath it.
sudo docker compose -f "$COMPOSE" restart app
