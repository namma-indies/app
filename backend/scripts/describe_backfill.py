"""Queue AI photo descriptions for sightings logged before they existed (#160).

New sightings with no note are queued as they are logged (app/describe.py).
This queues the older ones the same way, marked `backfill`: the worker takes
them only after every voice note and new sighting, and at most
DESCRIBE_BACKFILL_PER_HOUR an hour. That setting is 0 by default, so queueing
alone describes nothing until it is raised.

A sighting qualifies if it has photos, finished processing with an animal,
and has no typed note, no voice note (at upload or added later) and no
speech heard in its clip: exactly the rule new sightings follow, checked
again when the worker asks.

**Dry-run is the default** and writes nothing: it queues inside a transaction
it then rolls back, so its count is the real run's.

Usage, from /app/backend inside the container. Running it against production
data is Aswin's call; nothing runs it automatically:

    uv run python scripts/describe_backfill.py                       # dry run: how many would queue
    uv run python scripts/describe_backfill.py --apply --limit 200   # queue the 200 newest
    uv run python scripts/describe_backfill.py --apply --since 2026-09-01

then set DESCRIBE_BACKFILL_PER_HOUR (say 30) with DESCRIBE_ENABLED=true and
restart the API; set it back to 0 to pause. Safe to repeat: a sighting
already queued is left as it is.
"""

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import effective_dsn  # noqa: E402

ELIGIBLE = """
    SELECT s.id FROM sightings s JOIN captures c ON c.id = s.capture_id
    WHERE s.processing_state IN ('legacy', 'ready')
      AND ($1::timestamptz IS NULL OR s.created_at >= $1)
      AND btrim(coalesce(s.attrs->>'note', '')) = ''
      AND s.audio_s3_key IS NULL AND c.audio_s3_key IS NULL
      AND EXISTS (SELECT 1 FROM photos p WHERE p.sighting_id = s.id)
      AND NOT EXISTS (SELECT 1 FROM photo_descriptions d WHERE d.sighting_id = s.id)
      -- A note about this animal, or the whole capture; not one about another animal of it.
      AND NOT EXISTS (SELECT 1 FROM voice_notes n WHERE n.capture_id = s.capture_id
                      AND (n.sighting_id IS NULL OR n.sighting_id = s.id))
      AND NOT EXISTS (SELECT 1 FROM voice_transcripts t LEFT JOIN voice_notes n ON n.id = t.note_id
                      WHERE t.capture_id = s.capture_id AND t.outcome = 'transcribed'
                        AND (n.id IS NULL OR n.sighting_id IS NULL OR n.sighting_id = s.id))
    ORDER BY s.created_at DESC, s.id
    LIMIT $2
"""


class _DryRun(Exception):
    """Raised inside the transaction to roll it back after counting."""


async def run(pool: asyncpg.Pool, *, apply: bool, limit: int | None = None,
              since: datetime | None = None) -> int:
    from app.describe import enqueue

    queued = 0
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                for row in await conn.fetch(ELIGIBLE, since, limit):
                    await enqueue(conn, row["id"], backfill=True)
                    queued += 1
                if not apply:
                    raise _DryRun
        except _DryRun:
            pass
    return queued


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="queue them; without it nothing is written")
    ap.add_argument("--limit", type=int, default=None, help="at most this many, newest first")
    ap.add_argument("--since", type=date.fromisoformat, default=None,
                    help="only sightings logged on or after this date (YYYY-MM-DD)")
    args = ap.parse_args()
    since = datetime.combine(args.since, datetime.min.time(), timezone.utc) if args.since else None
    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    try:
        queued = await run(pool, apply=args.apply, limit=args.limit, since=since)
    finally:
        await pool.close()
    print(f"{'APPLIED' if args.apply else 'DRY RUN -- nothing written'}\n"
          f"sightings {'queued' if args.apply else 'that would be queued'} for a description: {queued}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
