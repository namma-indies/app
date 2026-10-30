"""AI photo descriptions for sightings nobody described (#160)."""
import json
import logging
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app import describe
from app.config import settings
from app.ids import uuid7
from app.transcription_jobs import MODEL_NAME, PIPELINE_VERSION
from tests.test_suggestions import _jpeg, _two_dog_capture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import describe_backfill  # noqa: E402

GPU = {"Authorization": "Bearer gpu-secret"}
CLEAN = "A tan short-haired dog with a black muzzle and a red collar. Its left ear is notched."


# ---------------------------------------------------------------------------
# The privacy rule: the animal only, or nothing at all
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,rule", [
    ("A brown dog sleeping near the temple gate.", "denied_word"),
    ("A black dog beside a red Activa.", "proper_noun"),
    ("A black dog beside a red scooter.", "denied_word"),
    ("The sign behind it reads Ganesh Bakery.", "proper_noun"),
    ("the sign behind it reads ganesh bakery.", "denied_word"),
    ("A white dog with a man.", "denied_word"),
    ("A white dog lying outside a house.", "denied_word"),
    ("A brown dog outside house no. 12.", "digit"),
    ("A dog on the footpath by the bus stop.", "denied_word"),
    ("A tan dog at Indiranagar.", "proper_noun"),
    ("A tan dog by the shops on MG Road.", "proper_noun"),
    ("A dog sitting on a parked car.", "denied_word"),
    ("Two kids are feeding a brown dog.", "denied_word"),
    ("A dog in front of a board that says \"Sri Lakshmi\".", "quotes"),
    ("A dog under a banner.", "denied_word"),
    ("A brown dog. It is thin. It limps.", "too_long"),
    ("A black dog with ಕನ್ನಡ written behind it.", "script"),
    ("A dog by the ಅಂಗಡಿ.", "script"),
    ("A brown dog with a collar tag reading KA01.", "digit"),
    ("A dog lying across the lane, a rickshaw behind.", "denied_word"),
    ("A dog in the parking lot.", "denied_word"),
    ("A brown dog with its owner.", "denied_word"),
    ("A brown dog [near a gate].", "quotes"),
    # Words nobody thought to deny: only what describes an animal is allowed.
    ("Indiranagar has a tan dog.", "unlisted_word"),       # a place, where a capital is expected
    ("A tan dog walks with a pedestrian.", "unlisted_word"),
    ("A dog stands next to a taxi.", "unlisted_word"),
    ("A brown dog waiting by the chaiwala.", "unlisted_word"),
    ("A black dog lying in the dust by the drain.", "unlisted_word"),
    ("x" * 241, "too_long"),
    ("   ", "empty"),
])
def test_a_description_that_says_where_or_who_is_dropped_whole(text, rule):
    assert describe.privacy_problem(text) == rule


@pytest.mark.parametrize("text", [
    CLEAN,
    "A thin black-and-white dog with mange on its back.",
    "A large brown dog's coat is patchy and it has a wound on its right hind leg.",
    "A ginger cat with white paws and a tipped left ear.",
    "A healthy tan dog with a fluffy tail and white feet.",
    "A small black puppy with a blue collar.",
    "A brown dog with a limp.",
    "A tan dog with a wound on its left foreleg and ticks on its ears.",
])
def test_a_description_of_the_animal_alone_is_kept(text):
    assert describe.privacy_problem(text) is None


def test_the_prompt_forbids_what_the_validator_drops():
    for word in ("roads", "shops", "sign", "text", "person", "vehicle", "numbers", "quotation"):
        assert word in describe.PROMPT


def _answer(description, suggestions=()):
    return json.dumps({"description": description, "suggestions": list(suggestions)})


def test_a_dropped_description_takes_its_suggestions_with_it():
    description, dropped, kept = describe.check_answer(_answer(
        "A limping dog outside the bakery.", [{"field": "condition", "value": "injured", "quote": "limping dog"}]))
    assert (description, dropped, kept) == (None, "denied_word", [])


