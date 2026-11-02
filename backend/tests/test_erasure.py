"""Deleting a photo, or one animal in it (#54).

The test that matters most is the first: a photo that took part in everything
-- a confirmed dog with someone else's sighting, a pending match, a duplicate,
a report, a post on X -- is gone from every surface anyone can read, and from
every table, the moment its owner deletes it, while what belongs to others
stays.
"""
import json
from datetime import date
from uuid import UUID

import pytest

from app.config import settings
from app.deps import get_storage
from app.erasure import REFERENCES
from app.ids import uuid7
from app.media_purges import settled
from app.security import issue_session
from tests.test_dogs_route import _make_individual, _post, _second_observer
from tests.test_later_voice_notes import enabled  # noqa: F401  (fixture)


def _as(client, oid):
    client.cookies.set("session", issue_session(oid))


async def _pool(client):
    return client._transport.app.state.pool


async def _surfaces(client, viewer, moderator):
    """Everything another person, or a moderator, can read."""
    _as(client, viewer)
    out = {
        "map": (await client.get("/map")).json()["sightings"],
        "dogs": (await client.get("/dogs")).json()["dogs"],
        "proposals": (await client.get("/proposals")).json()["proposals"],
        "stats": (await client.get("/stats")).json()["totals"],
    }
    _as(client, moderator)
    out["queue"] = (await client.get("/moderation/queue")).json()["items"]
    out["social"] = (await client.get("/social/queue")).json()
    out["observers"] = (await client.get("/stats/observers")).json()["observers"]
    return out


def _mentions(value, needle: str) -> bool:
    return needle in json.dumps(value, default=str)


async def _rich_world(client, owner):
    """The owner's photo, entangled with a viewer's sightings in every way the app allows."""
    pool = await _pool(client)
    viewer, moderator = await _second_observer(client, "Viewer"), await _second_observer(client, "Mod")
    async with pool.acquire() as c:
        await c.execute("UPDATE observers SET trust_tier='moderator' WHERE id=$1", moderator)
        await c.execute("UPDATE observers SET display_name='Owner' WHERE id=$1", owner)
    _as(client, owner)
    mine = await _post(client, when="2026-07-19T09:00:00Z")
    _as(client, viewer)
    theirs = await _post(client, when="2026-07-18T09:00:00Z")
    pending = await _post(client, when="2026-07-20T09:00:00Z")
    copy = await _post(client, when="2026-07-21T09:00:00Z")
    dog = await _make_individual(client, [mine, theirs])
    async with pool.acquire() as c:
        # The viewer confirmed their sighting is the owner's dog: the verdict
        # is recorded against *their* sighting, the proposal names the owner's.
        confirmed = uuid7()
        await c.execute("""INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, score, method, status, resolved_by)
                           VALUES ($1,$2,$3,0.9,'test','confirmed',$4)""", confirmed, UUID(theirs), UUID(mine), viewer)
        await c.execute("""INSERT INTO confirmations (id, sighting_id, individual_id, observer_id, proposal_id, verdict)
                           VALUES ($1,$2,$3,$4,$5,'same')""", uuid7(), UUID(theirs), dog, viewer, confirmed)
        await c.execute("""INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, score, method, status)
                           VALUES ($1,$2,$3,0.8,'test','pending')""", uuid7(), UUID(pending), UUID(mine))
        await c.execute("UPDATE sightings SET match_status='proposed' WHERE id=$1", UUID(pending))
        await c.execute("UPDATE sightings SET duplicate_of=$2 WHERE id=$1", UUID(copy), UUID(mine))
        await c.execute("""INSERT INTO sighting_reports (sighting_id, reporter_id, reason) VALUES ($1,$2,'other')""",
                        UUID(mine), viewer)
        photo = await c.fetchval("SELECT id FROM photos WHERE sighting_id=$1", UUID(mine))
        await c.execute("UPDATE sightings SET shared_with_team_at=now() WHERE id=$1", UUID(mine))
        await c.execute("""INSERT INTO social_picks (id, sighting_id, photo_id, caption, status, posted_at, posted_on, x_post_id)
                           VALUES ($1,$2,$3,'a dog','posted',now(),$4,'x-111')""", uuid7(), UUID(mine), photo, date.today())
    return {"viewer": viewer, "moderator": moderator, "mine": mine, "theirs": theirs,
            "pending": pending, "copy": copy, "dog": dog, "confirmed": confirmed}


