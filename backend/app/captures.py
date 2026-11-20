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
from app.reid_recipe import ACTIVE_RECIPE
from app.suggestions import for_capture
from app.voice_notes import for_sighting, heard


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


async def receipt(conn, capture, *, duplicate=False):
    ids = await conn.fetch("SELECT id FROM sightings WHERE capture_id=$1 ORDER BY id", capture["id"])
    return {"capture_id": capture["id"], "processing_state": capture["processing_state"],
            "sighting_ids": [r["id"] for r in ids], "duplicate": duplicate}


def _box_quality(r) -> float:
    """Confidence x share of the frame: which crop is the animal's best view."""
    x1, y1, x2, y2 = decoded(r["bbox"])
    area = max(0, x2 - x1) * max(0, y2 - y1)
    return float(r["confidence"]) * area / max(1, r["source_width"] * r["source_height"])


def representative(members):
    return max(members, key=lambda r: (_box_quality(r), str(r["id"])))


def vote(members):
    """(species, share, uncertain) over stored boxes, as the worker votes."""
    from app.detect_reid import AnimalDetection
    from app.tracking import species_vote
    from app.capture_jobs import grouping_policy
    return species_vote([AnimalDetection(0, r["species"], float(r["confidence"]), (0, 0, 1, 1), (0, 0, 1, 1),
        None if r.get("det_dog") is None else float(r["det_dog"]),
        None if r.get("det_cat") is None else float(r["det_cat"])) for r in members], grouping_policy())


async def stored_tracks(conn, capture_id):
    return {r["track_id"]: r for r in await conn.fetch(
        "SELECT * FROM capture_tracks WHERE capture_id=$1", capture_id)}


def track_of(members, tracks, all_rows):
    """The worker's track when a group is exactly one of them, else None."""
    ids = {r["track_id"] for r in members}
    if len(ids) != 1:
        return None
    track = tracks.get(next(iter(ids)))
    if track is None or {r["id"] for r in all_rows if r["track_id"] == track["track_id"]} != {
            r["id"] for r in members}:
        return None
    return track


def group_species(members, group, tracks, all_rows):
    """The species a group publishes as: the person's, else the worker's vote
    for exactly this animal, else a vote over its stored boxes."""
    if group.species is not None:
        return group.species
    track = track_of(members, tracks, all_rows)
    if track is not None:
        return track["species"]
    return vote(members)[0]


def hint_for(members, group, tracks, all_rows):
    from app.capture_contracts import AnimalHint
    track = track_of(members, tracks, all_rows)
    species, share, uncertain = vote(members)
    if track is not None:
        species, share, uncertain = track["species"], track["species_share"], track["species_uncertain"]
    if group.species is not None:
        # The person (or a saved draft) chose; a close vote stays worth saying.
        species = group.species
    ids = {r["id"] for r in members}
    best = representative(members)["id"]
    if track is not None and track["representative_instance_id"] in ids:
        best = track["representative_instance_id"]
    return AnimalHint(instance_ids=[r["id"] for r in members], species=species,
        species_uncertain=bool(uncertain), species_share=share,
        confidence=None if track is None else track["confidence"],
        confident=None if track is None else track["confident"],
        representative_instance_id=best,
        reasons=[] if track is None else decoded(track["reasons"]))


INSTANCE_ROWS = """SELECT a.*,p.s3_key,p.phash,src.s3_key AS source_key,
        src.width AS source_width,src.height AS source_height,d.dog AS det_dog,d.cat AS det_cat
        FROM animal_instances a JOIN photos p ON p.id=a.evidence_photo_id
        JOIN photos src ON src.id=a.source_photo_id
        LEFT JOIN detections d ON d.photo_id=a.evidence_photo_id AND d.model=$2
        WHERE a.capture_id=$1"""


