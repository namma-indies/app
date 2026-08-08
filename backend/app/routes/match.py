"""Re-identification endpoints: what does this sighting look like, and what did
a human decide about it.

The decision itself lives in `app.matching`; this layer is transport only.
"""

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException

from app.auth.deps import require_observer
from app.config import settings
from app.deps import get_conn
from app.ids import uuid7
from app.matching import resolve_sighting

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

    outcome = await resolve_sighting(
        conn,
        sighting_id,
        auto_merge_min=settings.reid_auto_merge_min,
        propose_min=settings.reid_propose_min,
        radius_m=settings.reid_radius_m,
        max_candidates=settings.reid_max_candidates,
        new_uuid=uuid7,
        thin_evidence_frames=settings.reid_thin_evidence_frames,
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
        "status": outcome.status,
        "individual_id": str(outcome.individual_id) if outcome.individual_id else None,
        # The client should ask for a short clip rather than a yes/no here: the
        # score cleared the bar on too little evidence to answer confidently.
        "suggest_video": outcome.suggest_video,
        "candidates": [
            {
                "sighting_id": str(c.sighting_id),
                "photo_id": str(c.photo_id),
                "individual_id": str(c.individual_id) if c.individual_id else None,
                "similarity": round(c.similarity, 4),
                "distance_m": round(c.distance_m) if c.distance_m is not None else None,
            }
            for c in outcome.candidates
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
