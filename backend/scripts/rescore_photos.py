"""Score every photo with the current detector, and record which one that was.

`sightings.dog_confidence` is not comparable across rows. Older captures were
scored by YOLOv8n, newer ones by YOLO26x, and the two disagree badly -- v8n
scored a clearly visible dog at 0.021 where 26x gives 0.800. Which model
produced a given score was never recorded, so filtering on the column would
silently hide real dogs whose only crime was being uploaded early (issue #67).

This makes one number mean one thing. After it runs, every photo has a
`detections` row for `DETECTOR_NAME`, and `sightings.animal_confidence` is the
max over each sighting's photos of `max(dog, cat)` under that detector.

Safe to run repeatedly and safe to interrupt: pending is "no row for THIS
model", each photo is written in its own statement, and `save_detection`
upserts. `recompute_animal_confidence` runs right after `save_detection` for
every photo, not batched at the end -- a photo that is no longer pending has
therefore already had its sighting's number folded in, so killing the process
mid-run leaves `animal_confidence` consistent with whatever got scored, and
loses at most the photo in flight. Deferring the recompute to a set collected
across the whole run would mean a kill after the last `save_detection` but
before that batch loses every one of those updates silently, and a re-run
would not repair it: those photos are no longer pending, so they never
re-enter the batch. That resume key is why the table is keyed on the model at
all -- `backfill_embeddings.py` documents what the `IS NULL` alternative
costs.

One connection, not a pool, unlike the sibling: the run is deliberately
serial (see below), so there is never more than one query in flight and a
pool would only add ceremony.

Usage, from /app/backend inside the container:

    uv run python scripts/rescore_photos.py --dry-run
    uv run python scripts/rescore_photos.py
    uv run python scripts/rescore_photos.py --embed
    uv run python scripts/rescore_photos.py --histogram

`--embed` also fills a missing MiewID vector from the same detection pass.
Rescoring and then running `backfill_embeddings.py` would be two forward
passes over identical bytes, which is exactly the waste #49 removed from the
capture path -- and note that the older script calls `embed.embed_photo`,
which re-decodes and re-detects internally. This one uses `analyse()` +
`embed_analysis()`, the pair the capture path uses, so one pass serves both.

`--histogram` scores nothing and prints the distribution of what is already
stored. That is the input to choosing `animal_confidence_min` -- but only the
input. A histogram says where the scores cluster, not where the detector
starts being wrong; for that, walk /moderation/animals from the bottom.

Deliberately serial. Inference is CPU-bound ONNX on the same box that serves
requests; a parallel backfill would compete with live uploads for the same two
cores. At roughly 0.9 s per pass the whole corpus is single-digit minutes, and
`--sleep` throttles it further if it has to run during the day.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import asyncpg

# Run as a script rather than a module, so `app` is not importable yet.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analyse import analyse, embed_analysis  # noqa: E402
from app.db import effective_dsn  # noqa: E402
from app.detect_reid import DETECTOR_NAME  # noqa: E402
from app.embed import EMBED_DIM, MODEL_NAME  # noqa: E402
from app.ids import uuid7  # noqa: E402
from app.scoring import recompute_animal_confidence, save_detection  # noqa: E402
from app.storage.s3 import storage_from_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rescore")

PENDING_SQL = """
    SELECT p.id, p.s3_key, p.sighting_id
    FROM photos p
    LEFT JOIN detections d
           ON d.photo_id = p.id AND d.model = $1
    WHERE d.photo_id IS NULL
    ORDER BY p.created_at
"""

HISTOGRAM_SQL = """
    SELECT width_bucket(greatest(d.dog, d.cat), 0, 1, 10) AS bucket,
           count(*) AS photos
    FROM detections d
    WHERE d.model = $1
    GROUP BY 1
    ORDER BY 1
