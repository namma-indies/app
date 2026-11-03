"""Frozen, read-only corpus inventory. No media references are selected.

Vector/hash values live only in process memory; only scores and leakage-group
UUIDs enter the snapshot. Geometry becomes cell-to-cell booleans in SQL.
"""

import asyncio
from collections import defaultdict
from uuid import NAMESPACE_OID, uuid5
from zoneinfo import ZoneInfo

import numpy as np

from app.aggregates import animal_present, coarse_geog, owner_active
from app.capture_contracts import MULTI_PIPELINE_VERSION
from app.config import settings
from app.detect_reid import DETECTOR_NAME, REID_CONF_THRESHOLD
from app.embed import EMBED_DIM, MODEL_NAME
from app.ids import uuid7
from app.matching import ANN_SHORTLIST, _to_pgvector
from app.media_jobs import PIPELINE_VERSION

from .core import copy_families
from .schema import validate_snapshot


def active_recipe():
    # I-10 can land independently of I-2. Once I-2 lands, use its single active
    # recipe; never silently fall back if code and schema disagree.
    try:
        from app.reid_recipe import ACTIVE_RECIPE
    except ModuleNotFoundError as exc:
        if exc.name != "app.reid_recipe":
            raise
        return None
    return ACTIVE_RECIPE


def vector(raw):
    if raw is None:
        return None
    value = np.asarray([float(v) for v in raw.strip("[]").split(",")], dtype=np.float32)
    norm = np.linalg.norm(value)
    return value if value.shape == (EMBED_DIM,) and np.isfinite(norm) and norm > 1e-8 else None


def cosine_max(left, right):
    if not left or not right:
        return None
    a, b = np.asarray(left, dtype=np.float64), np.asarray(right, dtype=np.float64)
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    b /= np.linalg.norm(b, axis=1, keepdims=True)
    return float(np.clip(np.max(a @ b.T), -1, 1))


async def ann_frames(conn, probes, frames, *, located):
    """The same halfvec expression and shortlist as production, on earlier data.

    Eligibility, vicinity, species and capture checks already selected these
    frame IDs. This adds the availability restriction production had at the
    time; current-state gaps are still labelled counterfactual in the report.
    """
    if not probes or not frames:
        return set()
    await conn.execute("SET LOCAL hnsw.iterative_scan = " + ("relaxed_order" if located else "off"))
    ids, result = [r["embedding_id"] for r in frames], set()
    for probe in probes:
        hits = await conn.fetch(f"""SELECT e.id FROM embeddings e
            WHERE e.id=ANY($1::uuid[]) AND e.vec_miew IS NOT NULL
            ORDER BY e.vec_miew::halfvec({EMBED_DIM}) <=> $2::halfvec({EMBED_DIM})
            LIMIT {ANN_SHORTLIST}""", ids, _to_pgvector(probe))
        result.update(r["id"] for r in hits)
    return result


