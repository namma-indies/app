"""Queue today's model pass over voice transcripts made before it (#176).

Transcripts from before #153 have no English line, and some never got
suggestions (the pass was skipped, or failed in the #108 era). This queues
them for the DGX worker (app/transcript_backfill.py), in one of two modes:

- full: the suggestion pass never completed (skipped or failed) and the
  transcript has no suggestions: English (if not English) and suggestions;
- translate: any other transcript not in English and with no English line:
  English only.

A transcript already being read, already queued, or in English with its
pass done is left alone. Suggestions show only for details still blank, and
nothing in a field changes until its observer taps.

The worker takes them last of all, at TRANSCRIPT_BACKFILL_PER_HOUR (0 by
default: queueing alone reads nothing). **Dry-run is the default**: it lists
how many would be queued in each mode, and their ids; never the words.

Usage, from /app/backend inside the container. Running it against production
data is Aswin's call; nothing runs it automatically:

    uv run python scripts/transcript_backfill.py                    # dry run
    uv run python scripts/transcript_backfill.py --apply --limit 200

then set TRANSCRIPT_BACKFILL_PER_HOUR (say 30) and restart the API; set it
back to 0 to pause. Safe to repeat: a transcript already queued is not
queued again, and one transcribed again since (redo, retranscribe_misdetected)
takes the normal path instead, its queue entry gone with its old transcript.
"""

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import effective_dsn  # noqa: E402
from app.transcription_jobs import PIPELINE_VERSION  # noqa: E402

FOUND = """
    SELECT t.id, CASE WHEN t.suggest_status IN ('skipped', 'failed')
                       AND NOT EXISTS (SELECT 1 FROM detail_suggestions d WHERE d.transcript_id = t.id)
                      THEN 'full' ELSE 'translate' END AS mode
    FROM voice_transcripts t
    WHERE t.pipeline_version = $1 AND t.outcome = 'transcribed'
      AND t.suggest_status NOT IN ('pending', 'running')
      AND NOT EXISTS (SELECT 1 FROM transcript_backfill b WHERE b.transcript_id = t.id)
      AND ((t.suggest_status IN ('skipped', 'failed')
            AND NOT EXISTS (SELECT 1 FROM detail_suggestions d WHERE d.transcript_id = t.id))
           OR (t.language <> 'en' AND t.english IS NULL AND char_length(t.text) <= $2))
    ORDER BY t.created_at, t.id
    LIMIT $3
"""


async def run(pool: asyncpg.Pool, *, apply: bool, limit: int | None = None) -> list[dict]:
    """What is (or would be) queued: [{"id", "mode"}], oldest first."""
    from app.transcript_backfill import enqueue

    async with pool.acquire() as conn:
        found = [dict(r) for r in await conn.fetch(
            FOUND, PIPELINE_VERSION, settings.suggest_translate_max_chars, limit)]
        if apply:
            async with conn.transaction():
                for row in found:
                    await enqueue(conn, row["id"], row["mode"])
    return found


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="queue them; without it nothing is written")
    ap.add_argument("--limit", type=int, default=None, help="at most this many, oldest first")
    args = ap.parse_args()
    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    try:
        found = await run(pool, apply=args.apply, limit=args.limit)
    finally:
        await pool.close()
    full = [r for r in found if r["mode"] == "full"]
    print(f"{'APPLIED' if args.apply else 'DRY RUN -- nothing written'}\n"
          f"transcripts {'queued' if args.apply else 'that would be queued'}: {len(found)} "
          f"(full: {len(full)}, translate only: {len(found) - len(full)})")
    for row in found:
        print(f"  {row['id']}  {row['mode']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
