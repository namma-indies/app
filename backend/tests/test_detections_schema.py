"""The shape #67 needs: one score per (photo, model), and a human verdict
that outranks it.

`sightings.dog_confidence` recorded a number with no record of which model
produced it, and YOLOv8n and YOLO26x disagree badly -- v8n scored a real dog
at 0.021 where 26x gives 0.800. Keying on the model is what stops that
happening again on the next detector swap, and it is what gives the backfill
a resume key that is not `IS NULL`.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def _cols(conn, table):
    rows = await conn.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = $1",
        table,
    )
    return {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}


async def test_detections_table_is_keyed_on_photo_and_model(migrated_db):
    cols = await _cols(migrated_db, "detections")
    assert cols["dog"][0] == "real"
    assert cols["cat"][0] == "real"
    # Both NOT NULL: a row exists only because a detector ran and produced
    # both numbers. "We did not measure this" is the absence of the row.
    assert cols["dog"][1] == "NO"
    assert cols["cat"][1] == "NO"

    pk = await migrated_db.fetchval(
        "SELECT array_agg(a.attname::text ORDER BY a.attname) "
        "FROM pg_index i JOIN pg_attribute a "
        "  ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'detections'::regclass AND i.indisprimary"
    )
    assert sorted(pk) == ["model", "photo_id"]


async def test_a_photo_can_hold_one_row_per_model(migrated_db):
    """The whole point: two detectors' opinions coexist and stay
    distinguishable, instead of the second silently overwriting the first."""
    from app.ids import uuid7

    oid, sid, pid = uuid7(), uuid7(), uuid7()
    await migrated_db.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')", oid)
    await migrated_db.execute(
        "INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2,now())", sid, oid)
    await migrated_db.execute(
        "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,'k')", pid, sid)

    await migrated_db.execute(
        "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolov8n',0.021,0.0)", pid)
    await migrated_db.execute(
        "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolo26x',0.800,0.0)", pid)

    assert await migrated_db.fetchval(
        "SELECT count(*) FROM detections WHERE photo_id = $1", pid) == 2

    with pytest.raises(Exception):
        await migrated_db.execute(
            "INSERT INTO detections (photo_id, model, dog, cat) "
            "VALUES ($1,'yolo26x',0.5,0.5)", pid)


async def test_detections_go_when_the_photo_goes(migrated_db):
    from app.ids import uuid7

    oid, sid, pid = uuid7(), uuid7(), uuid7()
    await migrated_db.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')", oid)
    await migrated_db.execute(
        "INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2,now())", sid, oid)
    await migrated_db.execute(
        "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,'k')", pid, sid)
    await migrated_db.execute(
        "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolo26x',0.9,0.0)", pid)

    await migrated_db.execute("DELETE FROM sightings WHERE id = $1", sid)
    assert await migrated_db.fetchval("SELECT count(*) FROM detections") == 0


async def test_the_human_verdict_columns_exist_and_start_null(migrated_db):
    """NULL override means nobody has ruled and the model's number decides.
    Separate from review_status/reviewed_at on purpose: "a person ruled on a
    report" and "a person ruled on whether there is an animal in it" are
    different questions about the same photo, and both can be answered."""
    cols = await _cols(migrated_db, "sightings")
    assert cols["animal_confidence"][0] == "real"
    assert cols["animal_override"][0] == "boolean"
    assert cols["animal_reviewed_at"][0] == "timestamp with time zone"
    assert cols["animal_reviewed_by"][0] == "uuid"
    for c in ("animal_confidence", "animal_override", "animal_reviewed_at"):
        assert cols[c][1] == "YES", f"{c} must be nullable"
