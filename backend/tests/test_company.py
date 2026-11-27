"""Often in the company of: who a dog is photographed with.

Two individuals are company when a sighting of each came from the same upload,
i.e. one camera saw both at once. Everything here is about not overstating
that, and about building it from the same sightings the card already shows.
"""
import pytest

from app.ids import uuid7
from app.security import issue_session
from tests.test_dogs_route import _make_individual, _pool, _post, _second_observer


async def _sql(client, query, *args):
    async with (await _pool(client)).acquire() as c:
        return await c.fetch(query, *args)


async def _frame(client, n=2, when="2026-07-19T09:00:00Z"):
    """One upload with `n` animals in it: n sightings sharing a capture, as
    `captures.publish_groups` leaves them."""
    ids = [await _post(client, when=when) for _ in range(n)]
    capture = (await _sql(client, "SELECT capture_id FROM sightings WHERE id=$1::uuid", ids[0]))[0][0]
    assert capture is not None
    await _sql(client, "UPDATE sightings SET capture_id=$1 WHERE id = ANY($2::uuid[])", capture, ids[1:])
    return ids


async def _company(client):
    dogs = (await client.get("/dogs")).json()["dogs"]
    return {d["number"]: [(c["number"], c["together"]) for c in d["company"]] for d in dogs}


@pytest.mark.asyncio
async def test_two_dogs_in_one_frame_are_company(authed_client):
    client, _ = authed_client
    a, b = await _frame(client)
    one = await _make_individual(client, [a, await _post(client)])
    two = await _make_individual(client, [b, await _post(client)])

    dogs = {d["id"]: d for d in (await client.get("/dogs")).json()["dogs"]}
    assert dogs[str(one)]["company"] == [{"id": str(two), "number": 2, "together": 1}]
    assert dogs[str(two)]["company"] == [{"id": str(one), "number": 1, "together": 1}]


@pytest.mark.asyncio
async def test_the_same_street_is_not_the_same_frame(authed_client):
    """Same person, same place, same minute, separate uploads: neighbours,
    perhaps, but no camera saw them together."""
    client, _ = authed_client
    await _make_individual(client, [await _post(client), await _post(client)])
    await _make_individual(client, [await _post(client), await _post(client)])
    assert await _company(client) == {1: [], 2: []}


@pytest.mark.asyncio
async def test_most_often_together_comes_first(authed_client):
    client, _ = authed_client
    a1, b1, c1 = await _frame(client, 3, when="2026-07-01T09:00:00Z")
    a2, b2 = await _frame(client, 2, when="2026-07-02T09:00:00Z")
    await _make_individual(client, [a1, a2])
    await _make_individual(client, [b1, b2])
    await _make_individual(client, [c1, await _post(client)])

    assert await _company(client) == {
        1: [(2, 2), (3, 1)],
        2: [(1, 2), (3, 1)],
        3: [(1, 1), (2, 1)],
    }


@pytest.mark.asyncio
async def test_an_unrecognised_companion_is_not_listed(authed_client):
    """The other animal in the frame has no identity yet, so there is nobody to
    name. They appear the day they are recognised."""
    client, _ = authed_client
    a, b = await _frame(client)
    await _make_individual(client, [a, await _post(client)])
    assert await _company(client) == {1: []}

    await _make_individual(client, [b, await _post(client)])
    assert (await _company(client))[1] == [(2, 1)]


@pytest.mark.asyncio
async def test_company_is_built_only_from_sightings_the_card_may_show(authed_client):
    client, me = authed_client
    a, b = await _frame(client)
    await _make_individual(client, [a, await _post(client)])
    await _make_individual(client, [b, await _post(client)])
    assert (await _company(client))[1] == [(2, 1)]

    # The frame's sighting of the second dog is reported: it is off every
    # shared surface, and so is the company it was evidence of.
    await _sql(client, "UPDATE sightings SET review_status='pending' WHERE id=$1::uuid", b)
    assert await _company(client) == {1: [], 2: []}


@pytest.mark.asyncio
async def test_a_blocked_persons_upload_contributes_no_company(authed_client):
    client, me = authed_client
    other = await _second_observer(client, "Priya")
    client.cookies.set("session", issue_session(other))
    a, b = await _frame(client)
    client.cookies.set("session", issue_session(me))
    await _make_individual(client, [a, await _post(client)])
    await _make_individual(client, [b, await _post(client)])
    assert (await _company(client))[1] == [(2, 1)]

    await _sql(client, "INSERT INTO observer_blocks (id, blocker_id, blocked_id) VALUES ($1,$2,$3)",
               uuid7(), me, other)
    assert await _company(client) == {1: [], 2: []}


@pytest.mark.asyncio
async def test_a_merged_away_record_is_nobodys_company(authed_client):
    client, _ = authed_client
    a, b = await _frame(client)
    one = await _make_individual(client, [a, await _post(client)])
    await _make_individual(client, [b], merged_into=one, status="merged")
    assert await _company(client) == {1: []}


@pytest.mark.asyncio
async def test_a_row_of_faces_not_a_census_of_the_pack(authed_client):
    from app.routes.dogs import COMPANY_PER_DOG
    client, _ = authed_client
    frame = await _frame(client, COMPANY_PER_DOG + 3)
    for sid in frame:
        await _make_individual(client, [sid, await _post(client)])
    company = await _company(client)
    assert all(len(v) == COMPANY_PER_DOG for v in company.values())
    # Ties are broken by number, so the row does not shuffle between loads.
    assert [n for n, _ in company[1]] == [2, 3, 4, 5]
