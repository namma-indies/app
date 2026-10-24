"""Suggested details from a transcript: proposed by a model, decided by a person."""
import io
import json
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from app import suggestions
from app.config import settings
from app.ids import uuid7
from app.transcription_jobs import MODEL_NAME, PIPELINE_VERSION

HEARD = "She is asleep near the tea stall. Her name is Kaalu. Her left ear has a notch."


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (400, 300), (100, 140, 90)).save(b, "JPEG")
    return b.getvalue()


# ---------------------------------------------------------------------------
# validate: the model's output is evidence-checked before anyone sees it
# ---------------------------------------------------------------------------

def test_keeps_supported_suggestions():
    kept = suggestions.validate([
        {"field": "sex", "value": "Female", "quote": "She is asleep"},
        {"field": "known_name", "value": " Kaalu ", "quote": "Her name is Kaalu."},
        {"field": "ear_notch", "value": "left", "quote": "her LEFT ear  has a notch"},
    ], HEARD)
    assert kept == [
        {"field": "sex", "value": "female", "quote": "She is asleep", "animal": None},
        {"field": "known_name", "value": "Kaalu", "quote": "Her name is Kaalu.", "animal": None},
        {"field": "ear_notch", "value": "left", "quote": "her LEFT ear  has a notch", "animal": None},
    ]


@pytest.mark.parametrize("item", [
    {"field": "condition", "value": "injured", "quote": "he is limping"},   # not said
    {"field": "sex", "value": "unsure", "quote": "She is asleep"},          # not a suggestion
    {"field": "sex", "value": "neutered", "quote": "She is asleep"},        # not an option
    {"field": "colour", "value": "brown", "quote": "She is asleep"},        # not a field
    {"field": "known_name", "value": "Kaa\nlu", "quote": "Kaalu"},          # not one line
    {"field": "sex", "value": "female", "quote": ""},                       # no evidence
    "female",
])
def test_drops_what_the_note_does_not_support(item):
    assert suggestions.validate([item], HEARD) == []


def test_one_suggestion_per_field_first_wins():
    kept = suggestions.validate([
        {"field": "sex", "value": "female", "quote": "She is asleep"},
        {"field": "sex", "value": "male", "quote": "She is asleep"},
    ], HEARD)
    assert [k["value"] for k in kept] == ["female"]


def test_quotes_match_in_any_script():
    kannada = "ಇವಳ ಹೆಸರು ಕಾಳು. ಅವಳು ಮಲಗಿದ್ದಾಳೆ."
    kept = suggestions.validate([{"field": "known_name", "value": "ಕಾಳು", "quote": "ಇವಳ ಹೆಸರು ಕಾಳು"}], kannada)
    assert kept[0]["value"] == "ಕಾಳು"


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------

def model_reply(items):
    return {"choices": [{"message": {"content": json.dumps({"suggestions": items})}}]}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "suggest_api_key", "sk-test")
    monkeypatch.setattr(settings, "suggest_model", "test-model")
    monkeypatch.setattr(settings, "suggest_base_url", "https://llm.example/v1")


