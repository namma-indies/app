"""Blocking another observer, and a moderator's ban (#180).

A block hides each person from the other on every surface, both ways and
silently, and changes nothing for anyone else. A ban hides someone from
everyone and ends their sessions.
"""
import json
import re
from uuid import UUID

import pytest

from app import accounts
from app.config import settings
from app.deps import get_storage
from app.ids import uuid7
from tests.test_dogs_route import _post, _second_observer
from tests.test_erasure import _as, _mentions, _pool, _rich_world


def _unsigned(value):
    """A view without its presigned URLs' signatures, which change by the second."""
    return json.loads(re.sub(r"\?X-Amz-[^\"]*", "", json.dumps(value, default=str)))


async def _views(client, viewer):
    _as(client, viewer)
    return {
        "map": (await client.get("/map")).json()["sightings"],
        "dogs": (await client.get("/dogs")).json()["dogs"],
        "proposals": (await client.get("/proposals")).json()["proposals"],
    }


@pytest.mark.asyncio
async def test_a_block_hides_each_from_the_other_and_nobody_else(authed_client):
    client, owner = authed_client
    w = await _rich_world(client, owner)            # owner "Owner", viewer "Viewer", entangled every way
    viewer, mine, theirs = w["viewer"], w["mine"], w["theirs"]
    third = await _second_observer(client, "Third")
    third_before = await _views(client, third)
    viewer_before = await _views(client, viewer)
    assert _mentions(viewer_before["map"], mine) and _mentions(viewer_before["dogs"], "Owner")

    _as(client, viewer)
    assert (await client.post(f"/sighting/{mine}/block")).status_code == 201

    # The viewer sees nothing of the owner: not their pins, their part of a
    # dog's card, their name, or any pair with one of theirs.
    seen = await _views(client, viewer)
    for name, surface in seen.items():
        assert not _mentions(surface, mine), f"{name} still shows the blocked person's sighting"
        assert not _mentions(surface, "Owner"), f"{name} still names the blocked person"
    (dog,) = seen["dogs"]
    assert dog["sighting_count"] == 1
    # The map is what it was, less exactly their sightings: a block decides on
    # who logged a sighting, never where (#148, #150).
    assert ({s["id"] for s in viewer_before["map"]} - {s["id"] for s in seen["map"]}) == {mine}
    assert _unsigned(seen["map"]) == _unsigned([s for s in viewer_before["map"] if s["id"] != mine])

    # And the owner sees nothing of the viewer -- without being told.
    blocked_view = await _views(client, owner)
    for name, surface in blocked_view.items():
        assert not _mentions(surface, theirs), f"{name} still shows the blocker's sighting to them"
        assert not _mentions(surface, "Viewer"), f"{name} still names the blocker to them"
    assert (await client.get("/blocks")).json() == {"blocks": []}

    # Nobody else's view changes, and nor do the counts.
    assert _unsigned(await _views(client, third)) == _unsigned(third_before)
    _as(client, third)
    stats = (await client.get("/stats")).json()["totals"]
    _as(client, viewer)
    assert (await client.get("/stats")).json()["totals"] == stats


@pytest.mark.asyncio
async def test_match_candidates_leave_out_a_blocked_persons_sightings(authed_client, monkeypatch):
    from app.matching import Candidate
    from app.routes import match
    client, owner = authed_client
    other = await _second_observer(client, "Other")
    mine = await _post(client)
    _as(client, other)
    theirs = await _post(client)
    pool = await _pool(client)
    async with pool.acquire() as c:
        photo = await c.fetchval("SELECT id FROM photos WHERE sighting_id=$1", UUID(theirs))
        from app.embed import EMBED_DIM
        await c.execute(f"""INSERT INTO embeddings (id, photo_id, model, dim, vec_miew)
                            SELECT $1, id, $2, {EMBED_DIM}, array_fill(0.1::real, ARRAY[{EMBED_DIM}])::vector
                            FROM photos WHERE sighting_id=$3""", uuid7(), match.MODEL_NAME, UUID(mine))
    async def one_candidate(*args, **kwargs):
        return [Candidate(sighting_id=UUID(theirs), photo_id=photo, individual_id=None, similarity=0.9,
                          distance_m=None)]
    monkeypatch.setattr(match, "find_candidates", one_candidate)
    _as(client, owner)
    assert [c["sighting_id"] for c in (await client.get(f"/sighting/{mine}/match")).json()["candidates"]] == [theirs]
    assert (await client.post(f"/sighting/{theirs}/block")).status_code == 201
    assert (await client.get(f"/sighting/{mine}/match")).json()["candidates"] == []


