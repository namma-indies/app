"""The dog catalogue: grouping, precision, and what it refuses to show.

The interesting cases are the ones where a dog is assembled from sightings that
disagree -- different observers, different attributes, one of them merged away.
"""

import io

import pytest
from PIL import Image

from app.ids import uuid7
from app.security import issue_session

MINE = (12.9716, 77.5946)


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 130, 110)).save(b, "JPEG")
    return b.getvalue()


async def _post(client, *, lat=MINE[0], lng=MINE[1], when="2026-07-19T09:00:00Z", **attrs):
    data = {"geo_source": "device_gps", "captured_at": when,
            "lat": str(lat), "lng": str(lng), "geo_accuracy_m": "8.0", **attrs}
    r = await client.post("/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")}, data=data)
    assert r.status_code == 201
    return r.json()["sighting_id"]


async def _pool(client):
    return client._transport.app.state.pool


async def _make_individual(client, sighting_ids, *, name=None, merged_into=None, status=None):
    """Attach sightings to one individual, as a confirmed match would."""
    iid = uuid7()
    async with (await _pool(client)).acquire() as c:
        await c.execute(
            "INSERT INTO individuals (id, name, created_by, status, merged_into) "
            "VALUES ($1,$2,'feeder',$3,$4)",
            iid, name, status, merged_into)
        for sid in sighting_ids:
            await c.execute(
                "UPDATE sightings SET individual_id=$1, match_status='confirmed' "
                "WHERE id=$2::uuid", iid, sid)
    return iid


async def _second_observer(client):
    oid = uuid7()
    async with (await _pool(client)).acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'B','test')", oid)
    return oid


@pytest.mark.asyncio
async def test_requires_auth(app_client):
    assert (await app_client.get("/dogs")).status_code == 401


@pytest.mark.asyncio
async def test_unidentified_sightings_are_not_dogs(authed_client):
    """A sighting nobody has matched is not an individual, and must not invent
    one -- otherwise the catalogue is just the journal with a different name."""
    client, _ = authed_client
    await _post(client)
    assert (await client.get("/dogs")).json()["dogs"] == []


@pytest.mark.asyncio
async def test_groups_sightings_into_one_dog(authed_client):
    client, _ = authed_client
    a = await _post(client, when="2026-07-01T09:00:00Z")
    b = await _post(client, when="2026-07-20T09:00:00Z")
    await _make_individual(client, [a, b])

    dogs = (await client.get("/dogs")).json()["dogs"]
    assert len(dogs) == 1
    assert dogs[0]["sighting_count"] == 2
    assert dogs[0]["first_seen"] == "2026-07-01"
    assert dogs[0]["last_seen"] == "2026-07-20"
    assert len(dogs[0]["photos"]) == 2


@pytest.mark.asyncio
async def test_merged_duplicates_are_hidden(authed_client):
    """When two records turn out to be one animal, the loser must disappear --
    a catalogue that lists the same dog twice is worse than no catalogue."""
    client, _ = authed_client
    survivor_s = await _post(client)
    dupe_s = await _post(client)
    survivor = await _make_individual(client, [survivor_s])
    await _make_individual(client, [dupe_s], merged_into=survivor, status="merged")

    dogs = (await client.get("/dogs")).json()["dogs"]
    assert [d["id"] for d in dogs] == [str(survivor)]


@pytest.mark.asyncio
async def test_location_is_full_precision_matching_the_map(authed_client):
    """No coarsening here, deliberately -- see the module docstring. `/map`
    already shows the cohort every sighting at full precision, so a stricter
    dog card would protect nothing while looking like it did. Issue #5's
    area-label coarsening must land on both at once."""
    client, _ = authed_client
    s = await _post(client)
    await _make_individual(client, [s])
    d = (await client.get("/dogs")).json()["dogs"][0]
    assert (round(d["lat"], 4), round(d["lng"], 4)) == (round(MINE[0], 4), round(MINE[1], 4))


@pytest.mark.asyncio
async def test_disagreeing_observers_resolve_to_the_common_answer(authed_client):
    """Two say injured, one says healthy -- the card should say injured rather
    than whichever sighting happened to sort first."""
    client, _ = authed_client
    ids = [
        await _post(client, when="2026-07-01T09:00:00Z", condition="injured", sex="female"),
        await _post(client, when="2026-07-02T09:00:00Z", condition="injured", sex="female"),
        await _post(client, when="2026-07-03T09:00:00Z", condition="healthy", sex="female"),
    ]
    await _make_individual(client, ids)
    d = (await client.get("/dogs")).json()["dogs"][0]
    assert "injured" in d["tags"] and "healthy" not in d["tags"]
    assert "female" in d["tags"]


@pytest.mark.asyncio
async def test_counts_how_many_people_have_seen_it(authed_client):
    """A dog several people know is a different thing from one person's
    repeated sighting, and the card should be able to say so."""
    client, oid_a = authed_client
    a = await _post(client)
    oid_b = await _second_observer(client)
    client.cookies.set("session", issue_session(oid_b))
    b = await _post(client)
    await _make_individual(client, [a, b])

    client.cookies.set("session", issue_session(oid_a))
    d = (await client.get("/dogs")).json()["dogs"][0]
    assert d["observer_count"] == 2
    assert d["seen_by_me"] is True


