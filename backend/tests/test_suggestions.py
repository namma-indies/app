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
        {"field": "known_name", "value": "Kaalu", "quote": "Her name is Kaalu", "animal": None},
        {"field": "ear_notch", "value": "left", "quote": "Her left ear has a notch", "animal": None},
    ]  # the quote stored is the note's own words (#115): the Journal highlights it literally


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


HEARD_PUNCTUATED = "She is a female. Her name is Kaalu, her left ear is notched. He\u2019s calm."


@pytest.mark.parametrize("field,value,quote", [
    ("known_name", "Kaalu", "Her name is Kaalu her left ear is notched"),  # a comma skipped
    ("sex", "female", "she is a female her name"),                           # a full stop skipped
    ("sex", "male", "He's calm"),                                            # straight for curly
    ("sex", "female", "SHE IS A FEMALE"),
])
def test_punctuation_is_not_part_of_what_was_said(field, value, quote):
    assert [s["field"] for s in suggestions.validate(
        [{"field": field, "value": value, "quote": quote}], HEARD_PUNCTUATED)] == [field]


@pytest.mark.parametrize("quote,stored", [
    ("Her name is Kaalu her left ear is notched", "Her name is Kaalu, her left ear is notched"),
    ("she is a female her name", "She is a female. Her name"),
    ("HE\u2019S CALM", "He\u2019s calm"),
    ("He's calm", "He\u2019s calm"),
])
def test_what_is_stored_is_the_notes_own_words_so_the_journal_can_highlight_it(quote, stored):
    (kept,) = suggestions.validate([{"field": "sex", "value": "female", "quote": quote}], HEARD_PUNCTUATED)
    assert kept["quote"] == stored
    # Transcript.tsx finds a quote by case-insensitive match with whitespace runs equal;
    # a literal substring of the transcript always satisfies that.
    assert stored.casefold() in HEARD_PUNCTUATED.casefold()


def test_a_quote_marks_the_note_not_what_follows_it():
    # Trailing punctuation the model added is not part of the span, and zero-width joiners it added vanish.
    (kept,) = suggestions.validate([{"field": "sex", "value": "female", "quote": "She is a female!!!\u200d"}], HEARD_PUNCTUATED)
    assert kept["quote"] == "She is a female"


@pytest.mark.parametrize("quote", ["...", "  ,  ", "\u200d", "She is a male", "female she is"])
def test_a_quote_still_has_to_be_words_that_were_said_in_that_order(quote):
    assert suggestions.validate([{"field": "sex", "value": "female", "quote": quote}], HEARD_PUNCTUATED) == []


def test_indic_vowel_signs_and_joiners_survive_the_comparison():
    heard = "ಇವಳ ಹೆಸರು ಕಾಳು, ಅವಳು ಮಲಗಿದ್ದಾಳೆ।"
    # Dropping the comma and the danda still matches; a zero-width joiner the model adds is ignored.
    kept = suggestions.validate([
        {"field": "known_name", "value": "ಕಾಳು", "quote": "ಹೆಸರು ಕಾಳು ಅವಳು"},
        {"field": "sex", "value": "female", "quote": "ಅವಳು ಮ\u200dಲಗಿದ್ದಾಳೆ"}], heard)
    assert [k["field"] for k in kept] == ["known_name", "sex"]
    # A different vowel sign is a different word.
    assert suggestions.validate([{"field": "known_name", "value": "ಕಾಳು", "quote": "ಕಾಳ"}], "ಕಾಳು") == []


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


async def _two_dog_capture(pool, observer_id, state="ready", dogs=2,
                           text="The brown one is Kaalu. The black one is limping."):
    """A capture with tracked dogs (two unless `dogs`), their evidence thumbnails in storage."""
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
        for track, colour in (("t0", b"brown"), ("t1", b"black"))[:dogs]:
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
            uuid7(), cid, PIPELINE_VERSION, MODEL_NAME, text)
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

    # The sheet previews what was heard, to highlight the words behind each (#112).
    assert detail["voice_transcripts"] == [
        {"source": "note", "language": "en", "text": "The brown one is Kaalu. The black one is limping."}]

    # Once published, the Journal lists each animal's own open suggestions,
    # to highlight and to point at the sheet, never to answer there.
    r = await client.post(f"/capture/{cid}/review", json={"revision": detail["revision"], "groups": [
        {"instance_ids": [str(brown)]}, {"instance_ids": [str(black)]}]})
    assert r.status_code == 200, r.text
    published = {str(i["instance_id"]): i["sighting_id"] for i in r.json()["instances"]}
    journal = {s["id"]: s for s in (await client.get("/dex")).json()["sightings"]}
    assert [(o["field"], o["quote"]) for o in journal[published[str(brown)]]["review_suggestions"]] == [
        ("known_name", "The brown one is Kaalu")]
    assert [o["field"] for o in journal[published[str(black)]]["review_suggestions"]] == ["condition"]
    assert all(not s["suggestions"] for s in journal.values())
    assert journal[published[str(brown)]]["voice_transcripts"][0]["text"].startswith("The brown one")
    detail = (await client.get(f"/capture/{cid}")).json()

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


# ---------------------------------------------------------------------------
# One dog: the model is not asked "which animal?" (#114)
# ---------------------------------------------------------------------------

