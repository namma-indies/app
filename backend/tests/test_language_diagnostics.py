"""How whisper settled a transcript's language, stored for diagnosis (#173)."""
import json

import pytest

from app.transcription_jobs import TranscriptCompletion
from tests.test_note_language import GPU, _claimed, _note, enabled  # noqa: F401  (fixture)
from tests.test_transcription_jobs import _done

DIAG = {"detected": "ms", "chosen": "en", "top": [["ms", 0.6], ["en", 0.3], ["hi", 0.05]]}


@pytest.mark.asyncio
async def test_the_claim_says_it_takes_them_and_they_are_stored_with_the_transcript(authed_client, enabled):
    client, _ = authed_client
    await _note(client)
    job = await _claimed(client)
    assert job["accepts"] == ["language_diagnostics"]
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU,
                          json={**_done(job["lease_token"], language="en", text="She is asleep.",
                                        segments=[{"start_ms": 0, "end_ms": 900, "text": "She is asleep."}]),
                                "language_diagnostics": DIAG})
    assert r.json() == {"status": "done"}
    async with client._transport.app.state.pool.acquire() as c:
        stored = await c.fetchval("SELECT language_diagnostics FROM voice_transcripts")
    assert json.loads(stored) == DIAG


@pytest.mark.asyncio
async def test_a_worker_from_before_sends_none_and_nothing_changes(authed_client, enabled):
    client, _ = authed_client
    await _note(client)
    job = await _claimed(client)
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU, json=_done(job["lease_token"]))
    assert r.json() == {"status": "done"}
    async with client._transport.app.state.pool.acquire() as c:
        assert await c.fetchval("SELECT language_diagnostics FROM voice_transcripts") is None


@pytest.mark.parametrize("bad", [
    {"detected": "Malay"},                                       # codes only
    {"top": [["ms", 0.6], ["en", 0.3], ["hi", 0.05], ["ta", 0.01]]},   # three at most
    {"top": [["ms", 1.5]]},
    {"top": [["Dia sedang tidur", 0.6]]},                        # never words
    {"words": "Dia sedang tidur."},
])
def test_only_codes_and_numbers_are_taken(bad):
    with pytest.raises(ValueError):
        TranscriptCompletion.model_validate({**_done("t" * 43), "language_diagnostics": bad})


def test_what_the_worker_sends_is_taken():
    TranscriptCompletion.model_validate({**_done("t" * 43), "language_diagnostics": DIAG})
    TranscriptCompletion.model_validate({**_done("t" * 43), "language_diagnostics": {"detected": None, "chosen": "kn", "top": []}})