@pytest.mark.asyncio
async def test_blocking_is_by_a_sighting_you_can_see_and_never_shows_an_id(authed_client, monkeypatch):
    client, me = authed_client
    other = await _second_observer(client, "Other")
    mine = await _post(client)
    _as(client, other)
    theirs = await _post(client)
    _as(client, me)
    assert (await client.post(f"/sighting/{mine}/block")).status_code == 404        # yourself
    assert (await client.post(f"/sighting/{uuid7()}/block")).status_code == 404     # nothing
    assert (await client.post(f"/sighting/{theirs}/block")).status_code == 201
    assert (await client.post(f"/sighting/{theirs}/block")).status_code == 404      # already out of sight
    listed = (await client.get("/blocks")).json()
    assert [b["name"] for b in listed["blocks"]] == ["Other"]
    assert str(other) not in json.dumps(listed)
    # Only the blocker can lift it.
    _as(client, other)
    assert (await client.delete(f"/blocks/{listed['blocks'][0]['id']}")).status_code == 404
    _as(client, me)
    assert (await client.delete(f"/blocks/{listed['blocks'][0]['id']}")).status_code == 204
    assert theirs in {s["id"] for s in (await client.get("/map")).json()["sightings"]}


@pytest.mark.asyncio
async def test_blocking_is_rate_limited(authed_client, monkeypatch):
    monkeypatch.setattr(settings, "rl_block_times", 2)
    client, me = authed_client
    targets = []
    for i in range(3):
        other = await _second_observer(client, f"O{i}")
        _as(client, other)
        targets.append(await _post(client))
    _as(client, me)
    codes = [(await client.post(f"/sighting/{t}/block")).status_code for t in targets]
    assert codes == [201, 201, 429]


async def _moderator(client):
    mod = await _second_observer(client, "Mod")
    async with (await _pool(client)).acquire() as c:
        await c.execute("UPDATE observers SET trust_tier='moderator' WHERE id=$1", mod)
    return mod


@pytest.mark.asyncio
async def test_a_ban_hides_everything_from_everyone_and_ends_the_session(authed_client, monkeypatch):
    client, target = authed_client
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute("UPDATE observers SET email='bad@example.org', display_name='Bad' WHERE id=$1", target)
    mine = await _post(client)
    viewer, mod = await _second_observer(client, "Viewer"), await _moderator(client)
    _as(client, viewer)
    before = (await client.get("/stats")).json()["totals"]["sightings"]

    _as(client, viewer)
    assert (await client.post(f"/moderation/observer/{target}/ban", json={"reason": "Reviewed account behaviour"})).status_code == 404   # not a moderator
    _as(client, mod)
    assert (await client.post(f"/moderation/observer/{mod}/ban", json={"reason": "Reviewed account behaviour"})).status_code == 409      # not yourself
    r = await client.post(f"/moderation/observer/{target}/ban", json={"reason": "harassment"})
    assert r.json() == {"status": "banned"}
    (row,) = [o for o in (await client.get("/stats/observers")).json()["observers"] if o["id"] == str(target)]
    assert row["banned"] is True

    _as(client, viewer)
    assert mine not in {s["id"] for s in (await client.get("/map")).json()["sightings"]}
    assert (await client.get("/stats")).json()["totals"]["sightings"] == before - 1
    _as(client, target)
    r = await client.get("/dex")
    assert (r.status_code, r.json()["detail"]) == (403, "account_suspended")
    assert (await client.get("/account")).json()["suspended"] is True
    # Their address is refused at sign-in.
    client.cookies.clear()
    r = await client.post("/auth/email", data={"email": "bad@example.org"}, headers={"Accept": "application/json"})
    assert r.status_code == 403

    _as(client, mod)
    assert (await client.post(f"/moderation/observer/{target}/unban", json={"reason": "Appeal upheld"})).json() == {"status": "active"}
    _as(client, viewer)
    assert mine in {s["id"] for s in (await client.get("/map")).json()["sightings"]}
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM banned_emails") == 0


