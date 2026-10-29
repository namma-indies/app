"""Who may say two sightings are one dog.

Anyone signed in. `POST /proposal/{id}` mints individuals, and for a while only
the observer who logged both sightings could call it (issue #29). That left
every cross-observer match pending with nobody allowed to decide it, so the
rule went. What replaces it later is disputes and verdicts weighted by who gave
them; what these pin now is that a stranger's verdict lands, that it is
recorded against them, and that the queue shows your own pairs first without
showing anyone where someone else's dog was.
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


async def _as(client, oid):
    client.cookies.set("session", issue_session(oid))


@pytest.mark.asyncio
async def test_you_may_merge_someone_elses_sighting_with_yours(authed_client):
    client, oid_a = authed_client
    mine = await _post(client)
    oid_b = await _observer(client)
    await _as(client, oid_b)
    theirs = await _post(client)
    await _as(client, oid_a)

    pid = await _proposal(client, mine, theirs)
    r = await client.post(f"/proposal/{pid}", data={"verdict": "same"})
    assert r.status_code == 200
    async with (await _pool(client)).acquire() as c:
        linked = {row["individual_id"] for row in await c.fetch(
            "SELECT individual_id FROM sightings WHERE id = ANY($1::uuid[])", [mine, theirs])}
    assert len(linked) == 1 and None not in linked


@pytest.mark.asyncio
async def test_a_stranger_may_decide_a_pair_they_took_neither_of(authed_client):
    """The case the old rule refused outright. The verdict lands, and
    `confirmations` says who gave it -- that row is what disputes and
    reputation weighting will read."""
    client, _ = authed_client
    oid_b, oid_c = await _observer(client, "B"), await _observer(client, "C")
    await _as(client, oid_b)
    a, b = await _post(client), await _post(client)
    pid = await _proposal(client, a, b)

    await _as(client, oid_c)
    assert pid in [p["id"] for p in (await client.get("/proposals")).json()["proposals"]]
    r = await client.post(f"/proposal/{pid}", data={"verdict": "same"})
    assert r.status_code == 200

    async with (await _pool(client)).acquire() as c:
        status, resolver = await c.fetchrow(
            "SELECT status, resolved_by FROM match_proposals WHERE id=$1::uuid", pid)
        decider = await c.fetchval(
            "SELECT observer_id FROM confirmations WHERE proposal_id=$1::uuid", pid)
        maker = await c.fetchval(
            "SELECT i.created_by_observer FROM individuals i "
            "JOIN sightings s ON s.individual_id = i.id WHERE s.id=$1::uuid", a)
    assert status == "confirmed"
    assert resolver == oid_c and decider == oid_c and maker == oid_c


@pytest.mark.asyncio
async def test_a_strangers_different_verdict_is_recorded_too(authed_client):
    client, _ = authed_client
    oid_b, oid_c = await _observer(client, "B"), await _observer(client, "C")
    await _as(client, oid_b)
    a, b = await _post(client), await _post(client)
    pid = await _proposal(client, a, b)

    await _as(client, oid_c)
    r = await client.post(f"/proposal/{pid}", data={"verdict": "different"})
    assert r.status_code == 200
    async with (await _pool(client)).acquire() as c:
        row = await c.fetchrow(
            "SELECT observer_id, verdict FROM confirmations WHERE proposal_id=$1::uuid", pid)
    assert row["observer_id"] == oid_c and row["verdict"] == "different"


@pytest.mark.asyncio
async def test_a_second_verdict_on_the_same_pair_is_a_conflict(authed_client):
    """With everyone working one queue, two people answering the same pair is
    ordinary. The second gets a 409 rather than overwriting the first."""
    client, oid_a = authed_client
    a, b = await _post(client), await _post(client)
    pid = await _proposal(client, a, b)
    assert (await client.post(f"/proposal/{pid}", data={"verdict": "different"})).status_code == 200

    await _as(client, await _observer(client))
    r = await client.post(f"/proposal/{pid}", data={"verdict": "same"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_a_hidden_sighting_still_blocks_the_verdict(authed_client):
    """Ownership went; visibility did not. A sighting reported since the queue
    was read must not be folded into an identity, whoever is deciding."""
    client, _ = authed_client
    oid_b = await _observer(client)
    await _as(client, oid_b)
    a, b = await _post(client), await _post(client)
    pid = await _proposal(client, a, b)
    async with (await _pool(client)).acquire() as c:
        await c.execute("UPDATE sightings SET review_status='pending' WHERE id=$1::uuid", b)

    await _as(client, await _observer(client, "C"))
    r = await client.post(f"/proposal/{pid}", data={"verdict": "same"})
    assert r.status_code == 409
    async with (await _pool(client)).acquire() as c:
        assert await c.fetchval(
            "SELECT status FROM match_proposals WHERE id=$1::uuid", pid) == "pending"


@pytest.mark.asyncio
async def test_the_queue_shows_everyones_pairs_with_your_own_first(authed_client):
    client, oid_a = authed_client
    a1, a2 = await _post(client), await _post(client)
    oid_b = await _observer(client)
    await _as(client, oid_b)
    b1, b2 = await _post(client), await _post(client)
    await _as(client, oid_a)

    # Scores chosen so that ordering by score alone would put these in the
    # reverse of the expected order.
    both_mine = await _proposal(client, a1, a2, score=0.41)
    one_mine = await _proposal(client, a1, b1, score=0.45)
    none_mine = await _proposal(client, b1, b2, score=0.60)
    none_mine_low = await _proposal(client, b2, b1, score=0.42)

    body = (await client.get("/proposals")).json()
    ids = [p["id"] for p in body["proposals"]]
    assert ids == [both_mine, one_mine, none_mine, none_mine_low]

    by_id = {p["id"]: p for p in body["proposals"]}
    assert by_id[one_mine]["a"]["mine"] is True
    assert by_id[one_mine]["b"]["mine"] is False
    assert not by_id[none_mine]["a"]["mine"] and not by_id[none_mine]["b"]["mine"]


@pytest.mark.asyncio
async def test_the_queue_says_nothing_about_where(authed_client):
    """Issue #45: exact location only for animals you photographed. A pair card
    is a thumbnail and a date for each side, and must stay that way for other
    people's sightings."""
    client, oid_a = authed_client
    await _as(client, await _observer(client))
    a, b = await _post(client), await _post(client)
    await _proposal(client, a, b)
    await _as(client, oid_a)

    p = (await client.get("/proposals")).json()["proposals"][0]
    for side in (p["a"], p["b"]):
        assert set(side) == {"sighting_id", "species", "date", "thumb_url", "mine"}
    assert set(p) == {"id", "score", "a", "b"}


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


