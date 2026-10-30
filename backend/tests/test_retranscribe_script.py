"""scripts/retranscribe_misdetected.py: transcribe again what whisper heard as another language (#164)."""
import json
import sys
from pathlib import Path

import pytest

from app.ids import uuid7
from tests.test_transcription_jobs import GPU, _done, _post_note, enabled  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import retranscribe_misdetected as script  # noqa: E402

MALAY = "Dia sedang tidur."   # what whisper wrote for English speech it took for Malay


async def _heard(client, **said):
    job = (await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU,
                          json=_done(job["lease_token"], **said))
    assert r.json() == {"status": "done"}
    return job


def _malay(**more):
    return dict(language="ms", text=MALAY, segments=[{"start_ms": 0, "end_ms": 900, "text": MALAY}], **more)


@pytest.mark.asyncio
async def test_a_misheard_note_is_listed_then_transcribed_again_keeping_what_was_chosen(authed_client, enabled):
    client, _ = authed_client
    misheard = await _post_note(client)
    first = await _heard(client, **_malay())
    kannada = await _post_note(client)
    await _heard(client)                                       # Kannada: allowed, never touched
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        transcript = await c.fetchval("SELECT id FROM voice_transcripts WHERE language='ms'")
        for status in ("open", "accepted"):
            await c.execute(
                """INSERT INTO detail_suggestions (id, capture_id, transcript_id, field, value, quote, model, status)
                   VALUES ($1,$2,$3,$4,'female','Dia',  'm',$5)""",
                uuid7(), misheard, transcript, "sex" if status == "open" else "condition", status)
        await c.execute("""UPDATE sightings SET attrs = attrs || '{"sex": "male"}'::jsonb WHERE id=$1""", misheard)

    rep = await script.run(pool, apply=False)
    assert [(str(t["capture_id"]), t["source"], t["language"]) for t in rep.found] == [(misheard, "note", "ms")]
    listing = script.describe(rep, apply=False)
    assert "DRY RUN" in listing and MALAY not in listing      # ids and codes, never the words
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_transcripts") == 2

    rep = await script.run(pool, apply=True)
    assert (rep.queued, rep.no_job) == (1, 0)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_transcripts WHERE capture_id=$1", misheard) == 0
        assert await c.fetchval("SELECT count(*) FROM voice_transcripts WHERE capture_id=$1", kannada) == 1
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions WHERE status='open'") == 0
        job = await c.fetchrow("SELECT status, attempts, lease_token_hash FROM transcription_jobs WHERE id=$1", first["id"])
        attrs = await c.fetchval("SELECT attrs FROM sightings WHERE id=$1", misheard)
    assert dict(job) == {"status": "pending", "attempts": 0, "lease_token_hash": None}
    assert (json.loads(attrs) if isinstance(attrs, str) else attrs)["sex"] == "male"     # chosen, so kept

    # The updated worker hears it as English this time; its suggestion pass follows as for any transcript.
    again = await _heard(client, language="en", text="She is asleep.",
                         segments=[{"start_ms": 0, "end_ms": 900, "text": "She is asleep."}])
    assert again["id"] == first["id"]
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT language, text, suggest_status FROM voice_transcripts WHERE capture_id=$1", misheard)
    assert dict(row) == {"language": "en", "text": "She is asleep.", "suggest_status": "pending"}
    assert (await script.run(pool, apply=True)).found == []    # safe to repeat


@pytest.mark.asyncio
async def test_one_without_a_job_to_requeue_is_left_alone_and_counted(authed_client, enabled):
    client, _ = authed_client
    await _post_note(client)
    await _heard(client, **_malay())
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute("DELETE FROM transcription_jobs")
    rep = await script.run(pool, apply=True)
    assert (len(rep.found), rep.queued, rep.no_job) == (1, 0, 1)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_transcripts") == 1


@pytest.mark.asyncio
async def test_a_limit_takes_the_oldest_first(authed_client, enabled):
    client, _ = authed_client
    for _ in range(2):
        await _post_note(client)
        await _heard(client, **_malay())
    pool = client._transport.app.state.pool
    assert len((await script.run(pool, apply=False, limit=1)).found) == 1


def test_applying_needs_the_dgx_update_confirmed(monkeypatch):
    import asyncio
    monkeypatch.setattr(sys, "argv", ["retranscribe_misdetected.py", "--apply"])
    with pytest.raises(SystemExit) as stopped:
        asyncio.run(script.main())
    assert stopped.value.code == 2


def test_it_uses_the_worker_s_own_list():
    from gpu_worker.transcribe import ALLOWED
    assert script.ALLOWED is ALLOWED and "ms" not in ALLOWED and "en" in ALLOWED
