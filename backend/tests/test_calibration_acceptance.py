"""End-to-end calibration acceptance, independent of the scoring implementation."""
import math

import pytest

from app.embed import EMBED_DIM, MODEL_NAME
from app.ids import uuid7
from tests.test_moderation_animals import _moderator, _pool

pytestmark = pytest.mark.asyncio
BASE = "/moderation/match-calibration"


async def _observation(conn, angle, *, mean_angle=None):
    sid, pid, oid = uuid7(), uuid7(), uuid7()
    vector = [math.cos(angle), math.sin(angle)] + [0.0] * (EMBED_DIM - 2)
    await conn.execute("INSERT INTO observers(id) VALUES($1)", oid)
    await conn.execute(
        "INSERT INTO sightings(id,observer_id,captured_at,species) VALUES($1,$2,now(),'dog')",
        sid, oid,
    )
    await conn.execute(
        "INSERT INTO photos(id,sighting_id,s3_key,width,height) VALUES($1,$2,$3,400,300)",
        pid, sid, f"calibration-test/{pid}.webp",
    )
    await conn.execute(
        "INSERT INTO embeddings(id,photo_id,model,dim,vec_miew,bbox) "
        "VALUES($1,$2,$3,$4,$5::vector,'[20,30,300,250]')",
        uuid7(), pid, MODEL_NAME, EMBED_DIM, str(vector),
    )
    if mean_angle is not None:
        mean = [math.cos(mean_angle), math.sin(mean_angle)] + [0.0] * (EMBED_DIM - 2)
        await conn.execute("UPDATE sightings SET vec_miew=$2::vector WHERE id=$1", sid, str(mean))
    return sid


async def _batch(client, count=100):
    response = await client.post(BASE, json={"sample_size": count})
    assert response.status_code == 201, response.text
    return response.json()


async def test_batches_are_paginated_and_older_reviews_remain_accessible(app_client):
    await _moderator(app_client)
    created = [await _batch(app_client) for _ in range(3)]
    first = (await app_client.get(BASE, params={"limit": 2})).json()
    assert [b["id"] for b in first["batches"]] == [created[2]["id"], created[1]["id"]]
    assert first["next_offset"] == 2
    last = (await app_client.get(BASE, params={"limit": 2, "offset": 2})).json()
    assert [b["id"] for b in last["batches"]] == [created[0]["id"]]
    assert last["next_offset"] is None


async def test_calibration_is_private_to_moderators(app_client):
    from app.security import issue_session
    unknown = uuid7()
    paths = [BASE, f"{BASE}/{unknown}/pairs", f"{BASE}/{unknown}/metrics",
             f"{BASE}/{unknown}/pairs/{uuid7()}"]
    for path in paths:
        assert (await app_client.get(path)).status_code == 401
    assert (await app_client.post(BASE, json={"sample_size": 20})).status_code == 401
    oid = uuid7()
    async with (await _pool(app_client)).acquire() as conn:
        await conn.execute("INSERT INTO observers(id,trust_tier) VALUES($1,'feeder')", oid)
    app_client.cookies.set("session", issue_session(oid))
    for path in paths:
        assert (await app_client.get(path)).status_code == 404
    assert (await app_client.post(BASE, json={"sample_size": 20})).status_code == 404


async def test_reviewed_labels_drive_counts_without_identity_side_effects(app_client):
    await _moderator(app_client)
    pool = await _pool(app_client)
    async with pool.acquire() as conn:
        await _observation(conn, 0)
        await _observation(conn, math.pi / 2)
        await _observation(conn, math.pi)
    batch = await _batch(app_client)
    assert batch["eligible_pairs"] == batch["sampled_pairs"] == 3
    path = f"{BASE}/{batch['id']}"
    page = (await app_client.get(path + "/pairs")).json()
    assert len(page["items"]) == 3
    assert all(pair["score"] < 0.5 for pair in page["items"])
    pair = page["items"][0]
    detail = (await app_client.get(f"{path}/pairs/{pair['id']}")).json()
    assert detail["left"]["photos"] and detail["right"]["photos"]
    response = await app_client.post(f"{path}/pairs/{pair['id']}/label", json={
        "verdict": "same", "evidence_token": detail["evidence_token"],
    })
    assert response.status_code == 200, response.text
    metrics = (await app_client.get(path + "/metrics")).json()
    assert metrics["same"] == 1
    assert metrics["unreviewed"] == 2
    at_cutoff = next(row for row in metrics["cutoffs"] if row["threshold"] == 0.5)
    assert at_cutoff["fn"] == 1
    assert at_cutoff["tp"] == at_cutoff["fp"] == 0
    assert at_cutoff["precision"] is None
    assert at_cutoff["recall"] == 0
    assert metrics["suggested_threshold"] is None
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM individuals") == 0
        assert await conn.fetchval("SELECT count(*) FROM confirmations") == 0
        assert await conn.fetchval("SELECT count(*) FROM match_proposals") == 0
        assert await conn.fetchval("SELECT count(*) FROM sightings WHERE individual_id IS NOT NULL") == 0


