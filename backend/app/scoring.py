"""Persisting what the detector saw, and the one number the surfaces read.

Three code paths analyse a photo -- the capture route, the rescore script,
and (once #64 lands) the GPU worker's completion handler. All three write the
same two things, so both writes live here rather than being restated three
times and drifting.

`detections` is the truth: one row per (photo, model), so a future detector
adds opinions instead of overwriting them. `sightings.animal_confidence` is a
cache of the max over a sighting's photos, kept because `/map` and `/dogs` are
geo and count queries that should filter on a column rather than join.
"""

from uuid import UUID

from app.detect_reid import DETECTOR_NAME


async def save_detection(conn, photo_id: UUID, dog: float, cat: float) -> None:
    """Record what the current detector saw in one photo.

    An upsert, not an insert: the backfill is re-runnable by design, and a
    second pass over a photo should correct its row rather than raise.
    """
    await conn.execute(
        """
        INSERT INTO detections (photo_id, model, dog, cat)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (photo_id, model) DO UPDATE
            SET dog = EXCLUDED.dog,
                cat = EXCLUDED.cat,
                created_at = now()
        """,
        photo_id,
        DETECTOR_NAME,
        float(dog),
        float(cat),
    )


async def recompute_animal_confidence(conn, sighting_id: UUID) -> None:
    """Re-derive the sighting's number from its photos' current-model rows.

    `max(greatest(dog, cat))` -- any animal counts, and one good photo is
    enough for the sighting. Restricted to `DETECTOR_NAME` so a score from a
    superseded detector can never reach a surface; that restriction is the
    whole point of keying the table on the model.

    With no rows the aggregate is NULL, which writes NULL -- "never scored",
    and every filter downstream reads that as visible. That is deliberate: a
    detector failure must cost a label and nothing else.
    """
    await conn.execute(
        """
        UPDATE sightings s
           SET animal_confidence = (
                   SELECT max(greatest(d.dog, d.cat))
                   FROM photos p
                   JOIN detections d ON d.photo_id = p.id AND d.model = $2
                   WHERE p.sighting_id = s.id
               ),
               updated_at = now()
         WHERE s.id = $1
        """,
        sighting_id,
        DETECTOR_NAME,
    )
