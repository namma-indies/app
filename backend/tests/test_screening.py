"""Screening uploads before anyone else sees them, and the held-upload queue (#181).

Two halves. The first proves a held upload is on no surface another person can
read -- the map, dog cards, match proposals and candidates, public counts, the
daily social post -- while its owner still has it, and that approving it puts
it back exactly. The second proves what gets held, through the real upload
route with the detector stubbed.
"""
from app.reid_recipe import ACTIVE_RECIPE
from types import SimpleNamespace
from uuid import UUID

import numpy as np
import pytest

from app.config import settings
from app.ids import uuid7
from app.scene import SceneSignals
from tests.test_blocking import _moderator, _unsigned
from tests.test_dogs_route import _post, _second_observer
from tests.test_erasure import _as, _mentions, _pool, _rich_world


async def _capture_of(pool, sighting_id):
    async with pool.acquire() as c:
        return await c.fetchval("SELECT capture_id FROM sightings WHERE id=$1", UUID(sighting_id))


async def _state(pool, sighting_id):
    async with pool.acquire() as c:
        return await c.fetchrow(
            "SELECT c.screening, c.hold_reasons FROM captures c JOIN sightings s ON s.capture_id=c.id "
            "WHERE s.id=$1", UUID(sighting_id))


async def _public(client, viewer):
    """Everything another person can read, plus what the daily post would take."""
    from app.social import postable_photos
    _as(client, viewer)
    out = {
        "map": (await client.get("/map")).json()["sightings"],
        "dogs": (await client.get("/dogs")).json()["dogs"],
        "proposals": (await client.get("/proposals")).json()["proposals"],
        "stats": (await client.get("/stats")).json()["totals"],
    }
    async with (await _pool(client)).acquire() as c:
        shared = await c.fetch("SELECT id FROM sightings WHERE shared_with_team_at IS NOT NULL ORDER BY id")
        out["postable"] = {str(r["id"]): [str(p) for p in await postable_photos(c, r["id"])] for r in shared}
    return out


# --- held means hidden from everyone else --------------------------------------


@pytest.mark.asyncio
async def test_a_held_upload_is_on_no_shared_surface_until_a_moderator_approves_it(authed_client):
    client, owner = authed_client
    w = await _rich_world(client, owner)       # the owner's photo, entangled every way the app allows
    viewer, mod, mine = w["viewer"], w["moderator"], w["mine"]
    pool = await _pool(client)
    async with pool.acquire() as c:   # so the daily post may take their photo
        await c.execute("UPDATE observers SET social_consent=true, x_handle='owner_handle' WHERE id=$1", owner)
        # Every fixture upload is the same file, and a ruling now re-judges
        # copies (app.identical). Make the fixture a state the identical-photo
        # rule can leave: only `copy` shares the owner's bytes, and a copy is
        # always of the same dog as its original.
        await c.execute("UPDATE photos SET content_sha256 = 'bytes-' || sighting_id::text "
                        "WHERE sighting_id = ANY($1::uuid[])", [UUID(w["theirs"]), UUID(w["pending"])])
        await c.execute("UPDATE sightings SET individual_id = $2, match_status = 'confirmed' WHERE id = $1",
                        UUID(w["copy"]), w["dog"])
    before = await _public(client, viewer)
    assert _mentions(before["map"], mine) and _mentions(before["proposals"], mine)
    assert before["dogs"][0]["sighting_count"] == 2 and before["postable"][mine]

    capture = await _capture_of(pool, mine)
    async with pool.acquire() as c:
        await c.execute("UPDATE captures SET screening='held', hold_reasons='{person}' WHERE id=$1", capture)

    held = await _public(client, viewer)
    for name in ("map", "proposals"):
        assert not _mentions(held[name], mine), f"{name} still shows the held upload"
    assert held["dogs"][0]["sighting_count"] == 1, "the dog card still counts the held upload"
    assert held["stats"]["sightings"] == before["stats"]["sightings"] - 1
    assert held["postable"][mine] == [], "the daily post could still take the held upload"
    # The moderator's view of what is queued to post marks it, as for a takedown.
    _as(client, mod)
    social = (await client.get("/social/queue")).json()
    (posted,) = [p for p in social["posted"] if p["sighting_id"] == mine]
    assert posted["flagged"] is True

    # Its owner still has it, and is told why it is not on the map.
    _as(client, owner)
    (entry,) = [s for s in (await client.get("/dex")).json()["sightings"] if s["id"] == mine]
    assert (entry["on_map"], entry["off_map_reason"]) == (False, "held")

    # Reject keeps it off; approve puts back exactly what was there.
    _as(client, mod)
    r = await client.post(f"/moderation/capture/{capture}/screening", json={"verdict": "reject"})
    assert r.json() == {"status": "ok", "screening": "rejected"}
    assert not _mentions((await _public(client, viewer))["map"], mine)
    _as(client, owner)
    (entry,) = [s for s in (await client.get("/dex")).json()["sightings"] if s["id"] == mine]
    assert entry["off_map_reason"] == "hidden"
    _as(client, mod)
    assert (await client.post(f"/moderation/capture/{capture}/screening", json={"verdict": "approve"})).json()[
        "screening"] == "approved"
    assert _unsigned(await _public(client, viewer)) == _unsigned(before)


