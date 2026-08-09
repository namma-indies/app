"""Re-identification endpoints: what does this sighting look like, and what did
a human decide about it.

The decision itself lives in `app.matching`; this layer is transport only.
"""

import logging
from typing import Literal
from uuid import UUID

import numpy as np
from fastapi import APIRouter, Depends, Form, HTTPException

from app.auth.deps import require_observer
from app.config import settings
from app.deps import get_conn
from app.embed import MODEL_NAME
from app.ids import uuid7
from app.matching import find_candidates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sighting/{sighting_id}/match")
async def get_match(
    sighting_id: UUID,
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
):
    """Current match state plus ranked candidates.

    Safe to poll: the embedding is written by a background task a second or two
    after the 201, so the client may legitimately see `pending` first. That
    window is also the opening for asking the contributor for more evidence
    while they are still standing next to the animal.
    """
    row = await conn.fetchrow(
        "SELECT match_status, individual_id FROM sightings WHERE id=$1", sighting_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no such sighting")

    embedded = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM photos p
            JOIN embeddings e ON e.photo_id = p.id
            WHERE p.sighting_id = $1 AND e.vec_miew IS NOT NULL
        )
        """,
        sighting_id,
    )
    if not embedded:
        # Distinguishes "not scored yet" from "scored, found nothing" -- the
        # client should keep polling for the former and stop for the latter.
        return {"status": "pending", "candidates": [], "proposals": []}

    # Read-only from here. Resolution happens once, in the background task that
    # writes the embeddings; running it here meant every poll deleted and
    # recreated the pending proposals, so a client that read a proposal ID and
    # then acted on it got a 404 for a row it had just been handed.
    #
    # Candidates are still computed live because ranking is a pure query -- it
    # writes nothing, and recomputing keeps the list fresh as neighbours arrive.
    rows = await conn.fetch(
        """
        SELECT e.vec_miew::text AS vec,
               ST_Y(s.geog::geometry) AS lat,
               ST_X(s.geog::geometry) AS lng
        FROM sightings s
        JOIN photos p ON p.sighting_id = s.id
        JOIN embeddings e ON e.photo_id = p.id AND e.model = $2
        WHERE s.id = $1 AND e.vec_miew IS NOT NULL
        ORDER BY e.created_at
        """,
        sighting_id,
        MODEL_NAME,
    )
    vecs = [
        np.array([float(x) for x in r["vec"].strip("[]").split(",")], dtype=np.float32)
        for r in rows
    ]
    candidates = await find_candidates(
        conn,
        vecs,
        lat=rows[0]["lat"],
        lng=rows[0]["lng"],
        radius_m=settings.reid_radius_m,
        exclude_sighting_id=sighting_id,
        limit=settings.reid_max_candidates,
    )

    proposals = await conn.fetch(
        """
        SELECT mp.id, mp.candidate_individual_id, mp.score, i.name
        FROM match_proposals mp
        LEFT JOIN individuals i ON i.id = mp.candidate_individual_id
        WHERE mp.sighting_id = $1 AND mp.status = 'pending'
        ORDER BY mp.score DESC
        """,
        sighting_id,
    )

    return {
        # Straight from the row the background task wrote, rather than a
        # decision recomputed per request.
        "status": row["match_status"] or "unmatched",
        "individual_id": str(row["individual_id"]) if row["individual_id"] else None,
        # Ask for a short clip rather than a yes/no: something cleared the bar
        # on too little evidence for the contributor to answer confidently.
        "suggest_video": bool(proposals) and len(vecs) < settings.reid_thin_evidence_frames,
        "candidates": [
            {
                "sighting_id": str(c.sighting_id),
                "photo_id": str(c.photo_id),
                "individual_id": str(c.individual_id) if c.individual_id else None,
                "similarity": round(c.similarity, 4),
                "distance_m": round(c.distance_m) if c.distance_m is not None else None,
            }
            for c in candidates
        ],
        "proposals": [
            {
                "id": str(p["id"]),
                "individual_id": str(p["candidate_individual_id"])
                if p["candidate_individual_id"]
                else None,
                "name": p["name"],
                "score": round(float(p["score"]), 4),
            }
            for p in proposals
        ],
    }


@router.post("/proposal/{proposal_id}")
async def resolve_proposal(
    proposal_id: UUID,
    verdict: Literal["same", "different"] = Form(...),
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
):
    """Record a human verdict on a proposed match.

    `same` links the sighting to the individual, minting one if the candidate
    had no identity yet -- which is the normal case early on, when every prior
    sighting is unmatched. This is the only path that creates an `individual`:
    identities come from a person saying "these are the same animal", never from
    the model failing to find a match.

    Every verdict is also a labelled pair. `confirmations` is the calibration
    set for the thresholds in settings, which currently rest on almost no data.
    """
    p = await conn.fetchrow(
        "SELECT sighting_id, candidate_individual_id, candidate_sighting_id, status "
        "FROM match_proposals WHERE id=$1",
        proposal_id,
    )
    if p is None:
        raise HTTPException(status_code=404, detail="no such proposal")
    if p["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"already {p['status']}")

    sighting_id = p["sighting_id"]
    individual_id = p["candidate_individual_id"]

    async with conn.transaction():
        if verdict == "same":
            if individual_id is None:
                # Bootstrap case: neither sighting has an identity yet, so this
                # verdict is what brings one into existence. Both sightings are
                # attached to it -- the new one below, the candidate here.
                individual_id = uuid7()
                await conn.execute(
                    """
                    INSERT INTO individuals (id, created_by, created_by_observer,
                                             created_via, status, first_seen_at,
                                             last_seen_at)
                    SELECT $1, 'feeder', $2, 'match_confirmation', 'active',
                           MIN(captured_at), MAX(captured_at)
                    FROM sightings WHERE id = ANY($3::uuid[])
                    """,
                    individual_id,
                    observer_id,
                    [sighting_id, p["candidate_sighting_id"]],
                )
                if p["candidate_sighting_id"] is not None:
                    await conn.execute(
                        "UPDATE sightings SET individual_id=$1, "
                        "match_status='confirmed' WHERE id=$2",
                        individual_id,
                        p["candidate_sighting_id"],
                    )

            await conn.execute(
                "UPDATE sightings SET individual_id=$1, match_status='confirmed' "
                "WHERE id=$2",
                individual_id,
                sighting_id,
            )
            await conn.execute(
                "UPDATE match_proposals SET status='confirmed', resolved_by=$2, "
                "updated_at=now() WHERE id=$1",
                proposal_id,
                observer_id,
            )
            # Competing proposals for the same sighting are now moot.
            await conn.execute(
                "UPDATE match_proposals SET status='rejected', resolved_by=$2, "
                "updated_at=now() WHERE sighting_id=$1 AND id<>$3 AND status='pending'",
                sighting_id,
                observer_id,
                proposal_id,
            )
        else:
            await conn.execute(
                "UPDATE match_proposals SET status='rejected', resolved_by=$2, "
                "updated_at=now() WHERE id=$1",
                proposal_id,
                observer_id,
            )
            still_open = await conn.fetchval(
                "SELECT count(*) FROM match_proposals "
                "WHERE sighting_id=$1 AND status='pending'",
                sighting_id,
            )
            if not still_open:
                await conn.execute(
                    "UPDATE sightings SET match_status='unmatched' WHERE id=$1",
                    sighting_id,
                )

        await conn.execute(
            """
            INSERT INTO confirmations
                (id, sighting_id, individual_id, observer_id, proposal_id, verdict)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            uuid7(),
            sighting_id,
            individual_id if verdict == "same" else None,
            observer_id,
            proposal_id,
            verdict,
        )

    return {"status": "ok", "verdict": verdict,
            "individual_id": str(individual_id) if verdict == "same" else None}
