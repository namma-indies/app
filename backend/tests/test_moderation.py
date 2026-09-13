"""Reporting a sighting, and what actually happens to it.

`review_status` has existed since migration 0001 and `/map` has filtered on it
since `/map` existed. Nothing ever wrote anything but `valid`, so the filter was
unreachable code and there was no way, anywhere in the product, to take a photo
off the shared map. These tests exist to make that unreachability impossible to
reintroduce: every display surface is asserted to hide reported content, and
each one is a separate test so a regression names the surface it broke.
"""

import io

import pytest
from PIL import Image

from app.ids import uuid7
from app.security import issue_session

BLR = (12.9716, 77.5946)


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 130, 110)).save(b, "JPEG")
    return b.getvalue()


async def _post(client, *, when="2026-07-19T09:00:00Z"):
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "device_gps", "captured_at": when,
              "lat": str(BLR[0]), "lng": str(BLR[1]), "geo_accuracy_m": "8.0"},
    )
    assert r.status_code == 201, r.text
    return r.json()["sighting_id"]


async def _pool(client):
    return client._transport.app.state.pool


async def _observer(client, name="Priya", tier=None):
    oid = uuid7()
    async with (await _pool(client)).acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via, trust_tier) "
            "VALUES ($1,$2,'test',$3)", oid, name, tier)
    return oid


def _become(client, oid):
    client.cookies.set("session", issue_session(oid))


async def _make_moderator(client, oid):
    async with (await _pool(client)).acquire() as c:
        await c.execute("UPDATE observers SET trust_tier='moderator' WHERE id=$1", oid)


async def _status(client, sid):
    async with (await _pool(client)).acquire() as c:
        return await c.fetchval("SELECT review_status FROM sightings WHERE id=$1::uuid", sid)


async def _report(client, sid, reason="endangers_dog", note=None):
    data = {"reason": reason}
    if note is not None:
        data["note"] = note
    return await client.post(f"/sighting/{sid}/report", data=data)


# --- /me ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_requires_auth(app_client):
    assert (await app_client.get("/me")).status_code == 401


@pytest.mark.asyncio
async def test_me_reports_moderator_status(authed_client):
    client, oid = authed_client
    assert (await client.get("/me")).json()["is_moderator"] is False
    await _make_moderator(client, oid)
    assert (await client.get("/me")).json()["is_moderator"] is True