@pytest.mark.asyncio
async def test_an_unscreened_upload_is_hidden_too(authed_client):
    """Not yet screened is not public: the filter must not have a window."""
    client, owner = authed_client
    viewer = await _second_observer(client, "Viewer")
    mine = await _post(client)
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute("UPDATE captures SET screening='pending' WHERE id=$1", await _capture_of(pool, mine))
    _as(client, viewer)
    assert not _mentions((await client.get("/map")).json()["sightings"], mine)
    _as(client, owner)
    (entry,) = (await client.get("/dex")).json()["sightings"]
    assert entry["off_map_reason"] == "checking"


@pytest.mark.asyncio
async def test_two_uploads_held_at_once_still_pair_and_the_pair_shows_once_both_are_public(authed_client):
    """Matching searches uploads still being screened, and hides them where
    shown: so two photos of a new dog held together still meet, the pair is
    nobody's to see while held, and appears by itself when both are approved."""
    from app.embed import EMBED_DIM, MODEL_NAME
    from app.matching import resolve_sighting
    client, owner = authed_client
    other, viewer, mod = await _second_observer(client, "Other"), await _second_observer(client, "Viewer"), \
        await _moderator(client)
    first = await _post(client)
    _as(client, other)
    second = await _post(client)
    pool = await _pool(client)
    async with pool.acquire() as c:
        # Two different photos of one dog: the same vector, not the same file.
        await c.execute("UPDATE photos SET content_sha256 = NULL")
        for sid in (first, second):
            await c.execute(f"""INSERT INTO embeddings (id, photo_id, model, dim, vec_miew, recipe)
                                SELECT $1, id, $2, {EMBED_DIM}, array_fill(0.1::real, ARRAY[{EMBED_DIM}])::vector, '{ACTIVE_RECIPE}'
                                FROM photos WHERE sighting_id=$3""", uuid7(), MODEL_NAME, UUID(sid))
            await c.execute("UPDATE captures SET screening='held' WHERE id=$1", await _capture_of(pool, sid))
        outcome = await resolve_sighting(c, UUID(second), auto_merge_min=1.01, propose_min=0.5, radius_m=1000.0,
                                         max_candidates=5, new_uuid=uuid7)
    assert outcome.status == "proposed", "the held earlier upload was not searched"

    # Nobody sees the pair, or the other upload as a candidate, while held.
    _as(client, viewer)
    assert not _mentions((await client.get("/proposals")).json()["proposals"], first)
    _as(client, other)
    seen = (await client.get(f"/sighting/{second}/match")).json()
    assert seen["candidates"] == [] and seen["proposals"] == []

    _as(client, mod)
    for sid in (first, second):
        await client.post(f"/moderation/capture/{await _capture_of(pool, sid)}/screening", json={"verdict": "approve"})
    _as(client, viewer)
    assert _mentions((await client.get("/proposals")).json()["proposals"], first)
    _as(client, other)
    seen = (await client.get(f"/sighting/{second}/match")).json()
    assert [c["sighting_id"] for c in seen["candidates"]] == [first] and len(seen["proposals"]) == 1


