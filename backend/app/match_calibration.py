"""Bounded verification experiments; labels never enter the identity resolver."""
import hashlib
import json
import math
import random
import secrets
from collections import Counter
from contextlib import asynccontextmanager

import asyncpg
from fastapi import HTTPException

from app.aggregates import animal_present
from app.config import settings
from app.embed import MODEL_NAME
from app.ids import uuid7

POLICY = "resolver-exact-valid-pairs-v1"
MAX_SIGHTINGS = 300
MAX_PAIRS = 50_000
MAX_VECTORS = 5_000
MAX_REVIEW_PHOTOS = 5_000
BATCH_FIELDS = "id, created_at, eligible_pairs, sampled_pairs, policy, model, radius_m"
SOURCE_TABLES = "sightings, photos, embeddings, animal_instances"


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def fingerprint(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@asynccontextmanager
async def snapshot(conn, *, label=False):
    try:
        async with conn.transaction(isolation="read_committed" if label else "repeatable_read"):
            await conn.execute("SET LOCAL statement_timeout = '10s'")
            await conn.execute("SET LOCAL lock_timeout = '2s'")
            if label:
                # Unlike advisory locks, this also covers rescore scripts and
                # publication paths without requiring every writer to cooperate.
                # Never queue behind an upload while holding earlier table locks:
                # writers can acquire these tables in a different order.
                await conn.execute(f"LOCK TABLE {SOURCE_TABLES} IN SHARE MODE NOWAIT")
            yield
    except (asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError,
            asyncpg.DeadlockDetectedError, asyncpg.SerializationError) as exc:
        raise HTTPException(503, "calibration snapshot busy; retry this request") from exc


async def manifests(conn, ids, model):
    """Hash the inputs, not mutable row timestamps or derived identity labels."""
    rows = await conn.fetch("""
        SELECT s.id, jsonb_build_object(
            'id',s.id, 'capture_id',s.capture_id, 'species',s.species,
            'individual_id',s.individual_id, 'review_status',s.review_status,
            'animal_override',s.animal_override, 'animal_confidence',s.animal_confidence,
            'created_at',s.created_at, 'geog',ST_AsEWKT(s.geog::geometry),
            'mean_hash',md5(s.vec_miew::text),
            'siblings',COALESCE((SELECT jsonb_agg(jsonb_build_array(t.id,t.individual_id)
                ORDER BY t.id) FROM sightings t
                WHERE t.capture_id=s.capture_id AND t.id<>s.id),'[]'::jsonb)
        ) AS evidence FROM sightings s WHERE s.id=ANY($1::uuid[])
    """, ids)
    result = {str(r["id"]): {**decoded(r["evidence"]), "photos": []} for r in rows}
    photos = await conn.fetch("""
        SELECT p.sighting_id, jsonb_build_object(
            'id',p.id, 's3_key',p.s3_key, 'width',p.width,'height',p.height,'phash',p.phash,
            'embedding_id',e.id,'model',e.model,'vector_hash',md5(e.vec_miew::text),
            'bbox',e.bbox,'instance_id',e.instance_id,
            'instance',CASE WHEN a.id IS NULL THEN NULL ELSE jsonb_build_object(
                'id',a.id,'sighting_id',a.sighting_id,'source_photo_id',a.source_photo_id,
                'evidence_photo_id',a.evidence_photo_id,'bbox',a.bbox,
                'crop_bbox',a.crop_bbox,'species',a.species) END
        ) AS evidence
        FROM photos p LEFT JOIN embeddings e ON e.photo_id=p.id AND e.model=$2
        LEFT JOIN animal_instances a ON a.evidence_photo_id=p.id
        WHERE p.sighting_id=ANY($1::uuid[])
        ORDER BY p.id LIMIT $3
    """, ids, model, MAX_REVIEW_PHOTOS + 1)
    if len(photos) > MAX_REVIEW_PHOTOS:
        # Revalidation of an expanded corpus must fail closed, not truncate its
        # fingerprint into something resembling the original frozen evidence.
        return {}
    for row in photos:
        result[str(row["sighting_id"])]["photos"].append(decoded(row["evidence"]))
    return result


async def create_batch(conn, moderator_id, sample_size):
    async with snapshot(conn):
        ids = await conn.fetch(f"""
            SELECT s.id FROM sightings s
            WHERE s.review_status='valid' AND {animal_present()}
              AND EXISTS(SELECT 1 FROM photos p JOIN embeddings e ON e.photo_id=p.id
                  WHERE p.sighting_id=s.id AND e.model=$1 AND e.vec_miew IS NOT NULL)
            ORDER BY s.created_at,s.id LIMIT $2
        """, MODEL_NAME, MAX_SIGHTINGS + 1)
        ids = [r["id"] for r in ids]
        if len(ids) > MAX_SIGHTINGS:
            raise HTTPException(422, f"calibration corpus exceeds {MAX_SIGHTINGS} eligible sightings; no batch created")
        vectors = await conn.fetchval("""
            SELECT (SELECT count(*) FROM photos p JOIN embeddings e ON e.photo_id=p.id
                WHERE p.sighting_id=ANY($1::uuid[]) AND e.model=$2 AND e.vec_miew IS NOT NULL)
                + (SELECT count(*) FROM sightings WHERE id=ANY($1::uuid[]) AND vec_miew IS NOT NULL)
        """, ids, MODEL_NAME)
        if vectors > MAX_VECTORS:
            raise HTTPException(422, f"calibration corpus exceeds {MAX_VECTORS} vectors; no batch created")
        invalid = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM embeddings e JOIN photos p ON p.id=e.photo_id
                WHERE p.sighting_id=ANY($1::uuid[]) AND e.model=$2
                  AND e.vec_miew IS NOT NULL AND vector_norm(e.vec_miew)=0
                UNION ALL
                SELECT 1 FROM sightings WHERE id=ANY($1::uuid[])
                  AND vec_miew IS NOT NULL AND vector_norm(vec_miew)=0
            )
        """, ids, MODEL_NAME)
        if invalid:
            # Reject before drawing the sample: otherwise retrying succeeds
            # only when the draw happens to avoid corrupt evidence.
            raise HTTPException(422, "invalid zero embedding in eligible corpus; repair embeddings before sampling")
        eligible = await conn.fetch("""
            SELECT q.id AS query_id,c.id AS candidate_id
            FROM sightings q JOIN sightings c ON (q.created_at,q.id)>(c.created_at,c.id)
            WHERE q.id=ANY($1::uuid[]) AND c.id=ANY($1::uuid[])
              AND (q.species IS NULL OR c.species IS NULL OR q.species=c.species)
              AND q.capture_id IS DISTINCT FROM c.capture_id
              AND NOT EXISTS(SELECT 1 FROM sightings sibling WHERE sibling.capture_id=q.capture_id
                  AND sibling.id<>q.id AND sibling.individual_id=c.individual_id
                  AND c.individual_id IS NOT NULL)
              AND (q.geog IS NULL OR (c.geog IS NOT NULL AND ST_DWithin(q.geog,c.geog,$2)))
            ORDER BY q.created_at,q.id,c.created_at,c.id LIMIT $3
        """, ids, settings.reid_radius_m, MAX_PAIRS + 1)
        if len(eligible) > MAX_PAIRS:
            raise HTTPException(422, f"calibration corpus exceeds {MAX_PAIRS} eligible pairs; no batch created")
        evidence = await manifests(conn, ids, MODEL_NAME)
        if ids and not evidence:
            raise HTTPException(422, f"calibration corpus exceeds {MAX_REVIEW_PHOTOS} review photos; no batch created")
        seed = secrets.randbits(63)
        sampled = random.Random(seed).sample(list(eligible), min(sample_size, len(eligible)))
        batch_id = uuid7()
        policy_settings = {"animal_confidence_min": settings.animal_confidence_min,
            "propose_min": settings.reid_propose_min, "review_status": "valid",
            "query_orientation": "later_created_at_id", "query_vectors": "mean_else_photos",
            "candidate_vectors": "current_model_photos", "score": "max_exact_cosine",
            "date_filter": None, "observer_filter": None}
        row = await conn.fetchrow(f"""
            INSERT INTO match_calibration_batches
                (id,created_by,eligible_pairs,sampled_pairs,policy,model,radius_m,settings,sampling)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb) RETURNING {BATCH_FIELDS}
        """, batch_id, moderator_id, len(eligible), len(sampled), POLICY, MODEL_NAME,
            settings.reid_radius_m, json.dumps(policy_settings), json.dumps({"seed": seed,
            "method": "uniform_without_replacement", "requested_pairs": sample_size,
            "eligible_sightings": len(ids), "snapshot_sighting_ids": [str(sid) for sid in ids],
            "vectors": vectors,
            "caps": {"sightings": MAX_SIGHTINGS,"pairs": MAX_PAIRS,"vectors": MAX_VECTORS,
                     "review_photos": MAX_REVIEW_PHOTOS}}))
        for ordinal, pair in enumerate(sampled):
            qid, cid = pair["query_id"], pair["candidate_id"]
            score = await conn.fetchval("""
                WITH q AS (
                    SELECT vec_miew AS v FROM sightings WHERE id=$1 AND vec_miew IS NOT NULL
                    UNION ALL
                    SELECT e.vec_miew FROM photos p JOIN embeddings e ON e.photo_id=p.id
                    WHERE p.sighting_id=$1 AND e.model=$3 AND e.vec_miew IS NOT NULL
                      AND NOT EXISTS(SELECT 1 FROM sightings WHERE id=$1 AND vec_miew IS NOT NULL)
                ) SELECT max(1-(e.vec_miew <=> q.v)) FROM q CROSS JOIN embeddings e
                JOIN photos p ON p.id=e.photo_id
                WHERE p.sighting_id=$2 AND e.model=$3 AND e.vec_miew IS NOT NULL
            """, qid, cid, MODEL_NAME)
            # pgvector returns NaN for zero vectors, which cannot support a
            # meaningful experiment or a valid JSON score.
            if score is None or not math.isfinite(score):
                raise HTTPException(422, "invalid zero/nonfinite embedding; no batch created")
            manifest = {"left": evidence[str(qid)], "right": evidence[str(cid)]}
            await conn.execute("""INSERT INTO match_calibration_pairs
                (id,batch_id,ordinal,query_id,candidate_id,score,manifest,evidence_hash)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8)""", uuid7(), batch_id, ordinal,
                qid, cid, score, json.dumps(manifest), fingerprint(manifest))
        return dict(row)


async def batch(conn, batch_id):
    row = await conn.fetchrow("SELECT * FROM match_calibration_batches WHERE id=$1", batch_id)
    if row is None:
        raise HTTPException(404, "no such calibration batch")
    return row


async def validated_pairs(conn, batch_row, moderator_id, pair_id=None):
    rows = await conn.fetch("""SELECT p.*,
        (SELECT array_agg(DISTINCT verdict) FROM match_calibration_labels WHERE pair_id=p.id) AS votes,
        (SELECT verdict FROM match_calibration_labels WHERE pair_id=p.id AND moderator_id=$2) AS my_verdict
        FROM match_calibration_pairs p WHERE batch_id=$1 AND ($3::uuid IS NULL OR p.id=$3)
        ORDER BY ordinal""", batch_row["id"], moderator_id, pair_id)
    if pair_id is not None and not rows:
        raise HTTPException(404, "no such calibration pair")
    ids = list({r[key] for r in rows for key in ("query_id", "candidate_id")})
    current = await manifests(conn, ids, batch_row["model"])
    stale_ids, results = [], []
    for row in rows:
        value = dict(row)
        evidence = {"left": current.get(str(row["query_id"])), "right": current.get(str(row["candidate_id"]))}
        stale = row["stale"] or fingerprint(evidence) != row["evidence_hash"]
        if stale and not row["stale"]:
            stale_ids.append(row["id"])
        votes = row["votes"] or []
        value["status"] = ("stale" if stale else "conflict" if len(votes)>1
                           else votes[0] if votes else "unreviewed")
        value["manifest"] = decoded(row["manifest"])
        results.append(value)
    if stale_ids:
        await conn.execute("UPDATE match_calibration_pairs SET stale=true WHERE id=ANY($1::uuid[])", stale_ids)
    return results


def pair_item(pair):
    return {key: pair[key] for key in ("id", "score", "status", "my_verdict")}


def metrics(batch_row, pairs):
    counts = Counter(p["status"] for p in pairs)
    decisive = [p for p in pairs if p["status"] in ("same", "different")]
    def at_threshold(threshold):
        tp = sum(p["status"] == "same" and p["score"] >= threshold for p in decisive)
        fp = sum(p["status"] == "different" and p["score"] >= threshold for p in decisive)
        fn, tn = counts["same"]-tp, counts["different"]-fp
        return {"threshold": threshold, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": tp/(tp+fp) if tp+fp else None,
            "recall": tp/(tp+fn) if tp+fn else None}

    thresholds = {i / 10 for i in range(11)} | {settings.reid_propose_min}
    cutoffs = [at_threshold(t) for t in sorted(thresholds)]
    suggested = None
    if counts["same"] >= 20 and counts["different"] >= 20:
        def rank(c):
            denominator = 2*c["tp"]+c["fp"]+c["fn"]
            return (2*c["tp"]/denominator if denominator else 0,
                    c["precision"] or 0, c["threshold"])
        # Prediction sets change only at observed scores, including negatives.
        # A decile-only sweep can miss even a perfect separator.
        candidates = [at_threshold(t) for t in {p["score"] for p in decisive}]
        best = max(candidates, key=rank)
        if best["tp"] > 0:
            suggested = best["threshold"]
            if suggested not in thresholds:
                cutoffs.append(best)
                cutoffs.sort(key=lambda c: c["threshold"])
    reviewed = sum(counts[k] for k in ("same", "different", "unsure", "conflict"))
    note = (f"Validation coverage: {len(decisive)}/{len(pairs)} sampled pairs have usable same/different labels; "
        f"{reviewed}/{len(pairs)} reviewed and non-stale, from {batch_row['eligible_pairs']} eligible pairs. "
        "Uniform frozen sample, not top-k retrieval. Recall is sensitivity on reviewed pairs only, "
        "not street-wide or retrieval recall. Unsure, conflicts and stale evidence are excluded. "
        "Exploratory best-F1 sweep over distinct observed scores requires at least 20 same and 20 different labels; "
        "ties favor precision then higher cutoff. Best F1 is fitted and evaluated on the same labels, "
        "not independently validated. Exact pairwise scoring excludes ANN retrieval and candidate-limit failures. "
        "This sample is not production-ready validation; "
        "no settings are changed and no identity confirmations are pooled.")
    return {"eligible_pairs": batch_row["eligible_pairs"], "sampled_pairs": batch_row["sampled_pairs"],
        "reviewed_pairs": reviewed, **{k: counts[k] for k in
            ("same", "different", "unsure", "conflict", "stale", "unreviewed")},
        "current_threshold": settings.reid_propose_min, "cutoffs": cutoffs,
        "suggested_threshold": suggested, "suggestion_note": note}