def test_a_photo_suggests_only_what_a_photo_can_show_and_quotes_the_description():
    description, dropped, kept = describe.check_answer(_answer(CLEAN, [
        {"field": "ear_notch", "value": "left", "quote": "Its left ear is notched"},
        {"field": "ear_notch", "value": "none", "quote": "Its left ear"},          # not from a photo
        {"field": "condition", "value": "injured", "quote": "a bleeding wound"},  # not in the description
        {"field": "sex", "value": "female", "quote": "A tan short-haired dog"},    # never from a photo
    ]))
    assert description == CLEAN and dropped is None
    assert [(s["field"], s["value"]) for s in kept] == [("ear_notch", "left")]


@pytest.mark.parametrize("content", ["", "Sure!", "{}", '{"description": 3, "suggestions": []}', None])
def test_an_answer_off_the_agreed_shape_is_refused(content):
    with pytest.raises((ValueError, KeyError, TypeError)):
        describe.check_answer(content)


def test_the_request_shows_the_photos_and_asks_for_strict_output():
    request = describe.build_request(["https://s/a.webp", "https://s/b.webp"], "cat")
    parts = request["messages"][1]["content"]
    assert parts[0]["text"] == "The animal is a cat. Photos of it:"
    assert [p["image_url"]["url"] for p in parts[1:]] == ["https://s/a.webp", "https://s/b.webp"]
    schema = request["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert list(schema["schema"]["properties"]) == ["description", "suggestions"]
    assert schema["schema"]["properties"]["suggestions"]["items"]["properties"]["field"]["enum"] == [
        "ear_notch", "condition"]


# ---------------------------------------------------------------------------
# End to end on the worker path
# ---------------------------------------------------------------------------

@pytest.fixture
def on(monkeypatch):
    for name, value in (("media_jobs_enabled", True), ("media_gpu_token", "gpu-secret"),
                        ("media_cpu_fallback_enabled", False), ("suggestions_enabled", True),
                        ("suggest_via", "worker"), ("describe_enabled", True), ("transcription_enabled", True)):
        monkeypatch.setattr(settings, name, value)


async def _sighting(client, note=None, audio=False):
    files = [("photos", ("d.jpg", _jpeg(), "image/jpeg"))]
    if audio:
        files.append(("audio", ("note.m4a", b"\x00\x00\x00\x18ftypM4A", "audio/mp4")))
    data = {"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z", **({"note": note} if note else {})}
    r = await client.post("/sighting", files=files, data=data)
    assert r.status_code == 201, r.text
    sid = r.json()["sighting_id"]
    async with client._transport.app.state.pool.acquire() as c:
        await c.execute("UPDATE sightings SET processing_state='ready' WHERE id=$1", sid)
    return sid


async def _claim(client):
    r = await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)
    assert r.status_code == 200, r.text
    return r.json()["job"]


async def _complete(client, job, content):
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU,
                          json={"lease_token": job["lease_token"], "content": content})
    assert r.status_code == 200, r.text


def _entry(journal, sid=None):
    items = journal["sightings"]
    return items[0] if sid is None else next(i for i in items if i["id"] == str(sid))


@pytest.mark.asyncio
async def test_a_sighting_with_no_note_is_described_and_its_owner_decides(authed_client, on):
    client, _ = authed_client
    sid = await _sighting(client)
    job = await _claim(client)
    assert job["kind"] == "describe"
    images = [p["image_url"]["url"] for p in job["request"]["messages"][1]["content"] if p["type"] == "image_url"]
    assert len(images) == 1 and "_thumb" in images[0]                    # the worker inlines it, as for notes
    hb = await client.post(f"/internal/suggestion-jobs/{job['id']}/heartbeat",
                           json={"lease_token": job["lease_token"]}, headers=GPU)
    assert hb.status_code == 200
    await _complete(client, job, _answer(CLEAN, [{"field": "ear_notch", "value": "left", "quote": "left ear is notched"}]))

    entry = _entry((await client.get("/dex")).json())
    described = entry["photo_description"]
    assert (described["text"], described["status"]) == (CLEAN, "open")
    (offer,) = entry["suggestions"]
    assert (offer["field"], offer["value"], offer["description_id"], offer["note_id"]) == (
        "ear_notch", "left", described["id"], None)
    assert "ear_notch" not in entry["attrs"]                                 # a suggestion, never a fact

    r = await client.post(f"/suggestion/{offer['id']}", json={"verdict": "choose", "value": "left"})
    assert r.json()["status"] == "accepted"
    r = await client.post(f"/description/{described['id']}", json={"verdict": "use"})
    assert r.json() == {"status": "accepted", "text": None}
    entry = _entry((await client.get("/dex")).json())
    assert entry["photo_description"]["status"] == "accepted" and entry["attrs"]["ear_notch"] == "left"
    assert "note" not in entry["attrs"]                                      # kept apart from the typed note

    r = await client.post(f"/description/{described['id']}", json={"verdict": "edit", "text": "  Tan dog,   red collar. "})
    assert r.json() == {"status": "edited", "text": "Tan dog, red collar."}
    assert _entry((await client.get("/dex")).json())["photo_description"] == {
        "id": described["id"], "status": "edited", "text": "Tan dog, red collar."}
    assert (await client.post(f"/description/{described['id']}", json={"verdict": "edit", "text": " "})).status_code == 422
    assert (await client.post(f"/description/{described['id']}", json={"verdict": "dismiss"})).json()["status"] == "dismissed"
    assert _entry((await client.get("/dex")).json())["photo_description"] is None