@pytest.mark.asyncio
async def test_a_rejected_upload_is_never_searched(authed_client):
    from app.embed import EMBED_DIM, MODEL_NAME
    from app.matching import find_candidates
    client, owner = authed_client
    mine = await _post(client)
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute(f"""INSERT INTO embeddings (id, photo_id, model, dim, vec_miew, recipe)
                            SELECT $1, id, $2, {EMBED_DIM}, array_fill(0.1::real, ARRAY[{EMBED_DIM}])::vector, '{ACTIVE_RECIPE}'
                            FROM photos WHERE sighting_id=$3""", uuid7(), MODEL_NAME, UUID(mine))
        lat, lng = await c.fetchrow("SELECT ST_Y(geog::geometry), ST_X(geog::geometry) FROM sightings WHERE id=$1",
                                    UUID(mine))
        query = [np.full(EMBED_DIM, 0.1, dtype=np.float32)]

        async def candidates():
            return [x.sighting_id for x in await find_candidates(
                c, query, lat=lat, lng=lng, radius_m=500, exclude_sighting_id=None, limit=5)]

        await c.execute("UPDATE captures SET screening='held' WHERE id=$1", await _capture_of(pool, mine))
        assert await candidates() == [UUID(mine)]          # searched while held, hidden where shown
        await c.execute("UPDATE captures SET screening='rejected' WHERE id=$1", await _capture_of(pool, mine))
        assert await candidates() == []


@pytest.mark.parametrize("by", ["photo", "clip"])
@pytest.mark.asyncio
async def test_a_clean_copy_of_a_rejected_upload_is_not_folded_into_it(authed_client, by):
    """The identical-photo and same-clip rule must not make a cleared upload
    the duplicate of one a moderator rejected at screening: duplicates drop
    off the map with their original."""
    from app.matching import resolve_sighting
    client, owner = authed_client
    other, viewer = await _second_observer(client, "Other"), await _second_observer(client, "Viewer")
    rejected = await _post(client)
    _as(client, other)
    copy = await _post(client)
    pool = await _pool(client)
    async with pool.acquire() as c:
        if by == "clip":
            await c.execute("UPDATE photos SET content_sha256 = NULL")
            await c.execute("UPDATE captures SET clip_sha256 = 'same-clip' WHERE id = ANY($1::uuid[])",
                            [await _capture_of(pool, s) for s in (rejected, copy)])
        from app.embed import EMBED_DIM, MODEL_NAME
        for sid in (rejected, copy):         # resolution only runs on an embedded upload
            await c.execute(f"""INSERT INTO embeddings (id, photo_id, model, dim, vec_miew, recipe)
                                SELECT $1, id, $2, {EMBED_DIM}, array_fill(0.1::real, ARRAY[{EMBED_DIM}])::vector, '{ACTIVE_RECIPE}'
                                FROM photos WHERE sighting_id=$3""", uuid7(), MODEL_NAME, UUID(sid))
        await c.execute("UPDATE sightings SET individual_id = NULL, duplicate_of = NULL, match_status = 'unmatched'")
        await c.execute("UPDATE captures SET screening = 'rejected' WHERE id = $1", await _capture_of(pool, rejected))
        await resolve_sighting(c, UUID(copy), auto_merge_min=1.01, propose_min=0.5, radius_m=1000.0,
                               max_candidates=5, new_uuid=uuid7)
        row = await c.fetchrow("SELECT duplicate_of, individual_id FROM sightings WHERE id = $1", UUID(copy))
    assert (row["duplicate_of"], row["individual_id"]) == (None, None)
    _as(client, viewer)
    shown = (await client.get("/map")).json()["sightings"]
    assert _mentions(shown, copy) and not _mentions(shown, rejected)


