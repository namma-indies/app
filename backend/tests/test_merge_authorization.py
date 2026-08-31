"""Who may say two sightings are one dog.

`POST /proposal/{id}` mints individuals, and it used to accept any signed-in
observer -- while the join passcode is a shared string handed around over
WhatsApp. These pin the ownership rule from issue #29: you decide about
photographs you took, and everything else stays open rather than being consumed
by a verdict nobody was entitled to give.
"""

import io

import pytest
from PIL import Image

from app.ids import uuid7
from app.security import issue_session


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 130, 110)).save(b, "JPEG")
    return b.getvalue()


async def _post(client, when="2026-07-19T09:00:00Z"):
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "device_gps", "captured_at": when,
              "lat": "12.9716", "lng": "77.5946", "geo_accuracy_m": "8.0"},
    )
    assert r.status_code == 201
    return r.json()["sighting_id"]


async def _pool(client):
    return client._transport.app.state.pool


async def _observer(client, name="B"):
    oid = uuid7()
    async with (await _pool(client)).acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,$2,'test')",
            oid, name)
    return oid


async def _proposal(client, sighting_id, candidate_id, score=0.62):
    """Write a pending proposal directly. The background task that normally
    creates these needs the ONNX weights, which CI does not have."""
    pid = uuid7()
    async with (await _pool(client)).acquire() as c:
        await c.execute(
            "INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, "
            "score, method, status) VALUES ($1,$2::uuid,$3::uuid,$4,'test','pending')",
            pid, sighting_id, candidate_id, score)
    return str(pid)


@pytest.mark.asyncio
async def test_you_may_merge_two_sightings_you_logged(authed_client):
    client, _ = authed_client
    a, b = await _post(client), await _post(client)
    pid = await _proposal(client, a, b)

    r = await client.post(f"/proposal/{pid}", data={"verdict": "same"})
    assert r.status_code == 200

    async with (await _pool(client)).acquire() as c:
        ids = await c.fetch(
            "SELECT individual_id FROM sightings WHERE id = ANY($1::uuid[])", [a, b])
    linked = {row["individual_id"] for row in ids}
    assert len(linked) == 1 and None not in linked, "both sightings join one individual"


@pytest.mark.asyncio
async def test_you_may_not_merge_someone_elses_sighting(authed_client):
    """The case the ownership rule exists for: a stranger has only the pixels,
    and the model's own numbers say pixels are not enough."""
    client, oid_a = authed_client
    mine = await _post(client)
    oid_b = await _observer(client)
    client.cookies.set("session", issue_session(oid_b))
    theirs = await _post(client)
    client.cookies.set("session", issue_session(oid_a))

    pid = await _proposal(client, mine, theirs)
    r = await client.post(f"/proposal/{pid}", data={"verdict": "same"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_a_refused_verdict_leaves_the_proposal_decidable(authed_client):
    """403 must not consume the row. These are the cross-observer matches that
    unify the population -- they wait for an adjudicator, they do not die."""
    client, oid_a = authed_client
    mine = await _post(client)
    oid_b = await _observer(client)
    client.cookies.set("session", issue_session(oid_b))
    theirs = await _post(client)
    client.cookies.set("session", issue_session(oid_a))

    pid = await _proposal(client, mine, theirs)
    await client.post(f"/proposal/{pid}", data={"verdict": "same"})

    async with (await _pool(client)).acquire() as c:
        status = await c.fetchval("SELECT status FROM match_proposals WHERE id=$1::uuid", pid)
    assert status == "pending"


@pytest.mark.asyncio
async def test_the_queue_lists_only_what_you_can_act_on(authed_client):
    client, oid_a = authed_client
    a, b = await _post(client), await _post(client)
    mine = await _proposal(client, a, b)

    oid_b = await _observer(client)
    client.cookies.set("session", issue_session(oid_b))
    theirs = await _post(client)
    client.cookies.set("session", issue_session(oid_a))
    cross = await _proposal(client, a, theirs)

    body = (await client.get("/proposals")).json()
    ids = [p["id"] for p in body["proposals"]]
    assert mine in ids
    assert cross not in ids, "a row you cannot act on is a dead end, not a queue entry"


@pytest.mark.asyncio
async def test_the_queue_carries_both_photos(authed_client):
    """The question is "are these the same dog", which cannot be answered from
    ids and a score."""
    client, _ = authed_client
    a, b = await _post(client), await _post(client)
    await _proposal(client, a, b)

    p = (await client.get("/proposals")).json()["proposals"][0]
    assert p["a"]["thumb_url"] and p["b"]["thumb_url"]
    assert "_thumb" in p["a"]["thumb_url"]
    assert p["a"]["sighting_id"] != p["b"]["sighting_id"]
    assert p["score"] > 0


@pytest.mark.asyncio
async def test_a_multi_frame_sighting_appears_once(authed_client):
    """A video sighting holds several frames. The photo join must not multiply
    one proposal into one row per frame."""
    client, _ = authed_client
    a, b = await _post(client), await _post(client)
    async with (await _pool(client)).acquire() as c:
        row = await c.fetchrow(
            "SELECT s3_key, created_at FROM photos WHERE sighting_id=$1::uuid", a)
        for n in range(3):
            await c.execute(
                "INSERT INTO photos (id, sighting_id, s3_key, created_at) "
                "VALUES ($1,$2::uuid,$3,$4)",
                uuid7(), a, row["s3_key"].replace(".webp", f"-{n}.webp"), row["created_at"])
    await _proposal(client, a, b)
    assert len((await client.get("/proposals")).json()["proposals"]) == 1


@pytest.mark.asyncio
async def test_the_queue_needs_a_session(app_client):
    assert (await app_client.get("/proposals")).status_code == 401
