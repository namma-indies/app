"""A voice note added to a sighting after it was uploaded (#156)."""
import json
from uuid import uuid4

import pytest

from app import suggestions
from app.config import settings
from app.deps import get_storage
from app.ids import uuid7
from app.transcription_jobs import MODEL_NAME, PIPELINE_VERSION
from app.media_purges import settled
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


async def _add(client, path, audio=LATER, content_type="audio/mp4", token=None):
    return await client.post(path, files={"audio": ("note.m4a", audio, content_type)},
                             data={"client_token": token} if token else None)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["sighting", "capture"])
async def test_a_save_retried_after_its_answer_was_lost_finds_the_note_it_made(authed_client, enabled, monkeypatch, target):
    client, oid = authed_client
    monkeypatch.setattr(settings, "later_voice_notes_max", 1)
    if target == "sighting":
        path = f"/sighting/{await _photo_sighting(client)}/voice-note"
    else:
        pool = client._transport.app.state.pool
        cid, _, _ = await _two_dog_capture(pool, oid)
        path = f"/capture/{cid}/voice-note"
    first = await _add(client, path, token="rec-1")
    assert first.status_code == 202 and "duplicate" not in first.json()
    # The answer never arrived; the app saves the same recording again. The
    # limit is reached now, but this is not a new note.
    again = await _add(client, path, token="rec-1")
    assert again.status_code == 202
    assert again.json() == {"note_id": first.json()["note_id"], "duplicate": True}
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_notes") == 1
        assert await c.fetchval("SELECT count(*) FROM transcription_jobs WHERE note_id IS NOT NULL") == 1
    assert (await _add(client, path, token="rec-2")).status_code == 409   # a different recording is new


@pytest.mark.asyncio
async def test_each_suggestion_names_the_note_its_words_are_in(authed_client, enabled):
    client, _ = authed_client
    sid = await _post_note(client)
    await _transcribe(client, "She is limping.")
    await _suggest(client, [{"field": "condition", "value": "injured", "quote": "limping"}])
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    await _transcribe(client, "She is limping and she is a female.")
    await _suggest(client, [{"field": "sex", "value": "female", "quote": "she is a female"}])
    offered = {s["field"]: s["note_id"] for s in _entry((await client.get("/dex")).json())["suggestions"]}
    assert offered == {"condition": None, "sex": note_id}


@pytest.mark.asyncio
async def test_the_sheet_s_suggestions_name_their_note_too(authed_client, enabled):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, (brown, _), _ = await _two_dog_capture(pool, oid)
    job = (await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "content": json.dumps({"suggestions": [
            {"field": "known_name", "value": "Kaalu", "quote": "The brown one is Kaalu", "animal": "A"}]})})
    (s,) = (await client.get(f"/capture/{cid}")).json()["suggestions"]
    assert s["note_id"] is None


@pytest.mark.asyncio
async def test_two_saves_of_one_recording_at_once_leave_one_note_and_one_copy_of_the_audio(authed_client, enabled):
    import asyncio
    client, _ = authed_client
    sid = await _photo_sighting(client)
    path = f"/sighting/{sid}/voice-note"
    first, second = await asyncio.gather(_add(client, path, token="rec-1"), _add(client, path, token="rec-1"))
    assert first.status_code == second.status_code == 202
    assert first.json()["note_id"] == second.json()["note_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_notes") == 1
        assert await c.fetchval("SELECT count(*) FROM transcription_jobs WHERE note_id IS NOT NULL") == 1
        key = await c.fetchval("SELECT s3_key FROM voice_notes")
    assert await get_storage().list_keys(f"sightings/{sid}/notes/") == [key]