async def _identical_pair(client, older_state, newer_state):
    """Two observers upload the same file; the older upload is resolved first.
    Returns (older, newer, pool, moderator, viewer)."""
    from app.embed import EMBED_DIM, MODEL_NAME
    from app.reid_recipe import ACTIVE_RECIPE
    other, viewer, mod = await _second_observer(client, "Other"), await _second_observer(client, "Viewer"), \
        await _moderator(client)
    older = await _post(client)
    _as(client, other)
    newer = await _post(client)
    pool = await _pool(client)
    async with pool.acquire() as c:
        for sid in (older, newer):
            await c.execute(f"""INSERT INTO embeddings (id, photo_id, model, dim, vec_miew, recipe)
                                SELECT $1, id, $2, {EMBED_DIM}, array_fill(0.1::real, ARRAY[{EMBED_DIM}])::vector, $4
                                FROM photos WHERE sighting_id=$3""", uuid7(), MODEL_NAME, UUID(sid), ACTIVE_RECIPE)
        await c.execute("UPDATE sightings SET individual_id = NULL, duplicate_of = NULL, match_status = 'unmatched'")
        for sid, state in ((older, older_state), (newer, newer_state)):
            await c.execute("UPDATE captures SET screening = $2 WHERE id = $1", await _capture_of(pool, sid), state)
    return older, newer, pool, mod, viewer


async def _resolve(pool, sid):
    from app.matching import resolve_sighting
    async with pool.acquire() as c:
        await resolve_sighting(c, UUID(sid), auto_merge_min=1.01, propose_min=0.5, radius_m=1000.0,
                               max_candidates=5, new_uuid=uuid7)


async def _copy_of(pool, sid):
    async with pool.acquire() as c:
        found = await c.fetchval("SELECT duplicate_of FROM sightings WHERE id = $1", UUID(sid))
    return str(found) if found else None


@pytest.mark.parametrize("verdict", ["reject", "approve"])
@pytest.mark.asyncio
async def test_a_clean_copy_is_never_hidden_behind_a_held_original(authed_client, verdict):
    """Held original first, clean copy second: the copy counts at once, and a
    ruling on the original settles which of the two is the copy."""
    client, owner = authed_client
    held, clean, pool, mod, viewer = await _identical_pair(client, "held", "clear")
    await _resolve(pool, held)
    await _resolve(pool, clean)
    assert await _copy_of(pool, clean) is None, "a clean upload waits on someone else's moderation"
    _as(client, viewer)
    assert _mentions((await client.get("/map")).json()["sightings"], clean)

    _as(client, mod)
    r = await client.post(f"/moderation/capture/{await _capture_of(pool, held)}/screening",
                          json={"verdict": verdict})
    assert r.status_code == 200
    _as(client, viewer)
    shown = (await client.get("/map")).json()["sightings"]
    if verdict == "reject":
        assert _mentions(shown, clean) and not _mentions(shown, held)
        assert await _copy_of(pool, clean) is None
    else:
        # Both public now: the photo counts once, as for any two clean copies.
        assert await _copy_of(pool, clean) == held
        assert _mentions(shown, held) and not _mentions(shown, clean)


@pytest.mark.parametrize("verdict", ["reject", "approve"])
@pytest.mark.asyncio
async def test_a_held_later_copy_of_a_clean_upload_is_its_copy(authed_client, verdict):
    """Clean original first, held copy second: the held one is the copy, and
    either ruling leaves the clean original showing."""
    client, owner = authed_client
    clean, held, pool, mod, viewer = await _identical_pair(client, "clear", "held")
    await _resolve(pool, clean)
    await _resolve(pool, held)
    assert await _copy_of(pool, held) == clean
    _as(client, mod)
    await client.post(f"/moderation/capture/{await _capture_of(pool, held)}/screening", json={"verdict": verdict})
    _as(client, viewer)
    shown = (await client.get("/map")).json()["sightings"]
    assert _mentions(shown, clean) and not _mentions(shown, held)
    assert await _copy_of(pool, held) == clean and await _copy_of(pool, clean) is None


@pytest.mark.asyncio
async def test_rejecting_an_original_releases_its_copies(authed_client):
    """A copy marked while its original was still pending (and later held)
    counts again once the original is rejected, rather than vanishing with it."""
    client, owner = authed_client
    held, clean, pool, mod, viewer = await _identical_pair(client, "pending", "clear")
    await _resolve(pool, held)
    await _resolve(pool, clean)
    assert await _copy_of(pool, clean) == held       # pending settles by itself, so it may stand for it
    async with pool.acquire() as c:
        await c.execute("UPDATE captures SET screening = 'held' WHERE id = $1", await _capture_of(pool, held))
    _as(client, mod)
    await client.post(f"/moderation/capture/{await _capture_of(pool, held)}/screening", json={"verdict": "reject"})
    assert await _copy_of(pool, clean) is None
    _as(client, viewer)
    assert _mentions((await client.get("/map")).json()["sightings"], clean)


