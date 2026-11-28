"""Telling someone their sighting has been recognised.

"Every sighting starts anonymous and points at an individual slot that may
stay empty for months" (build-foundations.md). The app has filled those slots
since the first confirmed match and never said so: a sighting from September
quietly became No. 047 and its observer found out, if at all, by scrolling a
catalogue. This is the moment it tells them, with the two photographs that
were judged to be one animal side by side.

WHAT COUNTS
-----------
One of *your* sightings got its individual since you last looked
(`sightings.identified_at`, kept by a trigger in migration 0050), and there is
another sighting of that animal you are allowed to see. Without the second
photograph there is nothing to show and nothing has really been recognised:
the same picture uploaded twice mints an individual too (app/identical.py),
and announcing that would be the app congratulating you on a duplicate.

Not announced: someone else meeting an animal you already know. That is a
statement about where another person was and when, and a feed of them is a
step toward the live tracking this project has ruled out.

WHAT IS SHOWN
-------------
Your newly recognised sighting, and the other sighting of the same animal
furthest from it in time -- the pair that says most about how long this
animal has been known. Nothing here carries a place. The other photograph is
one `/dogs` already shows this viewer on the animal's card, under the same
`countable_sighting` and `visible_to` rules, and its observer is named exactly
as the card and the map name them.
"""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Response

from app.aggregates import countable_sighting, visible_to
from app.auth.deps import require_observer
from app.deps import get_conn, get_storage
from app.photos import thumb_key
from app.storage.s3 import S3Storage

router = APIRouter()

# Someone who has never been shown any gets the recent ones, not three months
# of history at once on the day this ships.
FIRST_WINDOW_DAYS = 30
# One at a time on screen; past a handful it is a queue to clear, not a moment.
MAX_SHOWN = 5


@router.get("/recognised")
async def get_recognised(
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    mark = await conn.fetchrow(
        "SELECT now() AS as_of, COALESCE(recognitions_seen_at, "
        "now() - make_interval(days => $2)) AS since FROM observers WHERE id = $1",
        observer_id, FIRST_WINDOW_DAYS)
    fresh = await conn.fetch(
        f"""
        SELECT s.id, s.individual_id, s.identified_at
        FROM sightings s
        JOIN individuals i ON i.id = s.individual_id
        WHERE s.observer_id = $1 AND s.identified_at > $2
          AND i.merged_into IS NULL AND i.status IS DISTINCT FROM 'merged'
          AND {countable_sighting()}
        """,
        observer_id, mark["since"])
    if not fresh:
        return {"as_of": mark["as_of"], "recognised": [], "more": 0}

    newly: dict[UUID, set[UUID]] = {}
    recognised_at: dict[UUID, datetime] = {}
    for r in fresh:
        newly.setdefault(r["individual_id"], set()).add(r["id"])
        recognised_at[r["individual_id"]] = max(
            recognised_at.get(r["individual_id"], r["identified_at"]), r["identified_at"])

    rows = await conn.fetch(
        f"""
        SELECT s.id, s.individual_id, s.captured_at, s.observer_id, s.species,
               o.display_name, i.number, i.name,
               (SELECT p.s3_key FROM photos p WHERE p.sighting_id = s.id
                 ORDER BY (p.id IS NOT DISTINCT FROM s.cover_photo_id) DESC, p.created_at, p.id
                 LIMIT 1) AS s3_key
        FROM sightings s
        JOIN individuals i ON i.id = s.individual_id
        JOIN observers o ON o.id = s.observer_id
        WHERE s.individual_id = ANY($1::uuid[]) AND {countable_sighting()} AND {visible_to('$2')}
        ORDER BY s.captured_at, s.id
        """,
        list(newly), observer_id)
    known: dict[UUID, list] = {}
    for r in rows:
        known.setdefault(r["individual_id"], []).append(r)

    cards = []
    for individual_id, sightings in known.items():
        mine_new = [s for s in sightings if s["id"] in newly[individual_id]]
        if not mine_new:
            continue
        yours = mine_new[-1]  # the latest of them, by when the animal was seen
        others = [s for s in sightings if s["id"] != yours["id"]]
        if not others:
            continue
        # Furthest in time; on a tie the earlier one, so the answer is stable.
        other = max(others, key=lambda s: abs((s["captured_at"] - yours["captured_at"]).total_seconds()))
        species = {s["species"] for s in sightings}
        cards.append({
            "individual": {
                "id": str(individual_id),
                "number": yours["number"],
                "name": yours["name"],
                "species": species.pop() if len(species) == 1 else None,
            },
            "recognised_at": recognised_at[individual_id],
            "yours": {"id": str(yours["id"]), "captured_at": yours["captured_at"], "_key": yours["s3_key"]},
            "other": {
                "captured_at": other["captured_at"],
                "mine": other["observer_id"] == observer_id,
                # Named as the card and the map name them; never for your own.
                "observer": None if other["observer_id"] == observer_id else other["display_name"],
                "_key": other["s3_key"],
            },
            "met_count": sum(1 for s in sightings if s["observer_id"] == observer_id),
            "sighting_count": len(sightings),
            "observers": sorted({s["display_name"] for s in sightings
                                 if s["observer_id"] != observer_id and s["display_name"]}),
        })

    cards.sort(key=lambda c: c["recognised_at"], reverse=True)
    shown = cards[:MAX_SHOWN]
    sides = [side for c in shown for side in (c["yours"], c["other"])]
    with_photo = [side for side in sides if side["_key"]]
    urls = await storage.urls([thumb_key(side["_key"]) for side in with_photo])
    for side in sides:
        side["thumb_url"] = None
    for side, url in zip(with_photo, urls):
        side["thumb_url"] = url
    for side in sides:
        del side["_key"]
    return {"as_of": mark["as_of"], "recognised": shown, "more": len(cards) - len(shown)}


@router.post("/recognised/seen", status_code=204)
async def mark_seen(
    as_of: datetime = Form(...),
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
):
    """Read up to `as_of`, the time the list was fetched -- not "now", or a
    recognition that landed while the card was on screen would be marked read
    without ever having been shown. Only ever moves forward, never past now."""
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    await conn.execute(
        "UPDATE observers SET recognitions_seen_at = LEAST(now(), GREATEST("
        "COALESCE(recognitions_seen_at, '-infinity'::timestamptz), $2::timestamptz)) WHERE id = $1",
        observer_id, as_of)
    return Response(status_code=204)
