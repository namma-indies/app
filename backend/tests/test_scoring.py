"""Writing a detection, and deriving the sighting's number from it."""

import pytest

from app.ids import uuid7

pytestmark = pytest.mark.asyncio


async def _sighting(conn, n_photos=1):
    oid, sid = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')", oid)
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2,now())", sid, oid)
    pids = []
    for i in range(n_photos):
        pid = uuid7()
        await conn.execute(
            "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,$3)", pid, sid, f"k{i}")
        pids.append(pid)
    return sid, pids


async def test_save_detection_is_an_upsert(migrated_db):
    """Re-running the detector on a photo corrects the row rather than
    failing on the primary key -- the backfill must be safe to re-run."""
    from app.scoring import save_detection

    sid, (pid,) = await _sighting(migrated_db)
    await save_detection(migrated_db, pid, dog=0.1, cat=0.2)
    await save_detection(migrated_db, pid, dog=0.9, cat=0.0)

    rows = await migrated_db.fetch("SELECT dog, cat FROM detections WHERE photo_id=$1", pid)
    assert len(rows) == 1
    assert rows[0]["dog"] == pytest.approx(0.9)
    assert rows[0]["cat"] == pytest.approx(0.0)


async def test_animal_confidence_is_the_max_over_photos_and_both_classes(migrated_db):
    """Any animal counts, so a confident cat sets the number as readily as a
    confident dog; and one good photo is enough for the sighting."""
    from app.scoring import recompute_animal_confidence, save_detection

    sid, (p1, p2) = await _sighting(migrated_db, n_photos=2)
    await save_detection(migrated_db, p1, dog=0.10, cat=0.00)
    await save_detection(migrated_db, p2, dog=0.05, cat=0.82)
    await recompute_animal_confidence(migrated_db, sid)

    got = await migrated_db.fetchval(
        "SELECT animal_confidence FROM sightings WHERE id=$1", sid)
    assert got == pytest.approx(0.82, abs=1e-6)


async def test_an_unscored_sighting_stays_null(migrated_db):
    """NULL is "never scored", which is not 0.0, "scored and saw nothing".
    Everything downstream fails open on NULL, so this distinction is load-
    bearing rather than tidy."""
    from app.scoring import recompute_animal_confidence

    sid, _ = await _sighting(migrated_db)
    await recompute_animal_confidence(migrated_db, sid)
    assert await migrated_db.fetchval(
        "SELECT animal_confidence FROM sightings WHERE id=$1", sid) is None


async def test_another_models_scores_are_ignored(migrated_db):
    """The whole reason the table is keyed on model. A YOLOv8n row must not
    contribute to a number the surfaces read, or #67 comes straight back."""
    from app.scoring import recompute_animal_confidence

    from app.scoring import save_detection

    sid, (pid,) = await _sighting(migrated_db)
    await save_detection(migrated_db, pid, 0.99, 0.0, model="yolov8n")
    assert await migrated_db.fetchval("SELECT model FROM detections WHERE photo_id=$1", pid) == "yolov8n"
    await recompute_animal_confidence(migrated_db, sid)
    assert await migrated_db.fetchval(
        "SELECT animal_confidence FROM sightings WHERE id=$1", sid) is None
