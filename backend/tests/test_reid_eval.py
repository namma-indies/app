"""Known answers and private-output invariants; no production data or models."""

import copy
import json
import math
import socket
from datetime import datetime, timedelta, timezone
from uuid import UUID

import numpy as np
import pytest

from app.embed import EMBED_DIM, MODEL_NAME
from app.ids import uuid7
from app.matching import _to_pgvector
from app.reid_eval.core import copy_families, evaluate, label_graph, ranking_metrics, ratio, split_plan
from app.reid_eval.schema import validate_metrics, validate_snapshot
from app.reid_eval.snapshot import take_snapshot
from scripts.reid_eval import main, write_private


def uid(n):
    return str(UUID(int=n))


def observation(n, **changes):
    row = {"id": uid(n), "capture_id": uid(1000+n), "created_ms": n*86400000,
           "captured_day": n, "available_ms": n*86400000+1000, "verified": True,
           "embedded": True, "eligible": True, "review": "valid", "species": "dog",
           "media": "photo", "pipeline": "legacy", "geo": "located", "copy_family": uid(n),
           "detection": "animal", "probe": "frames", "mean_state": "absent", "video_recipe": "not_video"}
    row.update(changes)
    return row


def label(a, b, verdict="same"):
    return {"a": a, "b": b, "verdict": verdict, "source": "owner"}


def world(rows, labels=(), overrides=None):
    overrides = overrides or {}
    pairs = []
    for q in rows:
        for c in rows:
            if (c["created_ms"], c["id"]) >= (q["created_ms"], q["id"]):
                continue
            value = .1 if q["embedded"] and c["embedded"] else None
            pair = {"query": q["id"], "candidate": c["id"], "model_score": value,
                    "policy_score": value, "ann_score": value, "within_reach": True,
                    "reverse_within_reach": True, "species_ok": True, "capture_ok": True,
                    "observer_relation": "same"}
            pair.update(overrides.get((q["id"], c["id"]), {}))
            pairs.append(pair)
    return {"meta": {"schema": 1, "snapshot_id": uid(9000), "recipe_id": uid(9001),
                     "as_of_ms": 100000000000, "threshold": .4, "cap": 5, "ann_shortlist": 50,
                     "phash_distance": 4, "holdout_fraction": .3, "seed": 189, "folds": 3,
                     "versioning": "baseline_unversioned", "replay": "counterfactual_current_state",
                     "orphan_labels": 0, "truncated": False},
            "observations": rows, "labels": list(labels), "pairs": pairs}


def report(data):
    return evaluate(data, split="holdout", decision_id=uid(9002))


def scored(value):
    return {"model_score": value, "policy_score": value, "ann_score": value}


def test_known_answer_rankings_and_cluster_intervals():
    assert ranking_metrics(["unknown", "a", "negative", "b"], {"a", "b"}) == (0, 1, .5)
    assert ranking_metrics(["a", "b"], {"a", "b"}) == (1, 1, 1)
    estimate = ratio([("one", 1, 2), ("two", 3, 4)], seed=1)
    assert estimate["value"] == pytest.approx(4/6)
    assert estimate["groups"] == 2
    assert estimate["ci_low"] <= estimate["value"] <= estimate["ci_high"]
    assert ratio([]) == {"hits": 0, "total": 0, "groups": 0, "status": "undefined"}


@pytest.mark.parametrize("stage", ["unscored", "detection", "embedding", "gallery_vector", "eligibility",
                                  "species", "capture", "vicinity", "ann", "cap", "threshold", "queue", "proposed"])
def test_funnel_attributes_the_first_loss(stage):
    gallery, query = observation(1), observation(20)
    edge = scored(.8)
    distractors = []
    if stage in ("unscored", "detection", "embedding"):
        query.update(embedded=False, probe="none", detection={"unscored": "unscored", "detection": "no_animal", "embedding": "animal"}[stage])
        edge = scored(None)
    elif stage == "gallery_vector":
        gallery.update(embedded=False, probe="none")
        edge = scored(None)
    elif stage == "eligibility":
        gallery["eligible"] = False
    elif stage in ("species", "capture"):
        edge[stage+"_ok"] = False
    elif stage == "vicinity":
        edge["within_reach"] = False
    elif stage == "ann":
        edge["ann_score"] = None
    elif stage == "cap":
        distractors = [observation(n, verified=False) for n in range(2, 7)]
    elif stage == "threshold":
        edge = scored(.39)
    elif stage == "queue":
        query["review"] = "pending"
    edges = {(query["id"], gallery["id"]): edge}
    edges.update({(query["id"], row["id"]): scored(.95) for row in distractors})
    got = report(world([gallery, *distractors, query], [label(query["id"], gallery["id"])], edges))
    assert got["populations"]["repeat_encounters"] == 1
    assert got["funnel"][stage] == 1 and sum(got["funnel"].values()) == 1
    assert got["policy"]["coverage_at_5"]["value"] == int(stage == "proposed")


