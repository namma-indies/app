"""The daily @nammaindies post: contributors offer sightings, moderators pick.

The rules -- who may be posted, which photos, what a caption may say -- live in
`app.social`; this layer is transport. Design:
docs/specs/2026-10-24-x-daily-post-design.md.
"""

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.auth.deps import require_moderator, require_observer
from app.deps import get_conn, get_storage
from app.ids import uuid7
from app.photos import thumb_key
from app.social import (
    area_name,
    can_share,
    default_caption,
    is_postable,
    mark_shared,
    postable_photos,
    unshare,
    validate_caption,
)
from app.storage.s3 import S3Storage

router = APIRouter()


class ShareBody(BaseModel):
    shared: bool


@router.post("/sighting/{sighting_id}/share")
async def share_sighting(
    sighting_id: UUID,
    body: ShareBody,
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
):
    """Offer one of your own sightings to the team, or withdraw it.

    Sharing needs consent; withdrawing never does -- someone who has since
    revoked consent must still be able to take a sighting back."""
    owner = await conn.fetchval(
        """SELECT COALESCE(c.observer_id, s.observer_id) FROM sightings s
           LEFT JOIN captures c ON c.id = s.capture_id WHERE s.id = $1""",
        sighting_id,
    )
    if owner is None:
        raise HTTPException(status_code=404, detail="no such sighting")
    if owner != observer_id:
        raise HTTPException(status_code=403, detail="only the person who logged it can share it")
    async with conn.transaction():
        if body.shared:
            if not await can_share(conn, observer_id):
                raise HTTPException(status_code=403, detail="sharing isn't enabled for you yet")
            await mark_shared(conn, sighting_id, observer_id)
        else:
            await unshare(conn, sighting_id)
    return {"shared": body.shared}


# Enough to work through in one sitting; the list is newest first, so anything
# past this is older than what a moderator would pick from anyway.
MAX_SHARED = 100
RECENT_POSTED = 30


