"""Blocking another observer (#180).

In a shared map with no messaging, what one person can learn about another is
where they go: the pins they drop and the dogs they log. A block hides each
from the other, both ways and silently -- their sightings, their photos on a
dog's card, their name, and the match questions that pair one with the other
(app/aggregates.py `visible_to`). The blocked person is not told. It changes
nothing for anyone else, so blocking cannot take anything off the map: that is
what reporting is for, and it needs two people and a moderator.

A block is made from a sighting the blocker can see, never from an observer
id: those never reach a client, so one person cannot be followed from one
surface to another by their id. The blocker's list carries the block's own id.

Moderators see how many people have blocked someone (/stats/observers). It is
something to read, never something that acts: a group of accounts blocking one
person must not be able to get them banned.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from app.aggregates import countable_sighting, visible_to
from app.auth.deps import require_observer
from app.deps import get_conn
from app.ids import uuid7
from app.ratelimit import block_limit, check

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sighting/{sighting_id}/block", status_code=201)
async def block(sighting_id: UUID, observer_id: UUID = Depends(require_observer), conn=Depends(get_conn)):
    """Block whoever logged this sighting. 404 for your own, or one you cannot
    see -- which includes one by someone already blocked either way."""
    await check(conn, f"block:obs:{observer_id}", block_limit())
    async with conn.transaction():
        for _ in range(3):
            other = await conn.fetchval(
                f"""SELECT s.observer_id FROM sightings s
                    WHERE s.id = $1 AND s.observer_id <> $2 AND {countable_sighting()} AND {visible_to('$2')}""",
                sighting_id, observer_id)
            if other is None:
                raise HTTPException(404, "no such sighting")
            # Both people's rows, shared-locked in id order -- the order a
            # sign-in's carry-over takes them in (auth/email_login.py) -- and
            # only while they are still in use. A carry-over that moved this
            # sighting to a new account meanwhile has retired the old one: look
            # again, rather than block an id nobody uses any more.
            live = {r["id"] for r in await conn.fetch(
                "SELECT id FROM observers WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL ORDER BY id FOR SHARE",
                [observer_id, other])}
            if observer_id not in live:
                raise HTTPException(401)
            if other in live and await conn.fetchval(
                    "SELECT observer_id FROM sightings WHERE id = $1", sighting_id) == other:
                break
        else:
            raise HTTPException(409, "that changed while blocking; try again")
        await conn.execute(
            """INSERT INTO observer_blocks (id, blocker_id, blocked_id, via_sighting_id) VALUES ($1,$2,$3,$4)
               ON CONFLICT (blocker_id, blocked_id) DO NOTHING""",
            uuid7(), observer_id, other, sighting_id)
    logger.info("observer=%s blocked the observer of sighting=%s", observer_id, sighting_id)
    return {"status": "blocked"}


@router.get("/blocks")
async def list_blocks(observer_id: UUID = Depends(require_observer), conn=Depends(get_conn)):
    """The people you have blocked, newest first, to unblock."""
    rows = await conn.fetch(
        """SELECT b.id, o.display_name, b.created_at FROM observer_blocks b
           JOIN observers o ON o.id = b.blocked_id
           WHERE b.blocker_id = $1 ORDER BY b.created_at DESC, b.id""", observer_id)
    return {"blocks": [{"id": str(r["id"]), "name": r["display_name"] or "someone",
                        "created_at": r["created_at"].isoformat()} for r in rows]}


@router.delete("/blocks/{block_id}", status_code=204)
async def unblock(block_id: UUID, observer_id: UUID = Depends(require_observer), conn=Depends(get_conn)):
    gone = await conn.fetchval(
        "DELETE FROM observer_blocks WHERE id = $1 AND blocker_id = $2 RETURNING id", block_id, observer_id)
    if gone is None:
        raise HTTPException(404, "no such block")
    return Response(status_code=204)
