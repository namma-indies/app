"""The owner deleting what they logged (#54).

`DELETE /capture/{id}` is "Delete photo": the whole upload, every animal in it,
its notes, transcripts and suggestions. `DELETE /sighting/{id}` is "Remove
this dog": one animal of a photo with several, and the whole upload when it
was the last one. app/erasure.py says what each takes with it.

Owner only. Anyone else gets the 404 a missing id gets, as /sighting/{id}/match
does, so these cannot be used to learn which ids exist. A second delete is a
404 too: it is gone. Gone for everyone at once; the objects follow from the
store after the response (media_purges).
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth.deps import require_observer
from app.deps import get_storage
from app.erasure import LedgerUnavailable, erase

router = APIRouter()


async def _erase(request: Request, storage, kind: str, target: UUID, observer_id: UUID) -> Response:
    try:
        erased = await erase(request.app.state.pool, storage, kind, target,
                             reason=f"{kind}_deleted_by_owner", owner=observer_id)
    except LedgerUnavailable:
        # Nothing was deleted; trying again later is safe.
        raise HTTPException(503, "couldn't delete that just now; try again")
    if erased is None:
        raise HTTPException(404, f"no such {kind}")
    return Response(status_code=204)


@router.delete("/capture/{capture_id}", status_code=204)
async def delete_capture(capture_id: UUID, request: Request,
                         observer_id: UUID = Depends(require_observer), storage=Depends(get_storage)):
    return await _erase(request, storage, "capture", capture_id, observer_id)


@router.delete("/sighting/{sighting_id}", status_code=204)
async def delete_sighting(sighting_id: UUID, request: Request,
                          observer_id: UUID = Depends(require_observer), storage=Depends(get_storage)):
    return await _erase(request, storage, "sighting", sighting_id, observer_id)