ONE_DOG_NOTE = "She is a female, her name is Kaalu, her left ear is notched."
# What a model says about a note when nothing tells it which animal: no "animal" key at all.
ONE_DOG_ANSWER = [
    {"field": "sex", "value": "female", "quote": "She is a female"},
    {"field": "known_name", "value": "Kaalu", "quote": "her name is Kaalu"},
    {"field": "ear_notch", "value": "left", "quote": "her left ear is notched"},
]


async def _published_one_dog(client, oid):
    pool = client._transport.app.state.pool
    cid, (dog,), storage = await _two_dog_capture(pool, oid, dogs=1, text=ONE_DOG_NOTE)
    detail = (await client.get(f"/capture/{cid}")).json()
    r = await client.post(f"/capture/{cid}/review", json={
        "revision": detail["revision"], "groups": [{"instance_ids": [str(dog)]}]})
    assert r.status_code == 200, r.text
    return cid, dog, pool, storage


def _journal_entry(journal):
    (entry,) = journal["sightings"]
    return entry


@pytest.mark.asyncio
async def test_one_dog_on_the_worker_path_gets_a_note_only_request_and_a_review_button(authed_client, on_worker):
    client, oid = authed_client
    cid, dog, pool, _ = await _published_one_dog(client, oid)
    job = await _claim(client)
    request = job["request"]
    assert [p["type"] for p in request["messages"][1]["content"]] == ["text"]  # no photo to fetch
    assert "several animals" not in request["messages"][0]["content"]
    properties = request["response_format"]["json_schema"]["schema"]["properties"]["suggestions"]["items"]
    assert "animal" not in properties["properties"]

    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "content": json.dumps({"suggestions": ONE_DOG_ANSWER})})
    assert r.json() == {"status": "done"}

    entry = _journal_entry((await client.get("/dex")).json())
    # What the Journal needs for "Review 3 suggestions from your note".
    assert entry["capture_id"] == str(cid)
    assert sorted((o["field"], o["value"]) for o in entry["review_suggestions"]) == [
        ("ear_notch", "left"), ("known_name", "Kaalu"), ("sex", "female")]
    assert entry["voice_transcripts"][0]["text"] == ONE_DOG_NOTE
    # ... and the sheet it opens lists them under that dog.
    sheet = (await client.get(f"/capture/{cid}")).json()
    assert {s["instance_id"] for s in sheet["suggestions"]} == {str(dog)}


@pytest.mark.asyncio
async def test_one_dog_on_the_openai_path_sends_no_photo_either(authed_client, configured):
    client, oid = authed_client
    cid, dog, pool, storage = await _published_one_dog(client, oid)
    sent = []

    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=model_reply(ONE_DOG_ANSWER))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as llm:
        assert await suggestions.run_once(pool, storage, llm)
    assert [p["type"] for p in sent[0]["messages"][1]["content"]] == ["text"]
    assert len(_journal_entry((await client.get("/dex")).json())["review_suggestions"]) == 3


@pytest.mark.asyncio
async def test_one_dog_keeps_a_suggestion_the_model_attributes_to_nobody_in_particular(authed_client, on_worker):
    # A model that still answers with an animal key (an older prompt, a chatty reply) changes nothing:
    # with one animal it can only be that one.
    client, oid = authed_client
    cid, dog, pool, _ = await _published_one_dog(client, oid)
    job = await _claim(client)
    answer = [{**item, "animal": "unclear"} for item in ONE_DOG_ANSWER[:1]]
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "content": json.dumps({"suggestions": answer})})
    assert r.json() == {"status": "done"}
    assert [o["field"] for o in _journal_entry((await client.get("/dex")).json())["review_suggestions"]] == ["sex"]


@pytest.mark.asyncio
async def test_a_quote_with_the_commas_left_out_still_gives_a_suggestion(authed_client, on_worker):
    client, oid = authed_client
    _, _, pool, _ = await _published_one_dog(client, oid)
    job = await _claim(client)
    answer = [{"field": "known_name", "value": "Kaalu", "quote": "her name is Kaalu her left ear is notched"}]
    await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "content": json.dumps({"suggestions": answer})})
    assert [o["field"] for o in _journal_entry((await client.get("/dex")).json())["review_suggestions"]] == ["known_name"]


@pytest.mark.asyncio
async def test_several_dogs_are_still_asked_which_is_which(authed_client, on_worker):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    await _two_dog_capture(pool, oid)
    request = (await _claim(client))["request"]
    assert [p["type"] for p in request["messages"][1]["content"]].count("image_url") == 2
    assert "animal" in request["response_format"]["json_schema"]["schema"]["properties"]["suggestions"]["items"]["properties"]


def test_the_count_line_reaches_the_log_under_the_deployed_logging_setup():
    # A fresh interpreter has uvicorn's setup, which leaves `app.*` at the root's WARNING level
    # unless the app opens its own logger (app.main.configure_logging).
    import os, subprocess, sys
    from pathlib import Path
    backend = str(Path(__file__).resolve().parent.parent)
    code = ("import logging; import app.main; "
            "logging.getLogger('app.suggestions').info('suggestions for transcript=T: model gave 2, kept 1')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=backend,
                          env={**os.environ, "PYTHONPATH": backend}, timeout=60)
    assert "model gave 2, kept 1" in done.stderr, done.stderr[-500:]