@pytest.mark.asyncio
async def test_a_deleted_photo_is_gone_from_every_read_surface_and_every_table(authed_client):
    client, owner = authed_client
    w = await _rich_world(client, owner)
    mine, pool = w["mine"], await _pool(client)
    storage = get_storage()
    assert await storage.list_keys(f"sightings/{mine}/")               # its photo and thumbnail exist

    before = await _surfaces(client, w["viewer"], w["moderator"])
    assert mine in {s["id"] for s in before["map"]}
    assert before["dogs"][0]["sighting_count"] == 2 and before["dogs"][0]["observers"] == ["Owner"]
    assert _mentions(before["proposals"], mine)
    assert mine in {i["sighting_id"] for i in before["queue"]}
    assert _mentions(before["social"], "x-111")

    _as(client, owner)
    assert (await client.delete(f"/capture/{mine}")).status_code == 204
    await settled()

    after = await _surfaces(client, w["viewer"], w["moderator"])
    for name, surface in after.items():
        assert not _mentions(surface, mine), f"{name} still shows the deleted sighting"
    # Gone from what was posted; listed only as owed a takedown on X (Q6).
    takedowns = after["social"].pop("takedowns")
    assert not _mentions(after["social"], "x-111")
    assert [t["x_post_id"] for t in takedowns] == ["x-111"]
    # The dog stays, with the viewer's sighting only (Q3), and the owner's
    # name is no longer on it.
    (dog,) = after["dogs"]
    assert (dog["id"], dog["sighting_count"], dog["observers"]) == (str(w["dog"]), 1, [])
    # The viewer's copy of the same photo is theirs, and comes back (0016).
    assert w["copy"] in {s["id"] for s in after["map"]}
    # Counted once each way: the deleted one out, the copy back in.
    assert after["stats"]["sightings"] == before["stats"]["sightings"]
    _as(client, owner)
    assert mine not in {s["id"] for s in (await client.get("/dex")).json()["sightings"]}
    assert (await client.get(f"/sighting/{mine}/match")).status_code == 404

    async with pool.acquire() as c:
        for (table, column, target) in REFERENCES:
            if target in ("sightings", "captures"):
                n = await c.fetchval(f"SELECT count(*) FROM {table} WHERE {column}=$1", UUID(mine))
                assert n == 0, f"{table}.{column} still points at the deleted {target[:-1]}"
        assert not await c.fetchval("SELECT EXISTS(SELECT 1 FROM sightings WHERE id=$1)", UUID(mine))
        assert not await c.fetchval("SELECT EXISTS(SELECT 1 FROM captures WHERE id=$1)", UUID(mine))
        # The viewer's verdict outlives the pair it was given on (0031).
        verdict = await c.fetchrow("SELECT proposal_id, individual_id FROM confirmations WHERE sighting_id=$1",
                                   UUID(w["theirs"]))
        assert (verdict["proposal_id"], verdict["individual_id"]) == (None, w["dog"])
        # Nothing left to decide on the pending sighting.
        assert await c.fetchval("SELECT match_status FROM sightings WHERE id=$1", UUID(w["pending"])) == "unmatched"
        # The dog's dates are the viewer's sighting's now.
        seen = await c.fetchrow("SELECT first_seen_at, last_seen_at FROM individuals WHERE id=$1", w["dog"])
        theirs_at = await c.fetchval("SELECT captured_at FROM sightings WHERE id=$1", UUID(w["theirs"]))
        assert seen["first_seen_at"] == seen["last_seen_at"] == theirs_at
        # Owed: the post on X (Q6). The media went from the store already.
        assert await c.fetchval("SELECT x_post_id FROM x_post_purges") == "x-111"
        assert await c.fetchval("SELECT count(*) FROM media_purges") == 0
    assert await storage.list_keys(f"sightings/{mine}/") == []