def test_no_vector_encounter_stays_in_the_policy_denominator():
    gallery, query = observation(1), observation(2, embedded=False, probe="none", detection="no_animal")
    got = report(world([gallery, query], [label(gallery["id"], query["id"])]))
    assert got["populations"] == {"verified_encounters": 1, "embedded_verified_encounters": 0,
                                  "repeat_encounters": 1, "model_repeat_encounters": 0, "repeat_components": 1}
    assert got["policy"]["coverage_at_5"]["total"] == 1
    assert got["model"]["top1"]["status"] == "undefined"
    assert "value" not in got["model"]["top1"]
    assert got["inventory"]["unembedded_verified"] == 1


def test_unknown_prompts_are_never_negative_labels():
    a, unknown, negative, q = observation(1), observation(2, verified=False), observation(3, verified=False), observation(20)
    data = world([a, unknown, negative, q], [label(a["id"], q["id"]), label(negative["id"], q["id"], "different")],
                 {(q["id"], a["id"]): scored(.7), (q["id"], unknown["id"]): scored(.9), (q["id"], negative["id"]): scored(.8)})
    got = report(data)
    assert got["model"]["top1"]["value"] == 0
    assert got["model"]["top5"]["value"] == 1
    assert got["model"]["mean_ap"]["value"] == pytest.approx(1/3)
    assert got["model"]["unknown_before_first_true"] == 1
    assert got["policy"]["unknown_prompts"] == 1
    assert got["policy"]["precision_on_labelled_prompts"]["value"] == .5
    assert got["policy"]["wrong_per_correct"]["value"] == 1


def test_contradictions_quarantine_the_whole_positive_component():
    a, b, q = observation(1), observation(2), observation(20)
    data = world([a, b, q], [label(a["id"], b["id"]), label(b["id"], q["id"]), label(a["id"], q["id"], "different")])
    got = report(data)
    assert got["inventory"]["conflicting_pairs"] == 1
    assert got["inventory"]["conflicted_encounters"] == 3
    assert got["populations"]["verified_encounters"] == 0
    assert got["status"] == "inconclusive"


def test_copy_families_group_exact_near_and_source_copies_without_identity_labels():
    rows = [observation(n) for n in range(1, 5)]
    photos = [{"sighting_id": rows[0]["id"], "content_sha256": "a"*64, "phash": "0000000000000001"},
              {"sighting_id": rows[1]["id"], "content_sha256": "a"*64, "phash": None},
              {"sighting_id": rows[2]["id"], "content_sha256": None, "phash": "0000000000000003"},
              {"sighting_id": rows[3]["id"], "content_sha256": None, "phash": None,
               "source_sha256": "a"*64}]
    families = copy_families(rows, photos, phash_distance=1)
    assert len(set(families.values())) == 1
    roots, conflicts, _, _ = label_graph(world(rows))
    assert len(set(roots.values())) == 4 and not conflicts


def test_no_copy_family_or_human_component_crosses_development_folds():
    rows = [observation(n) for n in range(1, 11)]
    rows[8]["copy_family"] = rows[0]["copy_family"]  # crosses the time boundary
    rows[3]["copy_family"] = rows[1]["copy_family"]  # same side, same fold
    data = world(rows, [label(rows[1]["id"], rows[2]["id"])])
    for seed in range(10):
        data["meta"]["seed"] = seed
        plan, counts = split_plan(data)
        assert plan[rows[0]["id"]]["split"] == plan[rows[8]["id"]]["split"] == "quarantined"
        assert plan[rows[1]["id"]]["fold"] == plan[rows[2]["id"]]["fold"] == plan[rows[3]["id"]]["fold"]
        dev = [r["created_ms"] for r in rows if plan[r["id"]]["split"] == "development"]
        holdout = [r["created_ms"] for r in rows if plan[r["id"]]["split"] == "holdout"]
        assert max(dev) < counts["cutoff_ms"] <= min(holdout)


def test_holdout_is_locked_by_default_and_incomplete_snapshots_are_refused():
    a, q = observation(1), observation(2)
    data = world([a, q], [label(a["id"], q["id"])], {(q["id"], a["id"]): scored(.8)})
    assert evaluate(data)["populations"]["repeat_encounters"] == 0
    with pytest.raises(ValueError, match="decision"):
        evaluate(data, split="holdout")
    data["pairs"].clear()
    with pytest.raises(ValueError, match="complete"):
        validate_snapshot(data)


