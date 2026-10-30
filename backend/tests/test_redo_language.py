""""Wrong language? Redo in <yours>": transcribe one recording again, once (#169)."""
import json
from uuid import UUID, uuid4

import pytest

from app.config import settings
from app.ids import uuid7
from tests.test_note_language import GPU, _claimed, _jpeg, _note, enabled  # noqa: F401  (fixture)
from tests.test_transcription_jobs import _done


@pytest.fixture
def redo_on(enabled, monkeypatch):
    monkeypatch.setattr(settings, "language_hints_enabled", True)


async def _heard(client, language="hi", text="वह सो रही है"):
    job = await _claimed(client)
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU, json=_done(
        job["lease_token"], language=language, text=text, segments=[{"start_ms": 0, "end_ms": 900, "text": text}]))
    assert r.json() == {"status": "done"}
    return job


def _entry(client_json):
    return client_json["sightings"][0]


@pytest.mark.asyncio
async def test_switched_off_it_is_neither_offered_nor_taken(authed_client, enabled):
    client, _ = authed_client
    await client.put("/me/note-language", json={"language": "en"})
    await _note(client)
    await _heard(client)
    (heard,) = _entry((await client.get("/dex")).json())["voice_transcripts"]
    assert heard["redo_language"] is None
    r = await client.post(f"/voice-transcript/{heard['id']}/redo", json={"language": "en"})
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_english_heard_as_hindi_is_offered_and_transcribed_again_once(authed_client, redo_on):
    client, _ = authed_client
    await client.put("/me/note-language", json={"language": "en"})
    sid = (await _note(client)).json()["sighting_id"]
    first = await _heard(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        transcript = await c.fetchval("SELECT id FROM voice_transcripts")
        for field, status in (("sex", "open"), ("condition", "accepted")):
            await c.execute(
                """INSERT INTO detail_suggestions (id, capture_id, transcript_id, field, value, quote, model, status)
                   VALUES ($1,$2,$3,$4,'female','वह','m',$5)""", uuid7(), sid, transcript, field, status)
        await c.execute("""UPDATE sightings SET attrs = attrs || '{"condition": "injured"}'::jsonb WHERE id=$1""", sid)

    (heard,) = _entry((await client.get("/dex")).json())["voice_transcripts"]
    assert (heard["language"], heard["redo_language"]) == ("hi", "en")
    r = await client.post(f"/voice-transcript/{heard['id']}/redo", json={"language": "en"})
    assert r.status_code == 202 and r.json() == {"state": "transcribing"}

    entry = _entry((await client.get("/dex")).json())
    assert entry["voice_transcripts"] == [] and entry["relistening"] == [{"source": "note", "state": "listening"}]
    assert entry["attrs"]["condition"] == "injured"                         # chosen, so kept
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions WHERE status='open'") == 0
    job = await _claimed(client)
    assert job["id"] == first["id"] and (job["language_hint"], job["language_forced"]) == ("en", True)
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU, json=_done(
        job["lease_token"], language="en", text="She is asleep.",
        segments=[{"start_ms": 0, "end_ms": 900, "text": "She is asleep."}]))
    assert r.json() == {"status": "done"}

    entry = _entry((await client.get("/dex")).json())
    (again,) = entry["voice_transcripts"]
    assert (again["language"], again["text"], again["redo_language"]) == ("en", "She is asleep.", None)
    assert entry["relistening"] == []
    r = await client.post(f"/voice-transcript/{again['id']}/redo", json={"language": "kn"})
    assert r.status_code == 409                                              # once per recording


@pytest.mark.asyncio
@pytest.mark.parametrize("usual", ["hi", "auto", None])
async def test_not_offered_when_it_was_heard_as_expected_or_there_is_no_usual_language(authed_client, redo_on, usual):
    client, _ = authed_client
    if usual:
        await client.put("/me/note-language", json={"language": usual})
    await _note(client)
    await _heard(client)
    (heard,) = _entry((await client.get("/dex")).json())["voice_transcripts"]
    assert heard["redo_language"] is None