# --- reporting ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_reporting_requires_auth(app_client):
    r = await app_client.post(f"/sighting/{uuid7()}/report", data={"reason": "other"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_reporting_an_unknown_sighting_is_a_404(authed_client):
    client, _ = authed_client
    assert (await _report(client, uuid7())).status_code == 404


@pytest.mark.asyncio
async def test_a_report_hides_the_sighting(authed_client):
    client, _ = authed_client
    sid = await _post(client)
    assert await _status(client, sid) == "valid"

    assert (await _report(client, sid)).status_code == 200

    assert await _status(client, sid) == "pending"


@pytest.mark.asyncio
async def test_an_unrecognised_reason_is_refused(authed_client):
    client, _ = authed_client
    sid = await _post(client)
    assert (await _report(client, sid, reason="because")).status_code == 422


@pytest.mark.asyncio
async def test_an_overlong_note_is_refused(authed_client):
    client, _ = authed_client
    sid = await _post(client)
    r = await _report(client, sid, note="x" * 501)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_reporting_twice_does_not_inflate_the_count(authed_client):
    """A double tap on a slow connection must not read to a moderator as
    'several people are worried about this'."""
    client, _ = authed_client
    sid = await _post(client)
    await _report(client, sid)
    await _report(client, sid, reason="other")

    async with (await _pool(client)).acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM sighting_reports WHERE sighting_id=$1::uuid", sid)
    assert n == 1


@pytest.mark.asyncio
async def test_a_new_report_does_not_overturn_a_moderator(authed_client):
    """Once a human has looked and said it is fine, one person tapping report
    again must not put it back down -- otherwise the moderator decision is
    advisory and the last tap wins."""
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid)
    await _make_moderator(client, oid)
    await client.post(f"/sighting/{sid}/review", data={"verdict": "valid"})

    other = await _observer(client, name="Someone")
    _become(client, other)
    await _report(client, sid)

    assert await _status(client, sid) == "valid"


# --- every surface has to hide it --------------------------------------------


@pytest.mark.asyncio
async def test_a_reported_sighting_leaves_the_map(authed_client):
    client, _ = authed_client
    sid = await _post(client)
    assert sid in [s["id"] for s in (await client.get("/map")).json()["sightings"]]

    await _report(client, sid)

    assert sid not in [s["id"] for s in (await client.get("/map")).json()["sightings"]]


@pytest.mark.asyncio
async def test_a_reported_sighting_leaves_the_dog_catalogue(authed_client):
    client, _ = authed_client
    a = await _post(client)
    b = await _post(client, when="2026-07-20T09:00:00Z")
    iid = uuid7()
    async with (await _pool(client)).acquire() as c:
        await c.execute(
            "INSERT INTO individuals (id, created_by, status) VALUES ($1,'feeder','active')",
            iid)
        for sid in (a, b):
            await c.execute(
                "UPDATE sightings SET individual_id=$1, match_status='confirmed' "
                "WHERE id=$2::uuid", iid, sid)
    assert (await client.get("/dogs")).json()["dogs"][0]["sighting_count"] == 2

    await _report(client, a)

    assert (await client.get("/dogs")).json()["dogs"][0]["sighting_count"] == 1


@pytest.mark.asyncio
async def test_a_reported_sighting_leaves_the_match_queue(authed_client):
    """A `same` verdict on a pair where one photo is hidden would fold content
    under review into an identity, and no later moderator decision unpicks that."""
    client, oid = authed_client
    a = await _post(client)
    b = await _post(client, when="2026-07-20T09:00:00Z")
    async with (await _pool(client)).acquire() as c:
        await c.execute(
            "INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, "
            "score, method, status) VALUES ($1,$2::uuid,$3::uuid,0.6,'test','pending')",
            uuid7(), a, b)
    assert len((await client.get("/proposals")).json()["proposals"]) == 1

    await _report(client, b)

    assert (await client.get("/proposals")).json()["proposals"] == []


@pytest.mark.asyncio
async def test_your_own_dex_still_shows_it_and_says_why(authed_client):
    """It is your photograph. You keep seeing it -- but you should be told it
    has been taken off the shared map rather than wondering why nobody can
    see it."""
    client, _ = authed_client
    sid = await _post(client)
    await _report(client, sid)

    mine = {s["id"]: s for s in (await client.get("/dex")).json()["sightings"]}

    assert sid in mine
    assert mine[sid]["review_status"] == "pending"


# --- the queue ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_queue_is_invisible_to_everyone_else(authed_client):
    """404 rather than 403: 403 confirms the endpoint is there and that this
    account merely lacks the tier, which makes it worth probing."""
    client, _ = authed_client
    assert (await client.get("/moderation/queue")).status_code == 404


@pytest.mark.asyncio
async def test_the_queue_carries_what_was_said(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid, reason="endangers_dog", note="shows the gate she sleeps by")
    await _make_moderator(client, oid)

    items = (await client.get("/moderation/queue")).json()["items"]

    assert [i["sighting_id"] for i in items] == [sid]
    assert items[0]["reasons"] == ["endangers_dog"]
    assert items[0]["notes"] == ["shows the gate she sleeps by"]
    assert items[0]["report_count"] == 1
    assert items[0]["thumb_url"]


@pytest.mark.asyncio
async def test_the_most_reported_comes_first(authed_client):
    client, oid = authed_client
    quiet = await _post(client)
    loud = await _post(client, when="2026-07-20T09:00:00Z")
    await _report(client, quiet)
    await _report(client, loud)
    for name in ("B", "C"):
        _become(client, await _observer(client, name=name))
        await _report(client, loud)

    _become(client, oid)
    await _make_moderator(client, oid)
    items = (await client.get("/moderation/queue")).json()["items"]

    assert [i["sighting_id"] for i in items] == [loud, quiet]
    assert items[0]["report_count"] == 3


# --- the verdict -------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewing_requires_a_moderator(authed_client):
    client, _ = authed_client
    sid = await _post(client)
    r = await client.post(f"/sighting/{sid}/review", data={"verdict": "rejected"})
    assert r.status_code == 404
    assert await _status(client, sid) == "valid"


@pytest.mark.asyncio
async def test_rejecting_keeps_it_off_the_map_for_good(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid)
    await _make_moderator(client, oid)

    r = await client.post(f"/sighting/{sid}/review", data={"verdict": "rejected"})

    assert r.status_code == 200
    assert await _status(client, sid) == "rejected"
    assert (await client.get("/map")).json()["sightings"] == []


@pytest.mark.asyncio
async def test_marking_it_valid_puts_it_back(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid)
    await _make_moderator(client, oid)

    await client.post(f"/sighting/{sid}/review", data={"verdict": "valid"})

    assert sid in [s["id"] for s in (await client.get("/map")).json()["sightings"]]


@pytest.mark.asyncio
async def test_rejecting_deletes_nothing(authed_client):
    """A photograph is evidence of something that happened. Hiding is
    reversible and deletion is not, so the row and its S3 objects stay."""
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid)
    await _make_moderator(client, oid)
    await client.post(f"/sighting/{sid}/review", data={"verdict": "rejected"})

    async with (await _pool(client)).acquire() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM photos WHERE sighting_id=$1::uuid", sid) == 1
        assert await c.fetchval(
            "SELECT count(*) FROM sighting_reports WHERE sighting_id=$1::uuid", sid) == 1


@pytest.mark.asyncio
async def test_an_unknown_verdict_is_refused(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _make_moderator(client, oid)
    r = await client.post(f"/sighting/{sid}/review", data={"verdict": "delete"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_fresh_report_after_a_review_still_reaches_a_human(authed_client):
    """The other half of a sticky decision. Reports after a review no longer
    hide the sighting, so they must still surface -- otherwise a new concern
    about an already-cleared photo goes nowhere, and 'sticky' becomes 'deaf'."""
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid)
    await _make_moderator(client, oid)
    await client.post(f"/sighting/{sid}/review", data={"verdict": "valid"})
    assert (await client.get("/moderation/queue")).json()["items"] == []

    other = await _observer(client, name="Someone")
    _become(client, other)
    await _report(client, sid, reason="offensive")

    _become(client, oid)
    items = (await client.get("/moderation/queue")).json()["items"]
    assert [i["sighting_id"] for i in items] == [sid]
    assert await _status(client, sid) == "valid"


@pytest.mark.asyncio
async def test_the_verdict_records_who_made_it(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid)
    await _make_moderator(client, oid)
    await client.post(f"/sighting/{sid}/review", data={"verdict": "rejected"})

    async with (await _pool(client)).acquire() as c:
        row = await c.fetchrow(
            "SELECT reviewed_by, reviewed_at FROM sightings WHERE id=$1::uuid", sid)
    assert row["reviewed_by"] == oid
    assert row["reviewed_at"] is not None