# ---------------------------------------------------------------------------
# Deleting a note added later (#158)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deleting_a_note_removes_its_audio_words_and_open_suggestions_but_not_what_was_chosen(authed_client, enabled):
    client, _ = authed_client
    sid = await _post_note(client)
    await _transcribe(client, "Brown dog by the gate.")
    await _suggest(client, [])
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    await _transcribe(client, "She is a female and her left ear is notched.")
    await _suggest(client, [{"field": "sex", "value": "female", "quote": "She is a female"},
                            {"field": "ear_notch", "value": "left", "quote": "her left ear is notched"}])
    offered = {s["field"]: s for s in _entry((await client.get("/dex")).json())["suggestions"]}
    r = await client.post(f"/suggestion/{offered['sex']['id']}", json={"verdict": "choose", "value": "female"})
    assert r.status_code == 200
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        key = await c.fetchval("SELECT s3_key FROM voice_notes WHERE id=$1", note_id)

    r = await client.delete(f"/voice-note/{note_id}")
    assert r.status_code == 204
    entry = _entry((await client.get("/dex")).json())
    assert entry["later_notes"] == []
    assert [h["text"] for h in entry["voice_transcripts"]] == ["Brown dog by the gate."]   # the upload's own stays
    assert entry["suggestions"] == [] and entry["answered_suggestions"] == []
    assert entry["attrs"]["sex"] == "female"                                           # chosen, so kept
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_notes") == 0
        # The open ear-notch suggestion is gone; the answered one stays on record, without the words.
        kept = await c.fetch("SELECT field, value, status, decided_value, quote, transcript_id FROM detail_suggestions")
        assert [dict(r) for r in kept] == [{"field": "sex", "value": "female", "status": "accepted",
                                            "decided_value": "female", "quote": "", "transcript_id": None}]
        assert await c.fetchval("SELECT count(*) FROM voice_transcripts") == 1
        assert await c.fetchval("SELECT count(*) FROM transcription_jobs WHERE note_id IS NOT NULL") == 0
    assert key not in await get_storage().list_keys(f"sightings/{sid}/")
    assert (await client.delete(f"/voice-note/{note_id}")).status_code == 404


@pytest.mark.asyncio
async def test_only_the_owner_deletes_a_note(authed_client, enabled):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        stranger = uuid4()
        await c.execute("INSERT INTO observers(id) VALUES($1)", stranger)
        await c.execute("UPDATE voice_notes SET observer_id=$1", stranger)
    assert (await client.delete(f"/voice-note/{note_id}")).status_code == 404
    assert (await client.delete(f"/voice-note/{uuid7()}")).status_code == 404
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_notes") == 1


@pytest.mark.asyncio
async def test_a_store_that_refuses_still_deletes_the_note_and_owes_the_audio(authed_client, enabled, monkeypatch, caplog):
    import logging
    client, _ = authed_client
    sid = await _photo_sighting(client)
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        key = await c.fetchval("SELECT s3_key FROM voice_notes")
    from app.storage.s3 import S3Storage
    real_delete = S3Storage.delete

    async def refuse(self, key):
        raise PermissionError("AccessDenied")
    monkeypatch.setattr(S3Storage, "delete", refuse)
    with caplog.at_level(logging.WARNING):
        assert (await client.delete(f"/voice-note/{note_id}")).status_code == 204   # never the store's to refuse
        await settled()
    assert _entry((await client.get("/dex")).json())["later_notes"] == []
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_notes") == 0
        owed = dict(await c.fetchrow("SELECT s3_key, reason, attempts, last_error FROM media_purges"))
    assert owed == {"s3_key": key, "reason": "voice_note_deleted", "attempts": 1, "last_error": "PermissionError"}
    assert key in caplog.text
    assert key in await get_storage().list_keys(f"sightings/{sid}/notes/")             # still there, and on record

    # Aswin's script, with credentials that may delete: dry run first, then for real.
    monkeypatch.setattr(S3Storage, "delete", real_delete)
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import purge_media
    assert await purge_media.run(pool, get_storage(), apply=False) == (1, 0, 0)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM media_purges") == 1
    assert await purge_media.run(pool, get_storage(), apply=True) == (1, 1, 0)
    assert key not in await get_storage().list_keys(f"sightings/{sid}/notes/")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM media_purges") == 0
    assert await purge_media.run(pool, get_storage(), apply=True) == (0, 0, 0)       # safe to repeat


