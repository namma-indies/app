"""The backfill's contract: resumable, serial, and re-runnable to a no-op.

The photo-level resume key is why `detections` is keyed on the model.
`backfill_embeddings.py` documents what the alternative costs: with no key
but `IS NULL`, a photo that legitimately produces nothing is re-examined on
every run and the pending count never reaches zero.
"""

import pytest

from app.ids import uuid7
from scripts.rescore_photos import PENDING_SQL

pytestmark = pytest.mark.asyncio


async def _photo(conn, *, key="k"):
    oid, sid, pid = uuid7(), uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')", oid)
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2,now())", sid, oid)
    await conn.execute(
        "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,$3)", pid, sid, key)
    return sid, pid


async def test_pending_ignores_photos_this_model_already_scored(migrated_db):
    _, scored = await _photo(migrated_db, key="a")
    _, unscored = await _photo(migrated_db, key="b")
    await migrated_db.execute(
        "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolo26x',0.9,0.0)",
        scored)

    rows = await migrated_db.fetch(PENDING_SQL, "yolo26x")
    assert [r["id"] for r in rows] == [unscored]


async def test_a_row_from_another_model_does_not_count_as_scored(migrated_db):
    """The trap this whole piece of work exists to remove: a YOLOv8n score is
    not a YOLO26x score, and must not make the photo look done."""
    _, pid = await _photo(migrated_db)
    await migrated_db.execute(
        "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolov8n',0.021,0.0)",
        pid)

    rows = await migrated_db.fetch(PENDING_SQL, "yolo26x")
    assert [r["id"] for r in rows] == [pid]