@pytest.mark.asyncio
async def test_a_ban_outlives_the_banned_persons_account_being_deleted(authed_client, monkeypatch):
    from app.routes import account
    monkeypatch.setattr(account, "send", lambda *a, **k: _noop())
    client, target = authed_client
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute("UPDATE observers SET email='bad@example.org' WHERE id=$1", target)
    await _post(client)
    mod = await _moderator(client)
    _as(client, mod)
    assert (await client.post(f"/moderation/observer/{target}/ban", json={"reason": "Reviewed account behaviour"})).status_code == 200
    # Deleting your account is yours to do, banned or not.
    _as(client, target)
    assert (await client.post("/account/delete", json={"confirm": "delete"})).status_code == 200
    async with pool.acquire() as c:
        await c.execute("UPDATE account_deletions SET purge_after = now() - interval '1 second'")
    assert await accounts.purge_one(pool, get_storage()) == target
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT email FROM observers WHERE id=$1", target) is None
        assert await c.fetchval("SELECT count(*) FROM banned_emails") == 1
    client.cookies.clear()
    r = await client.post("/auth/email", data={"email": "bad@example.org"}, headers={"Accept": "application/json"})
    assert r.status_code == 403


async def _noop():
    return None


@pytest.mark.asyncio
async def test_deleting_an_account_takes_its_blocks_both_ways(authed_client, monkeypatch):
    from app.routes import account
    monkeypatch.setattr(account, "send", lambda *a, **k: _noop())
    client, me = authed_client
    other = await _second_observer(client, "Other")
    mine = await _post(client)
    _as(client, other)
    theirs = await _post(client)
    assert (await client.post(f"/sighting/{mine}/block")).status_code == 201
    _as(client, me)
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO observer_blocks (id, blocker_id, blocked_id) VALUES ($1,$2,$3)", uuid7(), me,
                        await c.fetchval("SELECT observer_id FROM sightings WHERE id=$1", UUID(theirs)))
    assert (await client.post("/account/delete", json={"confirm": "delete"})).status_code == 200
    async with pool.acquire() as c:
        await c.execute("UPDATE account_deletions SET purge_after = now() - interval '1 second'")
    assert await accounts.purge_one(pool, get_storage()) == me
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM observer_blocks") == 0


@pytest.mark.asyncio
async def test_blocks_follow_a_passcode_account_into_its_email_account(app_client):
    """Through the real sign-in: the absorbed account blocked someone and was
    blocked by someone, and both blocks hold for the email account after."""
    from app.auth.logintoken import issue
    from app.security import issue_session
    pool = await _pool(app_client)
    passcode, email_account, x, y = uuid7(), uuid7(), uuid7(), uuid7()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO observers (id, display_name, created_via) VALUES ($1,'t@example.org','passcode')",
                        passcode)
        await c.execute("INSERT INTO observers (id, display_name, email, created_via) VALUES ($1,'T','t@example.org','email')",
                        email_account)
        for oid, name in ((x, "X"), (y, "Y")):
            await c.execute("INSERT INTO observers (id, display_name, created_via) VALUES ($1,$2,'test')", oid, name)
    sightings = {}
    for oid in (passcode, x, y, email_account):
        app_client.cookies.set("session", issue_session(oid))
        sightings[oid] = await _post(app_client)
    app_client.cookies.set("session", issue_session(passcode))
    assert (await app_client.post(f"/sighting/{sightings[x]}/block")).status_code == 201       # T blocked X
    assert (await app_client.post(f"/sighting/{sightings[email_account]}/block")).status_code == 201  # itself, in effect
    app_client.cookies.set("session", issue_session(y))
    assert (await app_client.post(f"/sighting/{sightings[passcode]}/block")).status_code == 201  # Y blocked T

    async with pool.acquire() as c:
        token = await issue(c, email_account)
    app_client.cookies.clear()
    assert (await app_client.get(f"/auth/email/consume?token={token}")).status_code == 303

    async with pool.acquire() as c:
        rows = {(r["blocker_id"], r["blocked_id"]) for r in await c.fetch("SELECT * FROM observer_blocks")}
    assert rows == {(email_account, x), (y, email_account)}
    app_client.cookies.set("session", issue_session(email_account))
    assert [b["name"] for b in (await app_client.get("/blocks")).json()["blocks"]] == ["X"]
    assert sightings[x] not in {s["id"] for s in (await app_client.get("/map")).json()["sightings"]}
    app_client.cookies.set("session", issue_session(y))
    seen = {s["id"] for s in (await app_client.get("/map")).json()["sightings"]}
    assert sightings[passcode] not in seen and sightings[email_account] not in seen