@pytest.mark.asyncio
async def test_redoing_in_the_language_it_was_heard_in_is_refused(authed_client, redo_on):
    client, _ = authed_client
    await _note(client)
    await _heard(client)
    (heard,) = _entry((await client.get("/dex")).json())["voice_transcripts"]
    assert (await client.post(f"/voice-transcript/{heard['id']}/redo", json={"language": "hi"})).status_code == 409
    assert (await client.post(f"/voice-transcript/{heard['id']}/redo", json={"language": "auto"})).status_code == 422


@pytest.mark.asyncio
async def test_only_the_owner_redoes(authed_client, redo_on):
    client, _ = authed_client
    sid = (await _note(client)).json()["sighting_id"]
    await _heard(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        transcript = await c.fetchval("SELECT id FROM voice_transcripts")
        stranger = uuid4()
        await c.execute("INSERT INTO observers(id) VALUES($1)", stranger)
        await c.execute("UPDATE captures SET observer_id=$1 WHERE id=$2", stranger, sid)
    assert (await client.post(f"/voice-transcript/{transcript}/redo", json={"language": "en"})).status_code == 404
    assert (await client.post(f"/voice-transcript/{uuid7()}/redo", json={"language": "en"})).status_code == 404


@pytest.mark.asyncio
async def test_a_note_added_later_says_so_through_its_own_state(authed_client, redo_on):
    client, _ = authed_client
    await client.put("/me/note-language", json={"language": "en"})
    r = await client.post("/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z"})
    sid = r.json()["sighting_id"]
    await client.post(f"/sighting/{sid}/voice-note", files={"audio": ("n.m4a", b"\x00\x00\x00\x18ftypM4A", "audio/mp4")})
    await _heard(client)
    (heard,) = _entry((await client.get("/dex")).json())["voice_transcripts"]
    assert heard["redo_language"] == "en"
    assert (await client.post(f"/voice-transcript/{heard['id']}/redo", json={"language": "en"})).status_code == 202
    entry = _entry((await client.get("/dex")).json())
    assert [n["state"] for n in entry["later_notes"]] == ["transcribing"] and entry["relistening"] == []



@pytest.mark.asyncio
async def test_a_redo_that_hears_nothing_or_fails_says_so(authed_client, redo_on, monkeypatch):
    client, _ = authed_client
    await client.put("/me/note-language", json={"language": "en"})
    await _note(client)
    await _heard(client)
    (heard,) = _entry((await client.get("/dex")).json())["voice_transcripts"]
    await client.post(f"/voice-transcript/{heard['id']}/redo", json={"language": "en"})
    job = await _claimed(client)
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU,
                          json=_done(job["lease_token"], outcome="no_speech", language=None, text="", segments=[]))
    assert r.json() == {"status": "done"}
    entry = _entry((await client.get("/dex")).json())
    assert entry["voice_transcripts"] == [] and entry["relistening"] == [{"source": "note", "state": "no_speech"}]

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:                                          # and one that gave up
        await c.execute("DELETE FROM voice_transcripts")
        await c.execute("UPDATE transcription_jobs SET status='failed'")
    entry = _entry((await client.get("/dex")).json())
    assert entry["relistening"] == [{"source": "note", "state": "failed"}]



@pytest.mark.asyncio
async def test_a_redone_note_says_it_is_being_read_until_its_suggestions_are_in(authed_client, redo_on, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "suggestions_enabled", True)
    monkeypatch.setattr(settings, "suggest_via", "worker")
    await client.put("/me/note-language", json={"language": "en"})
    await _note(client)
    await _heard(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute("UPDATE voice_transcripts SET suggest_status='done'")
    (heard,) = _entry((await client.get("/dex")).json())["voice_transcripts"]
    assert heard["reading"] is False                                        # never redone: nothing to say
    await client.post(f"/voice-transcript/{heard['id']}/redo", json={"language": "en"})
    await _heard(client, language="en", text="She is asleep.")              # whisper is done before Qwen
    (again,) = _entry((await client.get("/dex")).json())["voice_transcripts"]
    assert again["reading"] is True
    job = (await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"],
        "content": json.dumps({"suggestions": [{"field": "sex", "value": "female", "quote": "She is asleep"}]})})
    entry = _entry((await client.get("/dex")).json())
    assert entry["voice_transcripts"][0]["reading"] is False
    assert [s["field"] for s in entry["suggestions"]] == ["sex"]