@pytest.mark.parametrize("minutes_ago, trusted_now", [(30, True), (1, False)])
@pytest.mark.asyncio
async def test_uploads_showing_after_the_model_wait_count_towards_trust(migrated_db, screening_on,
                                                                         monkeypatch, minutes_ago, trusted_now):
    """The new-account count is of uploads others can see: `checking` past its
    wait shows (aggregates.screened), so it counts; one still waiting does not."""
    from app.screening import reasons_for
    monkeypatch.setattr(settings, "screening_trusted_after", 2)
    monkeypatch.setattr(settings, "screening_model_wait_s", 600)
    conn, oid = migrated_db, uuid7()
    await conn.execute("INSERT INTO observers(id) VALUES($1)", oid)
    for _ in range(2):
        await conn.execute("""INSERT INTO captures (id, observer_id, kind, captured_at, screening, screened_at)
            VALUES ($1, $2, 'photo', now(), 'checking', now() - $3 * interval '1 minute')""",
                           uuid7(), oid, minutes_ago)
    new = uuid7()
    await conn.execute("INSERT INTO captures (id, observer_id, kind, captured_at) VALUES ($1,$2,'photo',now())",
                       new, oid)
    assert ("new_account" not in await reasons_for(conn, new, oid)) is trusted_now


@pytest.mark.asyncio
async def test_the_daily_post_reads_the_model_wait_when_it_asks(authed_client, monkeypatch):
    """Not frozen at import: changing the wait changes what may be posted."""
    from app.social import postable_photos
    client, owner = authed_client
    mine = await _post(client, share_with_team="true")
    pool = await _pool(client)
    async with pool.acquire() as c:
        await c.execute("UPDATE observers SET social_consent=true, x_handle='owner_handle' WHERE id=$1", owner)
        await c.execute("UPDATE sightings SET shared_with_team_at = now() WHERE id = $1", UUID(mine))
        await c.execute("UPDATE captures SET screening='checking', screened_at = now() - interval '5 minutes' "
                        "WHERE id=$1", await _capture_of(pool, mine))
        monkeypatch.setattr(settings, "screening_model_wait_s", 600)
        assert await postable_photos(c, UUID(mine)) == []
        monkeypatch.setattr(settings, "screening_model_wait_s", 60)
        assert await postable_photos(c, UUID(mine)) != []


@pytest.mark.asyncio
async def test_rejecting_a_held_upload_undoes_its_automatic_links_only(authed_client):
    """Held uploads are matched on purpose; a reject undoes the links that came
    from that and leaves the clean partner's identity alone."""
    client, owner = authed_client
    held, clean, pool, mod, viewer = await _identical_pair(client, "held", "clear")
    await _resolve(pool, held)
    await _resolve(pool, clean)
    async with pool.acquire() as c:
        dog = await c.fetchval("SELECT individual_id FROM sightings WHERE id = $1", UUID(held))
        assert dog is not None and dog == await c.fetchval(
            "SELECT individual_id FROM sightings WHERE id = $1", UUID(clean))
        assert await c.fetchval("SELECT count(*) FROM auto_links WHERE sighting_id = $1", UUID(held)) == 1
    _as(client, mod)
    await client.post(f"/moderation/capture/{await _capture_of(pool, held)}/screening", json={"verdict": "reject"})
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT individual_id, match_status FROM sightings WHERE id = $1", UUID(held))
        assert (row["individual_id"], row["match_status"]) == (None, "unmatched")
        assert await c.fetchval("SELECT count(*) FROM auto_links WHERE sighting_id = $1", UUID(held)) == 0
        assert await c.fetchval("SELECT individual_id FROM sightings WHERE id = $1", UUID(clean)) == dog