async def test_real_legacy_bbox_is_normalized_for_review(app_client):
    await _moderator(app_client)
    async with (await _pool(app_client)).acquire() as conn:
        sid = await _observation(conn, 0)
        await _observation(conn, 0.1)
        await conn.execute(
            "UPDATE embeddings SET bbox='{\"x1\":20,\"y1\":30,\"x2\":300,\"y2\":250}' "
            "WHERE photo_id IN (SELECT id FROM photos WHERE sighting_id=$1)", sid,
        )
    batch = await _batch(app_client)
    path = f"{BASE}/{batch['id']}"
    pair = (await app_client.get(path + "/pairs")).json()["items"][0]
    detail = (await app_client.get(f"{path}/pairs/{pair['id']}")).json()
    for side in ("left", "right"):
        assert detail[side]["photos"][0]["bbox"] == [20, 30, 300, 250]


async def test_actual_configured_cutoff_is_reported_without_changing_it(app_client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "reid_propose_min", 0.37)
    await _moderator(app_client)
    async with (await _pool(app_client)).acquire() as conn:
        await _observation(conn, 0)
        await _observation(conn, 0.1)
    batch = await _batch(app_client)
    path = f"{BASE}/{batch['id']}"
    metrics = (await app_client.get(path + "/metrics")).json()
    assert metrics["current_threshold"] == 0.37
    assert 0.37 in {row["threshold"] for row in metrics["cutoffs"]}
    assert settings.reid_propose_min == 0.37
    assert metrics["suggested_threshold"] is None


async def test_score_uses_newer_query_mean_not_best_query_frame(app_client):
    await _moderator(app_client)
    async with (await _pool(app_client)).acquire() as conn:
        older = await _observation(conn, 0)
        newer = await _observation(conn, 0, mean_angle=math.pi / 2)
        await conn.execute("UPDATE sightings SET created_at='2026-01-01' WHERE id=$1", older)
        await conn.execute("UPDATE sightings SET created_at='2026-01-02' WHERE id=$1", newer)
    batch = await _batch(app_client)
    path = f"{BASE}/{batch['id']}"
    pair = (await app_client.get(path + "/pairs")).json()["items"][0]
    assert pair["score"] == pytest.approx(0, abs=1e-6)
    detail = (await app_client.get(f"{path}/pairs/{pair['id']}")).json()
    assert detail["left"]["sighting_id"] == str(newer)
    assert detail["right"]["sighting_id"] == str(older)


async def test_review_correction_replaces_label_and_unsure_excludes_metrics(app_client):
    await _moderator(app_client)
    async with (await _pool(app_client)).acquire() as conn:
        await _observation(conn, 0)
        await _observation(conn, 0.1)
    batch = await _batch(app_client)
    path = f"{BASE}/{batch['id']}"
    pair = (await app_client.get(path + "/pairs")).json()["items"][0]
    detail = (await app_client.get(f"{path}/pairs/{pair['id']}")).json()
    for verdict in ("same", "same", "different", "unsure"):
        response = await app_client.post(f"{path}/pairs/{pair['id']}/label", json={
            "verdict": verdict, "evidence_token": detail["evidence_token"],
        })
        assert response.status_code == 200, response.text
        metrics = (await app_client.get(path + "/metrics")).json()
        assert metrics[verdict] == 1
        assert metrics["same"] + metrics["different"] + metrics["unsure"] == 1
    assert all(row["precision"] is None and row["recall"] is None for row in metrics["cutoffs"])


async def test_moderator_disagreement_is_not_counted_as_two_independent_pairs(app_client):
    await _moderator(app_client)
    async with (await _pool(app_client)).acquire() as conn:
        await _observation(conn, 0)
        await _observation(conn, 0.1)
    batch = await _batch(app_client)
    path = f"{BASE}/{batch['id']}"
    pair = (await app_client.get(path + "/pairs")).json()["items"][0]
    detail = (await app_client.get(f"{path}/pairs/{pair['id']}")).json()
    for verdict in ("same", "different"):
        if verdict == "different":
            await _moderator(app_client)
        response = await app_client.post(f"{path}/pairs/{pair['id']}/label", json={
            "verdict": verdict, "evidence_token": detail["evidence_token"],
        })
        assert response.status_code == 200, response.text
    metrics = (await app_client.get(path + "/metrics")).json()
    assert metrics["conflict"] == 1
    assert metrics["same"] == metrics["different"] == 0
    assert all(row["precision"] is None and row["recall"] is None for row in metrics["cutoffs"])


async def test_existing_label_becomes_ineligible_after_embedding_change(app_client):
    await _moderator(app_client)
    pool = await _pool(app_client)
    async with pool.acquire() as conn:
        sid = await _observation(conn, 0)
        await _observation(conn, 0.1)
    batch = await _batch(app_client)
    path = f"{BASE}/{batch['id']}"
    pair = (await app_client.get(path + "/pairs")).json()["items"][0]
    detail = (await app_client.get(f"{path}/pairs/{pair['id']}")).json()
    body = {"verdict": "same", "evidence_token": detail["evidence_token"]}
    assert (await app_client.post(f"{path}/pairs/{pair['id']}/label", json=body)).status_code == 200
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE embeddings SET bbox='[0,0,100,100]' WHERE photo_id IN "
            "(SELECT id FROM photos WHERE sighting_id=$1)", sid,
        )
    assert (await app_client.post(f"{path}/pairs/{pair['id']}/label", json=body)).status_code == 409
    metrics = (await app_client.get(path + "/metrics")).json()
    assert metrics["stale"] == 1
    assert metrics["same"] == 0
    assert all(row["precision"] is None and row["recall"] is None for row in metrics["cutoffs"])