@pytest.mark.asyncio
async def test_a_sighting_used_as_a_match_candidate_can_be_deleted(authed_client):
    """Fails on main: the verdict on the other sighting held the proposal, and
    the proposal could not go with the candidate."""
    client, owner = authed_client
    viewer = await _second_observer(client)
    mine = await _post(client)
    _as(client, viewer)
    theirs = await _post(client)
    pool = await _pool(client)
    async with pool.acquire() as c:
        for verdict, status in (("same", "confirmed"), ("different", "rejected")):
            pid = uuid7()
            await c.execute("""INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, method, status)
                               VALUES ($1,$2,$3,'test',$4)""", pid, UUID(theirs), UUID(mine), status)
            await c.execute("""INSERT INTO confirmations (id, sighting_id, observer_id, proposal_id, verdict)
                               VALUES ($1,$2,$3,$4,$5)""", uuid7(), UUID(theirs), viewer, pid, verdict)
    _as(client, owner)
    assert (await client.delete(f"/sighting/{mine}")).status_code == 204
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM confirmations WHERE proposal_id IS NULL") == 2


@pytest.mark.asyncio
async def test_only_the_owner_can_delete_and_a_second_delete_is_404(authed_client):
    client, owner = authed_client
    mine = await _post(client)
    _as(client, await _second_observer(client))
    for path in (f"/capture/{mine}", f"/sighting/{mine}", f"/capture/{uuid7()}", f"/sighting/{uuid7()}"):
        r = await client.delete(path)
        assert r.status_code == 404
    _as(client, owner)
    assert (await client.delete(f"/sighting/{mine}")).status_code == 204
    assert (await client.delete(f"/sighting/{mine}")).status_code == 404
    assert (await client.delete(f"/capture/{mine}")).status_code == 404
    _as(client, owner)
    client.cookies.clear()
    assert (await client.delete(f"/capture/{mine}")).status_code == 401


async def _published_two_dogs(client, owner):
    from tests.test_suggestions import _two_dog_capture
    pool = await _pool(client)
    cid, (brown, black), storage = await _two_dog_capture(pool, owner, state="needs_review")
    capture = (await client.get(f"/capture/{cid}")).json()
    r = await client.post(f"/capture/{cid}/review", json={
        "revision": capture["revision"], "publish": True,
        "groups": [{"instance_ids": [str(brown)]}, {"instance_ids": [str(black)]}]})
    assert r.status_code == 200, r.text
    async with pool.acquire() as c:
        sids = {r["species_track"]: r["sighting_id"] for r in await c.fetch(
            "SELECT track_id AS species_track, sighting_id FROM animal_instances WHERE capture_id=$1", cid)}
    return cid, sids["t0"], sids["t1"], storage


@pytest.mark.asyncio
async def test_removing_one_dog_keeps_the_photo_and_the_other_dog(authed_client):
    client, owner = authed_client
    pool = await _pool(client)
    cid, brown, black, storage = await _published_two_dogs(client, owner)
    evidence = f"captures/{cid}/evidence-t0.webp"
    from app.photos import thumb_key
    assert thumb_key(evidence) in await storage.list_keys(f"captures/{cid}/")

    assert (await client.delete(f"/sighting/{brown}")).status_code == 204
    await settled()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1", cid) == 1
        assert await c.fetchval("SELECT count(*) FROM animal_instances WHERE capture_id=$1", cid) == 1
        assert await c.fetchval("SELECT count(*) FROM photos WHERE s3_key=$1", f"captures/{cid}/source.webp") == 1
        assert await c.fetchval("SELECT count(*) FROM media_purges") == 0
    assert thumb_key(evidence) not in await storage.list_keys(f"captures/{cid}/")      # its crop went
    assert thumb_key(f"captures/{cid}/evidence-t1.webp") in await storage.list_keys(f"captures/{cid}/")
    # The other dog's details can still be edited: the saved review covers
    # exactly the boxes that are left.
    capture = (await client.get(f"/capture/{cid}")).json()
    assert [g["instance_ids"] for g in capture["groups"]] == [[i["instance_id"] for i in capture["instances"]]]
    r = await client.post(f"/capture/{cid}/review", json={
        "revision": capture["revision"], "publish": True, "groups": capture["groups"]})
    assert r.status_code == 200, r.text

    # The last dog takes the whole upload with it.
    assert (await client.delete(f"/sighting/{black}")).status_code == 204
    await settled()
    async with pool.acquire() as c:
        assert not await c.fetchval("SELECT EXISTS(SELECT 1 FROM captures WHERE id=$1)", cid)
        assert await c.fetchval("SELECT count(*) FROM photos WHERE capture_id=$1", cid) == 0
    assert await storage.list_keys(f"captures/{cid}/") == []