@pytest.mark.asyncio
async def test_rejecting_an_upload_keeps_a_link_a_person_confirmed(authed_client):
    client, owner = authed_client
    held, clean, pool, mod, viewer = await _identical_pair(client, "approved", "clear")
    await _resolve(pool, held)
    await _resolve(pool, clean)
    async with pool.acquire() as c:
        dog = await c.fetchval("SELECT individual_id FROM sightings WHERE id = $1", UUID(held))
        await c.execute("INSERT INTO confirmations (id, sighting_id, individual_id, verdict) VALUES ($1,$2,$3,'same')",
                        uuid7(), UUID(held), dog)
    _as(client, mod)
    await client.post(f"/moderation/capture/{await _capture_of(pool, held)}/screening", json={"verdict": "reject"})
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT individual_id FROM sightings WHERE id = $1", UUID(held)) == dog


# --- what gets held ---------------------------------------------------------------


def _detector(monkeypatch, scene=SceneSignals(), *, fail=False):
    """Stub the YOLO pass: an animal-free analysis carrying `scene`."""
    from app import analyse as analyse_mod

    def analyse(raw):
        if fail:
            raise RuntimeError("detector unavailable")
        return SimpleNamespace(dog_confidence=0.9, cat_confidence=0.0, box=None, image=None,
                               has_animal=False, scene=scene)
    monkeypatch.setattr(analyse_mod, "analyse", analyse)


@pytest.fixture
def trusted(screening_on, monkeypatch):
    """Screening on, with the new-account rule off, to test the photo rules alone.
    Off is the default; pinned here so a changed default cannot reword these tests."""
    monkeypatch.setattr(settings, "screening_trusted_after", 0)
    return screening_on


@pytest.mark.asyncio
async def test_a_clean_upload_from_a_trusted_account_goes_public(authed_client, trusted, monkeypatch):
    _detector(monkeypatch, SceneSignals(person=0.9, person_area=0.05, indoor=0.2))   # a passer-by
    client, _ = authed_client
    viewer = await _second_observer(client, "Viewer")
    mine = await _post(client)
    assert dict(await _state(await _pool(client), mine)) == {"screening": "clear", "hold_reasons": []}
    _as(client, viewer)
    assert _mentions((await client.get("/map")).json()["sightings"], mine)


@pytest.mark.parametrize("scene, reasons", [
    (SceneSignals(person=0.95, person_area=0.45), ["person"]),
    (SceneSignals(indoor=0.8), ["indoor"]),
    (SceneSignals(person=0.9, person_area=0.3, indoor=0.7), ["person", "indoor"]),
])
@pytest.mark.asyncio
async def test_people_filling_the_frame_or_an_indoor_scene_are_held(authed_client, trusted, monkeypatch,
                                                                      scene, reasons):
    _detector(monkeypatch, scene)
    client, owner = authed_client
    viewer, mod = await _second_observer(client, "Viewer"), await _moderator(client)
    mine = await _post(client)
    state = await _state(await _pool(client), mine)
    assert (state["screening"], list(state["hold_reasons"])) == ("held", reasons)
    _as(client, viewer)
    assert not _mentions((await client.get("/map")).json()["sightings"], mine)
    _as(client, mod)
    (item,) = (await client.get("/moderation/held")).json()["items"]
    assert item["reasons"] == reasons and item["observer"] == "Tester"
    assert [f["flagged"] for f in item["frames"]] == [True] and item["frame_count"] == 1
    assert item["frames"][0]["url"] != item["frames"][0]["thumb_url"]
    _as(client, owner)
    assert (await client.get("/dex")).json()["sightings"][0]["off_map_reason"] == "held"