@pytest.mark.parametrize("field", ["lat", "lng", "distance", "geog", "observer_id", "s3_key", "url", "vector", "free_text"])
def test_snapshot_and_metrics_reject_extra_fields_at_nested_levels(field):
    a, q = observation(1), observation(2)
    data = world([a, q], [label(a["id"], q["id"])])
    bad = copy.deepcopy(data)
    bad["observations"][0][field] = "forbidden"
    with pytest.raises(ValueError):
        validate_snapshot(bad)
    metrics = report(data)
    metrics["slices"]["dog"][field] = "forbidden"
    with pytest.raises(ValueError):
        validate_metrics(metrics)


def test_allowed_fields_reject_free_text_nonfinite_scores_and_encounter_ids_in_metrics():
    a, q = observation(1), observation(2)
    data = world([a, q], [label(a["id"], q["id"])])
    bad = copy.deepcopy(data)
    bad["labels"][0]["source"] = "private text"
    with pytest.raises(ValueError):
        validate_snapshot(bad)
    data["pairs"][0]["model_score"] = math.nan
    with pytest.raises(ValueError):
        validate_snapshot(data)
    data["pairs"][0]["model_score"] = .1
    metrics = report(data)
    metrics["failures"] = [a["id"]]
    with pytest.raises(ValueError):
        validate_metrics(metrics)


def test_score_is_offline_and_outputs_are_private_frozen_and_aggregate_only(tmp_path, monkeypatch):
    a, q = observation(1), observation(2)
    data = world([a, q], [label(a["id"], q["id"])])
    snap, out = tmp_path/"snapshot.json", tmp_path/"metrics.json"
    write_private(snap, data, validate_snapshot)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("scoring is offline"))
    assert main(["score", "--snap", str(snap), "--out", str(out)]) == 0
    metrics = json.loads(out.read_text())
    validate_metrics(metrics)
    assert out.stat().st_mode & 0o777 == 0o600
    assert a["id"] not in out.read_text() and q["id"] not in out.read_text()
    with pytest.raises(FileExistsError):
        write_private(out, metrics, validate_metrics)
    link = tmp_path/"existing-link.json"
    link.symlink_to(out)
    with pytest.raises(FileExistsError):
        write_private(link, metrics, validate_metrics)


async def db_encounter(conn, oid, day, *, cosine=1.0, verified=True, located=False, sha=None):
    sid, pid = uuid7(), uuid7()
    created = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
    await conn.execute("""INSERT INTO sightings(id,observer_id,captured_at,created_at,animal_override,species)
        VALUES($1,$2,$3,$3,$4,'dog')""", sid, oid, created, True if verified else None)
    if located:
        await conn.execute("UPDATE sightings SET geog=ST_SetSRID(ST_MakePoint(0,0),4326)::geography WHERE id=$1", sid)
    await conn.execute("INSERT INTO photos(id,sighting_id,s3_key,content_sha256) VALUES($1,$2,'synthetic', $3)", pid,sid,sha)
    if cosine is not None:
        v = np.zeros(EMBED_DIM, dtype=np.float32)
        v[:2] = [cosine, np.sqrt(1-cosine*cosine)]
        await conn.execute("""INSERT INTO embeddings(id,photo_id,model,dim,vec_miew,created_at)
            VALUES($1,$2,$3,$4,$5::vector,$6)""", uuid7(),pid,MODEL_NAME,EMBED_DIM,_to_pgvector(v),created+timedelta(seconds=1))
    return sid, pid


async def human_label(conn, oid, a, b, verdict="same", status="confirmed"):
    prop = uuid7()
    await conn.execute("""INSERT INTO match_proposals(id,sighting_id,candidate_sighting_id,score,method,status)
        VALUES($1,$2,$3,0.8,$4,$5)""", prop,a,b,MODEL_NAME,status)
    await conn.execute("""INSERT INTO confirmations(id,sighting_id,observer_id,proposal_id,verdict)
        VALUES($1,$2,$3,$4,$5)""", uuid7(),a,oid,prop,verdict)
    return prop


