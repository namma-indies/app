"""The local model as a second screening signal (#181, app/model_screening.py).

It can only add a hold. An upload the detector cleared waits, hidden, for the
model's look, at most `screening_model_wait_s`; after that it shows on the
detector's verdict, and a later hold from the model still takes it down.
"""
import json
from uuid import UUID

import pytest

from app import model_screening
from app.config import settings
from app.scene import SceneSignals
from tests.test_blocking import _moderator
from tests.test_dogs_route import _post, _second_observer
from tests.test_erasure import _as, _mentions, _pool
from tests.test_screening import _capture_of, _detector, _state

GPU = {"Authorization": "Bearer gpu-secret"}


@pytest.fixture
def model_on(screening_on, monkeypatch):
    for name, value in (("screening_trusted_after", 0), ("screening_model_enabled", True),
                        ("suggestions_enabled", True), ("suggest_via", "worker"),
                        ("media_gpu_token", "gpu-secret"), ("media_cpu_fallback_enabled", False)):
        monkeypatch.setattr(settings, name, value)
    _detector(monkeypatch, SceneSignals())        # nothing the detector would hold


async def _upload(client, monkeypatch, **attrs):
    """Upload on the inline path, then let the worker route answer (it needs
    media jobs on, which the inline path needs off)."""
    monkeypatch.setattr(settings, "media_jobs_enabled", False)
    sid = await _post(client, **attrs)
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    return sid


async def _claim(client):
    r = await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)
    assert r.status_code == 200, r.text
    return r.json()["job"]


async def _answer(client, job, flags):
    content = flags if isinstance(flags, str) else json.dumps({"flags": flags})
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU,
                          json={"lease_token": job["lease_token"], "content": content})
    assert r.status_code == 200, r.text


async def _on_map(client, viewer, sid):
    _as(client, viewer)
    return _mentions((await client.get("/map")).json()["sightings"], sid)


@pytest.mark.asyncio
async def test_the_model_looks_before_anyone_else_sees_it_and_can_hold_it(authed_client, model_on, monkeypatch):
    client, owner = authed_client
    viewer, mod = await _second_observer(client, "Viewer"), await _moderator(client)
    sid = await _upload(client, monkeypatch, note="sleeps by the bakery")
    assert (await _state(await _pool(client), sid))["screening"] == "checking"
    assert not await _on_map(client, viewer, sid)
    _as(client, owner)
    assert (await client.get("/dex")).json()["sightings"][0]["off_map_reason"] == "checking"

    job = await _claim(client)
    assert job["kind"] == "screen"
    content = job["request"]["messages"][1]["content"]
    assert [p["type"] for p in content].count("image_url") == 1
    assert "sleeps by the bakery" in content[-1]["text"]         # the typed text is checked too
    assert job["request"]["response_format"]["json_schema"]["schema"]["properties"]["flags"]["items"]["enum"] == \
        list(model_screening.FLAGS)

    await _answer(client, job, ["person", "graphic"])
    state = await _state(await _pool(client), sid)
    assert (state["screening"], list(state["hold_reasons"])) == ("held", ["model_graphic", "model_person"])
    assert not await _on_map(client, viewer, sid)
    _as(client, mod)
    (item,) = (await client.get("/moderation/held")).json()["items"]
    assert item["reasons"] == ["model_graphic", "model_person"]


@pytest.mark.asyncio
async def test_nothing_flagged_goes_public(authed_client, model_on, monkeypatch):
    client, _ = authed_client
    viewer = await _second_observer(client, "Viewer")
    sid = await _upload(client, monkeypatch)
    await _answer(client, await _claim(client), [])
    assert (await _state(await _pool(client), sid))["screening"] == "clear"
    assert await _on_map(client, viewer, sid)


@pytest.mark.asyncio
async def test_a_model_that_is_down_never_blanks_the_map(authed_client, model_on, monkeypatch):
    """Fail open: after the wait the detector's verdict stands; a late hold still counts."""
    client, owner = authed_client
    viewer = await _second_observer(client, "Viewer")
    sid = await _upload(client, monkeypatch)
    assert not await _on_map(client, viewer, sid)
    monkeypatch.setattr(settings, "screening_model_wait_s", 0)        # the model had its time
    assert await _on_map(client, viewer, sid)
    _as(client, owner)
    assert (await client.get("/dex")).json()["sightings"][0]["on_map"] is True
    await _answer(client, await _claim(client), ["sexual"])
    assert not await _on_map(client, viewer, sid)


@pytest.mark.asyncio
async def test_a_model_that_keeps_failing_leaves_the_detectors_verdict(authed_client, model_on, monkeypatch):
    client, _ = authed_client
    sid = await _upload(client, monkeypatch)
    pool = await _pool(client)
    for attempt in range(model_screening.MAX_ATTEMPTS):
        async with pool.acquire() as c:
            await c.execute("UPDATE upload_screens SET run_after=now()")
        job = await _claim(client)
        assert job is not None, f"attempt {attempt + 1} was not handed out"
        await _answer(client, job, "not json")
        expected = "clear" if attempt + 1 == model_screening.MAX_ATTEMPTS else "checking"
        assert (await _state(pool, sid))["screening"] == expected


@pytest.mark.asyncio
async def test_a_moderator_who_ruled_first_has_the_last_word(authed_client, model_on, monkeypatch):
    client, _ = authed_client
    mod = await _moderator(client)
    sid = await _upload(client, monkeypatch)
    job = await _claim(client)
    _as(client, mod)
    capture = await _capture_of(await _pool(client), sid)
    assert (await client.post(f"/moderation/capture/{capture}/screening", json={"verdict": "approve"})).status_code == 200
    await _answer(client, job, ["person"])
    assert (await _state(await _pool(client), sid))["screening"] == "approved"


