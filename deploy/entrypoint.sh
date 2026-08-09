#!/bin/sh
set -e

cd /app/backend

# The ONNX models are gitignored and not baked into the image, so a fresh
# container has none. Fetch them before serving: without them both ML
# background tasks raise, get caught, and every upload saves with no embedding
# -- re-ID looks deployed and does nothing. Deliberately NOT `set -e` fatal:
# a model-fetch failure should degrade re-ID, not take the whole site down.
# /health reports which models are present so a deploy check can catch it.
uv run python scripts/fetch_models.py || echo "!! model fetch failed -- re-ID will be DISABLED"

uv run alembic upgrade head
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
