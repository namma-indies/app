"""Erase again what a database restore brought back.

Backups are whole-database dumps (scripts/backup_db.py). Restoring one taken
before someone deleted a photo brings that photo's rows back. With
`ERASURE_LEDGER_ENABLED` on, every erasure also writes an id-only record to
`erasures/YYYY-MM-DD/{kind}-{id}.json` in the media bucket (app/erasure.py),
which a restore does not touch. Run this after a restore, with --since set to
when the restored backup was taken; it erases each recorded item again.

**Dry-run is the default**: it lists how many are recorded since then and how
many of those exist in the database again.

Usage, from /app/backend inside the container:

    uv run python scripts/replay_erasures.py --since 2026-11-01T03:00:00+05:30
    uv run python scripts/replay_erasures.py --since 2026-11-01T03:00:00+05:30 --apply

Safe to repeat: an item already gone is skipped. The media it owes go through
media_purges as any erasure's do (scripts/purge_media.py).
"""

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import effective_dsn  # noqa: E402

PREFIX = "erasures/"
_TABLES = {"capture": "captures", "sighting": "sightings"}


async def recorded_since(storage, since: datetime) -> list[dict]:
    """Every ledger entry at or after `since`, oldest first."""
    entries = []
    for key in await storage.list_keys(PREFIX):
        day = key[len(PREFIX):].split("/", 1)[0]
        try:
            if date.fromisoformat(day) < since.astimezone(timezone.utc).date():
                continue
        except ValueError:
            continue
        entry = json.loads(await storage.get(key))
        entry["at"] = datetime.fromisoformat(entry["at"])
        if entry["at"] >= since and entry["kind"] in _TABLES:
            entries.append(entry)
    return sorted(entries, key=lambda e: (e["at"], e["id"]))


async def run(pool, storage, *, since: datetime, apply: bool) -> tuple[int, int, int]:
    """(recorded since, present again, erased)."""
    from app.erasure import erase
    entries = await recorded_since(storage, since)
    present = []
    async with pool.acquire() as conn:
        for e in entries:
            if await conn.fetchval(f"SELECT EXISTS(SELECT 1 FROM {_TABLES[e['kind']]} WHERE id=$1)", UUID(e["id"])):
                present.append(e)
    erased = 0
    if apply:
        for e in present:
            # ledger=False: it is on the ledger already.
            if await erase(pool, storage, e["kind"], UUID(e["id"]), reason="replayed_after_restore", ledger=False):
                erased += 1
    return len(entries), len(present), erased


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, type=datetime.fromisoformat,
                    help="when the restored backup was taken (ISO 8601, with a timezone)")
    ap.add_argument("--apply", action="store_true", help="erase them; without it nothing is touched")
    args = ap.parse_args()
    if args.since.tzinfo is None:
        ap.error("--since needs a timezone, e.g. 2026-11-01T03:00:00+05:30")
    from app.deps import get_storage
    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    try:
        recorded, present, erased = await run(pool, get_storage(), since=args.since, apply=args.apply)
        from app.media_purges import settled
        await settled()
    finally:
        await pool.close()
    head = "APPLIED" if args.apply else "DRY RUN -- nothing erased"
    print(f"{head}\nrecorded since {args.since.isoformat()}: {recorded}\n"
          f"back in the database: {present}\nerased again: {erased}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
