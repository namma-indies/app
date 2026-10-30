"""Delete object-store media the app has let go of but could not delete itself.

When an observer deletes a voice note they added later (#158), its rows go at
once and the API then deletes the audio from the store. If the store refuses
(the API's credentials may lack s3:DeleteObject) or the API stops before
asking, the key stays in `media_purges`. This deletes those objects, using
whatever S3 credentials it is run with, and clears each key the store confirms.

**Dry-run is the default** and only lists how many are owed and the oldest.

Usage, from /app/backend inside the container, with S3_ACCESS_KEY /
S3_SECRET_KEY set to credentials allowed s3:DeleteObject on the media bucket
(on the `sightings/*/notes/*` and `captures/*/notes/*` prefixes at least):

    uv run python scripts/purge_media.py            # dry run: what is owed
    uv run python scripts/purge_media.py --apply

Safe to repeat: deleting an object already gone succeeds, and a key that
fails again is kept with its error for the next run.
"""

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import effective_dsn  # noqa: E402


async def run(pool: asyncpg.Pool, storage, *, apply: bool) -> tuple[int, int, int]:
    """(owed, purged, failed). A key a live note points at again is let go of
    without deleting anything (app/media_purges.py)."""
    from app.media_purges import purge

    async with pool.acquire() as conn:
        keys = [r["s3_key"] for r in await conn.fetch("SELECT s3_key FROM media_purges ORDER BY created_at, s3_key")]
    if not apply:
        return len(keys), 0, 0
    purged = failed = 0
    for key in keys:
        if await purge(pool, storage, key):
            purged += 1
        else:
            failed += 1
            print(f"could not delete {key}")
    return len(keys), purged, failed


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="delete them; without it nothing is touched")
    args = ap.parse_args()
    from app.deps import get_storage
    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    try:
        owed, purged, failed = await run(pool, get_storage(), apply=args.apply)
    finally:
        await pool.close()
    if args.apply:
        print(f"APPLIED\nowed: {owed}\ndeleted: {purged}\nstill owed (failed): {failed}")
    else:
        print(f"DRY RUN -- nothing deleted\nobjects owed a delete: {owed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