@router.get("/social/shared")
async def list_shared(
    _: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    """Sightings people have offered that nobody has picked or passed on yet.

    Each comes with every photo that could go out, at full size: the pick is
    the check on what the image shows (house numbers, faces), so the moderator
    must see exactly what would be posted, not a thumbnail of it."""
    rows = await conn.fetch(
        """
        SELECT s.id, s.captured_at, s.species, o.x_handle
        FROM sightings s
        LEFT JOIN captures c ON c.id = s.capture_id
        JOIN observers o ON o.id = COALESCE(c.observer_id, s.observer_id)
        WHERE s.shared_with_team_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM social_picks sp WHERE sp.sighting_id = s.id)
        ORDER BY s.shared_with_team_at DESC, s.id
        LIMIT $1
        """,
        MAX_SHARED,
    )
    items = []
    for r in rows:
        photo_ids = await postable_photos(conn, r["id"])
        if not photo_ids:
            continue  # rejected, a hidden copy, consent withdrawn, or a video
        keys = await conn.fetch(
            "SELECT id, s3_key FROM photos WHERE id = ANY($1::uuid[])", photo_ids)
        key_of = {k["id"]: k["s3_key"] for k in keys}
        ordered = [key_of[p] for p in photo_ids]
        urls = await storage.urls(ordered + [thumb_key(k) for k in ordered])
        area = await area_name(conn, r["id"])
        items.append({
            "sighting_id": str(r["id"]),
            "captured_at": r["captured_at"].isoformat(),
            "species": r["species"],
            "handle": r["x_handle"],
            "area": area,
            "default_caption": default_caption(area, r["x_handle"]),
            "photos": [
                {"photo_id": str(p), "url": urls[i], "thumb_url": urls[len(ordered) + i]}
                for i, p in enumerate(photo_ids)
            ],
        })
    return {"items": items}


class PickBody(BaseModel):
    sighting_id: UUID
    photo_id: UUID
    caption: str


class PassBody(BaseModel):
    sighting_id: UUID


@router.post("/social/pick", status_code=201)
async def pick(
    body: PickBody,
    moderator: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
):
    """Queue one photo of a shared sighting, with the caption it goes out with.

    The caption is stored as typed and posted exactly as stored; it is not
    rebuilt at posting time, so what the moderator approved is what appears."""
    problem = validate_caption(body.caption)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if not await is_postable(conn, body.sighting_id, body.photo_id):
        raise HTTPException(status_code=422, detail="that photo can't be posted for this sighting")
    pick_id = uuid7()
    try:
        await conn.execute(
            "INSERT INTO social_picks (id, sighting_id, photo_id, caption, status, picked_by) "
            "VALUES ($1,$2,$3,$4,'queued',$5)",
            pick_id, body.sighting_id, body.photo_id, body.caption, moderator,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="that sighting or photo is already picked")
    return {"id": str(pick_id)}


@router.post("/social/pass", status_code=201)
async def pass_on(
    body: PassBody,
    moderator: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
):
    """"Not this one": the sighting leaves the shared list without posting."""
    try:
        await conn.execute(
            "INSERT INTO social_picks (id, sighting_id, status, picked_by) "
            "VALUES ($1,$2,'passed',$3)",
            uuid7(), body.sighting_id, moderator,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="that sighting is already picked or passed")
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(status_code=404, detail="no such sighting")
    return {}


@router.delete("/social/pick/{pick_id}", status_code=204)
async def remove_pick(
    pick_id: UUID,
    _: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
):
    """Take a pick out of the queue (or undo a pass) so the sighting returns
    to the shared list. Once the daily run has committed to a row it is part
    of the account's history and stays."""
    status = await conn.fetchval("SELECT status FROM social_picks WHERE id = $1", pick_id)
    if status is None:
        raise HTTPException(status_code=404, detail="no such pick")
    if status not in ("queued", "passed"):
        raise HTTPException(status_code=409, detail=f"already {status}")
    await conn.execute(
        "DELETE FROM social_picks WHERE id = $1 AND status IN ('queued','passed')", pick_id)
    return Response(status_code=204)


@router.get("/social/queue")
async def queue(
    _: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    """What goes out next, what needs a person, and what has gone out.

    `attention` holds a row the daily run started and never heard back on
    (`posting`: X may or may not have it -- nothing else posts until someone
    has looked), and queued rows whose last attempt failed.

    `flagged` marks a post whose sighting has since been taken down, hidden as
    a copy or unshared. The app never deletes from X itself; the flag is the
    prompt for a person to (design decision)."""
    rows = await conn.fetch(
        """
        (SELECT sp.*, p.s3_key,
                (s.review_status <> 'valid' OR s.duplicate_of IS NOT NULL
                 OR s.shared_with_team_at IS NULL) AS gone
         FROM social_picks sp
         JOIN sightings s ON s.id = sp.sighting_id
         JOIN photos p ON p.id = sp.photo_id
         WHERE sp.status IN ('queued','posting'))
        UNION ALL
        (SELECT sp.*, p.s3_key,
                (s.review_status <> 'valid' OR s.duplicate_of IS NOT NULL
                 OR s.shared_with_team_at IS NULL) AS gone
         FROM social_picks sp
         JOIN sightings s ON s.id = sp.sighting_id
         JOIN photos p ON p.id = sp.photo_id
         WHERE sp.status = 'posted'
         ORDER BY sp.posted_at DESC NULLS LAST
         LIMIT $1)
        """,
        RECENT_POSTED,
    )
    urls = await storage.urls([thumb_key(r["s3_key"]) for r in rows])

    def view(i, r):
        return {
            "id": str(r["id"]),
            "sighting_id": str(r["sighting_id"]),
            "photo_id": str(r["photo_id"]),
            "thumb_url": urls[i],
            "caption": r["caption"],
            "status": r["status"],
            "picked_at": r["picked_at"].isoformat(),
            "posted_at": r["posted_at"].isoformat() if r["posted_at"] else None,
            "x_post_id": r["x_post_id"],
            "last_error": r["last_error"],
            "last_attempt_at": r["last_attempt_at"].isoformat() if r["last_attempt_at"] else None,
            "flagged": r["status"] == "posted" and r["gone"],
        }

    views = [view(i, r) for i, r in enumerate(rows)]
    # Same order the daily run takes them in (app.social.QUEUE_ORDER).
    queued = sorted((v for v in views if v["status"] == "queued"),
                    key=lambda v: (v["last_attempt_at"] is not None, v["last_attempt_at"] or "",
                                   v["picked_at"], v["id"]))
    return {
        "queued": queued,
        "attention": [v for v in views
                      if v["status"] == "posting" or (v["status"] == "queued" and v["last_error"])],
        "posted": [v for v in views if v["status"] == "posted"],
    }
