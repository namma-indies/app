"""Embed photos that predate the re-identification pipeline.

Deploying re-ID does not make existing sightings matchable. `find_candidates`
filters on `vec_miew IS NOT NULL`, so every photo uploaded before this feature
is invisible to matching until something re-derives its vector. That matters
more than it sounds: measured accuracy tracks how many photos of a dog are
already stored (one photo ~37% top-1, eight ~83%), so the existing corpus is
precisely what makes matching work on day one. Leaving it unembedded starts the
system at its worst point while the photos that would fix it sit in S3.

Safe to run repeatedly, and safe to interrupt: it only touches photos with no
vector, writes each one in its own transaction, and upserts on (photo_id,
model). Killing it mid-run loses at most the photo in flight.

Known cost: a photo with no detectable animal gets no row at all (a whole-frame
embedding would pollute candidate search), so it is re-examined on every run.
That is deliberate -- those photos *should* be retried after a detector
upgrade -- but it means the pending count never reaches zero on a corpus
containing dogless photos.

Usage, from /app/backend inside the container:

    uv run python scripts/backfill_embeddings.py --dry-run
    uv run python scripts/backfill_embeddings.py
    uv run python scripts/backfill_embeddings.py --resolve

`--resolve` additionally runs the matching decision for each affected sighting,
so proposals exist without waiting for someone to open the sighting.

Deliberately serial. Embedding is CPU-bound ONNX and this runs against the same
box that serves requests; a parallel backfill would compete with live uploads
for the same cores. `--sleep` throttles it further if it needs to run during
the day.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
import time
from uuid import UUID

import asyncpg

# Run as a script rather than a module, so `app` is not importable yet. Derive
# the backend root from this file instead of hardcoding the container path, or
# it only works inside the image.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import effective_dsn  # noqa: E402
from app.embed import EMBED_DIM, MODEL_NAME, embed_photo  # noqa: E402
from app.ids import uuid7  # noqa: E402
from app.storage.s3 import storage_from_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill")

PENDING_SQL = """
    SELECT p.id, p.s3_key, p.sighting_id
    FROM photos p
    LEFT JOIN embeddings e
           ON e.photo_id = p.id AND e.model = $1
    WHERE e.id IS NULL OR e.vec_miew IS NULL
    ORDER BY p.created_at
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="stop after N photos (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to pause between photos, to stay out of the way")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be embedded, touch nothing")
    ap.add_argument("--resolve", action="store_true",
                    help="also run matching for each affected sighting")
    args = ap.parse_args()

    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    storage = storage_from_settings()
    assert pool is not None

    async with pool.acquire() as conn:
        rows = await conn.fetch(PENDING_SQL, MODEL_NAME)
    if args.limit:
        rows = rows[: args.limit]

    log.info("%d photo(s) need a %s vector", len(rows), MODEL_NAME)
    if args.dry_run or not rows:
        await pool.close()
        return 0

    embedded = no_animal = failed = 0
    touched: set[UUID] = set()
    t0 = time.time()

    for i, r in enumerate(rows, 1):
        try:
            raw = await storage.get(r["s3_key"])
        except Exception:
            # A missing object is a data problem to look at, not a reason to
            # abandon the other several thousand photos.
            log.warning("photo=%s: could not read %s", r["id"], r["s3_key"], exc_info=True)
            failed += 1
            continue

        try:
            found = await asyncio.to_thread(embed_photo, raw)
        except Exception:
            log.warning("photo=%s: embedding failed", r["id"], exc_info=True)
            failed += 1
            continue

        if found is None:
            # Same rule as the capture path: an embedding of mostly-street would
            # pollute candidate search, so no row is better than a bad row.
            no_animal += 1
            continue

        vec, box = found
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO embeddings (id, photo_id, model, dim, vec_miew, bbox)
                VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb)
                ON CONFLICT (photo_id, model) DO UPDATE
                    SET vec_miew = EXCLUDED.vec_miew,
                        bbox = EXCLUDED.bbox,
                        created_at = now()
                """,
                uuid7(), r["id"], MODEL_NAME, EMBED_DIM,
                "[" + ",".join(f"{float(v):.7g}" for v in vec) + "]",
                json.dumps({"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]}),
            )
        embedded += 1
        touched.add(r["sighting_id"])

        if i % 25 == 0 or i == len(rows):
            rate = i / max(1e-6, time.time() - t0)
            left = (len(rows) - i) / max(1e-6, rate)
            log.info("  %d/%d  %.1f photo/s  ~%.0fs left", i, len(rows), rate, left)
        if args.sleep:
            await asyncio.sleep(args.sleep)

    log.info("embedded %d · no animal %d · failed %d · %d sighting(s) affected",
             embedded, no_animal, failed, len(touched))

    if args.resolve and touched:
        # Import here so a plain backfill does not require the matching module.
        from app.matching import resolve_sighting

        log.info("resolving %d sighting(s)", len(touched))
        counts: dict[str, int] = {}
        for sid in touched:
            async with pool.acquire() as conn:
                try:
                    outcome = await resolve_sighting(
                        conn, sid,
                        auto_merge_min=settings.reid_auto_merge_min,
                        propose_min=settings.reid_propose_min,
                        radius_m=settings.reid_radius_m,
                        max_candidates=settings.reid_max_candidates,
                        new_uuid=uuid7,
                        thin_evidence_frames=settings.reid_thin_evidence_frames,
                    )
                except Exception:
                    log.warning("sighting=%s: resolve failed", sid, exc_info=True)
                    continue
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        log.info("resolved: %s", counts or "nothing")

    await pool.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