@pytest.mark.asyncio
async def test_a_delete_the_store_accepts_owes_nothing(authed_client, enabled):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    assert (await client.delete(f"/voice-note/{note_id}")).status_code == 204
    await settled()
    async with client._transport.app.state.pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM media_purges") == 0
    assert await get_storage().list_keys(f"sightings/{sid}/notes/") == []


@pytest.mark.asyncio
async def test_a_note_deleted_mid_transcription_is_refused_to_the_worker(authed_client, enabled):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    job = (await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    assert (await client.delete(f"/voice-note/{note_id}")).status_code == 204
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "pipeline_version": PIPELINE_VERSION, "model": MODEL_NAME,
        "outcome": "transcribed", "language": "en", "text": "too late", "segments": [{"start_ms": 0, "end_ms": 1, "text": "too late"}]})
    assert r.status_code == 409
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_transcripts") == 0


@pytest.mark.asyncio
async def test_answered_suggestions_from_two_deleted_notes_can_share_a_detail(authed_client, enabled):
    client, _ = authed_client
    sid = await _photo_sighting(client)
    for text in ("She is a female.", "Yes she is a female."):
        note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
        await _transcribe(client, text)
        await _suggest(client, [{"field": "sex", "value": "female", "quote": "she is a female"}])
        (offer,) = _entry((await client.get("/dex")).json())["suggestions"]
        assert (await client.post(f"/suggestion/{offer['id']}", json={"verdict": "dismiss"})).status_code == 200
        assert (await client.delete(f"/voice-note/{note_id}")).status_code == 204
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions WHERE transcript_id IS NULL") == 2


@pytest.mark.asyncio
async def test_suggestions_stored_while_a_note_is_deleted_are_cleared_with_it(authed_client, enabled):
    """The model's answer is being stored (its transcript held) when the
    delete arrives: the delete waits for it, then clears what it wrote."""
    import asyncio
    client, _ = authed_client
    sid = await _photo_sighting(client)
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    await _transcribe(client, "She is a female.")
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        transcript = await c.fetchval("SELECT id FROM voice_transcripts WHERE note_id=$1", note_id)
    storing = await pool.acquire()
    try:
        tx = storing.transaction()
        await tx.start()
        # What _store does: hold the transcript, then write an open suggestion quoting it.
        await storing.execute("SELECT id FROM voice_transcripts WHERE id=$1 FOR KEY SHARE", transcript)
        await storing.execute(
            """INSERT INTO detail_suggestions (id, capture_id, transcript_id, field, value, quote, model)
               VALUES ($1,$2,$3,'sex','female','She is a female','m')""", uuid7(), sid, transcript)
        deleting = asyncio.create_task(client.delete(f"/voice-note/{note_id}"))
        await asyncio.sleep(0.3)
        assert not deleting.done()                     # fenced: waits for the write in flight
        await tx.commit()
    finally:
        await pool.release(storing)
    assert (await deleting).status_code == 204
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions") == 0


@pytest.mark.asyncio
async def test_an_answer_stored_after_its_note_was_deleted_writes_nothing(authed_client, enabled):
    from app import suggestions as s
    client, _ = authed_client
    sid = await _photo_sighting(client)
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    await _transcribe(client, "She is a female.")
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        row = dict(await c.fetchrow("SELECT id, capture_id, language, text FROM voice_transcripts WHERE note_id=$1", note_id))
    assert (await client.delete(f"/voice-note/{note_id}")).status_code == 204
    await s._store(pool, row, [{"field": "sex", "value": "female", "quote": "She is a female"}], None, "m")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions") == 0


