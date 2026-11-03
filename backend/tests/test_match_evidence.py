"""Proposal evidence follows the actual retrieval hit, without model weights."""

import numpy as np
import pytest

from app.embed import EMBED_DIM, MODEL_NAME
from app.ids import uuid7
from app.matching import _to_pgvector, resolve_sighting


def unit(seed):
    v = np.random.default_rng(seed).standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


async def photo(conn, sid, key, vector=None):
    pid = uuid7()
    await conn.execute("INSERT INTO photos(id,sighting_id,s3_key) VALUES($1,$2,$3)", pid, sid, key)
    if vector is not None:
        await conn.execute("""INSERT INTO embeddings(id,photo_id,model,dim,vec_miew)
            VALUES($1,$2,$3,$4,$5::vector)""", uuid7(), pid, MODEL_NAME, EMBED_DIM, _to_pgvector(vector))
    return pid


async def sighting(conn, oid):
    sid = uuid7()
    await conn.execute("INSERT INTO sightings(id,observer_id,captured_at) VALUES($1,$2,now())", sid, oid)
    return sid


async def proposal(conn, a, b, **kwargs):
    pid = uuid7()
    await conn.execute("""INSERT INTO match_proposals(id,sighting_id,candidate_sighting_id,
        score,method,matched_photo_id,query_photo_ids)
        VALUES($1,$2,$3,0.6,$4,$5,$6)""", pid, a, b, MODEL_NAME,
        kwargs.get("matched_photo_id"), kwargs.get("query_photo_ids"))
    return pid


@pytest.mark.asyncio
async def test_mean_probe_records_all_views_and_the_nonfirst_gallery_instance(authed_client):
    client, oid = authed_client
    pool = client._transport.app.state.pool
    async with pool.acquire() as conn:
        q1, q2 = unit(1), unit(2)
        mean = q1 + q2
        mean /= np.linalg.norm(mean)
        query, gallery = await sighting(conn, oid), await sighting(conn, oid)
        query_photos = [await photo(conn, query, "query-1.webp", q1),
                        await photo(conn, query, "query-2.webp", q2)]
        await photo(conn, query, "query-without-vector.webp")
        first = await photo(conn, gallery, "gallery-first.webp", -mean)
        best = await photo(conn, gallery, "gallery-best.webp", mean)
        iid = uuid7()
        await conn.execute("""INSERT INTO animal_instances(id,capture_id,source_photo_id,
            evidence_photo_id,sighting_id,track_id,species,confidence,bbox,crop_bbox)
            VALUES($1,$2,$3,$4,$2,'one','dog',0.9,'[0,0,1,1]','[0,0,1,1]')""",
            iid, gallery, first, best)
        await conn.execute("UPDATE embeddings SET instance_id=$2 WHERE photo_id=$1", best, iid)
        await conn.execute("UPDATE sightings SET vec_miew=$2::vector WHERE id=$1", query, _to_pgvector(mean))
        result = await resolve_sighting(conn, query, auto_merge_min=1.01, propose_min=0.4,
                                       radius_m=1000, max_candidates=5, new_uuid=uuid7)
        assert result.status == "proposed"
        saved = await conn.fetchrow("SELECT * FROM match_proposals WHERE id=$1", result.proposal_ids[0])
        assert saved["matched_photo_id"] == best
        assert saved["matched_instance_id"] == iid
        assert saved["query_photo_ids"] == query_photos
        # A later embedding must not silently become evidence for this score.
        await photo(conn, query, "later-query-view.webp", unit(3))

    got = (await client.get("/proposals")).json()["proposals"]
    assert len(got) == 1
    a, b = got[0]["a"], got[0]["b"]
    assert [p["photo_id"] for p in a["photos"]] == [str(pid) for pid in query_photos]
    assert b["photo_id"] == str(best)
    assert b["instance_id"] == str(iid)
    assert b["matched"] is True
    assert "gallery-best_thumb.webp" in b["thumb_url"]
    assert "gallery-best.webp" in b["photo_url"]
    assert len(b["photos"]) == 1
    assert "later-query-view" not in str(got)
    assert "query-without-vector" not in str(got)


@pytest.mark.asyncio
async def test_old_proposal_keeps_earliest_photo_fallback(authed_client):
    client, oid = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        a, b = await sighting(conn, oid), await sighting(conn, oid)
        first_a = await photo(conn, a, "old-query-first.webp")
        await photo(conn, a, "old-query-second.webp")
        first_b = await photo(conn, b, "old-gallery-first.webp")
        await photo(conn, b, "old-gallery-second.webp")
        await proposal(conn, a, b)
    got = (await client.get("/proposals")).json()["proposals"][0]
    assert got["a"]["photo_id"] == str(first_a)
    assert got["b"]["photo_id"] == str(first_b)
    assert got["b"]["matched"] is False
    assert len(got["a"]["photos"]) == len(got["b"]["photos"]) == 1


@pytest.mark.asyncio
async def test_deleted_gallery_evidence_falls_back_without_deleting_the_proposal(authed_client):
    client, oid = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        a, b = await sighting(conn, oid), await sighting(conn, oid)
        await photo(conn, a, "query.webp")
        first = await photo(conn, b, "gallery-first.webp")
        best = await photo(conn, b, "gallery-best.webp")
        pid = await proposal(conn, a, b, matched_photo_id=best)
        await conn.execute("DELETE FROM photos WHERE id=$1", best)
    got = (await client.get("/proposals")).json()["proposals"][0]
    assert got["id"] == str(pid)
    assert got["b"]["photo_id"] == str(first)
    assert got["b"]["matched"] is False


@pytest.mark.asyncio
async def test_evidence_ids_cannot_expose_an_unrelated_sighting(authed_client):
    client, oid = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        a, b, unrelated = [await sighting(conn, oid) for _ in range(3)]
        own = await photo(conn, a, "query.webp")
        gallery = await photo(conn, b, "gallery.webp")
        forbidden = await photo(conn, unrelated, "unrelated.webp")
        await proposal(conn, a, b, matched_photo_id=forbidden, query_photo_ids=[own, forbidden])
    response = await client.get("/proposals")
    got = response.json()["proposals"][0]
    assert [p["photo_id"] for p in got["a"]["photos"]] == [str(own)]
    assert got["b"]["photo_id"] == str(gallery)
    assert "unrelated.webp" not in response.text
