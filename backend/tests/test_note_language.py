"""The language someone speaks their notes in, asked once and passed to transcription as a hint (#167)."""
import io

import pytest
from PIL import Image

from app.config import settings

GPU = {"Authorization": "Bearer gpu-secret"}
NOTE = b"\x00\x00\x00\x18ftypM4A voice note bytes"


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (400, 300), (100, 140, 90)).save(b, "JPEG")
    return b.getvalue()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    monkeypatch.setattr(settings, "transcription_enabled", True)
    monkeypatch.setattr(settings, "media_gpu_token", "gpu-secret")
    monkeypatch.setattr(settings, "media_cpu_fallback_enabled", False)


async def _note(client, **form):
    r = await client.post("/sighting", files=[("photos", ("d.jpg", _jpeg(), "image/jpeg")),
                                              ("audio", ("note.m4a", NOTE, "audio/mp4"))],
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z", **form})
    return r


async def _claimed(client):
    r = await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)
    return r.json()["job"]


@pytest.mark.asyncio
async def test_asked_once_and_kept(authed_client):
    client, _ = authed_client
    assert (await client.get("/me")).json()["note_language"] is None        # not asked yet: the app asks
    r = await client.put("/me/note-language", json={"language": "kn"})
    assert r.json() == {"note_language": "kn"}
    assert (await client.get("/me")).json()["note_language"] == "kn"
    assert (await client.put("/me/note-language", json={"language": "auto"})).status_code == 200
    assert (await client.get("/me")).json()["note_language"] == "auto"
    for bad in ("fr", "", "KN", None):
        assert (await client.put("/me/note-language", json={"language": bad})).status_code == 422


@pytest.mark.asyncio
async def test_with_no_answer_and_no_choice_a_job_carries_no_hint(authed_client, enabled):
    client, _ = authed_client
    assert (await _note(client)).status_code == 201
    job = await _claimed(client)
    assert (job["language_hint"], job["language_forced"]) == (None, False)    # today's behaviour


@pytest.mark.asyncio
async def test_the_usual_language_is_a_hint(authed_client, enabled):
    client, _ = authed_client
    await client.put("/me/note-language", json={"language": "en"})
    await _note(client)
    job = await _claimed(client)
    assert (job["language_hint"], job["language_forced"]) == ("en", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("chosen,expected", [("kn", ("kn", True)), ("auto", (None, False))])
async def test_a_choice_for_one_note_is_forced_and_mixed_means_no_hint(authed_client, enabled, chosen, expected):
    client, _ = authed_client
    await client.put("/me/note-language", json={"language": "en"})
    assert (await _note(client, note_language=chosen)).status_code == 201
    job = await _claimed(client)
    assert (job["language_hint"], job["language_forced"]) == expected


@pytest.mark.asyncio
async def test_an_unknown_language_is_refused(authed_client, enabled):
    client, _ = authed_client
    r = await _note(client, note_language="fr")
    assert r.status_code == 422
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM sightings") == 0


@pytest.mark.asyncio
async def test_mixed_as_the_usual_language_means_no_hint(authed_client, enabled):
    client, _ = authed_client
    await client.put("/me/note-language", json={"language": "auto"})
    await _note(client)
    job = await _claimed(client)
    assert (job["language_hint"], job["language_forced"]) == (None, False)


@pytest.mark.asyncio
async def test_a_capture_s_note_and_clip_carry_the_hint(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "multi_animal_enabled", True)
    await client.put("/me/note-language", json={"language": "hi"})
    r = await client.post("/capture", files=[("photos", ("d.jpg", _jpeg(), "image/jpeg")),
                                             ("audio", ("note.m4a", NOTE, "audio/mp4"))],
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z", "note_language": "ta"})
    assert r.status_code == 201, r.text
    r = await client.post("/capture", files={"video": ("clip.mp4", b"opaque clip", "video/mp4")},
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z"})
    assert r.status_code == 201, r.text
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        rows = {r["source"]: (r["language_hint"], r["language_forced"]) for r in await c.fetch(
            "SELECT source, language_hint, language_forced FROM transcription_jobs")}
    assert rows == {"note": ("ta", True), "clip": ("hi", False)}          # a clip gets the usual one, as a hint


@pytest.mark.asyncio
async def test_a_note_added_later_carries_its_choice(authed_client, enabled):
    client, _ = authed_client
    r = await client.post("/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z"})
    sid = r.json()["sighting_id"]
    r = await client.post(f"/sighting/{sid}/voice-note", files={"audio": ("n.m4a", NOTE, "audio/mp4")},
                          data={"note_language": "ml"})
    assert r.status_code == 202, r.text
    job = await _claimed(client)
    assert (job["language_hint"], job["language_forced"]) == ("ml", True)
    r = await client.post(f"/sighting/{sid}/voice-note", files={"audio": ("n.m4a", NOTE, "audio/mp4")},
                          data={"note_language": "xx"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# The worker on the DGX today predates the hint, and must carry on as before
# ---------------------------------------------------------------------------

async def test_today_s_worker_ignores_the_hint():
    from tests.test_transcription_worker import FakeTranscriber, Server
    server, transcriber = Server(), FakeTranscriber()
    server.job.update(language_hint="en", language_forced=True)
    async with server.worker(transcriber) as worker:
        assert await worker.run_once()
    assert transcriber.calls == [b"voice-note-bytes"]                          # transcribed exactly as before
    assert server.completions[0]["outcome"] == "transcribed"
    assert "language_hint" not in server.completions[0]                        # and nothing new sent back


# ---------------------------------------------------------------------------
# An answer whose own save failed still reaches the job (#168 review)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_usual_language_sent_with_a_note_is_its_hint_and_kept_when_none_is(authed_client, enabled):
    client, _ = authed_client
    assert (await client.get("/me")).json()["note_language"] is None      # its PUT never arrived
    assert (await _note(client, usual_language="kn")).status_code == 201
    job = await _claimed(client)
    assert (job["language_hint"], job["language_forced"]) == ("kn", False)  # a hint, not forced
    assert (await client.get("/me")).json()["note_language"] == "kn"        # and kept


@pytest.mark.asyncio
async def test_what_is_kept_wins_over_what_a_stale_app_sends(authed_client, enabled):
    client, _ = authed_client
    await client.put("/me/note-language", json={"language": "en"})
    await _note(client, usual_language="kn")
    job = await _claimed(client)
    assert (job["language_hint"], job["language_forced"]) == ("en", False)
    assert (await client.get("/me")).json()["note_language"] == "en"


@pytest.mark.asyncio
async def test_a_note_added_later_carries_the_usual_language_too(authed_client, enabled):
    client, _ = authed_client
    r = await client.post("/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z"})
    sid = r.json()["sighting_id"]
    r = await client.post(f"/sighting/{sid}/voice-note", files={"audio": ("n.m4a", NOTE, "audio/mp4")},
                          data={"usual_language": "ta"})
    assert r.status_code == 202
    job = await _claimed(client)
    assert (job["language_hint"], job["language_forced"]) == ("ta", False)
    assert (await _note(client, usual_language="xx")).status_code == 422


@pytest.mark.asyncio
async def test_a_clip_uploaded_after_a_failed_save_still_gets_the_usual_language(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "multi_animal_enabled", True)
    r = await client.post("/capture", files={"video": ("clip.mp4", b"opaque clip", "video/mp4")},
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z", "usual_language": "kn"})
    assert r.status_code == 201, r.text
    job = await _claimed(client)
    assert (job["kind"], job["language_hint"], job["language_forced"]) == ("clip", "kn", False)
    assert (await client.get("/me")).json()["note_language"] == "kn"
