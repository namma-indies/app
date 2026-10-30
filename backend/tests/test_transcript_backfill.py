"""Today's model pass over transcripts made before it (#176)."""
import json
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.ids import uuid7
from tests.test_suggestions import GPU, KANNADA, _claim, _sighting_with_transcript, on_worker  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import transcript_backfill as script  # noqa: E402


async def _old(client, text, language, status, english=None, suggestion=None):
    sid, pool = await _sighting_with_transcript(client, text, language)
    async with pool.acquire() as c:
        tid = await c.fetchval("SELECT id FROM voice_transcripts WHERE capture_id=$1", sid)
        await c.execute("UPDATE voice_transcripts SET suggest_status=$2, english=$3 WHERE id=$1", tid, status, english)
        if suggestion:
            await c.execute(
                """INSERT INTO detail_suggestions (id, capture_id, transcript_id, field, value, quote, model, status)
                   VALUES ($1,$2,$3,'sex','female','x','m',$4)""", uuid7(), sid, tid, suggestion)
    return sid, tid, pool


def _entry(journal, sid):
    return next(i for i in journal["sightings"] if i["id"] == str(sid))


@pytest.mark.asyncio
async def test_the_script_queues_what_needs_it_in_the_right_mode_and_nothing_else(authed_client, on_worker, capsys):
    client, _ = authed_client
    _, translate, pool = await _old(client, KANNADA, "kn", "done", suggestion="accepted")    # answered: English only
    _, full_en, _ = await _old(client, "She is asleep.", "en", "skipped")                    # never read
    _, full_hi, _ = await _old(client, "वह सो रही है", "hi", "failed")                         # failed, #108 era
    await _old(client, "He is fine.", "en", "done")                                          # nothing to do
    await _old(client, "वह ठीक है", "hi", "pending")                                          # being read now
    await _old(client, KANNADA, "kn", "done", english="She is asleep.")                       # has English
    await _old(client, "She limps.", "en", "failed", suggestion="open")                      # has suggestions, in English
    found = await script.run(pool, apply=False)
    assert {r["id"]: r["mode"] for r in found} == {translate: "translate", full_en: "full", full_hi: "full"}
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM transcript_backfill") == 0           # a dry run writes nothing
    assert len(await script.run(pool, apply=True, limit=2)) == 2                             # oldest first
    assert len(await script.run(pool, apply=True)) == 1                                      # the rest; safe to repeat
    assert await script.run(pool, apply=True) == []


def test_the_dry_run_lists_ids_and_counts_never_words(monkeypatch, capsys):
    import asyncio
    rows = [{"id": uuid7(), "mode": "full"}, {"id": uuid7(), "mode": "translate"}]

    async def fake_run(pool, apply, limit=None):
        return rows

    class Pool:
        async def close(self):
            pass

    async def fake_pool(*a, **k):
        return Pool()
    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script.asyncpg, "create_pool", fake_pool)
    monkeypatch.setattr(sys, "argv", ["transcript_backfill.py"])
    asyncio.run(script.main())
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "full: 1, translate only: 1" in out and str(rows[0]["id"]) in out


@pytest.mark.asyncio
async def test_paused_by_default_and_last_in_line(authed_client, on_worker, monkeypatch):
    client, _ = authed_client
    _, tid, pool = await _old(client, "She is asleep.", "en", "skipped")
    await script.run(pool, apply=True)
    assert await _claim(client) is None                                                      # 0 an hour: paused
    monkeypatch.setattr(settings, "transcript_backfill_per_hour", 1)
    fresh, _ = await _sighting_with_transcript(client, "He is calm.")                        # a new note waiting
    first = await _claim(client)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT capture_id FROM voice_transcripts WHERE id=$1", first["id"]) is not None
    second = await _claim(client)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT transcript_id FROM transcript_backfill WHERE id=$1", second["id"]) == tid
    assert await _claim(client) is None                                                      # one this hour


@pytest.mark.asyncio
async def test_translate_only_adds_english_and_never_a_suggestion(authed_client, on_worker, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "transcript_backfill_per_hour", 5)
    sid, tid, pool = await _old(client, KANNADA, "kn", "done", suggestion="dismissed")
    await script.run(pool, apply=True)
    job = await _claim(client)
    assert "english" in job["request"]["response_format"]["json_schema"]["schema"]["properties"]
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"],
        "content": json.dumps({"suggestions": [{"field": "known_name", "value": "Kaalu", "quote": "ಕಾಳು"}],
                               "english": "She is asleep. Her name is Kaalu."})})
    assert r.json() == {"status": "done"}
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT english FROM voice_transcripts WHERE id=$1", tid) == "She is asleep. Her name is Kaalu."
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions") == 1               # only the old, answered one
        assert await c.fetchval("SELECT status FROM transcript_backfill") == "done"
    item = _entry((await client.get("/dex")).json(), sid)
    assert item["voice_transcripts"][0]["english"] == "She is asleep. Her name is Kaalu."