@pytest.mark.asyncio
async def test_delete_photo_takes_every_animal_note_and_transcript(authed_client):
    client, owner = authed_client
    pool = await _pool(client)
    cid, brown, black, storage = await _published_two_dogs(client, owner)
    assert (await client.delete(f"/capture/{cid}")).status_code == 204
    await settled()
    async with pool.acquire() as c:
        for table in ("sightings", "animal_instances", "photos", "voice_transcripts", "detail_suggestions"):
            assert await c.fetchval(f"SELECT count(*) FROM {table} WHERE capture_id=$1", cid) == 0, table
    assert await storage.list_keys(f"captures/{cid}/") == []


@pytest.mark.asyncio
async def test_a_store_that_refuses_owes_the_prefix_until_the_script_runs(authed_client, monkeypatch):
    client, _ = authed_client
    mine = await _post(client)
    from app.storage.s3 import S3Storage
    real_delete = S3Storage.delete

    async def refuse(self, key):
        raise PermissionError("AccessDenied")
    monkeypatch.setattr(S3Storage, "delete", refuse)
    assert (await client.delete(f"/capture/{mine}")).status_code == 204   # never the store's to refuse
    await settled()
    assert mine not in {s["id"] for s in (await client.get("/dex")).json()["sightings"]}
    pool = await _pool(client)
    async with pool.acquire() as c:
        owed = {r["s3_key"]: (r["kind"], r["attempts"], r["last_error"])
                for r in await c.fetch("SELECT * FROM media_purges")}
    # Its photo was stored under sightings/; captures/ held nothing to refuse.
    assert owed == {f"sightings/{mine}/": ("prefix", 1, "PermissionError")}
    storage = get_storage()
    assert await storage.list_keys(f"sightings/{mine}/")                   # still there, and on record

    monkeypatch.setattr(S3Storage, "delete", real_delete)
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import purge_media
    assert await purge_media.run(pool, storage, apply=False) == (1, 0, 0)
    assert await purge_media.run(pool, storage, apply=True) == (1, 1, 0)
    assert await storage.list_keys(f"sightings/{mine}/") == []
    assert await purge_media.run(pool, storage, apply=True) == (0, 0, 0)    # safe to repeat


@pytest.mark.asyncio
async def test_a_worker_finishing_on_a_deleted_upload_is_refused_and_writes_nothing(authed_client, monkeypatch):
    from tests.test_later_voice_notes import _add, _photo_sighting
    from tests.test_transcription_jobs import GPU
    from app.transcription_jobs import MODEL_NAME, PIPELINE_VERSION
    for name, value in (("media_jobs_enabled", True), ("transcription_enabled", True),
                        ("media_gpu_token", "gpu-secret"), ("media_cpu_fallback_enabled", False)):
        monkeypatch.setattr(settings, name, value)
    client, _ = authed_client
    sid = await _photo_sighting(client)
    assert (await _add(client, f"/sighting/{sid}/voice-note")).status_code == 202
    job = (await client.post("/internal/transcription-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    assert job is not None
    assert (await client.delete(f"/capture/{sid}")).status_code == 204
    r = await client.post(f"/internal/transcription-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "pipeline_version": PIPELINE_VERSION, "model": MODEL_NAME,
        "outcome": "transcribed", "language": "en", "text": "too late",
        "segments": [{"start_ms": 0, "end_ms": 1, "text": "too late"}]})
    assert r.status_code == 409
    pool = await _pool(client)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_transcripts") == 0
        assert await c.fetchval("SELECT count(*) FROM voice_notes") == 0


class _FakeX:
    def __init__(self, fail=False):
        self.deleted, self.fail = [], fail

    async def delete_post(self, post_id):
        if self.fail:
            raise RuntimeError("X is down")
        self.deleted.append(post_id)


@pytest.mark.asyncio
async def test_a_post_on_x_is_owed_until_x_confirms(authed_client):
    from app.social import purge_x_owed
    client, _ = authed_client
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO x_post_purges (id, x_post_id, reason) VALUES ($1,'x-1','test'), ($2,'x-2','test')",
                        uuid7(), uuid7())
    assert await purge_x_owed(pool, _FakeX(fail=True)) == (0, 2)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT sum(attempts) FROM x_post_purges") == 2
    x = _FakeX()
    assert await purge_x_owed(pool, x) == (2, 0)
    assert x.deleted == ["x-1", "x-2"]
    assert await purge_x_owed(pool, x) == (0, 0)


