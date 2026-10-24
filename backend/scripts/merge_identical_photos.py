"""Hash existing photos, then link every sighting pair that shares one.

The identical-photo rule (`app.identical`) runs on every new resolution, but
the corpus it was written for already exists: on 2026-10-23, 32 of 33 pending
proposals were one photo attached to two sightings. Nothing hashes those
photos or revisits those pairs on its own, so until this runs they stay in the
review queue.

Two phases:

1. **Hash.** Every uploaded photo with no `content_sha256` is read back from S3
   and hashed. The stored object is exactly what the live path hashes (see
   migration 0016), so backfilled and live rows mean the same thing. Only
   uploaded photos -- not clip frames, not evidence crops -- the same set the
   live path writes.
2. **Link.** Every embedded sighting whose photo also appears in another
   sighting goes through `link_identical`, the function resolution calls, so
   the backfill cannot disagree with live behaviour. Pending proposals the
   links answer are deleted, the later copy of each photo is marked
   `duplicate_of` the earlier so it counts once, and known names are proposed
   for the dog -- all as part of it.

**Dry-run is the default** and writes nothing. It still does all the work --
reads S3, hashes, links -- inside a transaction it then rolls back, so the
numbers it reports are the numbers a real run would produce rather than an
estimate computed by a second, different query.

Usage, from /app/backend inside the container:

    uv run python scripts/merge_identical_photos.py            # dry run
    uv run python scripts/merge_identical_photos.py --apply

Safe to repeat: hashing skips hashed photos, and a sighting already linked is
never written again. The S3 reads happen before the transaction opens, so the
matching lock is held only for the database work, not for the downloads --
live uploads resolve normally while it reads.
"""

import argparse
import asyncio
import hashlib
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import effective_dsn  # noqa: E402
from app.embed import MODEL_NAME  # noqa: E402
from app.ids import uuid7  # noqa: E402
from app.storage.s3 import storage_from_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("identical")

# Photos a contributor chose: /sighting photos that are not clip frames, and
# /capture sources of photo captures. Must agree with the live write sites in
# routes/sighting.py and routes/capture.py, or backfilled rows would cover
# photos live rows never do. `attrs.source = 'video'` has marked every clip
# sighting since video capture shipped.
UNHASHED_SQL = """
    SELECT p.id, p.s3_key
    FROM photos p
    LEFT JOIN sightings s ON s.id = p.sighting_id
    LEFT JOIN captures c ON c.id = p.capture_id
    WHERE p.content_sha256 IS NULL
      AND NOT EXISTS (SELECT 1 FROM animal_instances a WHERE a.evidence_photo_id = p.id)
      AND (
            (p.sighting_id IS NOT NULL AND p.capture_id IS NULL
             AND COALESCE(s.attrs->>'source', '') <> 'video')
         OR (p.sighting_id IS NULL AND c.kind = 'photo')
      )
    ORDER BY p.created_at, p.id
"""

# Sightings carrying a photo that some other sighting also carries, and that
# resolution would have run on -- i.e. embedded. Oldest first, so the earlier
# sighting is the one recorded as resolving and the later as its match.
DUPLICATED_SQL = """
    WITH ev AS (
        SELECT p.sighting_id, p.content_sha256 FROM photos p
        WHERE p.sighting_id IS NOT NULL AND p.content_sha256 IS NOT NULL
        UNION
        SELECT a.sighting_id, src.content_sha256 FROM animal_instances a
        JOIN photos src ON src.id = a.source_photo_id
        WHERE a.sighting_id IS NOT NULL AND src.content_sha256 IS NOT NULL
    ),
    dup AS (
        SELECT content_sha256 FROM ev
        GROUP BY content_sha256 HAVING count(DISTINCT sighting_id) > 1
    )
    SELECT s.id
    FROM sightings s
    WHERE s.id IN (SELECT ev.sighting_id FROM ev JOIN dup USING (content_sha256))
      AND (s.vec_miew IS NOT NULL OR EXISTS (
            SELECT 1 FROM photos p JOIN embeddings e ON e.photo_id = p.id
            WHERE p.sighting_id = s.id AND e.model = $1 AND e.vec_miew IS NOT NULL))
    ORDER BY s.created_at, s.id
"""