async def test_asks_for_strict_structured_output(configured):
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, json=model_reply([{"field": "sex", "value": "female", "quote": "She"}]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        got = await suggestions.ask_model(client, HEARD, "en")
    assert got == [{"field": "sex", "value": "female", "quote": "She"}]
    request = seen[0]
    assert str(request.url) == "https://llm.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert HEARD in body["messages"][1]["content"][0]["text"]
    assert body["reasoning_effort"] == "low"
    assert "animal" not in body["response_format"]["json_schema"]["schema"]["properties"][
        "suggestions"]["items"]["properties"]


# ---------------------------------------------------------------------------
# End to end: transcript -> suggestion -> the observer's decision
# ---------------------------------------------------------------------------

async def _sighting_with_transcript(client, text=HEARD):
    r = await client.post("/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z"})
    assert r.status_code == 201, r.text
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO voice_transcripts (id, capture_id, source, pipeline_version, model,
                   outcome, language, text, segments)
               VALUES ($1,$2,'note',$3,$4,'transcribed','en',$5,'[]')""",
            uuid7(), sid, PIPELINE_VERSION, MODEL_NAME, text)
    return sid, pool


def answering(items=None, status=200):
    async def handler(request):
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json=model_reply(items))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


MODEL_SAYS = [
    {"field": "sex", "value": "female", "quote": "She is asleep"},
    {"field": "known_name", "value": "Kaalu", "quote": "Her name is Kaalu"},
    {"field": "condition", "value": "injured", "quote": "she is bleeding"},  # invented
]


@pytest.mark.asyncio
async def test_suggestions_wait_for_the_observer(authed_client, configured):
    client, _ = authed_client
    sid, pool = await _sighting_with_transcript(client)
    async with answering(MODEL_SAYS) as llm:
        assert await suggestions.run_once(pool, None, llm)
        assert not await suggestions.run_once(pool, None, llm)

    item = (await client.get("/dex")).json()["sightings"][0]
    offered = {s["field"]: s for s in item["suggestions"]}
    assert set(offered) == {"sex", "known_name"}          # the invented one never shows
    assert "sex" not in item["attrs"]                       # nothing written yet

    r = await client.post(f"/suggestion/{offered['sex']['id']}", json={"verdict": "choose", "value": "female"})
    assert r.json() == {"status": "accepted", "field": "sex", "value": "female"}
    r = await client.post(f"/suggestion/{offered['known_name']['id']}", json={"verdict": "dismiss"})
    assert r.json()["status"] == "dismissed"

    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["attrs"]["sex"] == "female" and "known_name" not in item["attrs"]
    assert item["suggestions"] == []
    again = await client.post(f"/suggestion/{offered['sex']['id']}", json={"verdict": "dismiss"})
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_a_correction_is_recorded_as_one(authed_client, configured):
    client, _ = authed_client
    sid, pool = await _sighting_with_transcript(client)
    async with answering(MODEL_SAYS[:1]) as llm:
        await suggestions.run_once(pool, None, llm)
    sug = (await client.get("/dex")).json()["sightings"][0]["suggestions"][0]
    r = await client.post(f"/suggestion/{sug['id']}", json={"verdict": "choose", "value": "male"})
    assert r.json()["status"] == "corrected"
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT value, decided_value FROM detail_suggestions WHERE id=$1", sug["id"])
    assert (row["value"], row["decided_value"]) == ("female", "male")
    bad = await client.post(f"/suggestion/{sug['id']}", json={"verdict": "choose", "value": "neutered"})
    assert bad.status_code == 409  # already decided; checked before the value


@pytest.mark.asyncio
async def test_an_invalid_choice_is_refused(authed_client, configured):
    client, _ = authed_client
    sid, pool = await _sighting_with_transcript(client)
    async with answering(MODEL_SAYS[:1]) as llm:
        await suggestions.run_once(pool, None, llm)
    sug = (await client.get("/dex")).json()["sightings"][0]["suggestions"][0]
    r = await client.post(f"/suggestion/{sug['id']}", json={"verdict": "choose", "value": "neutered"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_detail_the_observer_already_chose_is_not_second_guessed(authed_client, configured):
    client, _ = authed_client
    r = await client.post("/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z", "sex": "male"})
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO voice_transcripts (id, capture_id, source, pipeline_version, model,
                   outcome, language, text, segments)
               VALUES ($1,$2,'note',$3,$4,'transcribed','en',$5,'[]')""",
            uuid7(), sid, PIPELINE_VERSION, MODEL_NAME, HEARD)
    async with answering(MODEL_SAYS[:1]) as llm:
        await suggestions.run_once(pool, None, llm)
    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["suggestions"] == [] and item["attrs"]["sex"] == "male"