@pytest.mark.asyncio
async def test_a_photo_deleted_while_its_post_is_on_the_way_is_owed_a_delete(authed_client):
    """The daily run commits `posting`, then calls X. If the owner deletes the
    photo in between, the post still goes out -- and must be taken down."""
    from app.erasure import erase
    from app.social import run_daily
    client, _ = authed_client
    mine = await _post(client)
    pool, storage = await _pool(client), get_storage()
    async with pool.acquire() as c:
        photo = await c.fetchval("SELECT id FROM photos WHERE sighting_id=$1", UUID(mine))
        await c.execute("UPDATE observers SET social_consent=true, x_handle='someone'")
        await c.execute("UPDATE sightings SET shared_with_team_at=now() WHERE id=$1", UUID(mine))
        await c.execute("INSERT INTO social_picks (id, sighting_id, photo_id, caption, status) VALUES ($1,$2,$3,'hi','queued')",
                        uuid7(), UUID(mine), photo)

    class Racing:
        async def upload_image(self, jpeg):
            return "m-1"

        async def create_post(self, text, media_id):
            assert await erase(pool, storage, "capture", UUID(mine), reason="test")
            return "x-raced"
    result = await run_daily(pool, storage, Racing(), today=date.today(), enabled=True)
    assert (result.outcome, result.x_post_id) == ("posted", "x-raced")
    await settled()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT reason FROM x_post_purges WHERE x_post_id='x-raced'") == "erased_while_posting"


@pytest.mark.asyncio
async def test_the_ledger_is_written_only_when_on_and_a_restore_is_replayed(authed_client, monkeypatch):
    import sys
    from pathlib import Path
    from datetime import datetime, timedelta, timezone
    from app.erasure import Erased, write_ledger
    client, _ = authed_client
    pool, storage = await _pool(client), get_storage()
    for key in await storage.list_keys("erasures/"):
        await storage.delete(key)

    off = await _post(client)
    assert (await client.delete(f"/capture/{off}")).status_code == 204
    assert await storage.list_keys("erasures/") == []

    monkeypatch.setattr(settings, "erasure_ledger_enabled", True)
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    on = await _post(client)
    assert (await client.delete(f"/capture/{on}")).status_code == 204
    (key,) = await storage.list_keys("erasures/")
    assert key.endswith(f"/capture-{on}.json")
    assert set(json.loads(await storage.get(key))) == {"kind", "id", "at"}       # ids only

    # A restore brings a deleted upload back: here, one recorded on the ledger
    # that is still in the database.
    back = await _post(client)
    await write_ledger(storage, Erased("capture", UUID(back)))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import replay_erasures
    assert await replay_erasures.run(pool, storage, since=start, apply=False) == (2, 1, 0)
    assert await replay_erasures.run(pool, storage, since=start, apply=True) == (2, 1, 1)
    await settled()
    assert back not in {s["id"] for s in (await client.get("/dex")).json()["sightings"]}
    assert len(await storage.list_keys("erasures/")) == 2               # the replay adds no entries
    assert await replay_erasures.run(pool, storage, since=start, apply=True) == (2, 0, 0)
    for key in await storage.list_keys("erasures/"):
        await storage.delete(key)


@pytest.mark.asyncio
async def test_a_ledger_the_store_refuses_deletes_nothing(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "erasure_ledger_enabled", True)
    mine = await _post(client)
    from app.storage.s3 import S3Storage
    real_put = S3Storage.put

    async def refuse(self, key, data, content_type):
        if key.startswith("erasures/"):
            raise PermissionError("AccessDenied")
        return await real_put(self, key, data, content_type)
    monkeypatch.setattr(S3Storage, "put", refuse)
    assert (await client.delete(f"/capture/{mine}")).status_code == 503
    assert mine in {s["id"] for s in (await client.get("/dex")).json()["sightings"]}