PENDING_SQL = "SELECT count(*) FROM match_proposals WHERE status = 'pending'"


@dataclass
class Report:
    to_hash: int = 0
    hashed: int = 0
    unreadable: int = 0
    duplicated_sightings: int = 0
    sightings_linked: int = 0
    individuals_minted: int = 0
    links_to_existing: int = 0
    proposals_cleared: int = 0
    duplicates_marked: int = 0
    pending_before: int = 0
    pending_after: int = 0
    skipped: Counter = field(default_factory=Counter)


class _DryRun(Exception):
    """Raised inside the transaction to roll it back after reporting."""


async def run(pool: asyncpg.Pool, storage, *, apply: bool) -> Report:
    from app.identical import link_identical
    from app.matching import lock_matching

    rep = Report()
    async with pool.acquire() as conn:
        rows = await conn.fetch(UNHASHED_SQL)
    rep.to_hash = len(rows)
    log.info("%d uploaded photo(s) have no content hash", rep.to_hash)

    hashes: list[tuple] = []
    for i, r in enumerate(rows, 1):
        try:
            data = await storage.get(r["s3_key"])
        except Exception:
            # A missing object is a data problem to look at, not a reason to
            # leave every other photo unhashed.
            log.warning("photo=%s: could not read %s", r["id"], r["s3_key"], exc_info=True)
            rep.unreadable += 1
            continue
        hashes.append((r["id"], hashlib.sha256(data).hexdigest()))
        if i % 100 == 0:
            log.info("  read %d/%d", i, len(rows))
    rep.hashed = len(hashes)

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await lock_matching(conn)
                rep.pending_before = await conn.fetchval(PENDING_SQL)
                if hashes:
                    # `IS NULL` again: a live upload may have hashed a row since
                    # it was read above, and its value is the authoritative one.
                    await conn.execute(
                        """
                        UPDATE photos p SET content_sha256 = h.sha
                        FROM unnest($1::uuid[], $2::text[]) AS h(id, sha)
                        WHERE p.id = h.id AND p.content_sha256 IS NULL
                        """,
                        [h[0] for h in hashes], [h[1] for h in hashes],
                    )
                sids = [r["id"] for r in await conn.fetch(DUPLICATED_SQL, MODEL_NAME)]
                rep.duplicated_sightings = len(sids)
                for sid in sids:
                    out = await link_identical(conn, sid, new_uuid=uuid7)
                    if out is None:
                        continue
                    if out.status == "skipped":
                        rep.skipped[out.reason] += 1
                        continue
                    rep.proposals_cleared += out.proposals_cleared
                    rep.duplicates_marked += len(out.duplicates_marked)
                    if out.newly_linked:
                        rep.sightings_linked += len(out.newly_linked)
                        if out.minted:
                            rep.individuals_minted += 1
                        else:
                            rep.links_to_existing += 1
                rep.pending_after = await conn.fetchval(PENDING_SQL)
                if not apply:
                    raise _DryRun
        except _DryRun:
            pass
    return rep


def describe(rep: Report, *, apply: bool) -> str:
    verb = "" if apply else "would be "
    skipped = ", ".join(f"{k} {v}" for k, v in sorted(rep.skipped.items())) or "none"
    return "\n".join([
        f"{'APPLIED' if apply else 'DRY RUN -- nothing written'}",
        f"photos hashed: {rep.hashed} of {rep.to_hash} ({rep.unreadable} unreadable)",
        f"sightings sharing a photo with another: {rep.duplicated_sightings}",
        f"sightings {verb}linked: {rep.sightings_linked} "
        f"({rep.individuals_minted} new dog(s), {rep.links_to_existing} onto an existing one)",
        f"copies {verb}hidden from /map, /dogs and /stats (kept in /dex): "
        f"{rep.duplicates_marked}",
        f"skipped, left to a person: {skipped}",
        f"pending proposals {verb}cleared: {rep.proposals_cleared} "
        f"({rep.pending_before} -> {rep.pending_after})",
    ])


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the hashes and links; without it nothing is written")
    args = ap.parse_args()

    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    try:
        rep = await run(pool, storage_from_settings(), apply=args.apply)
    finally:
        await pool.close()
    print(describe(rep, apply=args.apply))
    return 0 if rep.unreadable == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
