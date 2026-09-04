"""The catalogue of identified individuals -- dogs, as opposed to sightings.

A sighting is "someone saw a dog at a time and place". An individual is "this
is the same animal as that one", which only exists once a human has confirmed a
match. `individuals` has carried that since the first migration and
`match.py` writes to it on every confirmed verdict, but nothing has ever read
it back, so the identities the app produces have been invisible to the people
producing them.

Two things this deliberately does not do:

* It does not name dogs. `individuals.name` exists and nothing sets it, because
  naming needs rules first -- who may name, whether a name can change, and what
  happens when two feeders call one animal different things. The schema is
  ready (`named_by`, `named_at`); the policy is not. Cards show an identity
  number until then, which is honest: the identity is the re-ID grouping, not
  the label.
* It does not publish precise territory to everyone. See below.

LOCATION
--------
Full precision, matching `/map`. Both are gated on `require_observer`, and the
cohort is passcode- and allowlist-gated, so this shows vetted testers what they
could already see on the map -- it adds no reach.

It does not coarsen, deliberately. Issue #5 settled that a public surface
resolves an individual to a named `area` polygon and never to a jittered point,
because a fuzzed point leaks under aggregation while an area label cannot. That
work is not built yet, and a dog card is exactly the artefact it exists for: a
named animal with a last-known location. So when `resolve_precision` lands,
this endpoint needs it at the same time `/map` does -- the two should coarsen
together or the stricter one is pointless.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth.deps import require_observer
from app.config import settings
from app.deps import get_conn, get_storage
from app.embed import EMBED_DIM, MODEL_NAME
from app.photos import thumb_key
from app.storage.s3 import S3Storage

router = APIRouter()

MAX_DOGS = 500
PHOTOS_PER_DOG = 4

# How many look-alikes to offer per dog. Three is a review queue; twenty is a
# wall of thumbnails nobody reads.
LOOKALIKES_PER_DOG = 3
# Neighbours the ANN stage pulls per photo before exact re-ranking, mirroring
# matching.ANN_SHORTLIST. Cheap, and protects against fp16 mis-ranking.
ANN_PROBE = 20
_TAG_KEYS = ("sex", "ear_notch", "condition")


async def _look_alikes(conn, ids: list[UUID]) -> dict[UUID, list[dict]]:
    """Nearest visual neighbours for each individual, ranked -- not grouped.

    WHY RANKED AND NOT THRESHOLDED
    ------------------------------
    The obvious version of this feature buckets dogs by a similarity cut-off:
    everything above X is "probably the same dog". The measured data says no
    such X exists on this population. From the run recorded in config.py: the
    best score between two sightings of the *same* dog was 0.5532, while two
    different dogs that merely resemble each other scored 0.7122 -- and that
    pair was photographed seven days apart, so it is a stable resemblance, not
    a lighting artefact. Look-alikes outscore genuine matches. Any threshold
    either admits mostly wrong pairs or admits nothing:

        cut-off   pairs surfaced   correct   precision
          0.40          75            18       24.0%
          0.50          22             8       36.4%
          0.71           2             0        0.0%

    Ranking survives what thresholding does not. Across a JPEG quality sweep
    d' collapsed 4.58 -> 3.34 while top-1 barely moved: the *order* is robust
    even when the absolute scores drift. So this returns an ordered shortlist
    with its scores exposed, and leaves "same dog?" to the human -- the same
    line matching.py draws, for the same reason.

    Similarity between two dogs is the MAX over their photo pairs, not a
    centroid. A centroid of a dog photographed from both sides is a blur of
    neither, and averaging exactly destroys the one good angle that would have
    matched.
    """
    if not ids:
        return {}
    sql = f"""
        WITH dog_vecs AS (
            SELECT s.individual_id, e.vec_miew
            FROM sightings s
            JOIN photos p ON p.sighting_id = s.id
            JOIN embeddings e ON e.photo_id = p.id
            WHERE s.individual_id = ANY($1::uuid[])
              AND e.model = $2 AND e.vec_miew IS NOT NULL
        )
        SELECT dv.individual_id AS dog_id,
               n.individual_id  AS other_id,
               MAX(1 - (dv.vec_miew <=> n.vec_miew)) AS similarity
        FROM dog_vecs dv
        CROSS JOIN LATERAL (
            SELECT s2.individual_id, e2.vec_miew
            FROM embeddings e2
            JOIN photos p2 ON p2.id = e2.photo_id
            JOIN sightings s2 ON s2.id = p2.sighting_id
            WHERE e2.model = $2
              AND e2.vec_miew IS NOT NULL
              AND s2.individual_id IS NOT NULL
              AND s2.individual_id <> dv.individual_id
            -- Identical cast to ix_embeddings_vec_miew_hnsw, or this silently
            -- becomes a sequential scan over every embedding in the database.
            ORDER BY e2.vec_miew::halfvec({EMBED_DIM}) <=> dv.vec_miew::halfvec({EMBED_DIM})
            LIMIT {ANN_PROBE}
        ) n
        GROUP BY dv.individual_id, n.individual_id
        ORDER BY dog_id, similarity DESC
    """
    # The lateral filters to *matched* sightings after the index walk, which is
    # the case iterative_scan exists for -- without it a dog whose neighbours
    # are all still unmatched can come back empty.
    async with conn.transaction():
        await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        rows = await conn.fetch(sql, ids, MODEL_NAME)

    out: dict[UUID, list[dict]] = {}
    for r in rows:
        bucket = out.setdefault(r["dog_id"], [])
        if len(bucket) < LOOKALIKES_PER_DOG:
            bucket.append(
                {"id": str(r["other_id"]), "similarity": round(r["similarity"], 4)}
            )
    return out


@router.get("/dogs")
async def get_dogs(
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    dogs = await conn.fetch(
        """
        SELECT
            i.id,
            i.name,
            min(s.captured_at) AS first_seen,
            max(s.captured_at) AS last_seen,
            count(*) AS sighting_count,
            count(DISTINCT s.observer_id) AS observer_count,
            bool_or(s.observer_id = $1) AS seen_by_me,
            -- Who logged it, excluding the viewer. `/map` names the observer on
            -- every sighting that is not your own, so a dog card that only ever
            -- said "4 PEOPLE" would be strictly less informative than tapping
            -- any one of that dog's pins. Same rule, same phrasing.
            array_remove(array_agg(DISTINCT o.display_name)
                         FILTER (WHERE s.observer_id <> $1), NULL) AS observers
        FROM individuals i
        JOIN sightings s ON s.individual_id = i.id
        JOIN observers o ON o.id = s.observer_id
        -- A merged individual is a duplicate that lost; its sightings already
        -- hang off the survivor, so listing it would show the same dog twice.
        WHERE i.merged_into IS NULL
          AND i.status IS DISTINCT FROM 'merged'
          AND s.review_status = 'valid'
        GROUP BY i.id, i.name
        ORDER BY max(s.captured_at) DESC
        LIMIT $2
        """,
        observer_id,
        MAX_DOGS,
    )
    if not dogs:
        return {"dogs": []}

    ids = [d["id"] for d in dogs]

    # Most recent photos per dog, newest first. row_number rather than a
    # LATERAL per row: one pass over the set beats 500 correlated subqueries.
    photo_rows = await conn.fetch(
        """
        SELECT individual_id, s3_key FROM (
            SELECT s.individual_id, p.s3_key,
                   -- p.id tiebreaks: photos of one sighting share created_at
                   -- exactly, so without it *which* photos appear on a card is
                   -- arbitrary and can change between loads.
                   row_number() OVER (PARTITION BY s.individual_id
                                      ORDER BY s.captured_at DESC,
                                               p.created_at, p.id) AS rn
            FROM sightings s
            JOIN photos p ON p.sighting_id = s.id
            WHERE s.individual_id = ANY($1::uuid[])
        ) ranked
        WHERE rn <= $2
        """,
        ids,
        PHOTOS_PER_DOG,
    )

    # Latest located sighting per dog, plus who logged it -- that decides
    # whether this viewer gets the real coordinate or a grid cell.
    loc_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (s.individual_id)
               s.individual_id,
               ST_Y(s.geog::geometry) AS lat,
               ST_X(s.geog::geometry) AS lng,
               s.observer_id
        FROM sightings s
        WHERE s.individual_id = ANY($1::uuid[]) AND s.geog IS NOT NULL
        -- s.id tiebreaks. Two sightings of one dog sharing captured_at would
        -- otherwise leave "latest" to the plan, so the card's location could
        -- change between requests for no reason the reader can see.
        ORDER BY s.individual_id, s.captured_at DESC, s.id DESC
        """,
        ids,
    )

    attr_rows = await conn.fetch(
        "SELECT individual_id, attrs FROM sightings "
        "WHERE individual_id = ANY($1::uuid[])",
        ids,
    )

    # Sign every thumbnail over one client rather than one client per photo.
    keys = [thumb_key(r["s3_key"]) for r in photo_rows]
    urls = await storage.urls(keys)
    photos: dict[UUID, list[str]] = {}
    for row, url in zip(photo_rows, urls):
        photos.setdefault(row["individual_id"], []).append(url)

    locations = {r["individual_id"]: r for r in loc_rows}
    similar = await _look_alikes(conn, ids)

    # A dog's descriptors come from many sightings that may disagree (one
    # observer says injured, a later one says healthy). Take the most frequent
    # non-empty answer per key; ties break toward the value seen first.
    counts: dict[UUID, dict[str, dict[str, int]]] = {}
    for row in attr_rows:
        raw = row["attrs"]
        attrs = json.loads(raw) if isinstance(raw, str) else (raw or {})
        per_dog = counts.setdefault(row["individual_id"], {})
        for key in _TAG_KEYS:
            v = attrs.get(key)
            if v and v not in ("unsure", "none"):
                per_dog.setdefault(key, {})
                per_dog[key][v] = per_dog[key].get(v, 0) + 1

    out = []
    for d in dogs:
        loc = locations.get(d["id"])
        lat = lng = None
        if loc is not None:
            lat, lng = loc["lat"], loc["lng"]
        out.append(
            {
                "id": str(d["id"]),
                "name": d["name"],
                "first_seen": d["first_seen"].date().isoformat(),
                "last_seen": d["last_seen"].date().isoformat(),
                "sighting_count": d["sighting_count"],
                "observer_count": d["observer_count"],
                "seen_by_me": d["seen_by_me"],
                # Names, not just a count -- see the query. Display names are
                # typed by observers at /join, so they are user-supplied and get
                # escaped on the way out; React does that by construction.
                "observers": list(d["observers"] or []),
                "photos": photos.get(d["id"], []),
                "lat": lat,
                "lng": lng,
                "tags": [
                    max(vals.items(), key=lambda kv: kv[1])[0]
                    for key in _TAG_KEYS
                    if (vals := counts.get(d["id"], {}).get(key))
                ],
                "looks_like": similar.get(d["id"], []),
            }
        )
    return {
        "dogs": out,
        # Exposed so the UI can mark where the system would engage a human,
        # without hardcoding a number that is expected to move as verdicts
        # accumulate. It is a review hint, never a verdict.
        "propose_min": settings.reid_propose_min,
    }