@pytest.mark.asyncio
async def test_a_description_that_breaks_the_rule_is_never_stored_or_logged(authed_client, on, caplog):
    client, _ = authed_client
    await _sighting(client)
    job = await _claim(client)
    with caplog.at_level(logging.INFO):
        await _complete(client, job, _answer("A brown dog outside Ganesh Bakery on the corner.",
                                             [{"field": "condition", "value": "healthy", "quote": "A brown dog"}]))
    async with client._transport.app.state.pool.acquire() as c:
        row = await c.fetchrow("SELECT status, description, dropped FROM photo_descriptions")
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions") == 0
    assert dict(row) == {"status": "done", "description": None, "dropped": "proper_noun"}
    assert "Bakery" not in caplog.text and "dropped (proper_noun)" in caplog.text
    entry = _entry((await client.get("/dex")).json())
    assert entry["photo_description"] is None and entry["suggestions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["Indiranagar has a tan dog with a limp.", "A tan dog with a limp walks with a pedestrian.",
                                  "A tan dog with a limp stands next to a taxi."])
async def test_a_description_with_any_other_word_stores_nothing(authed_client, on, said):
    client, _ = authed_client
    await _sighting(client)
    await _complete(client, await _claim(client), _answer(said, [{"field": "condition", "value": "injured", "quote": "with a limp"}]))
    async with client._transport.app.state.pool.acquire() as c:
        row = await c.fetchrow("SELECT description, dropped FROM photo_descriptions")
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions") == 0
    assert dict(row) == {"description": None, "dropped": "unlisted_word"}


@pytest.mark.asyncio
async def test_the_journal_is_told_a_description_is_on_its_way(authed_client, on, monkeypatch):
    client, _ = authed_client
    await _sighting(client)
    described = _entry((await client.get("/dex")).json())["photo_description"]
    assert described["status"] == "describing" and described["text"] is None
    await _complete(client, await _claim(client), _answer(CLEAN))
    assert _entry((await client.get("/dex")).json())["photo_description"]["status"] == "open"


@pytest.mark.asyncio
async def test_backfill_waiting_its_turn_is_not_announced(authed_client, on):
    client, _ = authed_client
    sid = await _sighting(client)
    async with client._transport.app.state.pool.acquire() as c:
        await c.execute("UPDATE photo_descriptions SET backfill=true WHERE sighting_id=$1", sid)
    assert _entry((await client.get("/dex")).json())["photo_description"] is None