async def _pending_pair(client):
    actor, other, bystander = await _second_observer(client, "Actor"), await _second_observer(client, "Other"), \
        await _second_observer(client, "Bystander")
    _as(client, actor)
    a = await _post(client)
    _as(client, other)
    b = await _post(client)
    pid = uuid7()
    async with (await _pool(client)).acquire() as c:
        await c.execute("""INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, score, method, status)
                           VALUES ($1,$2,$3,0.8,'test','pending')""", pid, UUID(a), UUID(b))
    _as(client, actor)
    assert [p["id"] for p in (await client.get("/proposals")).json()["proposals"]] == [str(pid)]
    return actor, other, bystander, a, b, pid


@pytest.mark.asyncio
async def test_a_pair_read_before_a_block_cannot_be_ruled_on_after(authed_client):
    client, _ = authed_client
    actor, other, bystander, a, b, pid = await _pending_pair(client)
    assert (await client.post(f"/sighting/{b}/block")).status_code == 201
    assert (await client.post(f"/proposal/{pid}", data={"verdict": "same"})).status_code == 409
    # Someone who can see both sides still can.
    _as(client, bystander)
    assert (await client.post(f"/proposal/{pid}", data={"verdict": "different"})).status_code == 200


@pytest.mark.asyncio
async def test_a_pair_read_before_a_ban_cannot_be_ruled_on_after(authed_client):
    client, _ = authed_client
    actor, other, bystander, a, b, pid = await _pending_pair(client)
    mod = await _moderator(client)
    _as(client, mod)
    assert (await client.post(f"/moderation/observer/{other}/ban", json={"reason": "Reviewed account behaviour"})).status_code == 200
    for who in (actor, bystander):
        _as(client, who)
        assert (await client.post(f"/proposal/{pid}", data={"verdict": "same"})).status_code == 409


@pytest.mark.asyncio
async def test_a_block_racing_a_sign_in_carry_over_blocks_the_account_in_use(app_client):
    """The block finds the sighting's owner; before it is saved, that owner
    signs in by email and the passcode account is carried over and retired.
    The block must land on the account now in use, or it hides nobody."""
    from app.auth.email_login import absorb_passcode_observers
    from app.deps import get_conn
    from app.main import app
    from app.security import issue_session
    pool = await _pool(app_client)
    passcode, email_account, actor = uuid7(), uuid7(), uuid7()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO observers (id, display_name, created_via) VALUES ($1,'t@example.org','passcode')",
                        passcode)
        await c.execute("INSERT INTO observers (id, display_name, email, created_via) VALUES ($1,'T','t@example.org','email')",
                        email_account)
        await c.execute("INSERT INTO observers (id, display_name, created_via) VALUES ($1,'A','test')", actor)
    app_client.cookies.set("session", issue_session(passcode))
    theirs = await _post(app_client)
    carried = []

    class Interleaving:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def fetch(self, sql, *args):
            if "FOR SHARE" in sql and not carried:
                async with pool.acquire() as signing_in:
                    carried.append(await absorb_passcode_observers(
                        signing_in, target=email_account, email="t@example.org"))
            return await self._conn.fetch(sql, *args)

    async def interleaved_conn():
        async with pool.acquire() as conn:
            yield Interleaving(conn)
    app.dependency_overrides[get_conn] = interleaved_conn
    try:
        app_client.cookies.set("session", issue_session(actor))
        assert (await app_client.post(f"/sighting/{theirs}/block")).status_code == 201
    finally:
        app.dependency_overrides.pop(get_conn, None)
    assert carried == [1]
    async with pool.acquire() as c:
        assert [tuple(r) for r in await c.fetch("SELECT blocker_id, blocked_id FROM observer_blocks")] == [
            (actor, email_account)]
    assert theirs not in {s["id"] for s in (await app_client.get("/map")).json()["sightings"]}
