"""API side of voice-note transcription: enqueue, lease, store, read back."""
import io

import pytest
from PIL import Image

from app.config import settings
from app.transcription_jobs import MODEL_NAME, PIPELINE_VERSION, TranscriptCompletion

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
    # Keep the CPU image fallback from racing the test for the media job.
    monkeypatch.setattr(settings, "media_cpu_fallback_enabled", False)


async def _post_note(client):
    r = await client.post("/sighting", files=[
        ("photos", ("d.jpg", _jpeg(), "image/jpeg")),
        ("audio", ("note.m4a", NOTE, "audio/mp4")),
    ], data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z"})
    assert r.status_code == 201, r.text
    return r.json()["sighting_id"]


def _done(token, **overrides):
    body = dict(lease_token=token, pipeline_version=PIPELINE_VERSION, model=MODEL_NAME,
                outcome="transcribed", language="kn", text="ನಾಯಿ ಮಲಗಿದೆ",
                segments=[{"start_ms": 0, "end_ms": 1400, "text": "ನಾಯಿ ಮಲಗಿದೆ"}])
    return {**body, **overrides}


@pytest.mark.asyncio
async def test_a_voice_note_is_queued_transcribed_and_read_back(authed_client, enabled):
    client, _ = authed_client
    sid = await _post_note(client)

    claim = (await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"},
                               headers=GPU)).json()["job"]
    assert claim["kind"] == "note" and claim["model"] == MODEL_NAME
    assert "audio.m4a" in claim["source"]["url"]
    jid, token = claim["id"], claim["lease_token"]

    r = await client.post(f"/internal/transcription-jobs/{jid}/heartbeat",
                          json={"lease_token": token}, headers=GPU)
    assert r.status_code == 200

    r = await client.post(f"/internal/transcription-jobs/{jid}/complete", json=_done(token), headers=GPU)
    assert r.json() == {"status": "done"}
    # A completion whose reply was lost is replayed; it must not write twice.
    r = await client.post(f"/internal/transcription-jobs/{jid}/complete", json=_done(token), headers=GPU)
    assert r.json() == {"status": "done"}

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_transcripts WHERE capture_id=$1", sid) == 1
    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["voice_transcripts"] == [{"source": "note", "language": "kn", "text": "ನಾಯಿ ಮಲಗಿದೆ",
                                          "english": None, "note_id": None, "added_at": None, "audio_url": None}]

    nothing = await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)
    assert nothing.json() == {"job": None}


@pytest.mark.asyncio
async def test_nothing_is_queued_while_transcription_is_off(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "transcription_enabled", False)
    await _post_note(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM transcription_jobs") == 0


@pytest.mark.asyncio
async def test_a_clip_queues_its_own_sound(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "multi_animal_enabled", True)
    r = await client.post("/capture", files={"video": ("clip.mp4", b"opaque clip", "video/mp4")},
                          data={"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z"})
    assert r.status_code == 201, r.text
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT source, s3_key FROM transcription_jobs WHERE capture_id=$1",
                               r.json()["capture_id"])
    assert row["source"] == "clip" and row["s3_key"].endswith("/clip.mp4")


@pytest.mark.asyncio
async def test_a_failed_transcription_backs_off_then_gives_up(authed_client, enabled, monkeypatch):
    client, _ = authed_client
    await _post_note(client)
    job = (await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"},
                             headers=GPU)).json()["job"]
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/fail", headers=GPU,
                          json={"lease_token": job["lease_token"], "error": "processing_timeout"})
    assert r.json() == {"status": "queued"}
    # Backed off: not claimable again straight away.
    again = await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)
    assert again.json() == {"job": None}

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute("UPDATE transcription_jobs SET run_after=now()")
    job = (await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"},
                             headers=GPU)).json()["job"]
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/fail", headers=GPU,
                          json={"lease_token": job["lease_token"], "error": "invalid_protocol",
                                "retryable": False})
    assert r.json() == {"status": "failed"}


@pytest.mark.asyncio
async def test_a_stale_lease_cannot_complete(authed_client, enabled):
    client, _ = authed_client
    await _post_note(client)
    job = (await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"},
                             headers=GPU)).json()["job"]
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete",
                          json=_done("w" * 43), headers=GPU)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_the_queue_requires_the_worker_token(authed_client, enabled):
    client, _ = authed_client
    r = await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"})
    assert r.status_code == 401


@pytest.mark.parametrize("overrides", [
    {"text": "", "segments": []},
    {"outcome": "no_speech"},
    {"outcome": "no_audio", "text": "", "segments": [], "language": "en"},
    {"language": "English"},
    {"segments": [{"start_ms": 900, "end_ms": 100, "text": "x"}]},
])
def test_completion_shape_is_strict(overrides):
    with pytest.raises(ValueError):
        TranscriptCompletion.model_validate(_done("t" * 43, **overrides))


def test_empty_outcomes_are_accepted():
    TranscriptCompletion.model_validate(_done("t" * 43, outcome="no_speech", text="", segments=[], language=None))
    TranscriptCompletion.model_validate(_done("t" * 43, outcome="no_audio", text="", segments=[], language=None))