@pytest.mark.asyncio
async def test_the_detector_holding_it_means_no_model_look(authed_client, model_on, monkeypatch):
    _detector(monkeypatch, SceneSignals(person=0.9, person_area=0.5))
    client, _ = authed_client
    sid = await _upload(client, monkeypatch)
    assert (await _state(await _pool(client), sid))["screening"] == "held"
    assert await _claim(client) is None


@pytest.mark.parametrize("setting, value", [("screening_model_enabled", False), ("suggest_via", "api")])
@pytest.mark.asyncio
async def test_off_unless_on_the_local_model(authed_client, model_on, monkeypatch, setting, value):
    monkeypatch.setattr(settings, setting, value)
    client, _ = authed_client
    sid = await _upload(client, monkeypatch)
    assert (await _state(await _pool(client), sid))["screening"] == "clear"


@pytest.mark.parametrize("content", ['{"flags": ["fine"]}', '{"flags": "person"}', "[]", "x" * 5000])
def test_an_answer_outside_the_agreed_shape_is_refused(content):
    with pytest.raises((ValueError, KeyError, TypeError)):
        model_screening.check_answer(content)


def test_the_prompt_names_every_flag():
    for flag in model_screening.FLAGS:
        assert f"- {flag}:" in model_screening.PROMPT


@pytest.mark.asyncio
async def test_nobody_can_reclaim_a_job_while_its_answer_is_being_written(authed_client, model_on, monkeypatch):
    """The lease is checked and the answer written in one transaction on the
    job's row: a second worker asking for work in between, even with the
    first lease run out, gets nothing."""
    client, _ = authed_client
    sid = await _upload(client, monkeypatch)
    job = await _claim(client)
    pool = await _pool(client)
    settle, reclaimed = model_screening._settle, []

    async with pool.acquire() as c:          # the first worker's lease is about to run out
        await c.execute("UPDATE upload_screens SET lease_until = clock_timestamp() + interval '1 second'")

    async def settle_after_a_reclaim(conn, capture_id, reasons):
        import asyncio
        await asyncio.sleep(1.3)             # the lease has now run out, mid-completion
        async with pool.acquire() as other:
            reclaimed.append(await model_screening._claim(other, 60, "second-worker-" + "x" * 30))
        await settle(conn, capture_id, reasons)
    monkeypatch.setattr(model_screening, "_settle", settle_after_a_reclaim)
    await _answer(client, job, ["person"])
    assert reclaimed == [None], "another worker took the job while its answer was being written"
    assert (await _state(pool, sid))["screening"] == "held"


@pytest.mark.asyncio
async def test_an_answer_on_a_lease_since_passed_to_another_worker_is_refused(authed_client, model_on, monkeypatch):
    client, _ = authed_client
    sid = await _upload(client, monkeypatch)
    first = await _claim(client)
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute("UPDATE upload_screens SET lease_until = now() - interval '1 second'")
    second = await _claim(client)
    assert second["id"] == first["id"] and second["lease_token"] != first["lease_token"]
    for path, body in (("complete", {"content": json.dumps({"flags": ["sexual"]})}),
                       ("fail", {"error": "late", "retryable": False})):
        r = await client.post(f"/internal/suggestion-jobs/{first['id']}/{path}", headers=GPU,
                              json={"lease_token": first["lease_token"], **body})
        assert r.status_code == 409, path
    assert (await _state(pool, sid))["screening"] == "checking"
    await _answer(client, second, [])
    assert (await _state(pool, sid))["screening"] == "clear"


@pytest.mark.asyncio
async def test_two_uploads_waiting_on_the_model_still_pair_and_show_once_both_clear(authed_client, model_on,
                                                                                      monkeypatch):
    from app.embed import EMBED_DIM, MODEL_NAME
    from app.ids import uuid7
    from app.matching import resolve_sighting
    from tests.test_screening import _capture_of
    client, owner = authed_client
    other, viewer = await _second_observer(client, "Other"), await _second_observer(client, "Viewer")
    first = await _upload(client, monkeypatch)
    _as(client, other)
    second = await _upload(client, monkeypatch)
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute("UPDATE photos SET content_sha256 = NULL")       # two photos, not one file twice
        for sid in (first, second):
            assert await c.fetchval("SELECT screening FROM captures WHERE id=$1",
                                    await _capture_of(pool, sid)) == "checking"
            await c.execute(f"""INSERT INTO embeddings (id, photo_id, model, dim, vec_miew)
                                SELECT $1, id, $2, {EMBED_DIM}, array_fill(0.1::real, ARRAY[{EMBED_DIM}])::vector
                                FROM photos WHERE sighting_id=$3""", uuid7(), MODEL_NAME, UUID(sid))
        outcome = await resolve_sighting(c, UUID(second), auto_merge_min=1.01, propose_min=0.5, radius_m=1000.0,
                                         max_candidates=5, new_uuid=uuid7)
    assert outcome.status == "proposed"
    _as(client, viewer)
    assert not _mentions((await client.get("/proposals")).json()["proposals"], first)
    await _answer(client, await _claim(client), [])
    await _answer(client, await _claim(client), [])
    _as(client, viewer)
    assert _mentions((await client.get("/proposals")).json()["proposals"], first)
