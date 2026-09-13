"""The queue of photos the detector thinks have no animal in them.

Sorted by score ascending, which is the point: a histogram says where the
scores cluster, but walking this list from the bottom is how you find where
the detector starts being wrong -- and that is the threshold. So the pass that
checks the cleanup is the same pass that produces the number.

It works while `animal_confidence_min` is still 0.0 and nothing is hidden,
which is what lets review come before the filter rather than after it.
"""

import pytest

from app.ids import uuid7
from app.security import issue_session

pytestmark = pytest.mark.asyncio


async def _pool(client):
    return client._transport.app.state.pool


async def _moderator(client):
    oid = uuid7()
    async with (await _pool(client)).acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via, trust_tier) "
            "VALUES ($1,'Mod','test','moderator')", oid)
    client.cookies.set("session", issue_session(oid))
    return oid


async def _scored(client, *, dog, cat, key="k"):
    sid, pid = uuid7(), uuid7()
    async with (await _pool(client)).acquire() as c:
        oid = uuid7()
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')",
            oid)
        await c.execute(
            "INSERT INTO sightings (id, observer_id, captured_at, animal_confidence) "
            "VALUES ($1,$2,now(),$3)", sid, oid, max(dog, cat))
        await c.execute(
            "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,$3)", pid, sid, key)
        await c.execute(
            "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolo26x',$2,$3)",
            pid, dog, cat)
    return sid


async def test_the_queue_is_least_animal_like_first(app_client):
    await _moderator(app_client)
    mid = await _scored(app_client, dog=0.40, cat=0.00, key="b")
    low = await _scored(app_client, dog=0.02, cat=0.01, key="a")
    high = await _scored(app_client, dog=0.95, cat=0.00, key="c")

    r = await app_client.get("/moderation/animals")
    assert r.status_code == 200, r.text
    ids = [i["sighting_id"] for i in r.json()["items"]]
    assert ids == [str(low), str(mid), str(high)]


async def test_it_reports_dog_and_cat_separately(app_client):
    """Akash's ask was to confirm the photos being taken out have no *dog* in
    them. A bare 0.82 cannot answer that -- it might have been a cat."""
    await _moderator(app_client)
    sid = await _scored(app_client, dog=0.03, cat=0.82)

    item = (await app_client.get("/moderation/animals")).json()["items"][0]
    assert item["sighting_id"] == str(sid)
    assert item["dog"] == pytest.approx(0.03, abs=1e-6)
    assert item["cat"] == pytest.approx(0.82, abs=1e-6)


async def test_the_queue_works_while_the_filter_is_inert(app_client):
    """Review comes before the threshold, not after it."""
    from app.config import settings

    assert settings.animal_confidence_min == 0.0
    await _moderator(app_client)
    await _scored(app_client, dog=0.01, cat=0.00)
    assert len((await app_client.get("/moderation/animals")).json()["items"]) == 1


async def test_ruling_records_the_verdict_and_empties_it_from_the_queue(app_client):
    mod = await _moderator(app_client)
    sid = await _scored(app_client, dog=0.02, cat=0.00)

    r = await app_client.post(f"/sighting/{sid}/animal", data={"verdict": "animal"})
    assert r.status_code == 200, r.text

    async with (await _pool(app_client)).acquire() as c:
        row = await c.fetchrow(
            "SELECT animal_override, animal_reviewed_at, animal_reviewed_by, "
            "       review_status, reviewed_at "
            "FROM sightings WHERE id=$1", sid)
    assert row["animal_override"] is True
    assert row["animal_reviewed_at"] is not None
    assert row["animal_reviewed_by"] == mod
    # The two review states are independent questions about the same photo.
    assert row["review_status"] == "valid"
    assert row["reviewed_at"] is None

    assert (await app_client.get("/moderation/animals")).json()["items"] == []


async def test_no_animal_is_recorded_too(app_client):
    await _moderator(app_client)
    sid = await _scored(app_client, dog=0.02, cat=0.00)
    r = await app_client.post(f"/sighting/{sid}/animal", data={"verdict": "no_animal"})
    assert r.status_code == 200

    async with (await _pool(app_client)).acquire() as c:
        assert await c.fetchval(
            "SELECT animal_override FROM sightings WHERE id=$1", sid) is False


async def test_a_non_moderator_gets_404(app_client):
    """Same shape as the reported queue: require_moderator raises a 404, not
    a 403, so a non-moderator sees what they would see for a route that does
    not exist."""
    oid = uuid7()
    async with (await _pool(app_client)).acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')",
            oid)
    app_client.cookies.set("session", issue_session(oid))

    assert (await app_client.get("/moderation/animals")).status_code == 404