@pytest.mark.asyncio
async def test_a_store_that_never_answers_does_not_hold_up_the_delete(authed_client, enabled, monkeypatch):
    import asyncio
    from app import media_purges
    from app.storage.s3 import S3Storage
    client, _ = authed_client
    sid = await _photo_sighting(client)
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    answer = asyncio.Event()

    async def hang(self, key):
        await answer.wait()
    monkeypatch.setattr(S3Storage, "delete", hang)
    r = await asyncio.wait_for(client.delete(f"/voice-note/{note_id}"), timeout=5)
    assert r.status_code == 204                                                    # returned while the store is silent
    assert _entry((await client.get("/dex")).json())["later_notes"] == []          # and the viewer has it gone
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM media_purges") == 1         # owed, on record
    answer.set()
    await settled()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM media_purges") == 0
    # A store that never answers at all is given up on, bounded, and left recorded.
    note_id = (await _add(client, f"/sighting/{sid}/voice-note", token="rec-9")).json()["note_id"]
    monkeypatch.setattr(media_purges, "PURGE_TIMEOUT_S", 0.2)
    answer.clear()
    assert (await client.delete(f"/voice-note/{note_id}")).status_code == 204
    await settled()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT last_error FROM media_purges") == "TimeoutError"


@pytest.mark.asyncio
async def test_a_recording_saved_again_after_its_note_was_deleted_keeps_its_audio(authed_client, enabled, monkeypatch):
    """add with token T, delete while the store refuses, save T again, then purge: the live note keeps its audio."""
    import sys
    from pathlib import Path
    from app.storage.s3 import S3Storage
    client, _ = authed_client
    sid = await _photo_sighting(client)
    path = f"/sighting/{sid}/voice-note"
    first = (await _add(client, path, token="rec-1")).json()["note_id"]
    real_delete = S3Storage.delete

    async def refuse(self, key):
        raise PermissionError("AccessDenied")
    monkeypatch.setattr(S3Storage, "delete", refuse)
    assert (await client.delete(f"/voice-note/{first}")).status_code == 204
    await settled()
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        key = await c.fetchval("SELECT s3_key FROM media_purges")
    again = await _add(client, path, token="rec-1")
    assert again.status_code == 202 and again.json()["note_id"] == first        # the same recording, the same key
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM media_purges") == 0         # taken back from the purge
    monkeypatch.setattr(S3Storage, "delete", real_delete)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import purge_media
    assert await purge_media.run(pool, get_storage(), apply=True) == (0, 0, 0)
    assert await get_storage().get(key) == LATER                                   # still there for the live note
    # And a purge record that somehow survived would still not delete a live note's audio.
    async with pool.acquire() as c:
        await c.execute("INSERT INTO media_purges (s3_key, reason) VALUES ($1, 'test')", key)
    assert await purge_media.run(pool, get_storage(), apply=True) == (1, 1, 0)
    assert await get_storage().get(key) == LATER


@pytest.mark.asyncio
async def test_a_delete_and_a_transcription_completing_at_once_do_not_deadlock(authed_client, enabled):
    """Completion holds the note's job and then writes a transcript pointing at the
    note; a delete arriving in between waits for it rather than deadlocking."""
    import asyncio
    client, _ = authed_client
    sid = await _photo_sighting(client)
    note_id = (await _add(client, f"/sighting/{sid}/voice-note")).json()["note_id"]
    pool = client._transport.app.state.pool
    completing = await pool.acquire()
    try:
        tx = completing.transaction()
        await tx.start()
        job = await completing.fetchval("SELECT id FROM transcription_jobs WHERE note_id=$1 FOR UPDATE", note_id)
        deleting = asyncio.create_task(client.delete(f"/voice-note/{note_id}"))
        await asyncio.sleep(0.3)
        assert not deleting.done()
        await completing.execute(
            """INSERT INTO voice_transcripts (id, capture_id, source, pipeline_version, model, outcome, language,
                   text, segments, note_id) VALUES ($1,$2,'note',$3,$4,'transcribed','en','She limps.','[]',$5)""",
            uuid7(), sid, PIPELINE_VERSION, MODEL_NAME, note_id)
        await completing.execute("UPDATE transcription_jobs SET status='done' WHERE id=$1", job)
        await tx.commit()                                                          # no deadlock: this commits
    finally:
        await pool.release(completing)
    assert (await deleting).status_code == 204
    await settled()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_transcripts") == 0
        assert await c.fetchval("SELECT count(*) FROM transcription_jobs") == 0