async def status(conn, storage, capture):
    from app.detect_reid import DETECTOR_NAME
    # Time order everywhere: a clip by timestamp, a burst by upload order.
    # Never by track id, whose text sorts video:1005 before video:200.
    rows = [dict(r) for r in await conn.fetch(INSTANCE_ROWS + """
        ORDER BY a.timestamp_ms NULLS FIRST,src.created_at,src.id,a.id""", capture["id"], DETECTOR_NAME)]
    keys = [key for r in rows for key in (r["s3_key"], thumb_key(r["s3_key"]), thumb_key(r["source_key"]))]
    urls = iter(await storage.urls(keys))
    instances = [InstanceEvidence(instance_id=r["id"], source_photo_id=r["source_photo_id"],
        track_id=r["track_id"], sighting_id=r["sighting_id"], species=r["species"],
        confidence=r["confidence"], bbox=decoded(r["bbox"]), crop_bbox=decoded(r["crop_bbox"]),
        timestamp_ms=r["timestamp_ms"], photo_url=next(urls), thumb_url=next(urls),
        source_thumb_url=next(urls), source_width=r["source_width"], source_height=r["source_height"],
        details=AnimalDetails(**decoded(r["details"]))) for r in rows]
    tracks = await stored_tracks(conn, capture["id"])
    by_id = {r["id"]: r for r in rows}
    saved = decoded(capture["review_groups"])
    excluded = []
    if saved:
        groups = [AnimalGroup(**g) for g in saved]
        # A saved review covers every box; the ones it leaves out were excluded.
        grouped = {i for g in groups for i in g.instance_ids}
        excluded = [r["id"] for r in rows if r["id"] not in grouped]
    elif capture["processing_state"] == "no_animal":
        # Discarded in review (publish_groups); a worker no_animal has no boxes.
        groups = []
        excluded = [r["id"] for r in rows]
    else:
        # The worker's grouping, pre-filled, animals in order of first appearance.
        members = {}
        for r in rows:
            members.setdefault(r["track_id"], []).append(r)
        groups = []
        for m in members.values():
            group = AnimalGroup(instance_ids=[r["id"] for r in m])
            groups.append(group.model_copy(update={"species": group_species(m, group, tracks, rows)}))
    hints = []
    for group in groups:
        members = [by_id[i] for i in group.instance_ids if i in by_id]
        if members:
            hints.append(hint_for(members, group, tracks, rows))
    # Progress before suggestions: if the model finishes in between, the sheet
    # sees it still reading beside the new suggestions, and looks once more,
    # rather than done reading beside the old ones, and stops looking (#169).
    sounds = for_sighting((await heard(conn, storage, [capture["id"]]))[capture["id"]], None)
    open_suggestions, answered_suggestions = await for_capture(
        conn, capture["id"], {i.instance_id: i.details.model_dump() for i in instances})
    return CaptureStatus(**await receipt(conn, capture), revision=capture["revision"],
        captured_at=capture["captured_at"], note=decoded(capture["attrs"]).get("note"),
        instances=instances, groups=groups, excluded_instance_ids=excluded, animal_hints=hints,
        suggestions=open_suggestions, answered_suggestions=answered_suggestions,
        voice_transcripts=sounds["transcripts"], later_notes=sounds["notes"],
        relistening=sounds["relistening"])


def validate_groups(rows, groups, excluded=()):
    by_id = {r["id"]: r for r in rows}
    ids = [i for g in groups for i in g.instance_ids] + list(excluded)
    if set(ids) != set(by_id) or len(ids) != len(set(ids)):
        raise HTTPException(422, "every instance must be in exactly one group or excluded")
    # Species belongs to the animal, not the box: boxes of one animal may carry
    # different per-frame detector labels (a cat called a dog in one frame),
    # and the group's own species -- the person's, or the vote -- publishes.
    for group in groups:
        evidence = [by_id[i] for i in group.instance_ids]
        if len({r["source_photo_id"] for r in evidence}) != len(evidence):
            raise HTTPException(422, "co-visible animals cannot be grouped together")


