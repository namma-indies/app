"""Fetch the ONNX model files into app/ml/ if they are not already there.

The weights are gitignored (206 MB + 223 MB) and the Dockerfile does not copy
them, so `git pull` on the box never brings them. Without this, both background
tasks raise ModelUnavailable, get caught, and every upload saves with no
embedding -- re-ID looks deployed and does nothing.

Exporting them on the box is not an option: `scripts/export_*_onnx.py` need
torch, transformers and ultralytics, and keeping torch out of the runtime image
is a deliberate constraint. So the pre-exported artifacts are pulled from object
storage instead, using the same credentials the app already has.

Baking them into the image was rejected for two reasons: it adds ~430 MB to
every deploy, and MiewID-msv3 declares no licence upstream, so shipping it
inside a distributable artifact is a redistribution question nobody has
answered. A private bucket sidesteps both.

Idempotent and safe to run on every boot: a model already present at the
expected size is left alone, so the download happens once per volume rather
than once per deploy.

Upload the artifacts once with:

    uv run python scripts/fetch_models.py --upload
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.storage.s3 import storage_from_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("models")

ML_DIR = Path(__file__).resolve().parent.parent / "app" / "ml"

# name -> minimum plausible size, so a truncated or half-written file is
# re-fetched rather than silently used and failing later inside onnxruntime.
MODELS = {
    "miewid_msv3.onnx": 150_000_000,
    "yolo26x.onnx": 150_000_000,
}


def _prefix() -> str:
    return (settings.models_s3_prefix or "models/").strip("/") + "/"


async def fetch() -> int:
    storage = storage_from_settings()
    missing = []
    for name, min_size in MODELS.items():
        dest = ML_DIR / name
        if dest.exists() and dest.stat().st_size >= min_size:
            log.info("models: %s already present (%d MB)", name, dest.stat().st_size // 1_000_000)
            continue
        missing.append((name, min_size, dest))

    if not missing:
        return 0

    ML_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name, min_size, dest in missing:
        key = _prefix() + name
        try:
            log.info("models: fetching %s from s3://%s/%s", name, settings.s3_bucket, key)
            raw = await storage.get(key)
        except Exception as e:
            # Loud, but not fatal: taking the whole site down because a model
            # fetch failed is worse than running without re-ID. /health reports
            # the degraded state so a deploy check can catch it.
            log.error("models: COULD NOT FETCH %s (%s)", name, e)
            log.error("models: re-identification will be DISABLED until this is fixed")
            failed += 1
            continue
        if len(raw) < min_size:
            log.error("models: %s is only %d bytes, refusing to write", name, len(raw))
            failed += 1
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(raw)
        tmp.replace(dest)  # atomic, so a killed boot never leaves a partial model
        log.info("models: wrote %s (%d MB)", name, len(raw) // 1_000_000)
    return failed


async def upload() -> int:
    """Push local exports up, so a box can fetch them. Run once from a machine
    that has already run the export scripts."""
    storage = storage_from_settings()
    await storage.ensure_bucket()
    for name in MODELS:
        src = ML_DIR / name
        if not src.exists():
            log.error("upload: %s not found locally -- run scripts/export_*_onnx.py first", name)
            return 1
        key = _prefix() + name
        log.info("upload: %s -> s3://%s/%s (%d MB)",
                 name, settings.s3_bucket, key, src.stat().st_size // 1_000_000)
        await storage.put(key, src.read_bytes(), "application/octet-stream")
    log.info("upload: done")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--upload", action="store_true",
                    help="push local exports to object storage instead of fetching")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(upload() if args.upload else fetch()))
