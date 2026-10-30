"""Capture publication: no sighting exists publicly until its association is settled."""
import json
from uuid import UUID, uuid5

import numpy as np
from fastapi import HTTPException

from app.capture_contracts import AnimalDetails, AnimalGroup, CaptureStatus, InstanceEvidence
from app.config import settings
from app.ids import uuid7
from app.media_jobs import MODEL_NAME, vector_text
from app.photos import thumb_key
from app.suggestions import for_capture
from app.voice_notes import for_sighting, heard


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


async def receipt(conn, capture, *, duplicate=False):
    ids = await conn.fetch("SELECT id FROM sightings WHERE capture_id=$1 ORDER BY id", capture["id"])
    return {"capture_id": capture["id"], "processing_state": capture["processing_state"],
            "sighting_ids": [r["id"] for r in ids], "duplicate": duplicate}


async def status(conn, storage, capture):
    rows = await conn.fetch("""SELECT a.*,p.s3_key,src.s3_key AS source_key,
        src.width AS source_width,src.height AS source_height
        FROM animal_instances a JOIN photos p ON p.id=a.evidence_photo_id
        JOIN photos src ON src.id=a.source_photo_id
        WHERE a.capture_id=$1 ORDER BY a.track_id,a.timestamp_ms NULLS FIRST,a.id""", capture["id"])
    keys = [key for r in rows for key in (r["s3_key"], thumb_key(r["s3_key"]), thumb_key(r["source_key"]))]
    urls = iter(await storage.urls(keys))
    instances = [InstanceEvidence(instance_id=r["id"], source_photo_id=r["source_photo_id"],
        track_id=r["track_id"], sighting_id=r["sighting_id"], species=r["species"],
        confidence=r["confidence"], bbox=decoded(r["bbox"]), crop_bbox=decoded(r["crop_bbox"]),
        timestamp_ms=r["timestamp_ms"], photo_url=next(urls), thumb_url=next(urls),
        source_thumb_url=next(urls), source_width=r["source_width"], source_height=r["source_height"],
        details=AnimalDetails(**decoded(r["details"]))) for r in rows]
    groups = decoded(capture["review_groups"])
    excluded = []
    if groups:
        # A saved review covers every box; the ones it leaves out were excluded.
        grouped = {UUID(str(i)) for g in groups for i in g["instance_ids"]}
        excluded = [r["id"] for r in rows if r["id"] not in grouped]
    elif capture["processing_state"] == "no_animal":
        # Discarded in review (publish_groups); a worker no_animal has no boxes.
        excluded = [r["id"] for r in rows]
    else:
        tracks = {}
        for r in rows:
            tracks.setdefault(r["track_id"], []).append(r["id"])
        groups = [AnimalGroup(instance_ids=ids) for ids in tracks.values()]
    # Progress before suggestions: if the model finishes in between, the sheet
    # sees it still reading beside the new suggestions, and looks once more,
    # rather than done reading beside the old ones, and stops looking (#169).
    sounds = for_sighting((await heard(conn, storage, [capture["id"]]))[capture["id"]], None)
    open_suggestions, answered_suggestions = await for_capture(
        conn, capture["id"], {i.instance_id: i.details.model_dump() for i in instances})
    return CaptureStatus(**await receipt(conn, capture), revision=capture["revision"],
        captured_at=capture["captured_at"], note=decoded(capture["attrs"]).get("note"),
        instances=instances, groups=groups, excluded_instance_ids=excluded,
        suggestions=open_suggestions, answered_suggestions=answered_suggestions,
        voice_transcripts=sounds["transcripts"], later_notes=sounds["notes"],
        relistening=sounds["relistening"])


def validate_groups(rows, groups, excluded=()):
    by_id = {r["id"]: r for r in rows}
    ids = [i for g in groups for i in g.instance_ids] + list(excluded)
    if set(ids) != set(by_id) or len(ids) != len(set(ids)):
        raise HTTPException(422, "every instance must be in exactly one group or excluded")
    for group in groups:
        evidence = [by_id[i] for i in group.instance_ids]
        if len({r["species"] for r in evidence}) != 1:
            raise HTTPException(422, "a group cannot mix species")
        if len({r["source_photo_id"] for r in evidence}) != len(evidence):
            raise HTTPException(422, "co-visible animals cannot be grouped together")


async def publish_groups(conn, capture, groups, excluded=()):
    """Caller holds matching and capture locks. Crop photo IDs never change.

    Excluded boxes get no sighting. Excluding every box discards the upload:
    it ends as no_animal, the state the worker gives one with nothing in it."""
    from app.matching import resolve_sighting
    rows = await conn.fetch("""SELECT a.*,p.phash,e.vec_miew::text AS vec
        FROM animal_instances a JOIN photos p ON p.id=a.evidence_photo_id
        LEFT JOIN embeddings e ON e.instance_id=a.id AND e.model=$2
        WHERE a.capture_id=$1 ORDER BY a.id""", capture["id"], MODEL_NAME)
    validate_groups(rows, groups, excluded)
    by_id = {r["id"]: r for r in rows}
    existing = {}
    for r in rows:
        if r["sighting_id"] is not None:
            existing.setdefault(r["sighting_id"], set()).add(r["id"])
    if capture["published_at"] and {frozenset(v) for v in existing.values()} != {
            frozenset(g.instance_ids) for g in groups}:
        raise HTTPException(409, "published associations cannot be regrouped")
    if not groups:
        await conn.execute("""UPDATE captures SET processing_state='no_animal',review_groups='[]'::jsonb,
            updated_at=now() WHERE id=$1""", capture["id"])
        return []
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
        # The share chosen at capture time lands once, when the sighting is
        # created. Editing details re-runs publication, and re-applying it then
        # would undo a share the contributor has since withdrawn (and clear a
        # posted sighting's take-down flag). A group whose instances already
        # point at a sighting is a re-publication.
        created = members[0]["sighting_id"] is None
        if created:
            from app.describe import enqueue as enqueue_description
            await enqueue_description(conn, sid)
        if created and attrs.get("share_with_team") is True and capture["observer_id"] is not None:
            from app.social import mark_shared
            await mark_shared(conn, sid, capture["observer_id"])
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


async def recheck_held(conn, capture):
    """Publish a held capture if today's rule would not have held it.

    Caller holds the matching lock and the capture row. Returns the published
    sighting ids, or None when it still needs a person: some species is
    ambiguous (`tracking.covisibility`), or someone has already saved a draft
    of their own sorting, which says something the stored species cannot.
    Otherwise it groups as the worker's tracker does: every animal of a
    species never seen twice in one frame is one animal, and each box of a
    co-visible species (two in one frame, never seen again) is its own.
    """
    if capture["processing_state"] != "needs_review" or decoded(capture["review_groups"]):
        return None
    from app.tracking import covisibility
    rows = await conn.fetch("SELECT id,source_photo_id,species FROM animal_instances "
        "WHERE capture_id=$1 ORDER BY id", capture["id"])
    if not rows:
        return None
    frames = {}
    for r in rows:
        frames.setdefault(r["source_photo_id"], []).append(r["species"])
    covisible, ambiguous = covisibility(frames.values())
    if ambiguous:
        return None
    groups = {}
    for r in rows:
        key = r["id"] if r["species"] in covisible else r["species"]
        groups.setdefault(key, []).append(r["id"])
    sids = await publish_groups(conn, capture, [AnimalGroup(instance_ids=ids) for ids in groups.values()])
    await conn.execute("UPDATE captures SET revision=revision+1 WHERE id=$1", capture["id"])
    return sids