"""


async def _histogram(conn) -> int:
    rows = await conn.fetch(HISTOGRAM_SQL, DETECTOR_NAME)
    if not rows:
        log.info("nothing scored by %s yet", DETECTOR_NAME)
        return 1
    log.info("max(dog, cat) under %s, per photo:", DETECTOR_NAME)
    for r in rows:
        lo = (r["bucket"] - 1) / 10
        log.info("  %.1f-%.1f  %s", lo, lo + 0.1, "#" * r["photos"])
    log.info("")
    log.info("This says where scores cluster, not where the detector starts")
    log.info("being wrong. Walk /moderation/animals from the bottom for that.")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="stop after N photos (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to pause between photos, to stay out of the way")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be scored, touch nothing")
    ap.add_argument("--embed", action="store_true",
                    help="also fill a missing MiewID vector from the same pass")
    ap.add_argument("--histogram", action="store_true",
                    help="print the distribution of existing scores and exit")
    args = ap.parse_args()

    conn = await asyncpg.connect(effective_dsn())
    try:
        if args.histogram:
            return await _histogram(conn)

        pending = await conn.fetch(PENDING_SQL, DETECTOR_NAME)
        if args.limit:
            pending = pending[: args.limit]
        log.info("%d photo(s) with no %s score", len(pending), DETECTOR_NAME)
        if args.dry_run or not pending:
            return 0

        storage = storage_from_settings()
        touched: set = set()
        failed = 0

        for i, row in enumerate(pending, 1):
            try:
                raw = await storage.get(row["s3_key"])
                found = await asyncio.to_thread(analyse, raw)
            except Exception:
                # Fail open, as the capture path does: no row means "never
                # scored", the sighting stays visible, and a later run retries.
                failed += 1
                log.warning("  [%d/%d] %s FAILED", i, len(pending), row["id"],
                            exc_info=True)
                continue

            # Outside any swallowing try: a failure here should abort loudly,
            # not be mistaken for "photo not scored".
            await save_detection(conn, row["id"], found.dog_confidence,
                                 found.cat_confidence)
            log.info("  [%d/%d] %s dog=%.3f cat=%.3f", i, len(pending), row["id"],
                     found.dog_confidence, found.cat_confidence)

            # Recomputed immediately, not batched at the end: a kill after
            # this point leaves animal_confidence consistent with every
            # detections row written so far. See the module docstring.
            try:
                await recompute_animal_confidence(conn, row["sighting_id"])
                touched.add(row["sighting_id"])
            except Exception:
                # A failing recompute costs this sighting's number, not the
                # run -- the remaining photos still get scored.
                log.warning("    recompute failed for sighting=%s", row["sighting_id"],
                            exc_info=True)

            if args.embed and found.has_animal:
                await _maybe_embed(conn, row["id"], found)

            if args.sleep:
                time.sleep(args.sleep)

        log.info("scored %d, failed %d, %d sighting(s) updated",
                 len(pending) - failed, failed, len(touched))
        return 0 if failed == 0 else 1
    finally:
        await conn.close()


async def _maybe_embed(conn, photo_id, found) -> None:
    """Fill a missing vector from the detection pass we already paid for."""
    exists = await conn.fetchval(
        "SELECT 1 FROM embeddings WHERE photo_id=$1 AND model=$2 AND vec_miew IS NOT NULL",
        photo_id, MODEL_NAME)
    if exists:
        return
    try:
        vec = await asyncio.to_thread(embed_analysis, found)
    except Exception:
        log.warning("    embed failed for %s", photo_id, exc_info=True)
        return
    if vec is None:
        return
    box = found.box
    await conn.execute(
        """
        INSERT INTO embeddings (id, photo_id, model, dim, vec_miew, bbox)
        VALUES ($1,$2,$3,$4,$5::vector,$6::jsonb)
        ON CONFLICT (photo_id, model) DO UPDATE
            SET vec_miew = EXCLUDED.vec_miew, bbox = EXCLUDED.bbox, created_at = now()
        """,
        uuid7(), photo_id, MODEL_NAME, EMBED_DIM,
        "[" + ",".join(f"{float(v):.7g}" for v in vec) + "]",
        json.dumps({"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]}))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
