import json
from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth.deps import require_observer
from app.deps import get_conn, get_storage
from app.photos import thumb_key
from app.storage.s3 import S3Storage

router = APIRouter()


@router.get("/dex")
async def get_dex(
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    rows = await conn.fetch(
        """
        SELECT
            s.id AS sighting_id,
            s.captured_at,
            ST_Y(s.geog::geometry) AS lat,
            ST_X(s.geog::geometry) AS lng,
            s.geo_accuracy_m,
            s.attrs,
            s.review_status,
            p.id AS photo_id,
            p.s3_key
        FROM sightings s
        LEFT JOIN photos p ON p.sighting_id = s.id
        WHERE s.observer_id = $1
        -- p.id breaks the tie: photos of one sighting share created_at exactly
        -- (single transaction, constant now()), so without it the order of a
        -- sighting's photos is arbitrary and can differ between requests.
        ORDER BY s.captured_at DESC, p.created_at ASC, p.id ASC
        """,
        observer_id,
    )

    sightings: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        sid = str(row["sighting_id"])
        if sid not in sightings:
            raw_attrs = row["attrs"]
            attrs = json.loads(raw_attrs) if isinstance(raw_attrs, str) else (raw_attrs or {})
            sightings[sid] = {
                "id": sid,
                "captured_at": row["captured_at"],
                "lat": row["lat"],
                "lng": row["lng"],
                "geo_accuracy_m": row["geo_accuracy_m"],
                "attrs": attrs,
                # Your own sightings stay in your own dex whatever their status
                # -- but you should be told when one has been taken off the
                # shared map, rather than wondering why nobody can see it.
                "review_status": row["review_status"],
                "photos": [],
            }
            order.append(sid)
        if row["photo_id"] is not None:
            # Keys now, URLs in one batch below -- `storage.url` opens an
            # aioboto3 client per call (2.86 ms) where a shared one costs
            # 0.20 ms, and this loop presigns two per photo.
            sightings[sid]["photos"].append({"_key": row["s3_key"]})

    pending = [
        photo for sighting in sightings.values() for photo in sighting["photos"]
    ]
    signed = await storage.urls(
        [k for photo in pending for k in (photo["_key"], thumb_key(photo["_key"]))]
    )
    for photo, (url, thumb_url) in zip(pending, zip(signed[::2], signed[1::2])):
        del photo["_key"]
        photo["url"] = url
        photo["thumb_url"] = thumb_url

    return {"sightings": [sightings[sid] for sid in order]}
