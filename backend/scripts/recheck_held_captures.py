"""Publish uploads held for review under the old rule that today's rule would
not hold.

PR #87 narrowed review to two animals of a species in one frame that then
reappear elsewhere. Uploads already waiting stayed waiting: on 2026-10-24 a clip
with one dog in each of 48 frames had been asking its contributor to "separate
the animals" for a month. Each held capture goes through
`captures.recheck_held`, which reads only the stored detections -- no GPU pass.
A capture that is still ambiguous, or that someone has started sorting, is
left as it is.

**Dry-run is the default** and writes nothing: it does the work inside a
transaction it then rolls back, so its numbers are the real run's.

Usage, from /app/backend inside the container:

    uv run python scripts/recheck_held_captures.py            # dry run
    uv run python scripts/recheck_held_captures.py --apply

Safe to repeat: a published capture is no longer `needs_review`.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import effective_dsn  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("recheck")


@dataclass
class Report:
    held: int = 0
    published: int = 0
    sightings: int = 0
    still_held: int = 0


class _DryRun(Exception):
    """Raised inside the transaction to roll it back after reporting."""


async def run(pool: asyncpg.Pool, *, apply: bool) -> Report:
    from app.captures import recheck_held
    from app.matching import lock_matching

    rep = Report()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await lock_matching(conn)
                captures = await conn.fetch(
                    "SELECT * FROM captures WHERE processing_state='needs_review' "
                    "ORDER BY created_at, id FOR UPDATE")
                rep.held = len(captures)
                for capture in captures:
                    sids = await recheck_held(conn, capture)
                    if sids is None:
                        rep.still_held += 1
                        continue
                    rep.published += 1
                    rep.sightings += len(sids)
                    log.info("capture=%s: published %d animal(s)", capture["id"], len(sids))
                if not apply:
                    raise _DryRun
        except _DryRun:
            pass
    return rep


def describe(rep: Report, *, apply: bool) -> str:
    verb = "" if apply else "would be "
    return "\n".join([
        f"{'APPLIED' if apply else 'DRY RUN -- nothing written'}",
        f"uploads held for review: {rep.held}",
        f"{verb}published: {rep.published} ({rep.sightings} animal sighting(s))",
        f"still need a person: {rep.still_held}",
    ])


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="publish; without it nothing is written")
    args = ap.parse_args()
    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    try:
        rep = await run(pool, apply=args.apply)
    finally:
        await pool.close()
    print(describe(rep, apply=args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
