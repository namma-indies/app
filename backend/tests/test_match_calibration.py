"""Synthetic vectors exercise calibration without inference or production data."""
import asyncio
import json
import random
from uuid import UUID

import pytest

from app import match_calibration as cal
from app.config import settings
from app.embed import EMBED_DIM, MODEL_NAME
from app.ids import uuid7
from app.security import issue_session

BASE = "/moderation/match-calibration"


def vector(axis=0, sign=1):
    v = [0] * EMBED_DIM
    v[axis] = sign
    return json.dumps(v)


async def moderator(client):
    oid = uuid7()
    async with client._transport.app.state.pool.acquire() as conn:
        await conn.execute("INSERT INTO observers(id,trust_tier,created_via) VALUES($1,'moderator','test')", oid)
    client.cookies.set("session", issue_session(oid))
    return oid


async def sighting(conn, *, owner=None, axis=0, sign=1, species=None, geo=None,
                   capture=None, status="valid", animal=True, model=MODEL_NAME):
    owner = owner or uuid7()
    await conn.execute("INSERT INTO observers(id,created_via) VALUES($1,'test') ON CONFLICT DO NOTHING", owner)
    sid, pid = uuid7(), uuid7()
    await conn.execute("""INSERT INTO sightings(id,observer_id,captured_at,species,capture_id,
        review_status,animal_override,geog) VALUES($1,$2,now(),$3,$4,$5,$6,
        CASE WHEN $7::text IS NULL THEN NULL ELSE ST_GeogFromText($7) END)""",
        sid, owner, species, capture, status, animal, geo)
    await conn.execute("INSERT INTO photos(id,sighting_id,s3_key,width,height) VALUES($1,$2,$3,640,480)",
                       pid, sid, f"calibration/{pid}.webp")
    await conn.execute("""INSERT INTO embeddings(id,photo_id,model,dim,vec_miew,bbox)
        VALUES($1,$2,$3,$4,$5::vector,'[10,20,300,400]')""", uuid7(), pid, model, EMBED_DIM, vector(axis,sign))
    return sid, pid


async def sample(client, size=100):
    response = await client.post(BASE, json={"sample_size": size})
    assert response.status_code == 201, response.text
    batch = response.json()
    items = (await client.get(f"{BASE}/{batch['id']}/pairs?limit=100")).json()["items"]
    return batch, items


async def detail(client, batch, pair):
    response = await client.get(f"{BASE}/{batch['id']}/pairs/{pair['id']}")
    assert response.status_code == 200, response.text
    return response.json()


async def label(client, batch, pair, verdict, token=None):
    if token is None:
        token = (await detail(client,batch,pair))["evidence_token"]
    return await client.post(f"{BASE}/{batch['id']}/pairs/{pair['id']}/label",
                             json={"verdict":verdict,"evidence_token":token})


async def test_complete_uniform_population_and_stable_pagination(app_client, monkeypatch):
    await moderator(app_client)
    monkeypatch.setattr(cal.secrets, "randbits", lambda bits: 42)
    monkeypatch.setattr(settings,"reid_propose_min",0.95)
    pool = app_client._transport.app.state.pool
    async with pool.acquire() as conn:
        owner = uuid7()
        a, _ = await sighting(conn,owner=owner)
        b, _ = await sighting(conn,owner=owner,axis=1)
        c, _ = await sighting(conn,sign=-1)
        d, _ = await sighting(conn,axis=2)
    batch, items = await sample(app_client)
    assert batch["eligible_pairs"] == batch["sampled_pairs"] == 6
    assert sorted(p["score"] for p in items) == [-1,0,0,0,0,0]
    assert set(batch)=={"id","created_at","eligible_pairs","sampled_pairs","policy","model","radius_m"}
    assert (await app_client.get(BASE)).json()["batches"][0] == batch
    first = (await app_client.get(f"{BASE}/{batch['id']}/pairs?limit=2")).json()
    second = (await app_client.get(f"{BASE}/{batch['id']}/pairs?offset=2&limit=2")).json()
    assert first["items"]+second["items"] == items[:4]
    assert first["next_offset"] == 2
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT query_id,candidate_id FROM match_calibration_pairs WHERE batch_id=$1 ORDER BY ordinal",UUID(batch["id"]))
        universe = [(b,a),(c,a),(c,b),(d,a),(d,b),(d,c)]
        assert [tuple(r) for r in rows] == random.Random(42).sample(universe,6)
        stored = await conn.fetchrow("SELECT settings,sampling FROM match_calibration_batches WHERE id=$1",UUID(batch["id"]))
        assert json.loads(stored["sampling"])["seed"] == 42
        assert json.loads(stored["settings"])["observer_filter"] is None
    evidence = await detail(app_client,batch,items[0])
    assert evidence["left"]["photos"][0]["bbox"] == [10,20,300,400]
    assert "observer" not in json.dumps(evidence) and "geog" not in json.dumps(evidence)
    batch2, items2 = await sample(app_client,2)
    assert batch2["eligible_pairs"] == 6 and len(items2)==2
    assert (await app_client.get(f"{BASE}/{batch2['id']}/metrics")).json()["unreviewed"] == 2


