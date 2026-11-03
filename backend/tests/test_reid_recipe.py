"""Version isolation and cache repair, using synthetic vectors only."""

from dataclasses import replace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from app import capture_contracts, embed, media_jobs
from app.config import settings
from app.ids import uuid7
from app.matching import _to_pgvector, find_candidates, resolve_sighting
from app.reid_recipe import ACTIVE_RECIPE, RECIPES, validate_recipe_versions
from app.routes.sighting import _save_mean_vector
from gpu_worker.inference import preprocessing_identity
from scripts import backfill_embeddings as backfill


def unit(seed):
    v = np.random.default_rng(seed).standard_normal(embed.EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


async def photo(conn, sid, vector, *, model=embed.MODEL_NAME, recipe=ACTIVE_RECIPE):
    pid = uuid7()
    await conn.execute("INSERT INTO photos(id,sighting_id,s3_key) VALUES($1,$2,$3)", pid, sid, f"fixture/{pid}.webp")
    await conn.execute("""INSERT INTO embeddings(id,photo_id,model,recipe,dim,vec_miew)
        VALUES($1,$2,$3,$4,$5,$6::vector)""", uuid7(), pid, model, recipe, embed.EMBED_DIM, _to_pgvector(vector))
    return pid


async def sighting(conn, oid, vector, **kwargs):
    sid = uuid7()
    await conn.execute("""INSERT INTO sightings(id,observer_id,captured_at,processing_state)
        VALUES($1,$2,now(),'ready')""", sid, oid)
    return sid, await photo(conn, sid, vector, **kwargs)


async def cached(conn, sid, vector, model, recipe):
    await conn.execute("""UPDATE sightings SET vec_miew=$2::vector,
        vec_miew_model=$3,vec_miew_recipe=$4 WHERE id=$1""", sid, _to_pgvector(vector), model, recipe)


async def resolve(conn, sid):
    return await resolve_sighting(conn, sid, auto_merge_min=1.01, propose_min=0.9,
                                 radius_m=1000, max_candidates=5, new_uuid=uuid7)


def test_current_recipe_pins_both_unchanged_wire_versions_and_worker_source():
    validate_recipe_versions(ACTIVE_RECIPE, media_jobs.PIPELINE_VERSION,
                             capture_contracts.MULTI_PIPELINE_VERSION, preprocessing_identity())
    assert media_jobs.MODEL_NAME == embed.MODEL_NAME
    assert capture_contracts.MODEL_NAME == embed.MODEL_NAME


@pytest.mark.parametrize("reuse_legacy,reuse_multi", [(True, True), (True, False), (False, True)])
def test_a_new_recipe_cannot_reuse_either_pipeline_version(reuse_legacy, reuse_multi):
    old = RECIPES[0]
    new = replace(old, name="challenger", preprocessing="changed-source",
                  legacy_pipeline=old.legacy_pipeline if reuse_legacy else "legacy-next",
                  multi_pipeline=old.multi_pipeline if reuse_multi else "multi-next")
    with pytest.raises(ValueError, match="bump both"):
        validate_recipe_versions(new.name, new.legacy_pipeline, new.multi_pipeline,
                                 new.preprocessing, recipes=(*RECIPES, new))


def test_both_new_versions_and_a_registered_recipe_are_required():
    old = RECIPES[0]
    new = replace(old, name="challenger", preprocessing="changed-source",
                  legacy_pipeline="legacy-next", multi_pipeline="multi-next")
    with pytest.raises(ValueError, match="register"):
        validate_recipe_versions(new.name, new.legacy_pipeline, new.multi_pipeline, new.preprocessing)
    validate_recipe_versions(new.name, new.legacy_pipeline, new.multi_pipeline,
                             new.preprocessing, recipes=(*RECIPES, new))
    with pytest.raises(ValueError, match="must agree"):
        validate_recipe_versions(old.name, old.legacy_pipeline, old.multi_pipeline, new.preprocessing)


@pytest.mark.asyncio
@pytest.mark.parametrize("model,recipe", [("other-model", ACTIVE_RECIPE),
                                         (embed.MODEL_NAME, "old-recipe"), (None, None)])
async def test_stale_mean_never_overrides_active_frame_evidence(authed_client, model, recipe):
    client, oid = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        target, wrong = unit(1), unit(2)
        query, pid = await sighting(conn, oid, target)
        good, _ = await sighting(conn, oid, target)
        bad, _ = await sighting(conn, oid, wrong)
        await conn.execute("""INSERT INTO embeddings(id,photo_id,model,recipe,dim,vec_miew)
            VALUES($1,$2,'other-model',$3,$4,$5::vector)""", uuid7(), pid, ACTIVE_RECIPE,
            embed.EMBED_DIM, _to_pgvector(wrong))
        await cached(conn, query, wrong, model, recipe)
        out = await resolve(conn, query)
        assert out.status == "proposed"
        assert out.candidates[0].sighting_id == good
        assert out.candidates[0].sighting_id != bad
        assert await conn.fetchval("SELECT candidate_sighting_id FROM match_proposals WHERE id=$1",
                                   out.proposal_ids[0]) == good
    response = await client.get(f"/sighting/{query}/match")
    assert response.status_code == 200
    assert response.json()["candidates"][0]["sighting_id"] == str(good)


@pytest.mark.asyncio
@pytest.mark.parametrize("model,recipe", [("other-model", ACTIVE_RECIPE),
                                         (embed.MODEL_NAME, "old-recipe"), (embed.MODEL_NAME, None)])
async def test_only_inactive_vectors_are_pending_and_cannot_match(authed_client, model, recipe):
    client, oid = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        v = unit(4)
        query, _ = await sighting(conn, oid, v, model=model, recipe=recipe)
        await sighting(conn, oid, v)
        # Even a wrongly tagged cache cannot make a sighting with no active
        # frame evidence matchable. Historical caches are normally untagged.
        await cached(conn, query, v, embed.MODEL_NAME, ACTIVE_RECIPE)
        out = await resolve(conn, query)
        assert out.status == "unmatched" and not out.candidates and not out.proposal_ids
    body = (await client.get(f"/sighting/{query}/match")).json()
    assert body["status"] == "pending"
    assert body["candidates"] == body["proposals"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model,recipe", [("other-model", ACTIVE_RECIPE), (embed.MODEL_NAME, "old-recipe")])
async def test_inactive_gallery_vectors_do_not_take_candidate_slots(authed_client, model, recipe):
    client, oid = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        v = unit(5)
        active, _ = await sighting(conn, oid, v)
        await sighting(conn, oid, v, model=model, recipe=recipe)
        got = await find_candidates(conn, v, lat=None, lng=None, radius_m=1000, limit=5)
    assert [c.sighting_id for c in got] == [active]


@pytest.mark.asyncio
async def test_an_active_mean_is_used_for_a_multi_frame_probe(authed_client):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    q1, q2 = unit(6), unit(7)
    mean = q1 + q2
    mean /= np.linalg.norm(mean)
    async with pool.acquire() as conn:
        query, _ = await sighting(conn, oid, q1)
        await photo(conn, query, q2)
        aggregate, _ = await sighting(conn, oid, mean)
        await sighting(conn, oid, q1)
    await _save_mean_vector(pool, query, [q1, q2])
    async with pool.acquire() as conn:
        assert (await resolve(conn, query)).candidates[0].sighting_id == aggregate


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_empty_or_cancelling_rebuild_clears_the_old_mean_and_tags(authed_client, cancel):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    v = unit(8)
    async with pool.acquire() as conn:
        sid, _ = await sighting(conn, oid, v)
    await _save_mean_vector(pool, sid, [v])
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT vec_miew_recipe FROM sightings WHERE id=$1", sid) == ACTIVE_RECIPE
    await _save_mean_vector(pool, sid, [v, -v] if cancel else [])
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT vec_miew::text,vec_miew_model,vec_miew_recipe FROM sightings WHERE id=$1", sid)
    assert all(value is None for value in row.values())


@pytest.mark.asyncio
async def test_mean_backfill_uses_active_frames_including_capture_crops_and_reruns(authed_client):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    q1, q2, stale = unit(10), unit(11), unit(12)
    expected = q1 + q2
    expected /= np.linalg.norm(expected)
    async with pool.acquire() as conn:
        sid, pid = await sighting(conn, oid, q1)
        await photo(conn, sid, q2)
        await photo(conn, sid, stale, recipe="old-recipe")
        await conn.execute("""INSERT INTO embeddings(id,photo_id,model,recipe,dim,vec_miew)
            VALUES($1,$2,'other-model',$3,$4,$5::vector)""", uuid7(), pid, ACTIVE_RECIPE,
            embed.EMBED_DIM, _to_pgvector(stale))
        await conn.execute("UPDATE photos SET capture_id=$1 WHERE sighting_id=$1", sid)
        await cached(conn, sid, stale, None, None)
    repaired, failed = await backfill.rebuild_means(pool)
    assert repaired == {sid} and failed == 0
    for repeat in range(2):
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT vec_miew::text AS vec,vec_miew_model,vec_miew_recipe FROM sightings WHERE id=$1", sid)
            got = np.asarray([float(v) for v in row["vec"].strip("[]").split(",")])
            assert np.dot(got, expected) > 0.9999
            assert (row["vec_miew_model"], row["vec_miew_recipe"]) == (embed.MODEL_NAME, ACTIVE_RECIPE)
            if repeat == 0:
                # Repair even a stale cache already tagged active. Its healthy
                # tags alone do not trigger another proposal sweep.
                await cached(conn, sid, stale, embed.MODEL_NAME, ACTIVE_RECIPE)
        if repeat == 0:
            repaired, failed = await backfill.rebuild_means(pool)
            assert not repaired and failed == 0


@pytest.mark.asyncio
async def test_backfill_cli_rebuilds_means_when_no_photo_is_pending(authed_client, monkeypatch):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    async with pool.acquire() as conn:
        sid, _ = await sighting(conn, oid, unit(20))
        await cached(conn, sid, unit(21), None, None)
    monkeypatch.setattr(backfill, "effective_dsn", lambda: settings.test_database_url)
    monkeypatch.setattr("sys.argv", ["backfill_embeddings.py"])
    media = AsyncMock()
    monkeypatch.setattr(backfill, "storage_from_settings", lambda: media)
    monkeypatch.setattr(backfill, "embed_photo", lambda _: pytest.fail("no inference needed"))
    assert await backfill.main() == 0
    media.get.assert_not_called()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT vec_miew_recipe FROM sightings WHERE id=$1", sid) == ACTIVE_RECIPE


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_relabel_an_old_mean(authed_client, monkeypatch):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    async with pool.acquire() as conn:
        sid, _ = await sighting(conn, oid, unit(22))
        await cached(conn, sid, unit(23), None, None)
    monkeypatch.setattr(backfill, "effective_dsn", lambda: settings.test_database_url)
    monkeypatch.setattr("sys.argv", ["backfill_embeddings.py", "--dry-run"])
    assert await backfill.main() == 0
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT vec_miew_recipe FROM sightings WHERE id=$1", sid) is None


@pytest.mark.asyncio
async def test_backfill_replaces_an_old_recipe_and_rebuilds_its_mean(authed_client, monkeypatch):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    target = unit(30)
    async with pool.acquire() as conn:
        sid, pid = await sighting(conn, oid, unit(31), recipe="old-recipe")
        await cached(conn, sid, unit(31), embed.MODEL_NAME, "old-recipe")
    media = AsyncMock()
    media.get.return_value = b"synthetic photo"
    monkeypatch.setattr(backfill, "effective_dsn", lambda: settings.test_database_url)
    monkeypatch.setattr(backfill, "storage_from_settings", lambda: media)
    monkeypatch.setattr(backfill, "embed_photo", lambda _: (target, (0, 0, 10, 10)))
    monkeypatch.setattr("sys.argv", ["backfill_embeddings.py"])
    assert await backfill.main() == 0
    media.get.assert_awaited_once()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT recipe FROM embeddings WHERE photo_id=$1", pid) == ACTIVE_RECIPE
        row = await conn.fetchrow("SELECT vec_miew::text AS vec,vec_miew_recipe FROM sightings WHERE id=$1", sid)
        got = np.asarray([float(v) for v in row["vec"].strip("[]").split(",")])
        assert np.dot(got, target) > 0.9999 and row["vec_miew_recipe"] == ACTIVE_RECIPE


@pytest.mark.asyncio
async def test_mean_only_backfill_does_not_expand_the_resolve_sweep(authed_client, monkeypatch):
    from app import matching
    client, oid = authed_client
    pool = client._transport.app.state.pool
    async with pool.acquire() as conn:
        sid, _ = await sighting(conn, oid, unit(40))
        await cached(conn, sid, unit(41), None, None)
    resolver = AsyncMock()
    monkeypatch.setattr(matching, "resolve_sighting", resolver)
    monkeypatch.setattr(backfill, "effective_dsn", lambda: settings.test_database_url)
    monkeypatch.setattr("sys.argv", ["backfill_embeddings.py", "--resolve"])
    assert await backfill.main() == 0
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_two_limited_cli_runs_advance_past_the_repaired_mean(authed_client, monkeypatch):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    async with pool.acquire() as conn:
        first, _ = await sighting(conn, oid, unit(50))
        second, _ = await sighting(conn, oid, unit(51))
        await cached(conn, first, unit(52), None, None)
        await cached(conn, second, unit(53), None, None)
    monkeypatch.setattr(backfill, "effective_dsn", lambda: settings.test_database_url)
    monkeypatch.setattr(backfill, "embed_photo", lambda _: pytest.fail("all frames already exist"))
    monkeypatch.setattr("sys.argv", ["backfill_embeddings.py", "--limit", "1"])
    assert await backfill.main() == 0
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT vec_miew_recipe FROM sightings WHERE id=$1", first) == ACTIVE_RECIPE
        assert await conn.fetchval("SELECT vec_miew_recipe FROM sightings WHERE id=$1", second) is None
    assert await backfill.main() == 0
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT vec_miew_recipe FROM sightings WHERE id=$1", second) == ACTIVE_RECIPE


@pytest.mark.asyncio
async def test_look_alikes_filter_inactive_recipes_on_both_sides(authed_client):
    from app.routes.dogs import _look_alikes
    client, oid = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        ids = []
        for recipe in (ACTIVE_RECIPE, ACTIVE_RECIPE, "old-recipe"):
            sid, _ = await sighting(conn, oid, unit(42), recipe=recipe)
            iid = uuid7()
            ids.append(iid)
            await conn.execute("INSERT INTO individuals(id,created_by) VALUES($1,'feeder')", iid)
            await conn.execute("UPDATE sightings SET individual_id=$2,match_status='confirmed' WHERE id=$1", sid, iid)
        got = await _look_alikes(conn, [ids[0]], oid)
        assert [r["id"] for r in got[ids[0]]] == [str(ids[1])]
        assert await _look_alikes(conn, [ids[2]], oid) == {}


@pytest.mark.asyncio
async def test_rescore_upserts_the_active_recipe_and_rebuilds_the_mean(authed_client, monkeypatch):
    from types import SimpleNamespace
    from scripts import rescore_photos
    client, oid = authed_client
    target = unit(43)
    monkeypatch.setattr(rescore_photos, "embed_analysis", lambda _: target)
    async with client._transport.app.state.pool.acquire() as conn:
        sid, pid = await sighting(conn, oid, unit(44), recipe="old-recipe")
        await cached(conn, sid, unit(44), None, None)
        await rescore_photos._maybe_embed(conn, pid, SimpleNamespace(box=(0, 0, 10, 10)))
        assert await conn.fetchval("SELECT recipe FROM embeddings WHERE photo_id=$1", pid) == ACTIVE_RECIPE
        assert await conn.fetchval("SELECT vec_miew_recipe FROM sightings WHERE id=$1", sid) == ACTIVE_RECIPE
