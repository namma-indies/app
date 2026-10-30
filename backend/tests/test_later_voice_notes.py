"""A voice note added to a sighting after it was uploaded (#156)."""
import json
from uuid import uuid4

import pytest

from app import suggestions
from app.config import settings
from app.deps import get_storage
from app.ids import uuid7
from app.transcription_jobs import MODEL_NAME, PIPELINE_VERSION
from app.voice_notes import note_state
from tests.test_suggestions import _jpeg, _two_dog_capture
from tests.test_transcription_jobs import GPU, NOTE, _post_note

LATER = b"\x00\x00\x00\x18ftypM4A a note added later"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    monkeypatch.setattr(settings, "transcription_enabled", True)
    monkeypatch.setattr(settings, "media_gpu_token", "gpu-secret")
    monkeypatch.setattr(settings, "media_cpu_fallback_enabled", False)
    monkeypatch.setattr(settings, "suggestions_enabled", True)
    monkeypatch.setattr(settings, "suggest_via", "worker")


async def _photo_sighting(client):
    r = await client.post("/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z"})
    assert r.status_code == 201, r.text
    return r.json()["sighting_id"]


async def _add(client, path, audio=LATER, content_type="audio/mp4"):
    return await client.post(path, files={"audio": ("note.m4a", audio, content_type)})


async def _transcribe(client, text, language="en"):
    """The worker takes the next sound and hears `text` in it."""
    job = (await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    assert job is not None
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "pipeline_version": PIPELINE_VERSION, "model": MODEL_NAME,
        "outcome": "transcribed", "language": language, "text": text,
        "segments": [{"start_ms": 0, "end_ms": 1000, "text": text}]})
    assert r.json() == {"status": "done"}
    return job


async def _suggest(client, answer):
    job = (await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    assert job is not None
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "content": json.dumps({"suggestions": answer})})
    assert r.json() == {"status": "done"}
    return job


def _entry(client_json, sid=None):
    items = client_json["sightings"]
    return items[0] if sid is None else next(i for i in items if i["id"] == str(sid))


@pytest.mark.asyncio
async def test_a_note_added_later_is_stored_queued_transcribed_and_read(authed_client, enabled):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    r = await _add(client, f"/sighting/{sid}/voice-note")
    assert r.status_code == 202, r.text
    note_id = r.json()["note_id"]
    assert r.json()["state"] == "transcribing"

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        key = await c.fetchval("SELECT s3_key FROM voice_notes WHERE id=$1", note_id)
    assert key == f"sightings/{sid}/notes/{note_id}.m4a"   # beside the sighting's other media
    assert await get_storage().get(key) == LATER

    entry = _entry((await client.get("/dex")).json())
    (note,) = entry["later_notes"]
    assert (note["id"], note["state"]) == (note_id, "transcribing") and "notes/" in note["audio_url"]
    assert entry["voice_transcripts"] == []

    job = await _transcribe(client, "She is asleep.")
    assert job["kind"] == "note" and "notes/" in job["source"]["url"]   # nothing new for the worker
    entry = _entry((await client.get("/dex")).json())
    assert entry["later_notes"][0]["state"] == "reading"
    (heard,) = entry["voice_transcripts"]
    assert (heard["note_id"], heard["text"]) == (note_id, "She is asleep.")
    assert heard["added_at"] == note["added_at"] and heard["audio_url"]

    await _suggest(client, [{"field": "sex", "value": "female", "quote": "She is asleep"}])
    entry = _entry((await client.get("/dex")).json())
    assert entry["later_notes"][0]["state"] == "done"
    assert [(s["field"], s["value"]) for s in entry["suggestions"]] == [("sex", "female")]


@pytest.mark.asyncio
async def test_the_upload_note_and_later_notes_are_all_kept_in_order(authed_client, enabled):
    client, _ = authed_client
    sid = await _post_note(client)
    await _transcribe(client, "Brown dog by the gate.")
    for _ in range(2):
        assert (await _add(client, f"/sighting/{sid}/voice-note")).status_code == 202
    await _transcribe(client, "First added later.")
    await _transcribe(client, "Second added later.")
    heard = _entry((await client.get("/dex")).json())["voice_transcripts"]
    assert [(h["text"], h["note_id"] is None) for h in heard] == [
        ("Brown dog by the gate.", True), ("First added later.", False), ("Second added later.", False)]
    assert heard[0]["added_at"] is None and heard[1]["added_at"] <= heard[2]["added_at"]