async def test_eligibility_radius_species_capture_and_sibling_identity(migrated_db):
    c = migrated_db
    mod = uuid7()
    await c.execute("INSERT INTO observers(id,created_via) VALUES($1,'test')",mod)
    older, _ = await sighting(c, species="dog", geo="POINT(77 12)")
    query, _ = await sighting(c, species="dog", geo="POINT(77.0001 12)")
    cat, _ = await sighting(c, species="cat", geo="POINT(77 12)")
    far, _ = await sighting(c, species="dog", geo="POINT(78 12)")
    unknown, _ = await sighting(c, species=None)
    sibling, _ = await sighting(c, capture=query, species="dog",geo="POINT(77 12)")
    await sighting(c,status="pending")
    await sighting(c,status="rejected")
    await sighting(c,animal=False)
    await sighting(c,model="old-model")
    batch = await cal.create_batch(c,mod,500)
    rows = await c.fetch("SELECT query_id,candidate_id FROM match_calibration_pairs WHERE batch_id=$1",batch["id"])
    pairs = {tuple(r) for r in rows}
    assert (query,older) in pairs
    assert (cat,query) not in pairs and (far,query) not in pairs
    assert (unknown,far) in pairs and (unknown,cat) in pairs
    assert (sibling,query) not in pairs
    individual = uuid7()
    await c.execute("INSERT INTO individuals(id,created_by) VALUES($1,'feeder')",individual)
    await c.execute("UPDATE sightings SET individual_id=$1 WHERE id=ANY($2::uuid[])",individual,[older,sibling])
    new = await cal.create_batch(c,mod,500)
    assert not await c.fetchval("SELECT 1 FROM match_calibration_pairs WHERE batch_id=$1 AND query_id=$2 AND candidate_id=$3",new["id"],query,older)


async def test_mean_query_and_max_candidate_score(migrated_db):
    c = migrated_db
    owner = uuid7()
    a, _ = await sighting(c,owner=owner,axis=0)
    q, _ = await sighting(c,owner=owner,axis=1)
    first = await cal.create_batch(c,owner,10)
    assert await c.fetchval("SELECT score FROM match_calibration_pairs WHERE batch_id=$1",first["id"]) == 0
    await c.execute("UPDATE sightings SET vec_miew=$2::vector WHERE id=$1",q,vector(0))
    second = await cal.create_batch(c,owner,10)
    assert await c.fetchval("SELECT score FROM match_calibration_pairs WHERE batch_id=$1",second["id"]) == 1
    # A second candidate photo gives the mean query a perfect match even if its first frame does not.
    await c.execute("UPDATE sightings SET vec_miew=$2::vector WHERE id=$1",q,vector(2))
    _, photo = await sighting(c,owner=owner,axis=2)
    await c.execute("UPDATE photos SET sighting_id=$1 WHERE id=$2",a,photo)
    third = await cal.create_batch(c,owner,10)
    assert await c.fetchval("SELECT score FROM match_calibration_pairs WHERE batch_id=$1",third["id"]) == 1
    pairs = await cal.validated_pairs(c, await cal.batch(c,third["id"]),owner)
    assert len(pairs[0]["manifest"]["right"]["photos"]) == 2