@pytest.mark.asyncio
async def test_a_note_about_one_animal_leaves_the_other_to_be_described(authed_client, on):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, (brown, black), _ = await _two_dog_capture(pool, oid)
    async with pool.acquire() as c:
        await c.execute("DELETE FROM voice_transcripts")
    detail = (await client.get(f"/capture/{cid}")).json()
    await client.post(f"/capture/{cid}/review", json={"revision": detail["revision"], "groups": [
        {"instance_ids": [str(brown)]}, {"instance_ids": [str(black)]}]})
    async with pool.acquire() as c:
        brown_s = await c.fetchval("SELECT sighting_id FROM animal_instances WHERE id=$1", brown)
        black_s = await c.fetchval("SELECT sighting_id FROM animal_instances WHERE id=$1", black)
    assert (await client.post(f"/sighting/{brown_s}/voice-note",
                              files={"audio": ("n.m4a", b"\x00\x00\x00\x18ftypM4A", "audio/mp4")})).status_code == 202
    # Its note is still being transcribed: the brown dog waits, the black one need not.
    job = await _claim(client)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT sighting_id FROM photo_descriptions WHERE id=$1", UUID(job["id"])) == black_s
        await c.execute("UPDATE transcription_jobs SET status='done'")
        await c.execute(
            """INSERT INTO voice_transcripts (id, capture_id, source, pipeline_version, model, outcome, language, text,
                   segments, suggest_status, note_id)
               SELECT $1, $2, 'note', $3, $4, 'transcribed', 'en', 'This one limps.', '[]', 'done', id FROM voice_notes""",
            uuid7(), cid, PIPELINE_VERSION, MODEL_NAME)
        await c.execute("UPDATE photo_descriptions SET run_after=now()")
    assert await _claim(client) is None                                    # the brown dog now has a note
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM photo_descriptions WHERE sighting_id=$1", brown_s) == "skipped"
        # And backfill would have queued the black one on its own.
        await c.execute("DELETE FROM photo_descriptions")
        await c.execute("UPDATE sightings SET processing_state='ready'")
    assert await describe_backfill.run(pool, apply=False) == 1


@pytest.mark.asyncio
async def test_only_the_observer_ever_sees_it(authed_client, on):
    client, _ = authed_client
    sid = await _sighting(client)
    await _complete(client, await _claim(client), _answer(CLEAN))
    described = _entry((await client.get("/dex")).json())["photo_description"]
    await client.post(f"/description/{described['id']}", json={"verdict": "use"})
    shared = (await client.get("/map")).text
    assert CLEAN not in shared and "photo_description" not in shared
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        stranger = uuid4()
        await c.execute("INSERT INTO observers(id) VALUES($1)", stranger)
        await c.execute("UPDATE sightings SET observer_id=$1 WHERE id=$2", stranger, sid)
    assert (await client.post(f"/description/{described['id']}", json={"verdict": "dismiss"})).status_code == 404


@pytest.mark.asyncio
async def test_voice_notes_go_first(authed_client, on):
    client, _ = authed_client
    await _sighting(client)                                   # queued for a description
    talked = await _sighting(client, audio=True)
    async with client._transport.app.state.pool.acquire() as c:
        await c.execute(
            """INSERT INTO voice_transcripts (id, capture_id, source, pipeline_version, model, outcome, language, text, segments)
               VALUES ($1,$2,'note',$3,$4,'transcribed','en','She is asleep.','[]')""",
            uuid7(), talked, PIPELINE_VERSION, MODEL_NAME)
    assert (await _claim(client))["kind"] == "suggest"
    assert (await _claim(client))["kind"] == "describe"


@pytest.mark.asyncio
async def test_a_sighting_with_anything_noted_is_never_described(authed_client, on):
    client, _ = authed_client
    await _sighting(client, note="Friendly, by the tea stall")
    await _sighting(client, audio=True)
    async with client._transport.app.state.pool.acquire() as c:
        await c.execute("DELETE FROM transcription_jobs")
        assert await c.fetchval("SELECT count(*) FROM photo_descriptions") == 0
    assert await _claim(client) is None