@pytest.mark.asyncio
async def test_a_later_note_s_suggestion_replaces_an_open_one_for_the_same_detail(authed_client, enabled):
    client, _ = authed_client
    sid = await _post_note(client)
    await _transcribe(client, "He is a calm dog.")
    await _suggest(client, [{"field": "sex", "value": "male", "quote": "He is"}])
    await _add(client, f"/sighting/{sid}/voice-note")
    await _transcribe(client, "Actually she is a female.")
    await _suggest(client, [{"field": "sex", "value": "female", "quote": "she is a female"}])
    offered = _entry((await client.get("/dex")).json())["suggestions"]
    assert [(s["field"], s["value"]) for s in offered] == [("sex", "female")]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:   # the older one is still open, on record
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions WHERE status='open'") == 2


@pytest.mark.asyncio
async def test_a_detail_already_filled_in_is_not_second_guessed_by_a_later_note(authed_client, enabled):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute("""UPDATE sightings SET attrs='{"sex": "male"}'::jsonb WHERE id=$1""", sid)
    await _add(client, f"/sighting/{sid}/voice-note")
    await _transcribe(client, "She is asleep.")
    await _suggest(client, [{"field": "sex", "value": "female", "quote": "She is asleep"}])
    assert _entry((await client.get("/dex")).json())["suggestions"] == []


@pytest.mark.asyncio
async def test_a_note_from_one_animal_of_a_capture_is_about_that_animal_only(authed_client, enabled):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, (brown, black), _ = await _two_dog_capture(pool, oid)
    async with pool.acquire() as c:
        await c.execute("DELETE FROM voice_transcripts")   # this capture had no note of its own
    detail = (await client.get(f"/capture/{cid}")).json()
    r = await client.post(f"/capture/{cid}/review", json={"revision": detail["revision"], "groups": [
        {"instance_ids": [str(brown)]}, {"instance_ids": [str(black)]}]})
    assert r.status_code == 200, r.text
    async with pool.acquire() as c:
        sighting_of = dict(await c.fetch("SELECT id, sighting_id FROM animal_instances WHERE capture_id=$1", cid))
    brown_sighting = sighting_of[brown]

    assert (await _add(client, f"/sighting/{brown_sighting}/voice-note")).status_code == 202
    await _transcribe(client, "This one is limping.")
    job = (await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    # Read as that animal's note alone: no photos, no letters to choose between.
    assert [p["type"] for p in job["request"]["messages"][1]["content"]] == ["text"]
    await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"],
        "content": json.dumps({"suggestions": [{"field": "condition", "value": "injured", "quote": "limping"}]})})

    journal = (await client.get("/dex")).json()
    brown_entry, black_entry = _entry(journal, brown_sighting), _entry(journal, sighting_of[black])
    assert [h["text"] for h in brown_entry["voice_transcripts"]] == ["This one is limping."]
    assert len(brown_entry["later_notes"]) == 1
    assert black_entry["voice_transcripts"] == [] and black_entry["later_notes"] == []
    assert [s["field"] for s in brown_entry["review_suggestions"]] == ["condition"]
    assert black_entry["review_suggestions"] == []
    sheet = (await client.get(f"/capture/{cid}")).json()
    assert [s["instance_id"] for s in sheet["suggestions"]] == [str(brown)]