async def test_known_name_history_is_append_only_attributed_and_replay_safe():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.routes.match import propose_known_names

    author, other_author, individual, other_individual = uuid7(), uuid7(), uuid7(), uuid7()
    a, b = uuid7(), uuid7()
    conn = SimpleNamespace(fetch=AsyncMock(return_value=[
        {"id": a, "attrs": '{"known_name":"  Kaju  "}', "author": author},
        {"id": b, "attrs": {"known_name": "Kaju"}, "author": other_author},
        {"id": uuid7(), "attrs": {}, "author": author},
        {"id": uuid7(), "attrs": {"known_name": "   "}, "author": author},
        {"id": uuid7(), "attrs": {"known_name": 42}, "author": author},
    ]), execute=AsyncMock())
    await propose_known_names(conn, individual, [a, b])
    first = conn.execute.call_args_list[:]
    assert len(first) == 2
    assert first[0].args[1] != first[1].args[1]
    assert first[0].args[2:] == (individual, "Kaju", author)
    assert first[1].args[2:] == (individual, "Kaju", other_author)
    assert all("'proposed'" in call.args[0] and "ON CONFLICT (id) DO NOTHING" in call.args[0] for call in first)
    assert all("UPDATE" not in call.args[0] and "resolved_by" not in call.args[0] for call in first)
    query = conn.fetch.call_args.args[0]
    assert "COALESCE(c.observer_id,s.observer_id) AS author" in query
    await propose_known_names(conn, individual, [a, b])
    assert conn.execute.call_args_list[2:] == first
    conn.execute.reset_mock()
    await propose_known_names(conn, other_individual, [a, b])
    assert conn.execute.call_args_list[0].args[1] != first[0].args[1]


