import json
from uuid import UUID

from fastapi import APIRouter, Depends

from app import describe
from app.aggregates import animal_present
from app.auth.deps import require_observer
from app.deps import get_conn, get_storage
from app.photos import thumb_key
from app.storage.s3 import S3Storage
from app.suggestions import answered, newest_per_field
from app.voice_notes import for_sighting, heard as heard_on

router = APIRouter()


def _off_map_reason(row) -> str | None:
    if row["review_status"] == "pending":
        return "reported"
    if row["review_status"] == "rejected":
        return "hidden"
    if not row["animal_ok"]:
        return "no_animal"
    return None


def _on_map(row) -> bool:
    return _off_map_reason(row) is None


@router.get("/dex")
async def get_dex(
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    rows = await conn.fetch(
        f"""
        SELECT
            s.id AS sighting_id,
            s.captured_at,
            ST_Y(s.geog::geometry) AS lat,
            ST_X(s.geog::geometry) AS lng,
            s.geo_accuracy_m,
            s.attrs,
            s.review_status,
            s.shared_with_team_at IS NOT NULL AS shared_with_team,
            s.processing_state,
            s.capture_id,
            c.processing_state = 'ready' AS details_editable,
            s.species,
            -- A /capture upload keeps its note on the capture, which all its
            -- published sightings share.
            COALESCE(s.audio_s3_key, c.audio_s3_key) AS audio_s3_key,
            {animal_present()} AS animal_ok,
            p.id AS photo_id,
            p.s3_key
        FROM sightings s
        LEFT JOIN captures c ON c.id = s.capture_id
        LEFT JOIN photos p ON p.sighting_id = s.id
        WHERE s.observer_id = $1
        -- p.id breaks the tie: photos of one sighting share created_at exactly
        -- (single transaction, constant now()), so without it the order of a
        -- sighting's photos is arbitrary and can differ between requests.
        ORDER BY s.captured_at DESC, p.created_at ASC, p.id ASC
        """,
        observer_id,
    )

    sightings: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        sid = str(row["sighting_id"])
        if sid not in sightings:
            raw_attrs = row["attrs"]
            attrs = json.loads(raw_attrs) if isinstance(raw_attrs, str) else (raw_attrs or {})
            sightings[sid] = {
                "id": sid,
                "captured_at": row["captured_at"],
                "lat": row["lat"],
                "lng": row["lng"],
                "geo_accuracy_m": row["geo_accuracy_m"],
                "attrs": attrs,
                # Yours stays in your own dex whatever its status -- but you
                # should be told when one has been taken off the shared map,
                # rather than wondering why nobody can see it. Two independent
                # reasons that can both be true; a person acting is the more
                # useful one to hear, so it wins.
                "review_status": row["review_status"],
                # Drives the Journal's "share with the team" toggle.
                "shared_with_team": row["shared_with_team"],
                "processing_state": row["processing_state"],
                "capture_id": str(row["capture_id"]) if row["capture_id"] else None,
                # Whether the capture has per-animal details to edit. A
                # /sighting upload's capture is a 'legacy' stand-in with no
                # animals, and its sheet would be empty.
                "details_editable": row["details_editable"] is True,
                "species": row["species"],
                "on_map": _on_map(row),
                "off_map_reason": _off_map_reason(row),
                "photos": [],
                "_audio_key": row["audio_s3_key"],
            }
            order.append(sid)
        if row["photo_id"] is not None:
            # Keys now, URLs in one batch below -- `storage.url` opens an
            # aioboto3 client per call (2.86 ms) where a shared one costs
            # 0.20 ms, and this loop presigns two per photo.
            sightings[sid]["photos"].append({"_key": row["s3_key"]})

    pending = [
        photo for sighting in sightings.values() for photo in sighting["photos"]
    ]
    signed = await storage.urls(
        [k for photo in pending for k in (photo["_key"], thumb_key(photo["_key"]))]
    )
    for photo, (url, thumb_url) in zip(pending, zip(signed[::2], signed[1::2])):
        del photo["_key"]
        photo["url"] = url
        photo["thumb_url"] = thumb_url

    # Present only so the observer can hear that their note arrived (#96).
    with_audio = [s for s in sightings.values() if s["_audio_key"]]
    audio_urls = dict(zip(
        map(id, with_audio), await storage.urls([s["_audio_key"] for s in with_audio])
    ))
    for sighting in sightings.values():
        del sighting["_audio_key"]
        sighting["audio_url"] = audio_urls.get(id(sighting))

    # What the GPU worker heard in the note or the clip, shown in the viewer
    # with the words each open suggestion came from highlighted (#112), and
    # each note added later with how far along it is (#156). A later note
    # recorded about one animal of a capture shows on that animal only.
    capture_ids = {s["capture_id"] for s in sightings.values() if s["capture_id"]}
    heard = await heard_on(conn, storage, [UUID(c) for c in capture_ids])
    for sighting in sightings.values():
        found = heard.get(UUID(sighting["capture_id"])) if sighting["capture_id"] else None
        shown = for_sighting(found, UUID(sighting["id"])) if found else {"transcripts": [], "notes": []}
        sighting["voice_transcripts"] = shown["transcripts"]
        sighting["later_notes"] = shown["notes"]

    # Details a model read from that transcript, for the observer to confirm
    # or correct (app/suggestions.py). Only for a detail still left blank: a
    # value the observer chose themselves is never second-guessed.
    #
    # A /sighting upload's are answered right here ("suggestions"). A
    # multi-animal capture's belong to one animal and are saved with the rest
    # of its details in the review sheet, so here they are only listed
    # ("review_suggestions"), to highlight and to point at that sheet.
    offered: dict[str, list[dict]] = {}
    for_animal: dict[str, list[dict]] = {}
    # Every row, answered or not: the answered ones are reported in
    # `answered_suggestions` so the transcript can keep marking where a saved
    # detail came from. `suggestions` and `review_suggestions` stay open-only.
    closed: dict[str, list] = {}
    if capture_ids:
        for row in await conn.fetch(
            "SELECT d.id, d.capture_id, d.instance_id, d.field, d.value, d.quote, d.status, "
            "d.decided_value, a.sighting_id, t.note_id, d.description_id "
            "FROM detail_suggestions d LEFT JOIN voice_transcripts t ON t.id = d.transcript_id "
            "LEFT JOIN animal_instances a ON a.id = d.instance_id "
            # From a note or a photo still there: one from a deleted note is only a record now.
            "WHERE d.capture_id = ANY($1::uuid[]) "
            "AND (d.transcript_id IS NOT NULL OR d.description_id IS NOT NULL) ORDER BY d.field, d.id",
            list(capture_ids),
        ):
            own = str(row["capture_id"]) if row["instance_id"] is None else (
                str(row["sighting_id"]) if row["sighting_id"] is not None else None)
            if own is not None:
                closed.setdefault(own, []).append(row)
            if row["status"] != "open":
                continue
            # Which note's words it came from, so only that transcript marks them.
            item = {"id": str(row["id"]), "field": row["field"], "value": row["value"],
                    "quote": row["quote"], "note_id": str(row["note_id"]) if row["note_id"] else None,
                    # Read from the AI photo description rather than a note (#160).
                    "description_id": str(row["description_id"]) if row["description_id"] else None}
            if row["instance_id"] is None:
                offered.setdefault(str(row["capture_id"]), []).append(item)
            elif row["sighting_id"] is not None:
                for_animal.setdefault(str(row["sighting_id"]), []).append(item)
    for sighting in sightings.values():
        # Two notes can suggest the same detail; the newest is offered.
        sighting["suggestions"] = [
            o for o in newest_per_field(offered.get(sighting["capture_id"], []))
            if sighting["capture_id"] == sighting["id"] and not sighting["attrs"].get(o["field"])
        ]
        sighting["review_suggestions"] = [
            o for o in newest_per_field(for_animal.get(sighting["id"], []))
            if sighting["details_editable"] and not sighting["attrs"].get(o["field"])
        ]
        # A /sighting upload's suggestions are keyed by its capture; an animal's by itself.
        own = sighting["capture_id"] if sighting["capture_id"] == sighting["id"] else sighting["id"]
        sighting["answered_suggestions"] = [
            {**o, "id": str(o["id"]), "instance_id": None if o["instance_id"] is None else str(o["instance_id"]),
             "note_id": None if o["note_id"] is None else str(o["note_id"]),
             "description_id": None if o["description_id"] is None else str(o["description_id"])}
            for o in answered(closed.get(own, []), lambda r, s=sighting: s["attrs"].get(r["field"]))
        ]

    # An AI-written description of the animal where nothing was noted (#160),
    # for its observer alone: /map never carries it.
    described = await describe.for_sightings(conn, [UUID(sid) for sid in order])
    for sid in order:
        sightings[sid]["photo_description"] = described.get(UUID(sid))

    return {"sightings": [sightings[sid] for sid in order]}