async def test_labels_conflicts_unsure_and_no_identity_side_effects(app_client,monkeypatch):
    first_mod = await moderator(app_client)
    async with app_client._transport.app.state.pool.acquire() as c:
        await sighting(c)
        await sighting(c)
    batch, pairs = await sample(app_client)
    before_threshold = settings.reid_propose_min
    for verdict in ("same","same","unsure"):
        assert (await label(app_client,batch,pairs[0],verdict)).status_code == 200
    metrics = (await app_client.get(f"{BASE}/{batch['id']}/metrics")).json()
    assert metrics["unsure"] == metrics["reviewed_pairs"] == 1
    assert all(c["precision"] is None and c["recall"] is None for c in metrics["cutoffs"])
    await moderator(app_client)
    await label(app_client,batch,pairs[0],"different")
    assert (await detail(app_client,batch,pairs[0]))["status"] == "conflict"
    app_client.cookies.set("session",issue_session(first_mod))
    await label(app_client,batch,pairs[0],"different")
    metrics = (await app_client.get(f"{BASE}/{batch['id']}/metrics")).json()
    assert metrics["different"] == 1 and metrics["conflict"] == 0
    assert metrics["suggested_threshold"] is None
    async with app_client._transport.app.state.pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM match_calibration_labels") == 2
        for table in ("individuals","match_proposals","confirmations"):
            assert await c.fetchval(f"SELECT count(*) FROM {table}") == 0
        assert not await c.fetchval("SELECT 1 FROM sightings WHERE individual_id IS NOT NULL OR match_status <> 'unmatched'")
    assert settings.reid_propose_min == before_threshold


@pytest.mark.parametrize("change",["vector","bbox","mean","association","species","review","photo","sibling"])
async def test_changed_evidence_is_sticky_stale_and_label_rejected(app_client,change):
    await moderator(app_client)
    async with app_client._transport.app.state.pool.acquire() as c:
        await sighting(c)
        sid, pid = await sighting(c)
    batch, pairs = await sample(app_client)
    token = (await detail(app_client,batch,pairs[0]))["evidence_token"]
    await label(app_client,batch,pairs[0],"same",token)
    async with app_client._transport.app.state.pool.acquire() as c:
        if change=="vector":
            await c.execute("UPDATE embeddings SET vec_miew=$2::vector WHERE photo_id=$1",pid,vector(1))
        elif change=="bbox":
            await c.execute("UPDATE embeddings SET bbox='[1,2,3,4]' WHERE photo_id=$1",pid)
        elif change=="mean":
            await c.execute("UPDATE sightings SET vec_miew=$2::vector WHERE id=$1",sid,vector(1))
        elif change=="association":
            iid=uuid7()
            await c.execute("INSERT INTO individuals(id,created_by) VALUES($1,'feeder')",iid)
            await c.execute("UPDATE sightings SET individual_id=$2 WHERE id=$1",sid,iid)
        elif change=="species":
            await c.execute("UPDATE sightings SET species='cat' WHERE id=$1",sid)
        elif change=="review":
            await c.execute("UPDATE sightings SET review_status='pending' WHERE id=$1",sid)
        elif change=="photo":
            await c.execute("DELETE FROM photos WHERE id=$1",pid)
        else:
            await sighting(c,capture=sid)
    assert (await label(app_client,batch,pairs[0],"different",token)).status_code == 409
    stale_detail = await detail(app_client,batch,pairs[0])
    assert stale_detail["status"] == "stale"
    assert stale_detail["left"]["photos"] == stale_detail["right"]["photos"] == []
    metrics = (await app_client.get(f"{BASE}/{batch['id']}/metrics")).json()
    assert metrics["stale"] == 1 and metrics["same"] == metrics["reviewed_pairs"] == 0
    async with app_client._transport.app.state.pool.acquire() as c:
        assert await c.fetchval("SELECT verdict FROM match_calibration_labels") == "same"
        assert await c.fetchval("SELECT stale FROM match_calibration_pairs")