@pytest.mark.asyncio
async def test_a_clip_with_words_in_it_is_waited_for_then_left_alone(authed_client, on):
    client, _ = authed_client
    sid = await _sighting(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        job = uuid7()
        await c.execute("""INSERT INTO transcription_jobs (id, capture_id, source, s3_key, pipeline_version)
                           VALUES ($1,$2,'clip','k',$3)""", job, sid, PIPELINE_VERSION)
    assert await _claim(client) is None                                    # still listening
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM photo_descriptions") == "pending"
        await c.execute("UPDATE transcription_jobs SET status='done'")
        await c.execute(
            """INSERT INTO voice_transcripts (id, capture_id, source, pipeline_version, model, outcome, language, text,
                   segments, suggest_status) VALUES ($1,$2,'clip',$3,$4,'transcribed','en','Good boy.','[]','done')""",
            uuid7(), sid, PIPELINE_VERSION, MODEL_NAME)
        await c.execute("UPDATE photo_descriptions SET run_after=now()")
    assert await _claim(client) is None
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM photo_descriptions") == "skipped"


@pytest.mark.asyncio
async def test_a_note_added_while_the_model_looked_wins(authed_client, on):
    client, _ = authed_client
    sid = await _sighting(client)
    job = await _claim(client)
    assert (await client.post(f"/sighting/{sid}/voice-note",
                              files={"audio": ("n.m4a", b"\x00\x00\x00\x18ftypM4A", "audio/mp4")})).status_code == 202
    await _complete(client, job, _answer(CLEAN))
    async with client._transport.app.state.pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM photo_descriptions") == "skipped"
    assert _entry((await client.get("/dex")).json())["photo_description"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("setting,value", [("suggest_via", "api"), ("describe_enabled", False)])
async def test_never_sent_anywhere_but_the_local_model(authed_client, on, monkeypatch, setting, value):
    client, _ = authed_client
    monkeypatch.setattr(settings, setting, value)
    await _sighting(client)
    async with client._transport.app.state.pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM photo_descriptions") == 0


@pytest.mark.asyncio
async def test_a_multi_animal_sighting_is_described_from_its_own_crops(authed_client, on):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, (brown, black), _ = await _two_dog_capture(pool, oid)
    async with pool.acquire() as c:
        await c.execute("DELETE FROM voice_transcripts")
    detail = (await client.get(f"/capture/{cid}")).json()
    await client.post(f"/capture/{cid}/review", json={"revision": detail["revision"], "groups": [
        {"instance_ids": [str(brown)]}, {"instance_ids": [str(black)]}]})
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM photo_descriptions") == 2
        brown_sighting = await c.fetchval("SELECT sighting_id FROM animal_instances WHERE id=$1", brown)
        await c.execute("DELETE FROM photo_descriptions WHERE sighting_id <> $1", brown_sighting)
    job = await _claim(client)
    images = [p["image_url"]["url"] for p in job["request"]["messages"][1]["content"] if p["type"] == "image_url"]
    assert len(images) == 1 and "evidence-t0" in images[0]                 # the brown dog's crop
    await _complete(client, job, _answer("A brown dog with a limp.",
                                         [{"field": "condition", "value": "injured", "quote": "with a limp"}]))
    sheet = (await client.get(f"/capture/{cid}")).json()
    (s,) = sheet["suggestions"]
    assert (s["instance_id"], s["field"], s["description_id"] is not None) == (str(brown), "condition", True)
    entry = _entry((await client.get("/dex")).json(), brown_sighting)
    assert [r["field"] for r in entry["review_suggestions"]] == ["condition"]


@pytest.mark.asyncio
async def test_a_reply_off_the_agreed_shape_is_a_failed_attempt(authed_client, on):
    client, _ = authed_client
    await _sighting(client)
    await _complete(client, await _claim(client), "Here is a lovely dog!")
    async with client._transport.app.state.pool.acquire() as c:
        row = await c.fetchrow("SELECT status, error FROM photo_descriptions")
    assert dict(row) == {"status": "pending", "error": "unparsable_reply"}


@pytest.mark.asyncio
async def test_a_stale_lease_is_refused(authed_client, on):
    client, _ = authed_client
    await _sighting(client)
    job = await _claim(client)
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU,
                          json={"lease_token": "x" * 43, "content": _answer(CLEAN)})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Backfill: queued by hand, drained slowly, never run by itself
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_is_a_dry_run_unless_applied_and_drains_only_at_its_rate(authed_client, on, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "describe_enabled", False)              # these predate descriptions
    for _ in range(2):
        await _sighting(client)
    await _sighting(client, note="Friendly")
    pool = client._transport.app.state.pool
    assert await describe_backfill.run(pool, apply=False) == 2
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM photo_descriptions") == 0
    assert await describe_backfill.run(pool, apply=True, limit=5) == 2
    assert await describe_backfill.run(pool, apply=True) == 0             # safe to repeat
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM photo_descriptions WHERE backfill") == 2

    monkeypatch.setattr(settings, "describe_enabled", True)
    assert await _claim(client) is None                                   # 0 an hour: paused
    monkeypatch.setattr(settings, "describe_backfill_per_hour", 1)
    assert (await _claim(client))["kind"] == "describe"
    assert await _claim(client) is None                                   # one this hour
    new = await _sighting(client)                                         # new work still goes
    job = await _claim(client)
    async with pool.acquire() as c:
        assert str(await c.fetchval("SELECT sighting_id FROM photo_descriptions WHERE id=$1", job["id"])) == new