async def publish_groups(conn, capture, groups, excluded=()):
    """Caller holds matching and capture locks. Crop photo IDs never change.

    Excluded boxes get no sighting. Excluding every box discards the upload:
    it ends as no_animal, the state the worker gives one with nothing in it."""
    from app.matching import resolve_sighting
    from app.detect_reid import DETECTOR_NAME
    rows = [dict(r) for r in await conn.fetch("""SELECT a.*,p.phash,e.vec_miew::text AS vec,
        src.width AS source_width,src.height AS source_height,d.dog AS det_dog,d.cat AS det_cat
        FROM animal_instances a JOIN photos p ON p.id=a.evidence_photo_id
        JOIN photos src ON src.id=a.source_photo_id
        LEFT JOIN embeddings e ON e.instance_id=a.id AND e.model=$2 AND e.recipe=$3
        LEFT JOIN detections d ON d.photo_id=a.evidence_photo_id AND d.model=$4
        WHERE a.capture_id=$1 ORDER BY a.id""", capture["id"], MODEL_NAME, ACTIVE_RECIPE, DETECTOR_NAME)]
    validate_groups(rows, groups, excluded)
    tracks = await stored_tracks(conn, capture["id"])
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
        details = group.model_dump(exclude={"instance_ids", "species"}, exclude_none=True)
        species = group_species(members, group, tracks, rows)
        # The best view, not whichever box sorted first: its phash, and the
        # crop shown first for this animal (sightings.cover_photo_id).
        best = representative(members)
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
            vec_miew,processing_state,vec_miew_model,vec_miew_recipe,cover_photo_id)
            SELECT $2,id,observer_id,captured_at,reported_at,geog,geo_source,geo_accuracy_m,
                $3::jsonb,$4,$5,$6,$7,$8::vector,'ready',$9,$10,$11 FROM captures WHERE id=$1
            ON CONFLICT(id) DO UPDATE SET attrs=EXCLUDED.attrs,vec_miew=EXCLUDED.vec_miew,
                vec_miew_model=EXCLUDED.vec_miew_model,vec_miew_recipe=EXCLUDED.vec_miew_recipe,
                updated_at=now()""",
            capture["id"],sid,json.dumps(attrs),species,best["phash"],
            max(r["confidence"] for r in members),
            max(float(r["det_dog"]) if r.get("det_dog") is not None
                else r["confidence"] if r["species"] == "dog" else 0 for r in members),mean,
            MODEL_NAME if mean else None, ACTIVE_RECIPE if mean else None, best["evidence_photo_id"])
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
        json.dumps([{**g.model_dump(mode="json"), "species": group_species(
            [by_id[i] for i in g.instance_ids], g, tracks, rows)} for g in groups]))
    if not capture["published_at"]:
        for sid in sids:
            await resolve_sighting(conn,sid,auto_merge_min=1.01,propose_min=settings.reid_propose_min,
                radius_m=settings.reid_radius_m,max_candidates=settings.reid_max_candidates,new_uuid=uuid7,
                thin_evidence_frames=settings.reid_thin_evidence_frames)
    return sids


async def recheck_held(conn, capture):
    """Publish a held capture if today's rule would not have held it.

    Caller holds the matching lock and the capture row. Returns the published
    sighting ids, or None when it still needs a person, or someone has already
    saved a draft of their own sorting, which says something stored data
    cannot. A capture grouped by a confidence-aware worker (capture_tracks)
    publishes its stored grouping only if every animal in it was confident and
    no species was a toss-up. One from an older worker falls back to the
    stored species and the pre-2026-11-19 co-visibility rule
    (`tracking.covisibility`): every animal of a species never seen twice in
    one frame is one animal, and each box of a co-visible species (two in one
    frame, never seen again) is its own.
    """
    if capture["processing_state"] != "needs_review" or decoded(capture["review_groups"]):
        return None
    from app.tracking import covisibility
    rows = await conn.fetch("SELECT id,source_photo_id,species,track_id FROM animal_instances "
        "WHERE capture_id=$1 ORDER BY id", capture["id"])
    if not rows:
        return None
    tracks = await stored_tracks(conn, capture["id"])
    if tracks:
        if not all(t["confident"] and not t["species_uncertain"] for t in tracks.values()):
            return None
        members = {}
        for r in rows:
            members.setdefault(r["track_id"], []).append(r["id"])
        groups = [AnimalGroup(instance_ids=ids, species=tracks[t]["species"] if t in tracks else None)
                  for t, ids in members.items()]
    else:
        frames = {}
        for r in rows:
            frames.setdefault(r["source_photo_id"], []).append(r["species"])
        covisible, ambiguous = covisibility(frames.values())
        if ambiguous:
            return None
        by_key = {}
        for r in rows:
            key = r["id"] if r["species"] in covisible else r["species"]
            by_key.setdefault(key, []).append(r["id"])
        groups = [AnimalGroup(instance_ids=ids) for ids in by_key.values()]
    sids = await publish_groups(conn, capture, groups)
    await conn.execute("UPDATE captures SET revision=revision+1 WHERE id=$1", capture["id"])
    return sids