def test_exact_cutoffs_and_exploratory_suggestion(monkeypatch):
    monkeypatch.setattr(settings,"reid_propose_min",0.345678901)
    batch={"eligible_pairs":200,"sampled_pairs":40}
    pairs=[{"score":0.345678901,"status":"same"}]*20 + [{"score":0.2,"status":"different"}]*20
    result=cal.metrics(batch,pairs)
    cutoff=next(c for c in result["cutoffs"] if c["threshold"]==settings.reid_propose_min)
    assert (cutoff["tp"],cutoff["fp"],cutoff["fn"],cutoff["tn"]) == (20,0,0,20)
    assert cutoff["precision"] == cutoff["recall"] == 1
    assert result["suggested_threshold"] == settings.reid_propose_min
    assert "40/40" in result["suggestion_note"] and "not street-wide" in result["suggestion_note"]
    assert cal.metrics(batch,pairs[:-1])["suggested_threshold"] is None
    empty=cal.metrics({"eligible_pairs":0,"sampled_pairs":0},[])
    assert all(c["precision"] is None and c["recall"] is None for c in empty["cutoffs"])


@pytest.mark.parametrize("same_score,different_score", [(0.36, 0.34), (-0.1, -0.2)])
def test_suggestion_finds_separator_between_deciles_and_below_zero(monkeypatch, same_score, different_score):
    monkeypatch.setattr(settings, "reid_propose_min", 0.5)
    pairs = [{"score": same_score, "status": "same"}] * 20 + [{"score": different_score, "status": "different"}] * 20
    result = cal.metrics({"eligible_pairs": 40, "sampled_pairs": 40}, pairs)
    assert result["suggested_threshold"] == same_score
    row = next(c for c in result["cutoffs"] if c["threshold"] == same_score)
    assert row["precision"] == row["recall"] == 1
    assert row["tp"] == 20 and row["fp"] == 0


@pytest.mark.parametrize("cap",["MAX_SIGHTINGS","MAX_PAIRS","MAX_VECTORS","MAX_REVIEW_PHOTOS"])
async def test_caps_fail_without_partial_batches(app_client,monkeypatch,cap):
    await moderator(app_client)
    async with app_client._transport.app.state.pool.acquire() as c:
        for _ in range(3):
            await sighting(c)
    monkeypatch.setattr(cal,cap,1)
    response=await app_client.post(BASE,json={"sample_size":2})
    assert response.status_code==422,response.text
    assert (await app_client.get(BASE)).json()=={"batches":[], "next_offset": None}


@pytest.mark.parametrize("target", ["frame", "mean"])
async def test_invalid_vectors_rejected_before_random_sampling(app_client, monkeypatch, target):
    await moderator(app_client)
    async with app_client._transport.app.state.pool.acquire() as c:
        await sighting(c)
        sid, pid = await sighting(c)
        zero = json.dumps([0] * EMBED_DIM)
        if target == "mean":
            await c.execute("UPDATE sightings SET vec_miew=$2::vector WHERE id=$1", sid, zero)
        else:
            await c.execute("UPDATE embeddings SET vec_miew=$2::vector WHERE photo_id=$1", pid, zero)
    def unexpected_draw(*args):
        pytest.fail("invalid corpus must be rejected before the sampling draw")
    monkeypatch.setattr(cal.secrets, "randbits", unexpected_draw)
    response = await app_client.post(BASE, json={"sample_size": 1})
    assert response.status_code == 422
    assert (await app_client.get(BASE)).json()["batches"] == []