@pytest.mark.asyncio
async def test_only_the_owner_decides(authed_client, configured):
    client, _ = authed_client
    sid, pool = await _sighting_with_transcript(client)
    async with answering(MODEL_SAYS[:1]) as llm:
        await suggestions.run_once(pool, None, llm)
    sug = (await client.get("/dex")).json()["sightings"][0]["suggestions"][0]
    async with pool.acquire() as c:
        stranger = uuid4()
        await c.execute("INSERT INTO observers(id) VALUES($1)", stranger)
        await c.execute("UPDATE captures SET observer_id=$1 WHERE id=$2", stranger, sid)
    r = await client.post(f"/suggestion/{sug['id']}", json={"verdict": "dismiss"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_a_failing_model_backs_off_then_gives_up(authed_client, configured):
    client, _ = authed_client
    sid, pool = await _sighting_with_transcript(client)
    async with answering(status=503) as llm:
        assert await suggestions.run_once(pool, None, llm)
        assert not await suggestions.run_once(pool, None, llm)  # backed off
        for _ in range(suggestions.MAX_ATTEMPTS - 1):
            async with pool.acquire() as c:
                await c.execute("UPDATE voice_transcripts SET suggest_run_after=now()")
            assert await suggestions.run_once(pool, None, llm)
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT suggest_status, suggest_error FROM voice_transcripts")
    assert row["suggest_status"] == "failed" and row["suggest_error"] == "HTTPStatusError"


def test_with_several_animals_each_suggestion_must_name_one():
    heard = "The brown one is Kaalu. The black one is limping. She is friendly."
    kept = suggestions.validate([
        {"field": "known_name", "value": "Kaalu", "quote": "The brown one is Kaalu", "animal": "A"},
        {"field": "condition", "value": "injured", "quote": "The black one is limping", "animal": "B"},
        {"field": "sex", "value": "female", "quote": "She is friendly", "animal": "unclear"},
        {"field": "sex", "value": "female", "quote": "She is friendly", "animal": "C"},
    ], heard, ["A", "B"])
    assert [(k["field"], k["animal"]) for k in kept] == [("known_name", "A"), ("condition", "B")]


async def test_shows_the_model_one_photo_per_animal(configured):
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=model_reply([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await suggestions.ask_model(client, HEARD, "en", [("A", b"webp-a"), ("B", b"webp-b")])
    body = seen[0]
    parts = body["messages"][1]["content"]
    images = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert images == ["data:image/webp;base64,d2VicC1h", "data:image/webp;base64,d2VicC1i"]
    assert [p["text"] for p in parts if p["type"] == "text"][1:] == ["Animal A:", "Animal B:"]
    item = body["response_format"]["json_schema"]["schema"]["properties"]["suggestions"]["items"]
    assert item["properties"]["animal"]["enum"] == ["A", "B", "unclear"]
    assert "animal" in item["required"]
    assert "which animal" in body["messages"][0]["content"]


async def _two_dog_capture(pool, observer_id, state="ready"):
    """A capture with two tracked dogs, their evidence thumbnails in storage."""
    from app.deps import get_storage
    from app.photos import thumb_key
    storage = get_storage()
    cid = uuid7()
    instances = []
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO captures (id, observer_id, kind, processing_state, captured_at, attrs)
               VALUES ($1,$2,'photo',$3,now(),'{}')""", cid, observer_id, state)
        source = uuid7()
        await c.execute("INSERT INTO photos (id, capture_id, s3_key, width, height) VALUES ($1,$2,$3,400,300)",
                        source, cid, f"captures/{cid}/source.webp")
        for track, colour in (("t0", b"brown"), ("t1", b"black")):
            evidence, instance = uuid7(), uuid7()
            key = f"captures/{cid}/evidence-{track}.webp"
            await storage.put(thumb_key(key), b"thumb-of-" + colour, "image/webp")
            await c.execute("INSERT INTO photos (id, capture_id, s3_key, width, height) VALUES ($1,$2,$3,100,100)",
                            evidence, cid, key)
            await c.execute(
                """INSERT INTO animal_instances (id, capture_id, source_photo_id, evidence_photo_id,
                       track_id, species, confidence, bbox, crop_bbox)
                   VALUES ($1,$2,$3,$4,$5,'dog',0.9,'[0,0,10,10]','[0,0,10,10]')""",
                instance, cid, source, evidence, track)
            instances.append(instance)
        await c.execute(
            """INSERT INTO voice_transcripts (id, capture_id, source, pipeline_version, model,
                   outcome, language, text, segments)
               VALUES ($1,$2,'note',$3,$4,'transcribed','en',$5,'[]')""",
            uuid7(), cid, PIPELINE_VERSION, MODEL_NAME,
            "The brown one is Kaalu. The black one is limping.")
    return cid, instances, storage


@pytest.mark.asyncio
async def test_a_multi_animal_capture_gets_suggestions_per_animal(authed_client, configured):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, (brown, black), storage = await _two_dog_capture(pool, oid)
    sent = []

    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=model_reply([
            {"field": "known_name", "value": "Kaalu", "quote": "The brown one is Kaalu", "animal": "A"},
            {"field": "condition", "value": "injured", "quote": "The black one is limping", "animal": "B"},
        ]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as llm:
        assert await suggestions.run_once(pool, storage, llm)
    images = [p["image_url"]["url"] for p in sent[0]["messages"][1]["content"] if p["type"] == "image_url"]
    assert len(images) == 2

    detail = (await client.get(f"/capture/{cid}")).json()
    by_instance = {s["instance_id"]: (s["field"], s["value"]) for s in detail["suggestions"]}
    assert by_instance == {str(brown): ("known_name", "Kaalu"), str(black): ("condition", "injured")}
    # Not on the Journal's /sighting surface: the review sheet is where these are answered.
    assert all(not s["suggestions"] for s in (await client.get("/dex")).json()["sightings"])

    # Answering only records it; the review save is what wrote the value.
    sug = next(s for s in detail["suggestions"] if s["field"] == "condition")
    r = await client.post(f"/suggestion/{sug['id']}", json={"verdict": "choose", "value": "injured"})
    assert r.json()["status"] == "accepted"
    assert len((await client.get(f"/capture/{cid}")).json()["suggestions"]) == 1


@pytest.mark.asyncio
async def test_waits_while_the_animals_are_still_being_found(authed_client, configured):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, _, storage = await _two_dog_capture(pool, oid, state="processing")
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json=model_reply([]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as llm:
        assert await suggestions.run_once(pool, storage, llm)
    assert not calls
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT suggest_status, suggest_attempts, suggest_run_after > now() AS later "
                               "FROM voice_transcripts WHERE capture_id=$1", cid)
    assert (row["suggest_status"], row["suggest_attempts"], row["later"]) == ("pending", 0, True)


@pytest.mark.asyncio
async def test_a_capture_with_no_animal_is_skipped(authed_client, configured):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, _, storage = await _two_dog_capture(pool, oid, state="no_animal")
    async with answering([]) as llm:
        assert await suggestions.run_once(pool, storage, llm)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT suggest_status FROM voice_transcripts") == "skipped"


# ---------------------------------------------------------------------------
# The DGX path: the worker pulls a built request and returns the raw answer
# ---------------------------------------------------------------------------

GPU = {"Authorization": "Bearer gpu-secret"}


@pytest.fixture
def on_worker(monkeypatch):
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    monkeypatch.setattr(settings, "media_gpu_token", "gpu-secret")
    monkeypatch.setattr(settings, "suggestions_enabled", True)
    monkeypatch.setattr(settings, "suggest_via", "worker")


async def _claim(client):
    r = await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)
    assert r.status_code == 200, r.text
    return r.json()["job"]


@pytest.mark.asyncio
async def test_the_worker_gets_a_ready_request_and_its_answer_is_checked_here(authed_client, on_worker):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, (brown, black), _ = await _two_dog_capture(pool, oid)
    job = await _claim(client)
    request = job["request"]
    images = [p["image_url"]["url"] for p in request["messages"][1]["content"] if p["type"] == "image_url"]
    assert len(images) == 2 and all("evidence-" in url and url.startswith("http") for url in images)
    assert "model" not in request  # the worker names its own

    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/heartbeat",
                          json={"lease_token": job["lease_token"]}, headers=GPU)
    assert r.status_code == 200
    answer = json.dumps({"suggestions": [
        {"field": "known_name", "value": "Kaalu", "quote": "The brown one is Kaalu", "animal": "A"},
        {"field": "condition", "value": "injured", "quote": "is bleeding", "animal": "B"},  # not said
    ]})
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete",
                          json={"lease_token": job["lease_token"], "content": answer}, headers=GPU)
    assert r.json() == {"status": "done"}
    offered = (await client.get(f"/capture/{cid}")).json()["suggestions"]
    assert [(s["instance_id"], s["field"]) for s in offered] == [(str(brown), "known_name")]
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT model FROM detail_suggestions") == settings.suggest_local_model
    again = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete",
                              json={"lease_token": job["lease_token"], "content": answer}, headers=GPU)
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_a_garbled_answer_is_a_failed_attempt_not_a_suggestion(authed_client, on_worker):
    client, _ = authed_client
    sid, pool = await _sighting_with_transcript(client)
    job = await _claim(client)
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete",
                          json={"lease_token": job["lease_token"], "content": "Sure! Here are the details:"},
                          headers=GPU)
    assert r.json() == {"status": "done"}
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT suggest_status, suggest_error FROM voice_transcripts")
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions") == 0
    assert (row["suggest_status"], row["suggest_error"]) == ("pending", "unparsable_reply")


@pytest.mark.asyncio
async def test_the_worker_queue_is_closed_on_the_openai_path(authed_client, on_worker, monkeypatch):
    client, _ = authed_client
    await _sighting_with_transcript(client)
    monkeypatch.setattr(settings, "suggest_via", "api")
    assert await _claim(client) is None


@pytest.mark.asyncio
async def test_a_stale_worker_lease_is_refused(authed_client, on_worker):
    client, _ = authed_client
    await _sighting_with_transcript(client)
    job = await _claim(client)
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete",
                          json={"lease_token": "w" * 43, "content": "{}"}, headers=GPU)
    assert r.status_code == 409
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/fail",
                          json={"lease_token": job["lease_token"], "error": "processing_timeout"}, headers=GPU)
    assert r.json() == {"status": "failed"}


@pytest.mark.asyncio
async def test_the_worker_queue_requires_the_worker_token(authed_client, on_worker):
    client, _ = authed_client
    r = await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"})
    assert r.status_code == 401


def test_a_name_must_appear_in_its_own_quote():
    """Seen on a real Qwen3.8 run: "Kaaluu" from "Her name is Kaalu"."""
    assert suggestions.validate(
        [{"field": "known_name", "value": "Kaaluu", "quote": "Her name is Kaalu"}], HEARD) == []