async def _posting_pick(client, sighting_id, caption="hi"):
    pool = await _pool(client)
    async with pool.acquire() as c:
        photo = await c.fetchval("SELECT id FROM photos WHERE sighting_id=$1", UUID(sighting_id))
        await c.execute("UPDATE observers SET social_consent=true, x_handle='someone'")
        await c.execute("UPDATE sightings SET shared_with_team_at=now() WHERE id=$1", UUID(sighting_id))
        pick = uuid7()
        await c.execute("INSERT INTO social_picks (id, sighting_id, photo_id, caption, status) VALUES ($1,$2,$3,$4,'queued')",
                        pick, UUID(sighting_id), photo, caption)
    return pick


@pytest.mark.asyncio
async def test_an_unconfirmed_post_keeps_a_takedown_for_a_person_to_settle(authed_client):
    """A run that never heard back from X leaves its pick `posting`. Deleting
    the photo deletes the pick, but not what it knew: the post may be on X."""
    client, owner = authed_client
    mine = await _post(client)
    pick = await _posting_pick(client, mine, caption="maybe out")
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute("UPDATE social_picks SET status='posting' WHERE id=$1", pick)
        moderator = await _second_observer(client, "Mod")
        await c.execute("UPDATE observers SET trust_tier='moderator' WHERE id=$1", moderator)
    _as(client, owner)
    assert (await client.delete(f"/capture/{mine}")).status_code == 204
    _as(client, moderator)
    (takedown,) = (await client.get("/social/queue")).json()["takedowns"]
    assert (takedown["unconfirmed"], takedown["caption"], takedown["x_post_id"]) == (True, "maybe out", None)
    from app.social import purge_x_owed
    x = _FakeX()
    assert await purge_x_owed(pool, x) == (0, 0) and x.deleted == []        # nothing to delete without an id

    # The moderator finds it on the account: it is owed like any other.
    assert (await client.post(f"/social/takedown/{takedown['id']}", json={"x_post_id": "123"})).json() == {"status": "owed"}
    assert await purge_x_owed(pool, x) == (1, 0) and x.deleted == ["123"]
    # A second unconfirmed one turns out never to have gone out.
    async with pool.acquire() as c:
        await c.execute("INSERT INTO x_post_purges (id, reason) VALUES ($1, 'test:unconfirmed')", tid := uuid7())
    assert (await client.post(f"/social/takedown/{tid}", json={"x_post_id": None})).json() == {"status": "cleared"}
    assert (await client.get("/social/queue")).json()["takedowns"] == []
    _as(client, owner)
    assert (await client.post(f"/social/takedown/{tid}", json={})).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["uncertain", "rejected"])
async def test_a_photo_deleted_while_x_gives_no_clear_answer(authed_client, outcome):
    """X took the post and the answer was lost (XUncertain): the takedown stays
    for a person. X refused it outright: there is nothing to take down."""
    from app.erasure import erase
    from app.social import run_daily
    from app.x_client import XRejected, XUncertain
    client, _ = authed_client
    mine = await _post(client)
    await _posting_pick(client, mine)
    pool, storage = await _pool(client), get_storage()

    class Racing:
        async def upload_image(self, jpeg):
            return "m-1"

        async def create_post(self, text, media_id):
            assert await erase(pool, storage, "capture", UUID(mine), reason="test")
            raise (XUncertain if outcome == "uncertain" else XRejected)("no clear answer")
    result = await run_daily(pool, storage, Racing(), today=date.today(), enabled=True)
    assert result.outcome == "failed"
    await settled()
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT x_post_id FROM x_post_purges")
    assert [r["x_post_id"] for r in rows] == ([None] if outcome == "uncertain" else [])