async def take_snapshot(conn, *, limit=0, phash_distance=4, seed=189, folds=5, holdout_fraction=.3, sleep=0.0):
    if limit < 0 or sleep < 0 or not 0 <= phash_distance <= 64 or folds < 2 or not 0 < holdout_fraction < 1:
        raise ValueError("invalid snapshot settings")
    recipe = active_recipe()
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        clock = await conn.fetchrow("""SELECT extract(epoch FROM transaction_timestamp())*1000 AS ms,
            current_setting('transaction_read_only') AS readonly""")
        if clock["readonly"] != "on":
            raise ValueError("snapshot requires a read-only transaction")
        columns = await conn.fetch("""SELECT table_name,column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND (
                (table_name='embeddings' AND column_name='recipe') OR
                (table_name='sightings' AND column_name IN ('vec_miew_model','vec_miew_recipe')))""")
        versioned = len(columns) == 3
        if bool(recipe) != versioned or (columns and not versioned):
            raise ValueError("active recipe code and migrated schema must agree")
        mean_tags = "s.vec_miew_model,s.vec_miew_recipe" if versioned else "NULL::text AS vec_miew_model,NULL::text AS vec_miew_recipe"
        rows = await conn.fetch(f"""
            SELECT s.id,s.capture_id,s.created_at,s.captured_at,s.species,s.review_status,
                s.vec_miew::text AS mean,{mean_tags},
                (s.geog IS NOT NULL) AS located,
                (s.review_status<>'rejected' AND s.duplicate_of IS NULL AND {animal_present()}
                    AND {owner_active()}) AS eligible,
                (s.animal_override IS TRUE OR (s.animal_override IS NULL AND EXISTS (
                    SELECT 1 FROM confirmations cf LEFT JOIN match_proposals mp ON mp.id=cf.proposal_id
                    WHERE cf.verdict='same' AND (cf.sighting_id=s.id OR mp.candidate_sighting_id=s.id)))) AS verified,
                c.kind,
                (SELECT count(*) FROM photos p WHERE p.sighting_id=s.id) AS photo_count,
                (SELECT max(greatest(d.dog,d.cat)) FROM photos p JOIN detections d ON d.photo_id=p.id
                    WHERE p.sighting_id=s.id AND d.model=$2) AS detection,
                (SELECT j.pipeline_version FROM jobs j WHERE j.sighting_id=s.id OR j.capture_id=s.capture_id
                    ORDER BY j.created_at DESC,j.id DESC LIMIT 1) AS pipeline
            FROM sightings s LEFT JOIN captures c ON c.id=s.capture_id
            ORDER BY s.created_at,s.id LIMIT $1
        """, limit or None, DETECTOR_NAME)
        total = await conn.fetchval("SELECT count(*) FROM sightings")
        ids = [r["id"] for r in rows]
        recipe_filter = " AND e.recipe=$3" if versioned else ""
        photo_args = [MODEL_NAME, ids] + ([recipe] if versioned else [])
        photos = await conn.fetch(f"""SELECT p.id,p.sighting_id,p.content_sha256,p.phash,
                src.content_sha256 AS source_sha256,src.phash AS source_phash,
                e.id AS embedding_id,e.vec_miew::text AS vec,e.created_at AS embedded_at
            FROM photos p
            LEFT JOIN animal_instances ai ON ai.evidence_photo_id=p.id
            LEFT JOIN photos src ON src.id=ai.source_photo_id
            LEFT JOIN embeddings e ON e.photo_id=p.id AND e.model=$1{recipe_filter}
            WHERE p.sighting_id=ANY($2::uuid[]) ORDER BY p.created_at,p.id""", *photo_args)
        frames = defaultdict(list)
        for photo in photos:
            v = vector(photo["vec"])
            if v is not None:
                frames[str(photo["sighting_id"])].append({"embedding_id": photo["embedding_id"], "vector": v,
                    "available_ms": photo["embedded_at"].timestamp() * 1000})
        observations, probes = [], {}
        for row in rows:
            sid = str(row["id"])
            current = frames[sid]
            mean = vector(row["mean"])
            mean_state = ("absent" if row["mean"] is None else "untagged" if row["vec_miew_model"] is None else
                          "active" if row["vec_miew_model"] == MODEL_NAME and row["vec_miew_recipe"] == recipe else "inactive")
            use_mean = mean is not None and bool(current) and (not versioned or mean_state == "active")
            probes[sid] = [mean] if use_mean else [r["vector"] for r in current]
            pipeline = ("media_v1" if row["pipeline"] == PIPELINE_VERSION else
                        "capture_v2" if row["pipeline"] == MULTI_PIPELINE_VERSION else
                        "legacy" if row["pipeline"] is None else "unknown")
            media = "video" if row["kind"] == "video" else "burst" if row["photo_count"] > 1 else "photo"
            video_recipe = {"legacy": "legacy_frames", "media_v1": "media_v1_frames",
                            "capture_v2": "capture_v2_frames", "unknown": "unknown_frames"}[pipeline] if media == "video" else "not_video"
            observations.append({"id": sid, "capture_id": str(row["capture_id"]) if row["capture_id"] else None,
                "created_ms": row["created_at"].timestamp() * 1000,
                "captured_day": row["captured_at"].astimezone(ZoneInfo("Asia/Kolkata")).date().toordinal(),
                "available_ms": max([row["created_at"].timestamp() * 1000, *(r["available_ms"] for r in current)]),
                "verified": bool(row["verified"]), "embedded": bool(current), "eligible": bool(row["eligible"]),
                "review": row["review_status"], "species": row["species"] or "unknown", "media": media,
                "pipeline": pipeline, "geo": "located" if row["located"] else "none", "copy_family": sid,
                "detection": "unscored" if row["detection"] is None else "animal" if row["detection"] >= REID_CONF_THRESHOLD else "no_animal",
                "probe": "mean" if use_mean else "frames" if current else "none", "mean_state": mean_state,
                "video_recipe": video_recipe})
        families = copy_families(observations, [dict(p) for p in photos], phash_distance)
        for row in observations:
            row["copy_family"] = families[row["id"]]
        reach = settings.reid_radius_m + settings.reid_slack_m()
        def nearby(q, c):
            return f"CASE WHEN {q}.geog IS NULL THEN TRUE WHEN {c}.geog IS NULL THEN FALSE ELSE ST_DWithin({coarse_geog(q+'.geog')},{coarse_geog(c+'.geog')},$2::float8) END"
        flags = await conn.fetch(f"""SELECT q.id AS query,c.id AS candidate,
                {nearby('q','c')} AS within_reach,{nearby('c','q')} AS reverse_within_reach,
                (q.species IS NULL OR c.species IS NULL OR q.species=c.species) AS species_ok,
                ((q.capture_id IS NULL OR c.capture_id IS NULL OR q.capture_id<>c.capture_id) AND NOT EXISTS (
                    SELECT 1 FROM sightings sibling WHERE sibling.capture_id=q.capture_id AND sibling.id<>q.id
                    AND sibling.individual_id=c.individual_id AND c.individual_id IS NOT NULL)) AS capture_ok,
                CASE WHEN q.observer_id IS NULL OR c.observer_id IS NULL THEN 'unknown'
                    WHEN q.observer_id=c.observer_id THEN 'same' ELSE 'different' END AS observer_relation
            FROM sightings q CROSS JOIN sightings c WHERE q.id=ANY($1::uuid[]) AND c.id=ANY($1::uuid[])
                AND (c.created_at,c.id)<(q.created_at,q.id) ORDER BY q.created_at,q.id,c.id""", ids, reach)
        pairs_by_query = defaultdict(list)
        by_id = {r["id"]: r for r in observations}
        available = {}
        last_query = None
        for flag in flags:
            qid, cid = str(flag["query"]), str(flag["candidate"])
            if sleep and qid != last_query:
                await asyncio.sleep(sleep)
            last_query = qid
            earlier = [f for f in frames[cid] if f["available_ms"] <= by_id[qid]["available_ms"]]
            available[qid, cid] = earlier
            pairs_by_query[qid].append({"query": qid, "candidate": cid,
                "model_score": cosine_max([f["vector"] for f in frames[qid]], [f["vector"] for f in earlier]),
                "policy_score": cosine_max(probes[qid], [f["vector"] for f in earlier]), "ann_score": None,
                **{key: flag[key] for key in ("within_reach", "reverse_within_reach", "species_ok", "capture_ok", "observer_relation")}})
        for qid, pairs in pairs_by_query.items():
            if sleep:
                await asyncio.sleep(sleep)
            eligible = [f for p in pairs if by_id[p["candidate"]]["eligible"] and p["within_reach"]
                        and p["species_ok"] and p["capture_ok"] for f in available[qid, p["candidate"]]]
            hits = await ann_frames(conn, probes[qid], eligible, located=by_id[qid]["geo"] == "located")
            for pair in pairs:
                chosen = [f["vector"] for f in available[qid, pair["candidate"]] if f["embedding_id"] in hits]
                pair["ann_score"] = cosine_max(probes[qid], chosen)
        labels = await conn.fetch("""SELECT cf.sighting_id AS a,mp.candidate_sighting_id AS b,cf.verdict,
                CASE WHEN cf.observer_id IS NULL THEN 'unknown'
                    WHEN reviewer.trust_tier='moderator' THEN 'moderator'
                    WHEN cf.observer_id=a.observer_id OR cf.observer_id=b.observer_id THEN 'owner'
                    ELSE 'stranger' END AS source
            FROM confirmations cf JOIN match_proposals mp ON mp.id=cf.proposal_id
            JOIN sightings a ON a.id=cf.sighting_id JOIN sightings b ON b.id=mp.candidate_sighting_id
            LEFT JOIN observers reviewer ON reviewer.id=cf.observer_id
            WHERE a.id=ANY($1::uuid[]) AND b.id=ANY($1::uuid[]) AND a.id<>b.id""", ids)
        orphan_labels = await conn.fetchval("""SELECT count(*) FROM confirmations cf
            LEFT JOIN match_proposals mp ON mp.id=cf.proposal_id
            WHERE mp.id IS NULL OR mp.candidate_sighting_id IS NULL""")
        from gpu_worker.inference import preprocessing_identity
        data = {"meta": {"schema": 1, "snapshot_id": str(uuid7()),
                    "recipe_id": str(uuid5(NAMESPACE_OID, MODEL_NAME + "|" + str(recipe) + "|" + preprocessing_identity())),
                    "as_of_ms": float(clock["ms"]), "threshold": settings.reid_propose_min, "cap": settings.reid_max_candidates,
                    "ann_shortlist": ANN_SHORTLIST, "phash_distance": phash_distance, "holdout_fraction": holdout_fraction,
                    "seed": seed, "folds": folds, "versioning": "versioned" if versioned else "baseline_unversioned",
                    "replay": "counterfactual_current_state", "orphan_labels": orphan_labels, "truncated": total > len(rows)},
                "observations": observations, "pairs": [p for ps in pairs_by_query.values() for p in ps],
                "labels": [{"a": str(r["a"]), "b": str(r["b"]), "verdict": r["verdict"], "source": r["source"]} for r in labels]}
        validate_snapshot(data)
        return data