@pytest.mark.asyncio
async def test_a_note_added_to_the_whole_capture_shows_on_every_animal_and_the_sheet(authed_client, enabled):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, (brown, black), _ = await _two_dog_capture(pool, oid)
    async with pool.acquire() as c:
        await c.execute("DELETE FROM voice_transcripts")
    r = await _add(client, f"/capture/{cid}/voice-note")
    assert r.status_code == 202, r.text
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT s3_key FROM voice_notes") == f"captures/{cid}/notes/{r.json()['note_id']}.m4a"
    await _transcribe(client, "The brown one is Kaalu.")
    sheet = (await client.get(f"/capture/{cid}")).json()
    assert [h["text"] for h in sheet["voice_transcripts"]] == ["The brown one is Kaalu."]
    assert [n["state"] for n in sheet["later_notes"]] == ["reading"]
    job = (await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    assert len([p for p in job["request"]["messages"][1]["content"] if p["type"] == "image_url"]) == 2


@pytest.mark.asyncio
async def test_only_the_owner_adds_a_note(authed_client, enabled):
    client, oid = authed_client
    sid = await _photo_sighting(client)
    pool = client._transport.app.state.pool
    cid, _, _ = await _two_dog_capture(pool, oid)
    async with pool.acquire() as c:
        stranger = uuid4()
        await c.execute("INSERT INTO observers(id) VALUES($1)", stranger)
        await c.execute("UPDATE sightings SET observer_id=$1 WHERE id=$2", stranger, sid)
        await c.execute("UPDATE captures SET observer_id=$1 WHERE id=$2", stranger, cid)
    assert (await _add(client, f"/sighting/{sid}/voice-note")).status_code == 404
    assert (await _add(client, f"/capture/{cid}/voice-note")).status_code == 404
    assert (await _add(client, f"/sighting/{uuid7()}/voice-note")).status_code == 404
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_notes") == 0


@pytest.mark.asyncio
async def test_a_photo_sighting_s_stand_in_capture_takes_its_note_on_the_sighting(authed_client, enabled):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    assert (await _add(client, f"/capture/{sid}/voice-note")).status_code == 404


@pytest.mark.asyncio
async def test_no_note_is_taken_while_nothing_would_transcribe_it(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    monkeypatch.setattr(settings, "transcription_enabled", False)
    assert (await _add(client, f"/sighting/{sid}/voice-note")).status_code == 503
    me = (await client.get("/me")).json()
    assert me["later_voice_notes_enabled"] is False


@pytest.mark.asyncio
async def test_the_app_is_told_whether_and_how_many(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "later_voice_notes_max", 3)
    me = (await client.get("/me")).json()
    assert (me["later_voice_notes_enabled"], me["later_voice_notes_max"]) == (True, 3)


@pytest.mark.asyncio
async def test_a_sighting_takes_only_so_many_added_notes(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "later_voice_notes_max", 2)
    sid = await _photo_sighting(client)
    assert [(await _add(client, f"/sighting/{sid}/voice-note")).status_code for _ in range(3)] == [202, 202, 409]
    other = await _photo_sighting(client)
    assert (await _add(client, f"/sighting/{other}/voice-note")).status_code == 202


@pytest.mark.asyncio
async def test_one_person_cannot_queue_notes_without_end(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "rl_voice_note_times", 2)
    sid = await _photo_sighting(client)
    codes = [(await _add(client, f"/sighting/{sid}/voice-note")).status_code for _ in range(3)]
    assert codes == [202, 202, 429]


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type,audio,code", [
    ("image/jpeg", LATER, 422), ("audio/mp4", b"", 422)])
async def test_only_a_voice_note_is_taken(authed_client, enabled, content_type, audio, code):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    assert (await _add(client, f"/sighting/{sid}/voice-note", audio, content_type)).status_code == code


@pytest.mark.asyncio
async def test_a_note_too_large_is_refused(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "media_max_audio_bytes", 16)
    sid = await _photo_sighting(client)
    assert (await _add(client, f"/sighting/{sid}/voice-note")).status_code == 413


@pytest.mark.asyncio
async def test_a_silent_or_failed_note_says_so(authed_client, enabled):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    await _add(client, f"/sighting/{sid}/voice-note")
    job = (await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "pipeline_version": PIPELINE_VERSION, "model": MODEL_NAME,
        "outcome": "no_speech", "language": None, "text": "", "segments": []})
    entry = _entry((await client.get("/dex")).json())
    assert [n["state"] for n in entry["later_notes"]] == ["no_speech"] and entry["voice_transcripts"] == []


@pytest.mark.parametrize("job,outcome,suggest,active,state", [
    ("pending", None, None, True, "transcribing"),
    ("running", None, None, True, "transcribing"),
    ("failed", None, None, True, "failed"),
    (None, None, None, True, "failed"),                 # queued while transcription was off
    ("done", "no_speech", "skipped", True, "no_speech"),
    ("done", "transcribed", "pending", True, "reading"),
    ("done", "transcribed", "pending", False, "done"),  # nothing reads it: not left spinning
    ("done", "transcribed", "done", True, "done"),
    ("done", "transcribed", "failed", True, "done"),
])
def test_a_note_s_state(monkeypatch, job, outcome, suggest, active, state):
    monkeypatch.setattr(suggestions, "suggestions_active", lambda: active)
    import app.voice_notes
    monkeypatch.setattr(app.voice_notes, "suggestions_active", lambda: active)
    assert note_state(job, outcome, suggest) == state


def test_newest_per_field_keeps_the_newest_in_place():
    a, b, c = (str(uuid7()) for _ in range(3))
    items = [{"id": a, "field": "sex"}, {"id": c, "field": "condition"}, {"id": b, "field": "sex"}]
    assert suggestions.newest_per_field(items) == [items[1], items[2]]
