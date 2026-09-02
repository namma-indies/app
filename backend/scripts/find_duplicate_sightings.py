"""Report sightings that look like one capture stored twice.

Reads only. Writes nothing, deletes nothing, and is safe to run against
production at any time.

`POST /sighting` had no idempotency until migration 0009: the offline queue
deletes a queued item only after the response arrives, so a request that landed
but whose response was lost got posted again and became a second sighting. This
finds what that left behind.

WHAT COUNTS AS A DUPLICATE
--------------------------
Same observer, same `captured_at`, same image hash. All three, because none is
sufficient alone:

* Same image alone is not a duplicate. A person may log the same photo twice on
  purpose, and two frames of one clip share almost everything.
* Same `captured_at` alone is not either -- an imported photo carries the
  original EXIF timestamp, so two imports of one shoot legitimately share it.
* Same observer alone is obviously not.

`created_at` proximity is reported rather than required. A retry usually lands
seconds later, but an item that sat in the queue overnight lands hours later and
is no less a duplicate.

The output is deliberately a report. Deleting a sighting destroys a real
photograph and cannot be undone from here; merging the pair through the review
queue is the reversible route, and it is the same "are these the same dog?"
question a human already answers there.
"""

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402

SQL = """
SELECT s.observer_id,
       o.display_name,
       s.captured_at,
       p.phash,
       count(DISTINCT s.id)                        AS copies,
       array_agg(DISTINCT s.id::text)              AS sighting_ids,
       min(s.created_at)                           AS first_seen,
       max(s.created_at) - min(s.created_at)       AS spread,
       count(DISTINCT s.individual_id)
         FILTER (WHERE s.individual_id IS NOT NULL) AS identities,
       bool_or(s.client_token IS NOT NULL)          AS any_tokened
FROM sightings s
JOIN photos p     ON p.sighting_id = s.id
LEFT JOIN observers o ON o.id = s.observer_id
GROUP BY s.observer_id, o.display_name, s.captured_at, p.phash
HAVING count(DISTINCT s.id) > 1
ORDER BY count(DISTINCT s.id) DESC, min(s.created_at)
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=settings.database_url)
    args = ap.parse_args()

    conn = await asyncpg.connect(args.dsn)
    try:
        rows = await conn.fetch(SQL)
        total = await conn.fetchval("SELECT count(*) FROM sightings")
    finally:
        await conn.close()

    if not rows:
        print(f"no duplicate captures found across {total} sightings")
        return 0

    extra = sum(r["copies"] - 1 for r in rows)
    print(f"{len(rows)} duplicated capture(s) across {total} sightings "
          f"-- {extra} extra row(s)\n")
    for r in rows:
        who = r["display_name"] or str(r["observer_id"])[:8]
        print(f"  {r['copies']}x  {who}  captured {r['captured_at']:%Y-%m-%d %H:%M}")
        print(f"      first stored {r['first_seen']:%Y-%m-%d %H:%M:%S}, "
              f"copies {r['spread']} later")
        if r["identities"]:
            # Worth flagging loudly: a duplicate already merged into a dog means
            # the dog's sighting count is inflated, not just the table.
            print(f"      !! {r['identities']} of these already belong to an individual")
        for sid in r["sighting_ids"]:
            print(f"      {sid}")
        print()

    print("Nothing was changed. To resolve these, merge each pair through the")
    print("MATCHES tab -- it is the same question, and it is reversible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
