"""Capture publication: no sighting exists publicly until its association is settled."""
import json
from uuid import uuid5

import numpy as np
from fastapi import HTTPException

from app.capture_contracts import AnimalDetails, AnimalGroup, CaptureStatus, InstanceEvidence
from app.config import settings
from app.ids import uuid7
from app.media_jobs import MODEL_NAME, vector_text
from app.photos import thumb_key


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


async def receipt(conn, capture, *, duplicate=False):
    ids = await conn.fetch("SELECT id FROM sightings WHERE capture_id=$1 ORDER BY id", capture["id"])
    return {"capture_id": capture["id"], "processing_state": capture["processing_state"],
            "sighting_ids": [r["id"] for r in ids], "duplicate": duplicate}


async def status(conn, storage, capture):
    rows = await conn.fetch("""SELECT a.*,p.s3_key,src.s3_key AS source_key
        FROM animal_instances a JOIN photos p ON p.id=a.evidence_photo_id
        JOIN photos src ON src.id=a.source_photo_id
        WHERE a.capture_id=$1 ORDER BY a.track_id,a.timestamp_ms NULLS FIRST,a.id""", capture["id"])
    keys = [key for r in rows for key in (r["s3_key"], thumb_key(r["s3_key"]), thumb_key(r["source_key"]))]
    urls = iter(await storage.urls(keys))
    instances = [InstanceEvidence(instance_id=r["id"], source_photo_id=r["source_photo_id"],
        track_id=r["track_id"], sighting_id=r["sighting_id"], species=r["species"],
        confidence=r["confidence"], bbox=decoded(r["bbox"]), crop_bbox=decoded(r["crop_bbox"]),
        timestamp_ms=r["timestamp_ms"], photo_url=next(urls), thumb_url=next(urls),
        source_thumb_url=next(urls), details=AnimalDetails(**decoded(r["details"]))) for r in rows]
    groups = decoded(capture["review_groups"])
    if not groups:
        tracks = {}
        for r in rows:
            tracks.setdefault(r["track_id"], []).append(r["id"])
        groups = [AnimalGroup(instance_ids=ids) for ids in tracks.values()]
    return CaptureStatus(**await receipt(conn, capture), revision=capture["revision"],
        captured_at=capture["captured_at"], note=decoded(capture["attrs"]).get("note"),
        instances=instances, groups=groups)


def validate_groups(rows, groups):
    by_id = {r["id"]: r for r in rows}
    ids = [i for g in groups for i in g.instance_ids]
    if set(ids) != set(by_id) or len(ids) != len(set(ids)):
        raise HTTPException(422, "groups must include every instance exactly once")
    for group in groups:
        evidence = [by_id[i] for i in group.instance_ids]
        if len({r["species"] for r in evidence}) != 1:
            raise HTTPException(422, "a group cannot mix species")
        if len({r["source_photo_id"] for r in evidence}) != len(evidence):
            raise HTTPException(422, "co-visible animals cannot be grouped together")


async def publish_groups(conn, capture, groups):
    """Caller holds matching and capture locks. Crop photo IDs never change."""
    from app.matching import resolve_sighting
    rows = await conn.fetch("""SELECT a.*,p.phash,e.vec_miew::text AS vec
        FROM animal_instances a JOIN photos p ON p.id=a.evidence_photo_id
        LEFT JOIN embeddings e ON e.instance_id=a.id AND e.model=$2
        WHERE a.capture_id=$1 ORDER BY a.id""", capture["id"], MODEL_NAME)
    validate_groups(rows, groups)
    by_id = {r["id"]: r for r in rows}
    existing = {}
    for r in rows:
        if r["sighting_id"] is not None:
            existing.setdefault(r["sighting_id"], set()).add(r["id"])
    if capture["published_at"] and {frozenset(v) for v in existing.values()} != {
            frozenset(g.instance_ids) for g in groups}:
        raise HTTPException(409, "published associations cannot be regrouped")
    sids = []
    for group in groups:
        members = [by_id[i] for i in sorted(group.instance_ids, key=str)]
        sid = members[0]["sighting_id"] or uuid5(capture["id"], "animal:" + ",".join(str(r["id"]) for r in members))
        details = group.model_dump(exclude={"instance_ids"}, exclude_none=True)
        attrs = {**decoded(capture["attrs"]), **details}
        vectors = [np.asarray([float(v) for v in r["vec"].strip("[]").split(",")]) for r in members if r["vec"]]
        mean = None
        if vectors:
            v = np.mean(vectors, axis=0)
            norm = float(np.linalg.norm(v))
            if norm > 1e-8:
                mean = vector_text(v / norm)
        await conn.execute("""INSERT INTO sightings (id,capture_id,observer_id,captured_at,reported_at,
            geog,geo_source,geo_accuracy_m,attrs,species,phash,animal_confidence,dog_confidence,
            vec_miew,processing_state)
            SELECT $2,id,observer_id,captured_at,reported_at,geog,geo_source,geo_accuracy_m,
                $3::jsonb,$4,$5,$6,$7,$8::vector,'ready' FROM captures WHERE id=$1
            ON CONFLICT(id) DO UPDATE SET attrs=EXCLUDED.attrs,updated_at=now()""",
            capture["id"],sid,json.dumps(attrs),members[0]["species"],members[0]["phash"],
            max(r["confidence"] for r in members),
            max(r["confidence"] if r["species"] == "dog" else 0 for r in members),mean)
        await conn.execute("UPDATE animal_instances SET sighting_id=$2,details=$3::jsonb WHERE id=ANY($1::uuid[])",
            group.instance_ids,sid,json.dumps(details))
        await conn.execute("UPDATE photos SET sighting_id=$2 WHERE id=ANY($1::uuid[])",
            [r["evidence_photo_id"] for r in members],sid)
        sids.append(sid)
    await conn.execute("""UPDATE captures SET processing_state='ready',published_at=COALESCE(published_at,now()),
        review_groups=$2::jsonb,updated_at=now() WHERE id=$1""",capture["id"],
        json.dumps([g.model_dump(mode="json") for g in groups]))
    if not capture["published_at"]:
        for sid in sids:
            await resolve_sighting(conn,sid,auto_merge_min=1.01,propose_min=settings.reid_propose_min,
                radius_m=settings.reid_radius_m,max_candidates=settings.reid_max_candidates,new_uuid=uuid7,
                thin_evidence_frames=settings.reid_thin_evidence_frames)
    return sids