@pytest.mark.asyncio
@pytest.mark.parametrize("query_located", [True, False])
async def test_real_snapshot_sql_ann_no_gps_ordering_and_no_mutation(migrated_db, monkeypatch, query_located):
    from app import embed, detect_reid
    from app.storage.s3 import S3Storage
    conn, oid = migrated_db, uuid7()
    await conn.execute("INSERT INTO observers(id) VALUES($1)", oid)
    gallery, _ = await db_encounter(conn, oid, 1, located=not query_located)
    query, _ = await db_encounter(conn, oid, 2, located=query_located)
    missing, _ = await db_encounter(conn, oid, 3, cosine=None)
    await human_label(conn, oid, query, gallery)
    await human_label(conn, oid, missing, gallery)
    # A moot competitor carries rejected status without a human verdict.
    await conn.execute("""INSERT INTO match_proposals(id,sighting_id,candidate_sighting_id,score,method,status)
        VALUES($1,$2,$3,0.9,$4,'rejected')""", uuid7(),missing,query,MODEL_NAME)
    def forbidden(*args, **kwargs):
        pytest.fail("snapshot must not access media or inference")
    monkeypatch.setattr(S3Storage, "get", forbidden)
    monkeypatch.setattr(embed, "_get_session", forbidden)
    monkeypatch.setattr(detect_reid, "_get_session", forbidden)
    before = [await conn.fetchval(f"SELECT count(*) FROM {table}") for table in ("sightings", "photos", "embeddings", "match_proposals", "confirmations")]
    data = await take_snapshot(conn)
    validate_snapshot(data)
    after = [await conn.fetchval(f"SELECT count(*) FROM {table}") for table in ("sightings", "photos", "embeddings", "match_proposals", "confirmations")]
    assert before == after
    assert await conn.fetchval("SHOW transaction_read_only") == "off"
    pair = next(p for p in data["pairs"] if p["query"] == str(query) and p["candidate"] == str(gallery))
    assert pair["within_reach"] is (not query_located)
    assert pair["reverse_within_reach"] is query_located
    assert pair["model_score"] == pytest.approx(1)
    assert (pair["ann_score"] is None) is query_located
    assert len(data["labels"]) == 2 and all(r["verdict"] == "same" for r in data["labels"])
    got = report(data)
    assert got["populations"]["verified_encounters"] == 1
    assert got["populations"]["embedded_verified_encounters"] == 0
    assert got["policy"]["coverage_at_5"]["total"] == 1
    assert got["funnel"]["unscored"] == 2
    encoded = json.dumps(data)
    assert "synthetic" not in encoded and _to_pgvector(np.eye(1, EMBED_DIM, dtype=np.float32)[0]) not in encoded


@pytest.mark.asyncio
async def test_snapshot_inactive_recipe_filter_and_stale_mean_after_versioning(migrated_db, monkeypatch):
    from app.reid_eval import snapshot as module
    conn, oid = migrated_db, uuid7()
    await conn.execute("INSERT INTO observers(id) VALUES($1)", oid)
    a, _ = await db_encounter(conn, oid, 1)
    q, _ = await db_encounter(conn, oid, 2)
    columns = await conn.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='embeddings' AND column_name='recipe'")
    if not columns:
        await conn.execute("ALTER TABLE embeddings ADD COLUMN recipe text; ALTER TABLE sightings ADD COLUMN vec_miew_model text, ADD COLUMN vec_miew_recipe text")
    try:
        await conn.execute("UPDATE embeddings SET recipe='fixture-active'")
        old = np.zeros(EMBED_DIM, dtype=np.float32)
        old[1] = 1
        await conn.execute("""UPDATE sightings SET vec_miew=$2::vector,vec_miew_model='other-model',
            vec_miew_recipe='fixture-active' WHERE id=$1""", q,_to_pgvector(old))
        monkeypatch.setattr(module, "active_recipe", lambda: "fixture-active")
        data = await take_snapshot(conn)
        query = next(r for r in data["observations"] if r["id"] == str(q))
        assert query["mean_state"] == "inactive" and query["probe"] == "frames"
        assert data["meta"]["versioning"] == "versioned"
        assert data["pairs"][0]["policy_score"] == pytest.approx(1)
    finally:
        if not columns:
            await conn.execute("ALTER TABLE embeddings DROP COLUMN recipe; ALTER TABLE sightings DROP COLUMN vec_miew_model, DROP COLUMN vec_miew_recipe")


@pytest.mark.asyncio
async def test_snapshot_refuses_code_schema_disagreement(migrated_db, monkeypatch):
    from app.reid_eval import snapshot as module
    columns = await migrated_db.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='embeddings' AND column_name='recipe'")
    monkeypatch.setattr(module, "active_recipe", lambda: None if columns else "missing-migration")
    with pytest.raises(ValueError, match="must agree"):
        await take_snapshot(migrated_db)
