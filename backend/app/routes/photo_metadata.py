"""Camera-roll import preflight: what does this photo already know about itself?

The client is about to log a photo it did not just take. Before it can, it needs
to know whether the file carries its own date and place -- if it does, they are
used; if it does not, the person has to be asked, because the alternative is
recording the import as here-and-now and corrupting the 1km spatial prior that
`resolve_sighting` matches against.

Only the *head* of the file crosses the wire. EXIF's APP1 segment is capped at
64KB by the spec and sits at the front, so 128KB is always enough, and the
preflight costs a fraction of a 4MB photo instead of doubling the upload.

This is a question, not a validation gate: unreadable bytes answer "nothing
found" with a 200 and let the real upload be the thing that rejects them.
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.auth.deps import require_observer
from app.exif import read_capture_metadata

router = APIRouter()

# Generous against the 64KB the spec allows an APP1 segment, tight enough that
# this cannot be used to push a whole photo through an endpoint that stores
# nothing. Kept in sync with EXIF_HEAD_BYTES in frontend/src/api.ts.
MAX_HEAD_BYTES = 131_072

# Read in chunks so an oversized body is refused after one chunk rather than
# after being buffered in full.
_CHUNK = 32_768


@router.post("/photo/metadata")
async def photo_metadata(
    head: UploadFile = File(...),
    _observer_id=Depends(require_observer),
):
    raw = b""
    while chunk := await head.read(_CHUNK):
        raw += chunk
        if len(raw) > MAX_HEAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"send only the first {MAX_HEAD_BYTES} bytes of the file",
            )

    md = read_capture_metadata(raw)
    return {
        "captured_at_local": md.captured_at_local,
        "utc_offset_minutes": md.utc_offset_minutes,
        "lat": md.lat,
        "lng": md.lng,
        "has_date": md.has_date,
        "has_location": md.has_location,
    }