@pytest.mark.asyncio
async def test_every_frame_is_open_to_the_moderator_and_the_one_that_held_it_comes_first(
        authed_client, trusted, monkeypatch):
    """Approval shows every frame, so a moderator must be able to see every
    frame -- above all the one that held it, wherever it is in the upload."""
    from app import analyse as analyse_mod
    from tests.test_dogs_route import _jpeg
    seen = []

    def analyse(raw):
        seen.append(raw)
        person = SceneSignals(person=.95, person_area=.5) if len(seen) == 6 else SceneSignals()
        return SimpleNamespace(dog_confidence=.9, cat_confidence=0, box=None, image=None,
                               has_animal=False, scene=person)
    monkeypatch.setattr(analyse_mod, "analyse", analyse)
    client, _ = authed_client
    mod = await _moderator(client)
    r = await client.post("/sighting", files=[("photos", (f"{i}.jpg", _jpeg(), "image/jpeg")) for i in range(6)],
                          data={"geo_source": "none", "captured_at": "2026-07-19T09:00:00Z"})
    sid = r.json()["sighting_id"]
    sixth = UUID(r.json()["photo_ids"][5])
    pool = await _pool(client)
    assert list((await _state(pool, sid))["hold_reasons"]) == ["person"]
    _as(client, mod)
    (item,) = (await client.get("/moderation/held")).json()["items"]
    assert item["frame_count"] == 6
    assert [f["flagged"] for f in item["frames"]] == [True, False, False, False]
    async with pool.acquire() as c:
        sixth_key = await c.fetchval("SELECT s3_key FROM photos WHERE id=$1", sixth)
    assert sixth_key.rsplit(".", 1)[0] in item["frames"][0]["thumb_url"]
    frames = (await client.get(f"/moderation/capture/{item['capture_id']}/frames")).json()["frames"]
    assert len(frames) == 6 and [f["flagged"] for f in frames].count(True) == 1
    assert all(f["url"] and f["thumb_url"] for f in frames)
    # Moderators only, and only for an upload screening stopped.
    _as(client, await _second_observer(client, "Viewer"))
    assert (await client.get(f"/moderation/capture/{item['capture_id']}/frames")).status_code == 404


@pytest.mark.asyncio
async def test_a_detector_failure_holds_rather_than_publishing_blind(authed_client, trusted, monkeypatch):
    _detector(monkeypatch, fail=True)
    client, _ = authed_client
    mine = await _post(client)
    state = await _state(await _pool(client), mine)
    assert (state["screening"], list(state["hold_reasons"])) == ("held", ["unscreened"])


@pytest.mark.asyncio
async def test_by_default_a_brand_new_accounts_clean_first_upload_goes_public(authed_client, screening_on,
                                                                           monkeypatch):
    """Product default: new accounts do not wait for a moderator. The photo
    rules still hold their uploads like anyone else's."""
    from app.config import Settings
    assert Settings.model_fields["screening_trusted_after"].default == 0
    assert settings.screening_trusted_after == 0
    _detector(monkeypatch)
    client, _ = authed_client
    viewer = await _second_observer(client, "Viewer")
    first = await _post(client)
    assert dict(await _state(await _pool(client), first)) == {"screening": "clear", "hold_reasons": []}
    _as(client, viewer)
    assert _mentions((await client.get("/map")).json()["sightings"], first)


@pytest.mark.asyncio
async def test_a_new_accounts_first_uploads_wait_for_a_moderator(authed_client, screening_on, monkeypatch):
    monkeypatch.setattr(settings, "screening_trusted_after", 2)
    _detector(monkeypatch)
    client, owner = authed_client
    mod = await _moderator(client)
    pool = await _pool(client)
    first, second = await _post(client), await _post(client)
    for sid in (first, second):
        assert list((await _state(pool, sid))["hold_reasons"]) == ["new_account"]
    # A rejected upload does not count towards trust.
    _as(client, mod)
    await client.post(f"/moderation/capture/{await _capture_of(pool, first)}/screening", json={"verdict": "reject"})
    _as(client, owner)
    assert list((await _state(pool, await _post(client)))["hold_reasons"]) == ["new_account"]
    _as(client, mod)
    for sid in (first, second):
        await client.post(f"/moderation/capture/{await _capture_of(pool, sid)}/screening", json={"verdict": "approve"})
    _as(client, owner)
    assert (await _state(pool, await _post(client)))["screening"] == "clear"
    # A moderator is not new, but the photo rules still apply to them.
    _as(client, mod)
    assert (await _state(pool, await _post(client)))["screening"] == "clear"