@pytest.mark.parametrize("verdict", ["same", "different"])
async def test_only_explicit_same_verdict_appends_names(monkeypatch, verdict):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from app import matching
    from app.routes import match

    author, a, b, pid = uuid7(), uuid7(), uuid7(), uuid7()
    transaction = MagicMock()
    transaction.return_value.__aenter__ = AsyncMock()
    transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    conn = SimpleNamespace(transaction=transaction, execute=AsyncMock(), fetchval=AsyncMock(return_value=0),
        fetchrow=AsyncMock(side_effect=[{"sighting_id": a, "candidate_sighting_id": b,
            "candidate_individual_id": None, "status": "pending", "sighting_observer": author,
            "candidate_observer": author}, {"status": "pending"}]),
        fetch=AsyncMock(return_value=[{"id": sid, "individual_id": None, "observer_id": author,
            "review_status": "valid"} for sid in [a, b]]))
    monkeypatch.setattr(matching, "lock_matching", AsyncMock())
    monkeypatch.setattr(matching, "ensure_compatible_observations", AsyncMock())
    append = AsyncMock()
    monkeypatch.setattr(match, "propose_known_names", append)
    result = await match.resolve_proposal(pid, verdict, author, conn)
    if verdict == "same":
        assert result["individual_id"]
        assert append.await_args.args[2] == [a, b]
        assert append.await_args.args[1] is not None
    else:
        assert result["individual_id"] is None
        append.assert_not_awaited()
        assert all("INSERT INTO individuals" not in call.args[0] for call in conn.execute.call_args_list)


async def test_db_confirmation_proposes_names_without_overwriting_canonical_or_history(migrated_db):
    from uuid import UUID
    from fastapi import HTTPException
    from app.routes.match import resolve_proposal
    conn = migrated_db
    author, a, b, c, pid, second_pid = [uuid7() for _ in range(6)]
    await conn.execute("INSERT INTO observers(id) VALUES($1)", author)
    for sid in [a, b, c]:
        await conn.execute("""INSERT INTO sightings(id,observer_id,captured_at,attrs)
            VALUES($1,$2,now(),'{"known_name":"Kaju"}')""", sid, author)
    async def proposal(id, left, right):
        await conn.execute("""INSERT INTO match_proposals(id,sighting_id,candidate_sighting_id,score,method,status)
            VALUES($1,$2,$3,0.5,'test','pending')""", id, left, right)
    await proposal(pid, a, b)
    assert await conn.fetchval("SELECT count(*) FROM individuals") == 0
    assert await conn.fetchval("SELECT count(*) FROM individual_names") == 0
    response = await resolve_proposal(pid, "same", author, conn)
    individual = UUID(response["individual_id"])
    names = await conn.fetch("SELECT * FROM individual_names ORDER BY id")
    assert len(names) == 2
    assert all(r["status"] == "proposed" and r["name"] == "Kaju" and r["proposed_by"] == author
        and r["resolved_by"] is None for r in names)
    assert await conn.fetchval("SELECT name FROM individuals WHERE id=$1", individual) is None
    with pytest.raises(HTTPException) as exc:
        await resolve_proposal(pid, "same", author, conn)
    assert exc.value.status_code == 409
    active = uuid7()
    await conn.execute("""INSERT INTO individual_names(id,individual_id,name,proposed_by,status,resolved_by,resolved_at)
        VALUES($1,$2,'Original',$3,'active',$3,now())""", active, individual, author)
    await conn.execute("UPDATE individuals SET name='Original',named_by=$2,named_at=now() WHERE id=$1", individual, author)
    canonical = await conn.fetchrow("SELECT name,named_by,named_at FROM individuals WHERE id=$1", individual)
    await proposal(second_pid, c, a)
    await resolve_proposal(second_pid, "same", author, conn)
    assert await conn.fetchrow("SELECT name,named_by,named_at FROM individuals WHERE id=$1", individual) == canonical
    assert await conn.fetchval("SELECT count(*) FROM individual_names") == 4
    assert await conn.fetch("SELECT * FROM individual_names WHERE id=ANY($1::uuid[]) ORDER BY id", [r["id"] for r in names]) == names
    assert await conn.fetchval("SELECT status FROM individual_names WHERE id=$1", active) == "active"
