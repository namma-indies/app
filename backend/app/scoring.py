"""Persisting what the detector saw, and the one number the surfaces read.

Three code paths analyse a photo -- the capture route, the rescore script,
and the GPU worker's completion handler. All three write the
same two things, so both writes live here rather than being restated three
times and drifting.

`detections` is the truth: one row per (photo, model), so a future detector
adds opinions instead of overwriting them. `sightings.animal_confidence` is a
cache of the max over a sighting's photos, kept because `/map` and `/dogs` are
geo and count queries that should filter on a column rather than join.
"""

from uuid import UUID

from app.detect_reid import DETECTOR_NAME
from app.scene import SceneSignals


async def save_detection(
    conn, photo_id: UUID, dog: float, cat: float, *, model: str = DETECTOR_NAME,
    scene: SceneSignals | None = None,
) -> None:
    """Record what the current detector saw in one photo.

    An upsert, not an insert: the backfill is re-runnable by design, and a
    second pass over a photo should correct its row rather than raise.

    `scene` is what screening reads (#181). None -- a worker that does not
    report it yet -- stores NULLs, "not measured", and a re-run without it
    keeps what an earlier run measured rather than erasing it.
    """
    await conn.execute(
        """
        INSERT INTO detections (photo_id, model, dog, cat, person, person_area, indoor)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (photo_id, model) DO UPDATE
            SET dog = EXCLUDED.dog,
                cat = EXCLUDED.cat,
                person = COALESCE(EXCLUDED.person, detections.person),
                person_area = COALESCE(EXCLUDED.person_area, detections.person_area),
                indoor = COALESCE(EXCLUDED.indoor, detections.indoor),
                created_at = now()
        """,
        photo_id,
        model,
        float(dog),
        float(cat),
        None if scene is None else float(scene.person),
        None if scene is None else float(scene.person_area),
        None if scene is None else float(scene.indoor),
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