@pytest.mark.asyncio
async def test_a_full_pass_suggests_only_for_blank_details(authed_client, on_worker, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "transcript_backfill_per_hour", 5)
    sid, tid, pool = await _old(client, "She is asleep. Her left ear is notched.", "en", "failed")
    async with pool.acquire() as c:
        await c.execute("""UPDATE sightings SET attrs = attrs || '{"sex": "male"}'::jsonb WHERE id=$1""", sid)
    await script.run(pool, apply=True)
    job = await _claim(client)
    await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"],
        "content": json.dumps({"suggestions": [{"field": "sex", "value": "female", "quote": "She is asleep"},
                                               {"field": "ear_notch", "value": "left", "quote": "Her left ear is notched"}]})})
    item = _entry((await client.get("/dex")).json(), sid)
    assert [s["field"] for s in item["suggestions"]] == ["ear_notch"]                        # sex was chosen: not asked again
    assert item["attrs"]["sex"] == "male"
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT suggest_status FROM voice_transcripts WHERE id=$1", tid) == "done"


@pytest.mark.asyncio
async def test_a_note_transcribed_again_is_not_read_twice(authed_client, on_worker):
    client, _ = authed_client
    _, tid, pool = await _old(client, "She is asleep.", "en", "skipped")
    await script.run(pool, apply=True)
    async with pool.acquire() as c:
        await c.execute("DELETE FROM voice_transcripts WHERE id=$1", tid)                   # what a redo or retranscribe does
        assert await c.fetchval("SELECT count(*) FROM transcript_backfill") == 0


@pytest.mark.asyncio
async def test_an_answer_off_the_agreed_shape_is_a_failed_attempt_and_a_stale_lease_is_refused(authed_client, on_worker, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "transcript_backfill_per_hour", 5)
    _, tid, pool = await _old(client, "She is asleep.", "en", "skipped")
    await script.run(pool, apply=True)
    job = await _claim(client)
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU,
                          json={"lease_token": "x" * 43, "content": "{}"})
    assert r.status_code == 409
    hb = await client.post(f"/internal/suggestion-jobs/{job['id']}/heartbeat", headers=GPU, json={"lease_token": job["lease_token"]})
    assert hb.status_code == 200
    await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU,
                      json={"lease_token": job["lease_token"], "content": "Sure!"})
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT status, error FROM transcript_backfill")
    assert dict(row) == {"status": "pending", "error": "unparsable_reply"}



@pytest.mark.asyncio
async def test_a_cut_off_full_answer_is_retried_without_english(authed_client, on_worker, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "transcript_backfill_per_hour", 5)
    sid, tid, pool = await _old(client, KANNADA, "kn", "failed")
    await script.run(pool, apply=True)
    job = await _claim(client)
    assert "english" in job["request"]["response_format"]["json_schema"]["schema"]["properties"]
    cut_off = json.dumps({"suggestions": [], "english": "She is"})[:-12]
    await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU,
                      json={"lease_token": job["lease_token"], "content": cut_off})
    async with pool.acquire() as c:
        await c.execute("UPDATE transcript_backfill SET run_after=now()")
    retry = await _claim(client)
    assert retry["id"] == job["id"]
    assert list(retry["request"]["response_format"]["json_schema"]["schema"]["properties"]) == ["suggestions"]
    await client.post(f"/internal/suggestion-jobs/{retry['id']}/complete", headers=GPU, json={
        "lease_token": retry["lease_token"],
        "content": json.dumps({"suggestions": [{"field": "sex", "value": "female", "quote": "ಅವಳು ಮಲಗಿದ್ದಾಳೆ"}]})})
    assert [s["field"] for s in _entry((await client.get("/dex")).json(), sid)["suggestions"]] == ["sex"]


@pytest.mark.asyncio
async def test_a_cut_off_translation_is_not_asked_for_again(authed_client, on_worker, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "transcript_backfill_per_hour", 5)
    _, tid, pool = await _old(client, KANNADA, "kn", "done", suggestion="accepted")
    await script.run(pool, apply=True)
    job = await _claim(client)
    await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU,
                      json={"lease_token": job["lease_token"], "content": '{"suggestions": [], "english": "She'})
    async with pool.acquire() as c:
        await c.execute("UPDATE transcript_backfill SET run_after=now()")
    assert await _claim(client) is None
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM transcript_backfill") == "skipped"



@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ['{"suggestions": [], "english": ""}', '{"suggestions": []}',
                                 json.dumps({"suggestions": [], "english": KANNADA})])
async def test_a_translation_job_given_no_usable_english_is_tried_again_not_used_up(authed_client, on_worker, monkeypatch, bad):
    client, _ = authed_client
    monkeypatch.setattr(settings, "transcript_backfill_per_hour", 5)
    sid, tid, pool = await _old(client, KANNADA, "kn", "done", suggestion="accepted")
    await script.run(pool, apply=True)
    job = await _claim(client)
    await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU,
                      json={"lease_token": job["lease_token"], "content": bad})
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT status, error FROM transcript_backfill")
        assert dict(row) == {"status": "pending", "error": "unusable_translation"}
        await c.execute("UPDATE transcript_backfill SET run_after=now()")
    again = await _claim(client)
    assert again["id"] == job["id"]
    await client.post(f"/internal/suggestion-jobs/{again['id']}/complete", headers=GPU, json={
        "lease_token": again["lease_token"], "content": json.dumps({"suggestions": [], "english": "She is asleep."})})
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT english FROM voice_transcripts WHERE id=$1", tid) == "She is asleep."
        assert await c.fetchval("SELECT status FROM transcript_backfill") == "done"