async def test_auth_validation_and_missing_resources(app_client):
    assert (await app_client.get(BASE)).status_code==401
    async with app_client._transport.app.state.pool.acquire() as c:
        oid=uuid7()
        await c.execute("INSERT INTO observers(id,created_via) VALUES($1,'test')",oid)
    app_client.cookies.set("session",issue_session(oid))
    routes=[BASE,f"{BASE}/{uuid7()}/pairs",f"{BASE}/{uuid7()}/metrics",f"{BASE}/{uuid7()}/pairs/{uuid7()}"]
    for route in routes:
        assert (await app_client.get(route)).status_code==404
    assert (await app_client.post(BASE,json={"sample_size":1})).status_code==404
    assert (await app_client.post(f"{BASE}/{uuid7()}/pairs/{uuid7()}/label",json={"verdict":"same","evidence_token":"a"})).status_code==404
    await moderator(app_client)
    for size in (0,501):
        assert (await app_client.post(BASE,json={"sample_size":size})).status_code==422
    assert (await app_client.get(routes[1])).status_code==404
    batch,_=await sample(app_client)
    assert batch["eligible_pairs"] == batch["sampled_pairs"] == 0
    assert (await app_client.get(f"{BASE}/{batch['id']}/pairs/{uuid7()}")).status_code==404
    assert (await app_client.get(f"{BASE}/{batch['id']}/pairs?offset=-1")).status_code==422


@pytest.mark.parametrize("embedding", ["absent", "old-model"])
async def test_unembedded_sighting_photos_are_reviewed_and_fingerprinted(app_client, embedding):
    await moderator(app_client)
    pool = app_client._transport.app.state.pool
    async with pool.acquire() as c:
        await sighting(c)
        sid, _ = await sighting(c)
        pid = uuid7()
        await c.execute("INSERT INTO photos(id,sighting_id,s3_key) VALUES($1,$2,'unembedded.webp')", pid, sid)
        if embedding != "absent":
            await c.execute("INSERT INTO embeddings(id,photo_id,model,dim,vec_miew) VALUES($1,$2,$3,$4,$5::vector)",
                            uuid7(), pid, "old-model", EMBED_DIM, vector(1))
    batch, pairs = await sample(app_client)
    evidence = await detail(app_client, batch, pairs[0])
    assert len(evidence["left"]["photos"]) == 2
    assert pairs[0]["score"] == 1
    token = evidence["evidence_token"]
    assert (await label(app_client, batch, pairs[0], "same", token)).status_code == 200
    async with pool.acquire() as c:
        await c.execute("DELETE FROM photos WHERE id=$1", pid)
    assert (await label(app_client, batch, pairs[0], "different", token)).status_code == 409
    assert (await detail(app_client, batch, pairs[0]))["status"] == "stale"


async def test_unembedded_crop_retains_animal_focus_without_exposing_capture_source(app_client):
    await moderator(app_client)
    async with app_client._transport.app.state.pool.acquire() as c:
        await sighting(c)
        sid, _ = await sighting(c)
        source, crop, instance = uuid7(), uuid7(), uuid7()
        await c.execute("INSERT INTO photos(id,capture_id,s3_key) VALUES($1,$2,'private-source.webp')", source, sid)
        await c.execute("INSERT INTO photos(id,sighting_id,s3_key,width,height) VALUES($1,$2,'unembedded-crop.webp',140,140)", crop, sid)
        await c.execute("""INSERT INTO animal_instances(id,capture_id,source_photo_id,evidence_photo_id,
            sighting_id,track_id,species,confidence,bbox,crop_bbox)
            VALUES($1,$2,$3,$4,$2,'dog-1','dog',.9,'[20,30,120,130]','[10,10,150,150]')""",
            instance, sid, source, crop)
    batch, pairs = await sample(app_client)
    evidence = await detail(app_client, batch, pairs[0])
    photos = evidence["left"]["photos"]
    assert len(photos) == 2
    assert next(p for p in photos if p["id"] == str(crop))["bbox"] == [10, 20, 110, 120]
    assert "private-source.webp" not in json.dumps(evidence)