@pytest.mark.asyncio
async def test_a_dog_you_have_never_seen_says_so(authed_client):
    client, oid_a = authed_client
    oid_b = await _second_observer(client)
    client.cookies.set("session", issue_session(oid_b))
    s = await _post(client)
    await _make_individual(client, [s])

    client.cookies.set("session", issue_session(oid_a))
    assert (await client.get("/dogs")).json()["dogs"][0]["seen_by_me"] is False


@pytest.mark.asyncio
async def test_name_is_carried_through_when_one_exists(authed_client):
    """Nothing sets names yet, but the column does and the read path must not
    drop it -- otherwise naming looks broken the day it ships."""
    client, _ = authed_client
    s = await _post(client)
    await _make_individual(client, [s], name="Kalu")
    assert (await client.get("/dogs")).json()["dogs"][0]["name"] == "Kalu"


@pytest.mark.asyncio
async def test_newest_dog_first(authed_client):
    client, _ = authed_client
    old = await _post(client, when="2026-05-01T09:00:00Z")
    new = await _post(client, when="2026-08-01T09:00:00Z")
    i_old = await _make_individual(client, [old])
    i_new = await _make_individual(client, [new])
    dogs = (await client.get("/dogs")).json()["dogs"]
    assert [d["id"] for d in dogs] == [str(i_new), str(i_old)]


# --- look-alikes ---------------------------------------------------------
#
# These use synthetic vectors rather than MiewID, so they assert retrieval
# behaviour (ordering, exclusion, aggregation) and never the model's judgement.

import numpy as np

from app.embed import EMBED_DIM, MODEL_NAME


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _lit(v) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in v) + "]"


async def _embed(client, sighting_id, vec):
    """Attach a vector to a sighting's first photo, as the embed task would."""
    async with (await _pool(client)).acquire() as c:
        pid = await c.fetchval(
            "SELECT id FROM photos WHERE sighting_id=$1::uuid ORDER BY created_at LIMIT 1",
            sighting_id)
        await c.execute(
            "INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
            f"VALUES ($1,$2,$3,$4,$5::vector({EMBED_DIM})) "
            "ON CONFLICT (photo_id, model) DO UPDATE SET vec_miew = EXCLUDED.vec_miew",
            uuid7(), pid, MODEL_NAME, EMBED_DIM, _lit(vec))


@pytest.mark.asyncio
async def test_ranks_the_most_similar_dog_first(authed_client):
    client, _ = authed_client
    base = _unit(1)
    near = base * 0.98 + _unit(2) * 0.02          # nearly identical
    near /= np.linalg.norm(near)
    far = _unit(9)                                 # unrelated

    a = await _post(client); await _embed(client, a, base)
    b = await _post(client); await _embed(client, b, near)
    c_ = await _post(client); await _embed(client, c_, far)
    ia = await _make_individual(client, [a])
    ib = await _make_individual(client, [b])
    await _make_individual(client, [c_])

    dogs = {d["id"]: d for d in (await client.get("/dogs")).json()["dogs"]}
    top = dogs[str(ia)]["looks_like"][0]
    assert top["id"] == str(ib)
    assert top["similarity"] > 0.9


@pytest.mark.asyncio
async def test_a_dog_is_never_its_own_look_alike(authed_client):
    """Two sightings of one dog are its *own* photos; surfacing them as a
    candidate match would ask the reviewer to merge a dog with itself."""
    client, _ = authed_client
    v = _unit(3)
    a = await _post(client, when="2026-07-01T09:00:00Z"); await _embed(client, a, v)
    b = await _post(client, when="2026-07-02T09:00:00Z"); await _embed(client, b, v)
    ia = await _make_individual(client, [a, b])

    d = (await client.get("/dogs")).json()["dogs"][0]
    assert str(ia) not in [x["id"] for x in d["looks_like"]]


@pytest.mark.asyncio
async def test_similarity_is_the_best_photo_pair_not_an_average(authed_client):
    """A dog photographed from two sides must still match on the side that
    matches. Averaging its vectors would blur both angles into neither."""
    client, _ = authed_client
    side_a, side_b = _unit(4), _unit(5)     # deliberately unalike
    p = await _post(client, when="2026-07-01T09:00:00Z"); await _embed(client, p, side_a)
    q = await _post(client, when="2026-07-02T09:00:00Z"); await _embed(client, q, side_b)
    ipq = await _make_individual(client, [p, q])

    r = await _post(client)                  # matches only side_a
    await _embed(client, r, side_a)
    ir = await _make_individual(client, [r])

    dogs = {d["id"]: d for d in (await client.get("/dogs")).json()["dogs"]}
    hit = next(x for x in dogs[str(ipq)]["looks_like"] if x["id"] == str(ir))
    # ~1.0 from the matching side; a centroid would have roughly halved it.
    assert hit["similarity"] > 0.95


@pytest.mark.asyncio
async def test_unembedded_dogs_simply_have_no_look_alikes(authed_client):
    """Backfill has not run everywhere. A dog with no vectors must return an
    empty list, not fail the whole catalogue."""
    client, _ = authed_client
    a = await _post(client)
    await _make_individual(client, [a])
    body = (await client.get("/dogs")).json()
    assert body["dogs"][0]["looks_like"] == []


@pytest.mark.asyncio
async def test_response_carries_the_review_threshold(authed_client):
    """The UI marks where a human would be engaged; that number lives in
    settings and is expected to move as verdicts accumulate."""
    client, _ = authed_client
    a = await _post(client)
    await _make_individual(client, [a])
    from app.config import settings
    assert (await client.get("/dogs")).json()["propose_min"] == settings.reid_propose_min
