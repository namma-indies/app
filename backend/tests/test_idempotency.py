"""One capture must not become two sightings.

Reported from production: two identical photos side by side in the Dogs tab,
asking to be merged. Nothing prevented it -- `POST /sighting` had no idempotency
of any kind, and the offline queue only deletes a queued item *after* the
response arrives. When the request lands but the response does not, the item
stays pending and the next flush posts it again.
"""

import asyncio
import io

import pytest
from PIL import Image

from app.ids import uuid7
from app.security import issue_session


def _jpeg(colour=(90, 130, 110)):
    b = io.BytesIO()
    Image.new("RGB", (320, 240), colour).save(b, "JPEG")
    return b.getvalue()


BASE = {"geo_source": "device_gps", "captured_at": "2026-09-01T09:00:00Z",
        "lat": "12.9716", "lng": "77.5946", "geo_accuracy_m": "8.0"}


async def _post(client, token=None, colour=(90, 130, 110), **over):
    data = {**BASE, **over}
    if token:
        data["client_token"] = token
    return await client.post(
        "/sighting", files={"photos": ("d.jpg", _jpeg(colour), "image/jpeg")}, data=data)


async def _count(client):
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        return await c.fetchval("SELECT count(*) FROM sightings")


@pytest.mark.asyncio
async def test_a_retried_capture_does_not_become_a_second_sighting(authed_client):
    """The reported bug. Same token twice is one capture sent twice, not two
    captures -- which is what a lost response looks like from the client."""
    client, _ = authed_client
    token = str(uuid7())

    first = await _post(client, token)
    assert first.status_code == 201
    before = await _count(client)

    second = await _post(client, token)
    assert second.status_code == 201
    assert await _count(client) == before, "the retry created a second sighting"
    assert second.json()["sighting_id"] == first.json()["sighting_id"]
    assert second.json().get("duplicate") is True


@pytest.mark.asyncio
async def test_the_retry_returns_the_original_photos(authed_client):
    """A caller that lost the first response still needs usable ids back, or it
    cannot tell success from failure any better than before."""
    client, _ = authed_client
    token = str(uuid7())
    first = await _post(client, token)
    second = await _post(client, token)
    assert second.json()["photo_ids"] == first.json()["photo_ids"]
    assert second.json()["photo_ids"]


@pytest.mark.asyncio
async def test_two_genuine_captures_are_still_two_sightings(authed_client):
    """The guard must not collapse real repeat visits. Someone photographing
    the same dog twice in a morning is the normal case for a feeder."""
    client, _ = authed_client
    before = await _count(client)
    await _post(client, str(uuid7()))
    await _post(client, str(uuid7()))
    assert await _count(client) == before + 2


@pytest.mark.asyncio
async def test_identical_bytes_are_not_treated_as_a_duplicate(authed_client):
    """Deliberately NOT deduplicated on image content. Identical pixels are not
    the same event -- two frames of one clip share almost everything, and a
    person may legitimately log the same photo twice."""
    client, _ = authed_client
    before = await _count(client)
    await _post(client, str(uuid7()))
    await _post(client, str(uuid7()))          # same bytes, different capture
    assert await _count(client) == before + 2


@pytest.mark.asyncio
async def test_a_client_without_a_token_still_works(authed_client):
    """Older builds send no token. They get no protection, but must not break."""
    client, _ = authed_client
    before = await _count(client)
    r = await _post(client)
    assert r.status_code == 201
    assert await _count(client) == before + 1


@pytest.mark.asyncio
async def test_one_persons_token_cannot_claim_anothers_sighting(authed_client):
    """The lookup is scoped by observer. Tokens are client-generated, so a
    guessed or replayed one must not hand back someone else's sighting."""
    client, oid_a = authed_client
    token = str(uuid7())
    mine = await _post(client, token)

    oid_b = uuid7()
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'B','t')", oid_b)
    client.cookies.set("session", issue_session(oid_b))

    theirs = await _post(client, token)
    assert theirs.status_code == 201
    assert theirs.json()["sighting_id"] != mine.json()["sighting_id"]


@pytest.mark.asyncio
async def test_concurrent_retries_collapse_to_one(authed_client):
    """The second route in: the queue's in-flight guard is a module-level
    variable, so an installed PWA and a browser tab -- two JS contexts over one
    shared IndexedDB -- each run their own flush over the same rows. Both can
    pass the pre-check, and the unique index is what makes that safe."""
    client, _ = authed_client
    token = str(uuid7())
    before = await _count(client)

    results = await asyncio.gather(*(_post(client, token) for _ in range(4)))
    assert all(r.status_code == 201 for r in results), [r.status_code for r in results]
    assert await _count(client) == before + 1, "a race created extra sightings"
    assert len({r.json()["sighting_id"] for r in results}) == 1