async def test_crop_evidence_and_instance_fingerprint(app_client):
    await moderator(app_client)
    pool = app_client._transport.app.state.pool
    async with pool.acquire() as c:
        await sighting(c)
        sid, pid = await sighting(c)
        source, instance = uuid7(), uuid7()
        await c.execute("INSERT INTO photos(id,capture_id,s3_key) VALUES($1,$2,'source.webp')",source,sid)
        await c.execute("""INSERT INTO animal_instances(id,capture_id,source_photo_id,evidence_photo_id,
            sighting_id,track_id,species,confidence,bbox,crop_bbox)
            VALUES($1,$2,$3,$4,$2,'dog-1','dog',.9,'[20,30,120,130]','[10,10,150,150]')""",
            instance,sid,source,pid)
        await c.execute("UPDATE embeddings SET instance_id=$2 WHERE photo_id=$1",pid,instance)
    batch,pairs = await sample(app_client)
    evidence = await detail(app_client,batch,pairs[0])
    assert evidence["left"]["photos"][0]["bbox"] == [10,20,110,120]
    assert len(evidence["left"]["photos"]) == 1
    assert "source.webp" not in json.dumps(evidence)
    assert (await label(app_client,batch,pairs[0],"same","wrong-token")).status_code == 409
    assert (await detail(app_client,batch,pairs[0]))["status"] == "unreviewed"
    async with pool.acquire() as c:
        await c.execute("UPDATE animal_instances SET crop_bbox='[0,0,150,150]' WHERE id=$1",instance)
    assert (await label(app_client,batch,pairs[0],"same",evidence["evidence_token"])).status_code == 409


async def test_app_role_can_create_validate_and_label(migrated_db):
    c = migrated_db
    owner=uuid7()
    await sighting(c,owner=owner)
    await sighting(c,owner=owner)
    await c.execute("SET ROLE app_rw")
    try:
        batch=await cal.create_batch(c,owner,10)
        async with cal.snapshot(c,label=True):
            pairs=await cal.validated_pairs(c,await cal.batch(c,batch["id"]),owner)
            assert pairs[0]["status"]=="unreviewed"
            await c.execute("INSERT INTO match_calibration_labels(pair_id,moderator_id,verdict) VALUES($1,$2,'same')",pairs[0]["id"],owner)
    finally:
        await c.execute("RESET ROLE")


async def test_private_table_grants(migrated_db):
    for table in ("match_calibration_batches","match_calibration_pairs","match_calibration_labels"):
        assert await migrated_db.fetchval("SELECT has_table_privilege('app_rw',$1,'SELECT,INSERT,UPDATE,DELETE')",table)
        assert not await migrated_db.fetchval("SELECT has_table_privilege('public_read',$1,'SELECT')",table)


async def test_label_does_not_hold_evidence_tables_while_waiting_for_another_review(app_client):
    await moderator(app_client)
    pool = app_client._transport.app.state.pool
    async with pool.acquire() as c:
        await sighting(c)
        sid, _ = await sighting(c)
    batch, pairs = await sample(app_client)
    token = (await detail(app_client, batch, pairs[0]))["evidence_token"]
    async with pool.acquire() as other:
        async with other.transaction():
            await other.execute("SELECT id FROM match_calibration_pairs WHERE id=$1 FOR UPDATE", UUID(pairs[0]["id"]))
            response = await asyncio.wait_for(label(app_client, batch, pairs[0], "same", token), timeout=1)
            assert response.status_code == 503
            await other.execute("SET LOCAL lock_timeout='200ms'")
            await other.execute("UPDATE sightings SET animal_confidence=.9 WHERE id=$1", sid)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM match_calibration_labels") == 0


async def test_label_yields_to_rescore_then_revalidates(app_client):
    await moderator(app_client)
    pool=app_client._transport.app.state.pool
    async with pool.acquire() as c:
        await sighting(c)
        _,pid=await sighting(c)
    batch,pairs=await sample(app_client)
    token=(await detail(app_client,batch,pairs[0]))["evidence_token"]
    async with pool.acquire() as writer:
        async with writer.transaction():
            await writer.execute("UPDATE embeddings SET vec_miew=$2::vector WHERE photo_id=$1",pid,vector(1))
            response = await asyncio.wait_for(label(app_client,batch,pairs[0],"same",token), timeout=1)
            assert response.status_code == 503, response.text
            # The failed review must release its earlier sightings lock, so a
            # writer using embeddings-before-sightings cannot deadlock with it.
            await writer.execute("SET LOCAL lock_timeout='200ms'")
            await writer.execute("UPDATE sightings SET animal_confidence=.9 WHERE id=(SELECT sighting_id FROM photos WHERE id=$1)", pid)
        response = await label(app_client,batch,pairs[0],"same",token)
    assert response.status_code==409,response.text
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM match_calibration_labels")==0