@pytest.mark.asyncio
async def test_a_voice_note_saved_as_its_animal_is_deleted_writes_nothing(authed_client, enabled, monkeypatch):
    """The save checked the sighting before the delete; it must check again
    under the capture lock, before any bytes reach the store."""
    from tests.test_later_voice_notes import _add, _photo_sighting
    from app.erasure import erase
    from app.routes import voice_notes
    client, _ = authed_client
    sid = await _photo_sighting(client)
    pool, storage = await _pool(client), get_storage()
    real_count = voice_notes._count
    erased = []

    async def count_then_delete(conn, capture_id, sighting_id):
        n = await real_count(conn, capture_id, sighting_id)
        if not erased:
            erased.append(await erase(pool, storage, "capture", capture_id, reason="test"))
        return n
    monkeypatch.setattr(voice_notes, "_count", count_then_delete)
    r = await _add(client, f"/sighting/{sid}/voice-note")
    assert erased[0] is not None and r.status_code == 404
    await settled()
    assert await storage.list_keys(f"sightings/{sid}/") == []
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM voice_notes") == 0


@pytest.mark.asyncio
async def test_an_answer_about_a_removed_animal_is_never_pinned_on_another(authed_client, enabled):
    """Claim with two animals, remove one, then complete: the worker's letters
    name animals as they were, so its answer is refused and the note is read
    again about the animal left."""
    from tests.test_transcription_jobs import GPU
    client, owner = authed_client
    pool = await _pool(client)
    cid, brown, black, _ = await _published_two_dogs(client, owner)
    job = (await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    assert job is not None
    assert (await client.delete(f"/sighting/{brown}")).status_code == 204
    answer = [{"field": "known_name", "value": "Kaalu", "quote": "The brown one is Kaalu", "animal": "A"},
              {"field": "condition", "value": "injured", "quote": "The black one is limping", "animal": "B"}]
    r = await client.post(f"/internal/suggestion-jobs/{job['id']}/complete", headers=GPU, json={
        "lease_token": job["lease_token"], "content": json.dumps({"suggestions": answer})})
    assert r.status_code == 409
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions WHERE capture_id=$1", cid) == 0
    again = (await client.post("/internal/suggestion-jobs/claim", json={"owner": "dgx-1"}, headers=GPU)).json()["job"]
    assert again is not None and again["id"] == job["id"]


@pytest.mark.asyncio
async def test_an_in_process_answer_about_a_removed_animal_is_dropped(authed_client, monkeypatch):
    import httpx
    from app import suggestions
    from tests.test_suggestions import configured, model_reply  # noqa: F401
    for name, value in (("suggest_api_key", "sk-test"), ("suggest_model", "test-model"),
                        ("suggest_base_url", "https://llm.example/v1")):
        monkeypatch.setattr(settings, name, value)
    client, owner = authed_client
    pool = await _pool(client)
    cid, brown, black, storage = await _published_two_dogs(client, owner)

    async def handler(request):
        # The animal is removed while the model is thinking about both.
        assert (await client.delete(f"/sighting/{brown}")).status_code == 204
        return httpx.Response(200, json=model_reply([
            {"field": "known_name", "value": "Kaalu", "quote": "The brown one is Kaalu", "animal": "A"}]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as llm:
        assert await suggestions.run_once(pool, storage, llm)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM detail_suggestions WHERE capture_id=$1", cid) == 0
        assert await c.fetchval("SELECT suggest_status FROM voice_transcripts WHERE capture_id=$1", cid) == "pending"


@pytest.mark.asyncio
async def test_a_takedown_cleared_too_early_comes_back_when_x_answers_uncertain(authed_client):
    """A moderator checks X before the post appears and clears the takedown;
    then X answers with no clear outcome. The post may be there: a takedown
    must stand again."""
    from app.erasure import erase
    from app.social import run_daily
    from app.x_client import XUncertain
    client, _ = authed_client
    mine = await _post(client)
    await _posting_pick(client, mine, caption="in flight")
    pool, storage = await _pool(client), get_storage()

    class Racing:
        async def upload_image(self, jpeg):
            return "m-1"

        async def create_post(self, text, media_id):
            assert await erase(pool, storage, "capture", UUID(mine), reason="test")
            async with pool.acquire() as c:                 # "Nothing went out"
                assert await c.fetchval("DELETE FROM x_post_purges RETURNING id") is not None
            raise XUncertain("accepted, answer lost")
    assert (await run_daily(pool, storage, Racing(), today=date.today(), enabled=True)).outcome == "failed"
    await settled()
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT x_post_id, caption FROM x_post_purges")
    assert (row["x_post_id"], row["caption"]) == (None, "in flight")