@pytest.mark.asyncio
async def test_an_upload_whose_screening_never_finished_reaches_the_queue(authed_client, screening_on,
                                                                          monkeypatch):
    from app.routes import sighting as sighting_route

    async def never(*args, **kwargs):
        return None
    monkeypatch.setattr(sighting_route, "_analyse_and_save", never)   # the worker died mid-flight
    client, _ = authed_client
    mod = await _moderator(client)
    mine = await _post(client)
    assert (await _state(await _pool(client), mine))["screening"] == "pending"
    _as(client, mod)
    assert (await client.get("/moderation/held")).json()["items"] == []        # still within its time
    monkeypatch.setattr(settings, "screening_stale_s", 0)
    (item,) = (await client.get("/moderation/held")).json()["items"]
    assert item["reasons"] == ["not_screened"]
    await client.post(f"/moderation/capture/{item['capture_id']}/screening", json={"verdict": "approve"})
    _as(client, await _second_observer(client, "Viewer"))
    assert _mentions((await client.get("/map")).json()["sightings"], mine)


@pytest.mark.asyncio
async def test_only_moderators_rule_and_only_on_what_screening_stopped(authed_client, trusted, monkeypatch):
    _detector(monkeypatch)
    client, owner = authed_client
    mod = await _moderator(client)
    capture = await _capture_of(await _pool(client), await _post(client))      # screened clear
    assert (await client.get("/moderation/held")).status_code == 404
    r = await client.post(f"/moderation/capture/{capture}/screening", json={"verdict": "approve"})
    assert r.status_code == 404
    _as(client, mod)
    r = await client.post(f"/moderation/capture/{capture}/screening", json={"verdict": "reject"})
    assert r.status_code == 404, "a clear upload is a matter for reports, not screening"


@pytest.mark.asyncio
async def test_screening_off_publishes_on_save(authed_client, monkeypatch):
    """The suite's default (conftest) and the switch an operator has."""
    _detector(monkeypatch, SceneSignals(person=0.95, person_area=0.9))
    client, _ = authed_client
    mine = await _post(client)
    assert (await _state(await _pool(client), mine))["screening"] == "clear"


# --- the worker paths ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_worker_completion_is_screened_on_the_signals_it_reports(migrated_db, trusted):
    from app.capture_jobs import complete_capture_job
    from tests.test_multi_captures import seed, tracked
    conn = migrated_db
    body = tracked(("dog",))
    body = body.model_copy(update={"frames": [f.model_copy(update={"person": 0.97, "person_area": 0.6, "indoor": 0.0})
                                              for f in body.frames]})
    _, cid, jid, storage = await seed(conn, body)
    await conn.execute("UPDATE captures SET screening='pending' WHERE id=$1", cid)
    await complete_capture_job(conn, storage, jid, body)
    row = await conn.fetchrow("SELECT screening, hold_reasons FROM captures WHERE id=$1", cid)
    assert (row["screening"], list(row["hold_reasons"])) == ("held", ["person"])
    stored = await conn.fetchrow("SELECT person, person_area FROM detections WHERE photo_id=$1", body.frames[0].photo_id)
    assert stored["person_area"] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_a_worker_that_reports_no_signals_yet_is_not_held_for_them(migrated_db, trusted):
    """So the API can be deployed before the worker: not measured is not a failure."""
    from app.capture_jobs import complete_capture_job
    from tests.test_multi_captures import seed, tracked
    conn = migrated_db
    body = tracked(("dog",))
    _, cid, jid, storage = await seed(conn, body)
    await conn.execute("UPDATE captures SET screening='pending' WHERE id=$1", cid)
    await complete_capture_job(conn, storage, jid, body)
    assert await conn.fetchval("SELECT screening FROM captures WHERE id=$1", cid) == "clear"
    assert await conn.fetchval("SELECT person FROM detections WHERE photo_id=$1", body.frames[0].photo_id) is None


def test_scene_signals_measure_the_largest_confident_person():
    from app.scene import scene_signals
    rows = [
        (0, 0, 50, 50, 0.4, 0),       # unsure: scored, not measured
        (0, 0, 20, 10, 0.9, 0),       # 200 px of a 10,000 px frame
        (0, 0, 10, 10, 0.6, 59),      # a bed
        (0, 0, 90, 90, 0.99, 16),     # the dog, which is none of this
    ]
    s = scene_signals(rows, (100, 100))
    assert s.person == pytest.approx(0.9)
    assert s.person_area == pytest.approx(0.02)
    assert s.indoor == pytest.approx(0.6)