@pytest.mark.asyncio
async def test_the_sheet_never_says_done_reading_beside_the_old_suggestions(authed_client, redo_on, monkeypatch):
    """The model finishes between the sheet's two reads (#170 review): whichever
    way it falls, the sheet must not see reading=false without the new chips."""
    from app import captures
    from app.transcription_jobs import PIPELINE_VERSION
    from tests.test_suggestions import _two_dog_capture
    monkeypatch.setattr(settings, "suggestions_enabled", True)
    monkeypatch.setattr(settings, "suggest_via", "worker")
    client, oid = authed_client
    pool = client._transport.app.state.pool
    cid, (brown,), _ = await _two_dog_capture(pool, oid, dogs=1)
    async with pool.acquire() as c:
        await c.execute("""INSERT INTO transcription_jobs (id, capture_id, source, s3_key, pipeline_version, status, redo_count)
                           VALUES ($1,$2,'note','k',$3,'done',1)""", uuid7(), cid, PIPELINE_VERSION)
        transcript = await c.fetchval("SELECT id FROM voice_transcripts WHERE capture_id=$1", cid)
    real_heard, real_for_capture, reads = captures.heard, captures.for_capture, []

    async def qwen_finishes():
        async with pool.acquire() as c:
            await c.execute("UPDATE voice_transcripts SET suggest_status='done' WHERE id=$1", transcript)
            await c.execute("""INSERT INTO detail_suggestions (id, capture_id, transcript_id, instance_id, field, value, quote, model)
                               VALUES ($1,$2,$3,$4,'known_name','Kaalu','The brown one is Kaalu','m')""",
                            uuid7(), cid, transcript, brown)

    async def first_then_finish(real, *args):
        result = await real(*args)
        reads.append(real)
        if len(reads) == 1:
            await qwen_finishes()                                     # between the two reads
        return result
    monkeypatch.setattr(captures, "heard", lambda *a: first_then_finish(real_heard, *a))
    monkeypatch.setattr(captures, "for_capture", lambda *a: first_then_finish(real_for_capture, *a))
    sheet = (await client.get(f"/capture/{cid}")).json()
    (heard,) = sheet["voice_transcripts"]
    assert heard["reading"] or sheet["suggestions"], "done reading beside the old suggestions: the sheet would stop looking"
    assert heard["reading"] and [s["field"] for s in sheet["suggestions"]] == ["known_name"]


@pytest.mark.asyncio
async def test_a_redo_landing_between_the_reads_never_loses_both_its_words_and_its_progress(authed_client, redo_on):
    """ASR completes between heard()'s progress and transcript reads (#170 review):
    the answer must hold the new words or still say it is listening."""
    from app import voice_notes
    from app.deps import get_storage
    from app.transcription_jobs import MODEL_NAME, PIPELINE_VERSION
    client, _ = authed_client
    await client.put("/me/note-language", json={"language": "en"})
    sid = (await _note(client)).json()["sighting_id"]
    await _heard(client)
    (heard,) = _entry((await client.get("/dex")).json())["voice_transcripts"]
    await client.post(f"/voice-transcript/{heard['id']}/redo", json={"language": "en"})
    pool = client._transport.app.state.pool

    async def asr_completes():
        async with pool.acquire() as c:
            await c.execute(
                """INSERT INTO voice_transcripts (id, capture_id, source, pipeline_version, model, outcome, language, text, segments)
                   VALUES ($1,$2,'note',$3,$4,'transcribed','en','She is asleep.','[]')""", uuid7(), sid, PIPELINE_VERSION, MODEL_NAME)
            await c.execute("UPDATE transcription_jobs SET status='done'")

    class Between:
        """Commits the ASR result right after the first of the two reads."""
        def __init__(self, conn):
            self.conn, self.done = conn, False

        async def fetch(self, query, *args):
            rows = await self.conn.fetch(query, *args)
            if not self.done and ("redo_count > 0" in query or "o.note_language AS usual" in query):
                self.done = True
                await asr_completes()
            return rows

    async with pool.acquire() as c:
        found = (await voice_notes.heard(Between(c), get_storage(), [UUID(sid)]))[UUID(sid)]
    assert found["transcripts"] or found["relistening"], "neither the new words nor any progress: the viewer stops"
    assert [r["state"] for r in found["relistening"]] == ["listening"]
    assert [t["text"] for t in found["transcripts"]] == ["She is asleep."]
