# Animal rescore & the flagged queue — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score every photo with the current detector, record which detector did it, and give a moderator a queue of the photos it thinks have no animal in them — with a human verdict that outranks the model.

**Architecture:** A `detections` table at `(photo_id, model)` grain is the source of truth; `sightings.animal_confidence` is the denormalised max that read surfaces filter on, and `sightings.animal_override` is the human verdict that beats it. One predicate function, `animal_present()`, is composed into the three places that decide visibility (aggregates/map/dogs, candidate search, the match review queue). A resumable script backfills the corpus. The threshold ships at `0.0` — inert — and is chosen later by walking the new queue.

**Tech Stack:** FastAPI, asyncpg, Alembic (raw SQL migrations), pytest/pytest-asyncio, ONNX Runtime (YOLO26x), React + Vitest.

**Spec:** `docs/specs/2026-09-13-animal-rescore-design.md` — read it before Task 1. It carries the reasoning; this plan carries the steps.

## Global Constraints

- **Nothing gates a save.** A detector failure leaves `animal_confidence` NULL and the sighting exists, visible. NULL means "never scored" and must never be conflated with `0.0` ("scored, saw nothing").
- **`animal_confidence_min` defaults to `0.0`** and stays there for this whole plan. Every filter added here is a no-op on merge. Do not pick a threshold.
- **The model never writes `review_status` or `reviewed_at`.** Those mean "a person ruled on a report". Animal verdicts live in `animal_override` / `animal_reviewed_at` / `animal_reviewed_by`.
- **Nothing is deleted.** No row, no S3 object. Hiding is reversible; deletion is not.
- **`sightings.dog_confidence` is NOT dropped in this plan.** It is superseded and left in place; dropping it is phase 2, after the production rescore.
- **Detector name string is `"yolo26x"`**, from `DETECTOR_NAME` in `app/detect_reid.py`. Never a literal at a call site.
- Run backend tests from `backend/` with `uv run pytest`. Frontend tests from `frontend/` with `npm test`.
- Commit after every task. Conventional-commit prefixes, matching the repo (`feat:`, `fix:`, `docs:`, `chore:`).

---

### Task 1: The migration

**Files:**
- Create: `backend/migrations/versions/0013_detections.py`
- Create: `backend/tests/test_detections_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: table `detections (photo_id uuid, model text, dog real, cat real, created_at timestamptz)` PK `(photo_id, model)`; columns `sightings.animal_confidence real`, `sightings.animal_override boolean`, `sightings.animal_reviewed_at timestamptz`, `sightings.animal_reviewed_by uuid`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_detections_schema.py`:

```python
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd backend && uv run pytest tests/test_detections_schema.py -v`
Expected: FAIL — `relation "detections" does not exist`.

- [ ] **Step 3: Write the migration**

Create `backend/migrations/versions/0013_detections.py`:

```python
"""One score per (photo, model), and a human verdict that outranks it.

`sightings.dog_confidence` (0002) recorded a number and not which model
produced it. Older rows were scored by YOLOv8n and newer ones by YOLO26x, and
the two disagree badly -- v8n scored a clearly visible dog at 0.021 where 26x
gives 0.800. So the column cannot be filtered on: a naive threshold would
silently hide real dogs whose only crime was being uploaded early.

`detections` fixes that by construction. `model` is part of the key, so a
future detector adds rows rather than invalidating them, the old numbers stay
readable as history, and the backfill gets a resume key that is not `IS NULL`
-- the trap `backfill_embeddings.py` documents in its own docstring.

`animal_confidence` is the denormalised max over a sighting's photos that the
read surfaces filter on; `/map` and `/dogs` are geo and count queries and
should not join per row.

`animal_override` is a moderator saying the detector is wrong, either way. It
is deliberately NOT `review_status`: that column, and `reviewed_at`, mean "a
person ruled on a report" (0010, #66), and a model writing them would erase
that. Keeping the verdict separate also means it survives a threshold retune
and the next detector swap -- which is what makes reviewing the corpus a
thing you do once.

`dog_confidence` is superseded but NOT dropped here. It is the only record of
the old scores, and it stays until the rescore has run in production.

Revision ID: 0013_detections
Revises: 0012_individual_names
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013_detections"
down_revision: Union[str, None] = "0012_individual_names"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE detections (
            photo_id   uuid NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            model      text NOT NULL,
            dog        real NOT NULL,
            cat        real NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (photo_id, model)
        );
        """
    )
    # The backfill's pending query is "photos with no row for THIS model", and
    # the recompute reads every row of one model for one sighting's photos.
    op.execute("CREATE INDEX ix_detections_model ON detections (model);")

    op.execute("ALTER TABLE sightings ADD COLUMN animal_confidence real;")
    op.execute("ALTER TABLE sightings ADD COLUMN animal_override boolean;")
    op.execute("ALTER TABLE sightings ADD COLUMN animal_reviewed_at timestamptz;")
    op.execute(
        "ALTER TABLE sightings ADD COLUMN animal_reviewed_by uuid "
        "REFERENCES observers(id);"
    )
    # The moderator queue is "scored, and nobody has ruled", lowest first.
    # Rows already ruled on are not candidates for it, so the index is partial.
    op.execute(
        "CREATE INDEX ix_sightings_animal_confidence ON sightings (animal_confidence) "
        "WHERE animal_confidence IS NOT NULL AND animal_override IS NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sightings_animal_confidence;")
    for col in ("animal_reviewed_by", "animal_reviewed_at",
                "animal_override", "animal_confidence"):
        op.execute(f"ALTER TABLE sightings DROP COLUMN IF EXISTS {col};")
    op.execute("DROP TABLE IF EXISTS detections;")
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `cd backend && uv run pytest tests/test_detections_schema.py tests/test_migration.py -v`
Expected: PASS. Note: **no test in this repo exercises `downgrade`** — for any migration — so the `downgrade` here is verified by inspection only, as every other migration's is. Do not add a downgrade test for this one alone; that would be the only such test in the tree.

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/versions/0013_detections.py backend/tests/test_detections_schema.py
git commit -m "feat(db): score per (photo, model), and a verdict that outranks it

A number with no record of which model produced it cannot be filtered on.
Keying on the model is what stops #67 recurring on the next detector swap.
dog_confidence is superseded, not dropped: it is the only record of the
old scores until the rescore has run in production."
```

---

### Task 2: `app/scoring.py` — the one place a score is written

**Files:**
- Create: `backend/app/scoring.py`
- Modify: `backend/app/detect_reid.py` (add `DETECTOR_NAME`)
- Create: `backend/tests/test_scoring.py`

**Interfaces:**
- Consumes: the `detections` table from Task 1.
- Produces:
  - `app.detect_reid.DETECTOR_NAME: str` (`"yolo26x"`)
  - `async def save_detection(conn, photo_id: UUID, dog: float, cat: float) -> None`
  - `async def recompute_animal_confidence(conn, sighting_id: UUID) -> None`

Three callers will write scores: the capture path (Task 3), the rescore script (Task 6), and — once #64 lands — the GPU worker. Three writers of one derived number is a smell, so the recompute lives here and they all call it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_scoring.py`:

```python
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

    sid, (pid,) = await _sighting(migrated_db)
    await migrated_db.execute(
        "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolov8n',0.99,0.0)",
        pid)
    await recompute_animal_confidence(migrated_db, sid)
    assert await migrated_db.fetchval(
        "SELECT animal_confidence FROM sightings WHERE id=$1", sid) is None
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd backend && uv run pytest tests/test_scoring.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.scoring'`.

- [ ] **Step 3: Add `DETECTOR_NAME`**

In `backend/app/detect_reid.py`, immediately after the `_MODEL_PATH` assignment (around line 41), add:

```python
# Written to `detections.model`, the way `embed.MODEL_NAME` is written to
# `embeddings.model`. Issue #67 exists because nobody recorded which detector
# produced a score; this is that record. Change it when the weights change,
# never to rename a file.
DETECTOR_NAME = "yolo26x"
```

- [ ] **Step 4: Write `app/scoring.py`**

```python
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
```

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `cd backend && uv run pytest tests/test_scoring.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/scoring.py backend/app/detect_reid.py backend/tests/test_scoring.py
git commit -m "feat: one place that writes a detection, one that derives the sighting's number

Three paths analyse a photo -- capture, the rescore script, and the GPU
worker in #64. All three write the same two things, so they share one
function instead of restating it and drifting."
```

---

### Task 3: The capture path writes both

**Files:**
- Modify: `backend/app/routes/sighting.py` (`_analyse_and_save` ~L60-123, `_save_dog_confidence` ~L203-229, the two INSERTs at ~L457 and ~L482)
- Modify: `backend/tests/test_sighting.py:120-140`
- Create: `backend/tests/test_capture_scoring.py`

**Interfaces:**
- Consumes: `save_detection`, `recompute_animal_confidence` (Task 2).
- Produces: after `POST /sighting` completes, one `detections` row per photo and a non-NULL `sightings.animal_confidence` (unless detection failed).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_capture_scoring.py`:

```python
"""What a capture records about what was in the frame.

The contract 0002 established and this must not break: scoring is a label,
never a gate. A detector failure costs the label and nothing else -- the
sighting exists, is visible, and its number is NULL rather than 0.0.
"""

import io

import pytest
from PIL import Image

pytestmark = pytest.mark.asyncio

BLR = (12.9716, 77.5946)


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 130, 110)).save(b, "JPEG")
    return b.getvalue()


async def _post(client):
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "device_gps", "captured_at": "2026-07-19T09:00:00Z",
              "lat": str(BLR[0]), "lng": str(BLR[1]), "geo_accuracy_m": "8.0"},
    )
    assert r.status_code == 201, r.text
    return r.json()["sighting_id"]


async def test_capture_records_a_detection_row_per_photo(authed_client, monkeypatch):
    from types import SimpleNamespace

    from app import analyse as analyse_mod

    monkeypatch.setattr(
        analyse_mod, "analyse",
        lambda raw: SimpleNamespace(dog_confidence=0.42, cat_confidence=0.07,
                                    box=None, image=None, has_animal=False),
    )
    client, _ = authed_client
    sid = await _post(client)

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT d.model, d.dog, d.cat FROM detections d "
            "JOIN photos p ON p.id = d.photo_id WHERE p.sighting_id = $1", sid)
        conf = await c.fetchval(
            "SELECT animal_confidence FROM sightings WHERE id = $1", sid)

    assert len(rows) == 1
    assert rows[0]["model"] == "yolo26x"
    assert rows[0]["dog"] == pytest.approx(0.42, abs=1e-6)
    assert rows[0]["cat"] == pytest.approx(0.07, abs=1e-6)
    # Any animal counts, so the sighting's number is the max of the two.
    assert conf == pytest.approx(0.42, abs=1e-6)


async def test_a_detector_failure_leaves_the_sighting_visible_and_unscored(
    authed_client, monkeypatch
):
    from app import analyse as analyse_mod

    def boom(raw):
        raise RuntimeError("onnx exploded")

    monkeypatch.setattr(analyse_mod, "analyse", boom)
    client, _ = authed_client
    sid = await _post(client)

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT animal_confidence, review_status FROM sightings WHERE id = $1", sid)
        n = await c.fetchval(
            "SELECT count(*) FROM detections d JOIN photos p ON p.id = d.photo_id "
            "WHERE p.sighting_id = $1", sid)

    assert row["animal_confidence"] is None, "NULL means never scored, not 0.0"
    assert row["review_status"] == "valid"
    assert n == 0
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd backend && uv run pytest tests/test_capture_scoring.py -v`
Expected: FAIL — `relation "detections"` has no rows / `animal_confidence` is NULL in the first test.

- [ ] **Step 3: Rewrite the write path**

In `backend/app/routes/sighting.py`:

(a) Delete the import at line 14: `from app.detect import DOG_CONF_THRESHOLD`.

(b) Inside `_analyse_and_save`, after `conf = found.dog_confidence` (~L79), replace the `best_conf` bookkeeping with a `detections` write. The loop body becomes:

```python
        try:
            async with pool.acquire() as conn:
                await save_detection(
                    conn, photo_id, found.dog_confidence, found.cat_confidence
                )
        except Exception:
            # A lost score costs a label. It must not cost the embedding that
            # the rest of this loop is about to compute.
            logger.warning(
                "failed to store detection for photo=%s", photo_id, exc_info=True
            )
```

Add `from app.scoring import recompute_animal_confidence, save_detection` to the local import block at the top of the function, next to `from app.analyse import analyse, embed_analysis`.

Delete the `best_conf` local and every line that maintains it.

(c) Replace the call at ~L123, `await _save_dog_confidence(pool, sighting_id, best_conf)`, with:

```python
    await _save_animal_confidence(pool, sighting_id)
```

(d) Replace the whole `_save_dog_confidence` function (~L203-229) with:

```python
async def _save_animal_confidence(pool: asyncpg.Pool, sighting_id: UUID) -> None:
    """Derive the sighting's number from the rows just written.

    Scoring is a label, never a gate (migration 0002): a failure here leaves
    `animal_confidence` NULL, which means "never scored" and is deliberately
    distinct from 0.0, "scored and saw nothing". Every surface reads NULL as
    visible. It must never affect whether the sighting exists.
    """
    try:
        async with pool.acquire() as conn:
            await recompute_animal_confidence(conn, sighting_id)
    except Exception:
        logger.warning(
            "failed to save animal_confidence for sighting=%s", sighting_id, exc_info=True
        )
```

Add `from app.scoring import recompute_animal_confidence` to the module-level imports.

(e) In both `INSERT INTO sightings` statements (~L457 and ~L482), remove `dog_confidence` from the column list and remove the corresponding `None` argument and its `$n` placeholder, renumbering the placeholders after it. The column is nullable, so omitting it is equivalent to the `NULL` being passed today.

- [ ] **Step 4: Fix the test that asserted on the old constant**

In `backend/tests/test_sighting.py`, replace the block at lines 126-139 (which imports `DOG_CONF_THRESHOLD` and asserts `row["dog_confidence"] < DOG_CONF_THRESHOLD`) with:

```python
    from app.config import settings

    # A blank frame scores low and is saved anyway -- that is the contract
    # 0002 established, and the number is now on `animal_confidence`.
    assert row["animal_confidence"] is not None
    assert row["animal_confidence"] < 0.25
    assert settings.animal_confidence_min == 0.0, (
        "this suite assumes the filter is inert; see the spec's phase 2"
    )
```

(`settings.animal_confidence_min` is added in Task 4. If executing Task 3 alone, add the field to `backend/app/config.py` now — see Task 4 Step 3 — rather than leaving a broken import.)

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `cd backend && uv run pytest tests/test_capture_scoring.py tests/test_sighting.py tests/test_idempotency.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/sighting.py backend/tests/test_capture_scoring.py backend/tests/test_sighting.py
git commit -m "feat(capture): record dog and cat per photo, with the model that scored them

analyse() has always computed cat_confidence and the capture path threw it
away. Persisting both makes 'not a dog' and 'not an animal' two queries over
one table instead of a second migration later."
```

---

### Task 4: `animal_present()` and the config knob

**Files:**
- Modify: `backend/app/config.py` (add `animal_confidence_min`)
- Modify: `backend/app/aggregates.py:25` and its nine `{COUNTABLE_SIGHTING}` call sites (L84, L99, L123, L144, L166, L196, L197, L199)
- Modify: `backend/app/routes/map.py:34,108`
- Modify: `backend/app/routes/dogs.py:47,178`
- Create: `backend/tests/test_animal_filter.py`

**Interfaces:**
- Consumes: `sightings.animal_confidence`, `sightings.animal_override` (Task 1).
- Produces:
  - `settings.animal_confidence_min: float` (default `0.0`)
  - `app.aggregates.animal_present() -> str` — a SQL boolean expression over alias `s`
  - `app.aggregates.countable_sighting() -> str` — replaces the `COUNTABLE_SIGHTING` constant

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_animal_filter.py`:

```python
"""What the shared surfaces count, once the detector has an opinion.

Three properties, and the third is the one that makes the review pass worth
doing once: a human ruling outranks the model and survives a retune.
"""

import pytest

from app.ids import uuid7

pytestmark = pytest.mark.asyncio


async def _sighting(conn, *, conf=None, override=None):
    oid, sid = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')", oid)
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geog, geo_source, "
        "                       animal_confidence, animal_override) "
        "VALUES ($1,$2,now(), ST_SetSRID(ST_MakePoint(77.59,12.97),4326)::geography, "
        "        'device_gps', $3, $4)",
        sid, oid, conf, override)
    return sid


async def _countable(conn):
    from app.aggregates import countable_sighting

    return await conn.fetchval(
        f"SELECT count(*) FROM sightings s WHERE {countable_sighting()}")


def test_the_filter_is_inert_by_default():
    """It ships switched off. The threshold is chosen in phase 2, against
    rescored numbers -- not from the mixed corpus #67 measured."""
    from app.config import settings

    assert settings.animal_confidence_min == 0.0


async def test_unscored_sightings_stay_visible(migrated_db, monkeypatch):
    """Fail open. NULL means the detector never ran or failed, and that must
    not take someone's photograph off the map."""
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    await _sighting(migrated_db, conf=None)
    assert await _countable(migrated_db) == 1


async def test_a_low_score_drops_out_once_a_threshold_is_set(migrated_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    await _sighting(migrated_db, conf=0.05)
    await _sighting(migrated_db, conf=0.91)
    assert await _countable(migrated_db) == 1


async def test_a_human_verdict_outranks_the_model_both_ways(migrated_db, monkeypatch):
    """The property that makes a review pass a one-time job: the ruling
    survives a threshold change and the next detector swap."""
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    await _sighting(migrated_db, conf=0.01, override=True)   # "it IS a dog"
    await _sighting(migrated_db, conf=0.99, override=False)  # "no it isn't"
    assert await _countable(migrated_db) == 1

    monkeypatch.setattr(settings, "animal_confidence_min", 0.90)
    assert await _countable(migrated_db) == 1


async def test_review_status_still_counts(migrated_db, monkeypatch):
    """The animal rule is composed with the moderation rule, not instead of
    it. A rejected sighting stays off regardless of how doggy it is."""
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    sid = await _sighting(migrated_db, conf=0.99)
    await migrated_db.execute(
        "UPDATE sightings SET review_status='rejected' WHERE id=$1", sid)
    assert await _countable(migrated_db) == 0
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd backend && uv run pytest tests/test_animal_filter.py -v`
Expected: FAIL — `ImportError: cannot import name 'countable_sighting'`.

- [ ] **Step 3: Add the setting**

In `backend/app/config.py`, after the re-identification block, add:

```python
    # --- animal presence ---------------------------------------------------
    # A sighting whose photos score below this does not appear on /map, /dogs,
    # the public counts, or in re-ID candidate search. It stays in its owner's
    # /dex, and nothing is deleted.
    #
    # DELIBERATELY 0.0 -- the filter ships switched off. `dog_confidence` was
    # scored by two detectors that disagree badly (v8n scored a real dog at
    # 0.021 where 26x gives 0.800, see #67), so any number picked before the
    # corpus is rescored would hide real dogs. Choose it by walking
    # /moderation/animals from the bottom: where you stop being able to say
    # "no animal" is the threshold.
    animal_confidence_min: float = 0.0
```

- [ ] **Step 4: Replace the constant with the two functions**

In `backend/app/aggregates.py`, replace the `COUNTABLE_SIGHTING` assignment (L25, keeping the comment block above it) with:

```python
def animal_present() -> str:
    """Is there an animal in this sighting, as far as anyone can tell?

    `animal_override` is a moderator's ruling and wins outright; NULL means
    nobody has looked and the detector's number decides. A NULL score is
    "never scored" and reads as present -- fail open, so a detector failure
    costs a label and never someone's photograph.

    A function rather than a constant because the threshold has to be varied
    per test, and an f-string evaluated at import cannot be.
    """
    lo = settings.animal_confidence_min
    return (
        "COALESCE(s.animal_override, "
        f"s.animal_confidence IS NULL OR s.animal_confidence >= {lo:g})"
    )


def countable_sighting() -> str:
    """The one definition. `/map` and `/dogs` call it rather than restating it.

    `= 'valid'`, not `<> 'rejected'`. Since #46 gave `review_status` a writer,
    `pending` means someone reported this and no human has looked yet -- the
    whole point of that state is that it waits somewhere other than a public
    surface.
    """
    return f"s.review_status = 'valid' AND {animal_present()}"
```

Then at each of the nine call sites in this file, change `{COUNTABLE_SIGHTING}` to `{countable_sighting()}`.

In `backend/app/routes/map.py`, change the import on L34 to `from app.aggregates import countable_sighting` and L108 to `AND {countable_sighting()}`.

In `backend/app/routes/dogs.py`, change the import on L47 to `from app.aggregates import countable_sighting` and L178 to `AND {countable_sighting()}`.

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `cd backend && uv run pytest tests/test_animal_filter.py tests/test_aggregates.py tests/test_map.py tests/test_dogs_route.py tests/test_stats.py -v`
Expected: PASS. Every existing test is unaffected because the default threshold passes everything.

- [ ] **Step 6: Commit**

```bash
git add backend/app/config.py backend/app/aggregates.py backend/app/routes/map.py backend/app/routes/dogs.py backend/tests/test_animal_filter.py
git commit -m "feat: one animal predicate, composed with the moderation one

Ships inert at 0.0. The threshold is chosen in phase 2 by walking the
flagged queue, because a histogram says where scores cluster and not
where the detector starts being wrong.

A callable, not a module constant: the tests have to vary the threshold
and an import-time f-string cannot be varied."
```

---

### Task 5: The re-ID paths get the animal half too

**Files:**
- Modify: `backend/app/matching.py:143`
- Modify: `backend/app/routes/match.py:192-193`
- Create: `backend/tests/test_animal_reid.py`

**Interfaces:**
- Consumes: `animal_present()` (Task 4).
- Produces: no new symbols.

`COUNTABLE_SIGHTING` does not cover these two. They restate their own visibility rules for stated reasons, and those differences stay — only the animal half is added. The reason it matters: `REID_CONF_THRESHOLD = 0.10` is a *different knob* from the map threshold, so a photo can score below the map threshold, still produce a box, get embedded, and seed an identity.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_animal_reid.py`:

```python
"""A photo with no animal in it must not be proposed as the same animal as
something else.

The box threshold (REID_CONF_THRESHOLD = 0.10) is independent of the map
threshold, so a sighting can be too low for the map and still have an
embedding. `match.py`'s own comment says a `same` verdict folds content into
an identity that no later decision can unpick -- so this is the one place
where getting it wrong is not reversible.
"""

import pytest

from app.ids import uuid7

pytestmark = pytest.mark.asyncio


async def _observer(conn):
    oid = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')", oid)
    return oid


async def _embedded_sighting(conn, oid, *, conf, vec_seed=0.1):
    """A sighting with one photo and a real-shaped vector, scored `conf`."""
    from app.embed import EMBED_DIM, MODEL_NAME

    sid, pid = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geog, geo_source, "
        "                       animal_confidence) "
        "VALUES ($1,$2,now(), ST_SetSRID(ST_MakePoint(77.59,12.97),4326)::geography, "
        "        'device_gps', $3)",
        sid, oid, conf)
    await conn.execute(
        "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,'k')", pid, sid)
    vec = "[" + ",".join(f"{vec_seed:.7g}" for _ in range(EMBED_DIM)) + "]"
    await conn.execute(
        "INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
        "VALUES ($1,$2,$3,$4,$5::vector)",
        uuid7(), pid, MODEL_NAME, EMBED_DIM, vec)
    return sid


async def test_a_low_scoring_sighting_is_not_a_candidate(migrated_db, monkeypatch):
    from app.config import settings
    from app.matching import find_candidates

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    oid = await _observer(migrated_db)
    query = await _embedded_sighting(migrated_db, oid, conf=0.95)
    furniture = await _embedded_sighting(migrated_db, oid, conf=0.05)
    real = await _embedded_sighting(migrated_db, oid, conf=0.88)

    hits = await find_candidates(migrated_db, query)
    ids = {str(h["sighting_id"]) for h in hits}
    assert str(furniture) not in ids
    assert str(real) in ids


async def test_a_moderator_can_put_one_back(migrated_db, monkeypatch):
    """The override reaches re-ID too, or a moderator's 'yes it is a dog'
    would restore it to the map and still leave it unmatchable."""
    from app.config import settings
    from app.matching import find_candidates

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    oid = await _observer(migrated_db)
    query = await _embedded_sighting(migrated_db, oid, conf=0.95)
    rescued = await _embedded_sighting(migrated_db, oid, conf=0.05)
    await migrated_db.execute(
        "UPDATE sightings SET animal_override = true WHERE id = $1", rescued)

    ids = {str(h["sighting_id"]) for h in await find_candidates(migrated_db, query)}
    assert str(rescued) in ids
```

Before writing this, open `backend/app/matching.py` and read `find_candidates`'s real signature and return shape; adjust the two call sites and the key name (`sighting_id`) to match it exactly. Do not guess.

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd backend && uv run pytest tests/test_animal_reid.py -v`
Expected: FAIL — the furniture sighting is returned as a candidate.

- [ ] **Step 3: Compose the predicate into both**

In `backend/app/matching.py`, add `animal_present` to the `app.aggregates` import, and after the existing `AND s.review_status <> 'rejected'` (L143) add:

```sql
                  -- A photo with no animal in it must not seed an identity.
                  -- Note this is a different knob from the box threshold:
                  -- REID_CONF_THRESHOLD = 0.10 can produce a box, and so an
                  -- embedding, for a frame the map would not show.
                  AND {animal_present()}
```

In `backend/app/routes/match.py`, add the same import and, after the existing pair of `review_status = 'valid'` checks (L192-193), add the predicate for both sides. `animal_present()` is written against the alias `s`, and this query uses `a` and `b`, so use `str.replace` at the call site rather than a second function:

```python
          AND {animal_present().replace("s.", "a.")}
          AND {animal_present().replace("s.", "b.")}
```

Add a comment saying why the replace is there, and assert in `test_animal_filter.py` that `animal_present()` references no table alias other than `s.` so this stays safe:

```python
def test_the_predicate_only_touches_the_s_alias():
    """`routes/match.py` re-aliases this to `a.` and `b.` with a string
    replace, which is only safe while every column reference is `s.`-prefixed
    and nothing else in the string contains "s.". Guard it here."""
    from app.aggregates import animal_present

    sql = animal_present()
    assert sql.count("s.") == sql.count("s.animal")
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `cd backend && uv run pytest tests/test_animal_reid.py tests/test_animal_filter.py tests/test_matching.py tests/test_merge_authorization.py tests/test_verdict_durability.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/matching.py backend/app/routes/match.py backend/tests/test_animal_reid.py backend/tests/test_animal_filter.py
git commit -m "feat(reid): no animal, no candidate

The two re-ID paths restate their own visibility rules for stated reasons,
so only the animal half is added and those differences stay. This is the
one surface where being wrong is not reversible: match.py's own comment
says a 'same' verdict folds content into an identity nothing can unpick."
```

---

### Task 6: `rescore_photos.py`

**Files:**
- Create: `backend/scripts/rescore_photos.py`
- Create: `backend/tests/test_rescore.py`

**Interfaces:**
- Consumes: `save_detection`, `recompute_animal_confidence` (Task 2); `analyse`, `embed_analysis` (`app/analyse.py`).
- Produces: a CLI. No importable API other than `main()` and `PENDING_SQL`.

Read `backend/scripts/backfill_embeddings.py` first and mirror its structure, flags, logging and docstring register. This is its sibling, not a new idiom.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_rescore.py`:

```python
"""The backfill's contract: resumable, serial, and re-runnable to a no-op.

The photo-level resume key is why `detections` is keyed on the model.
`backfill_embeddings.py` documents what the alternative costs: with no key
but `IS NULL`, a photo that legitimately produces nothing is re-examined on
every run and the pending count never reaches zero.
"""

import pytest

from app.ids import uuid7

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
    import importlib

    mod = importlib.import_module("scripts.rescore_photos")

    _, scored = await _photo(migrated_db, key="a")
    _, unscored = await _photo(migrated_db, key="b")
    await migrated_db.execute(
        "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolo26x',0.9,0.0)",
        scored)

    rows = await migrated_db.fetch(mod.PENDING_SQL, "yolo26x")
    assert [r["id"] for r in rows] == [unscored]


async def test_a_row_from_another_model_does_not_count_as_scored(migrated_db):
    """The trap this whole piece of work exists to remove: a YOLOv8n score is
    not a YOLO26x score, and must not make the photo look done."""
    import importlib

    mod = importlib.import_module("scripts.rescore_photos")

    _, pid = await _photo(migrated_db)
    await migrated_db.execute(
        "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolov8n',0.021,0.0)",
        pid)

    rows = await migrated_db.fetch(mod.PENDING_SQL, "yolo26x")
    assert [r["id"] for r in rows] == [pid]
```

Note on importing: `backend/scripts/` is not a package on `sys.path` by default. Add `backend/tests/conftest.py`-level support by appending this to the test file's top, matching how the script itself does it:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

If `scripts/__init__.py` does not exist, create an empty one — check whether `test_backup_db.py` or `test_load_areas.py` already solved this and copy that solution rather than inventing a second one.

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd backend && uv run pytest tests/test_rescore.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.rescore_photos`.

- [ ] **Step 3: Write the script**

Create `backend/scripts/rescore_photos.py`:

```python
"""Score every photo with the current detector, and record which one that was.

`sightings.dog_confidence` is not comparable across rows. Older captures were
scored by YOLOv8n, newer ones by YOLO26x, and the two disagree badly -- v8n
scored a clearly visible dog at 0.021 where 26x gives 0.800. Which model
produced a given score was never recorded, so filtering on the column would
silently hide real dogs whose only crime was being uploaded early (issue #67).

This makes one number mean one thing. After it runs, every photo has a
`detections` row for `DETECTOR_NAME`, and `sightings.animal_confidence` is the
max over each sighting's photos of `max(dog, cat)` under that detector.

Safe to run repeatedly and safe to interrupt: pending is "no row for THIS
model", each photo is written in its own statement, and `save_detection`
upserts. Killing it mid-run loses at most the photo in flight. That resume key
is why the table is keyed on the model at all -- `backfill_embeddings.py`
documents what the `IS NULL` alternative costs.

Usage, from /app/backend inside the container:

    uv run python scripts/rescore_photos.py --dry-run
    uv run python scripts/rescore_photos.py
    uv run python scripts/rescore_photos.py --embed
    uv run python scripts/rescore_photos.py --histogram

`--embed` also fills a missing MiewID vector from the same detection pass.
Rescoring and then running `backfill_embeddings.py` would be two forward
passes over identical bytes, which is exactly the waste #49 removed from the
capture path -- and note that the older script calls `embed.embed_photo`,
which re-decodes and re-detects internally. This one uses `analyse()` +
`embed_analysis()`, the pair the capture path uses, so one pass serves both.

`--histogram` scores nothing and prints the distribution of what is already
stored. That is the input to choosing `animal_confidence_min` -- but only the
input. A histogram says where the scores cluster, not where the detector
starts being wrong; for that, walk /moderation/animals from the bottom.

Deliberately serial. Inference is CPU-bound ONNX on the same box that serves
requests; a parallel backfill would compete with live uploads for the same two
cores. At roughly 0.9 s per pass the whole corpus is single-digit minutes, and
`--sleep` throttles it further if it has to run during the day.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import asyncpg

# Run as a script rather than a module, so `app` is not importable yet.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analyse import analyse, embed_analysis  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import effective_dsn  # noqa: E402
from app.detect_reid import DETECTOR_NAME  # noqa: E402
from app.embed import EMBED_DIM, MODEL_NAME  # noqa: E402
from app.ids import uuid7  # noqa: E402
from app.scoring import recompute_animal_confidence, save_detection  # noqa: E402
from app.storage.s3 import storage_from_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rescore")

PENDING_SQL = """
    SELECT p.id, p.s3_key, p.sighting_id
    FROM photos p
    LEFT JOIN detections d
           ON d.photo_id = p.id AND d.model = $1
    WHERE d.photo_id IS NULL
    ORDER BY p.created_at
"""

HISTOGRAM_SQL = """
    SELECT width_bucket(greatest(d.dog, d.cat), 0, 1, 10) AS bucket,
           count(*) AS photos
    FROM detections d
    WHERE d.model = $1
    GROUP BY 1
    ORDER BY 1
"""


async def _histogram(conn) -> int:
    rows = await conn.fetch(HISTOGRAM_SQL, DETECTOR_NAME)
    if not rows:
        log.info("nothing scored by %s yet", DETECTOR_NAME)
        return 1
    log.info("max(dog, cat) under %s, per photo:", DETECTOR_NAME)
    for r in rows:
        lo = (r["bucket"] - 1) / 10
        log.info("  %.1f-%.1f  %s", lo, lo + 0.1, "#" * r["photos"])
    log.info("")
    log.info("This says where scores cluster, not where the detector starts")
    log.info("being wrong. Walk /moderation/animals from the bottom for that.")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="stop after N photos (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to pause between photos, to stay out of the way")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be scored, touch nothing")
    ap.add_argument("--embed", action="store_true",
                    help="also fill a missing MiewID vector from the same pass")
    ap.add_argument("--histogram", action="store_true",
                    help="print the distribution of existing scores and exit")
    args = ap.parse_args()

    conn = await asyncpg.connect(effective_dsn(settings.database_url))
    try:
        if args.histogram:
            return await _histogram(conn)

        pending = await conn.fetch(PENDING_SQL, DETECTOR_NAME)
        if args.limit:
            pending = pending[: args.limit]
        log.info("%d photo(s) with no %s score", len(pending), DETECTOR_NAME)
        if args.dry_run or not pending:
            return 0

        storage = storage_from_settings()
        touched: set = set()
        failed = 0

        for i, row in enumerate(pending, 1):
            try:
                raw = await storage.get(row["s3_key"])
                found = await asyncio.to_thread(analyse, raw)
            except Exception:
                # Fail open, as the capture path does: no row means "never
                # scored", the sighting stays visible, and a later run retries.
                failed += 1
                log.warning("  [%d/%d] %s FAILED", i, len(pending), row["id"],
                            exc_info=True)
                continue

            await save_detection(conn, row["id"], found.dog_confidence,
                                 found.cat_confidence)
            touched.add(row["sighting_id"])
            log.info("  [%d/%d] %s dog=%.3f cat=%.3f", i, len(pending), row["id"],
                     found.dog_confidence, found.cat_confidence)

            if args.embed and found.has_animal:
                await _maybe_embed(conn, row["id"], found)

            if args.sleep:
                time.sleep(args.sleep)

        for sid in touched:
            await recompute_animal_confidence(conn, sid)

        log.info("scored %d, failed %d, %d sighting(s) updated",
                 len(pending) - failed, failed, len(touched))
        return 0
    finally:
        await conn.close()


async def _maybe_embed(conn, photo_id, found) -> None:
    """Fill a missing vector from the detection pass we already paid for."""
    exists = await conn.fetchval(
        "SELECT 1 FROM embeddings WHERE photo_id=$1 AND model=$2 AND vec_miew IS NOT NULL",
        photo_id, MODEL_NAME)
    if exists:
        return
    try:
        vec = await asyncio.to_thread(embed_analysis, found)
    except Exception:
        log.warning("    embed failed for %s", photo_id, exc_info=True)
        return
    if vec is None:
        return
    box = found.box
    await conn.execute(
        """
        INSERT INTO embeddings (id, photo_id, model, dim, vec_miew, bbox)
        VALUES ($1,$2,$3,$4,$5::vector,$6::jsonb)
        ON CONFLICT (photo_id, model) DO UPDATE
            SET vec_miew = EXCLUDED.vec_miew, bbox = EXCLUDED.bbox, created_at = now()
        """,
        uuid7(), photo_id, MODEL_NAME, EMBED_DIM,
        "[" + ",".join(f"{float(v):.7g}" for v in vec) + "]",
        json.dumps({"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]}))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

Before running: confirm against `backfill_embeddings.py` that `storage_from_settings()` and the object-read method are named as used here (`storage.get`). If the real method differs, use the real one.

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `cd backend && uv run pytest tests/test_rescore.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Smoke the CLI against the dev database**

Run: `cd backend && uv run python scripts/rescore_photos.py --dry-run`
Expected: prints a count and exits 0, writing nothing.

- [ ] **Step 6: Commit**

```bash
git add backend/scripts/rescore_photos.py backend/tests/test_rescore.py
git commit -m "feat(scripts): rescore the corpus with the current detector

Resumable on 'no row for THIS model', which is the resume key the table's
shape exists to provide. --embed reuses the same pass to fill a missing
vector rather than paying for a second forward pass, which is the mistake
#49 removed from the capture path."
```

---

### Task 7: The flagged queue and the animal verdict

**Files:**
- Modify: `backend/app/routes/moderation.py` (add two endpoints at the end)
- Create: `backend/tests/test_moderation_animals.py`

**Interfaces:**
- Consumes: `require_moderator`, `thumb_key`, `get_storage` (already imported in `moderation.py`); `DETECTOR_NAME`.
- Produces:
  - `GET /moderation/animals` → `{"items": [{sighting_id, captured_at, observer, animal_confidence, dog, cat, thumb_url}]}`
  - `POST /sighting/{id}/animal` with form field `verdict` ∈ `{"animal", "no_animal"}` → `{"status": "ok", "animal_override": bool}`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_moderation_animals.py`:

```python
"""The queue of photos the detector thinks have no animal in them.

Sorted by score ascending, which is the point: a histogram says where the
scores cluster, but walking this list from the bottom is how you find where
the detector starts being wrong -- and that is the threshold. So the pass that
checks the cleanup is the same pass that produces the number.

It works while `animal_confidence_min` is still 0.0 and nothing is hidden,
which is what lets review come before the filter rather than after it.
"""

import pytest

from app.ids import uuid7
from app.security import issue_session

pytestmark = pytest.mark.asyncio


async def _pool(client):
    return client._transport.app.state.pool


async def _moderator(client):
    oid = uuid7()
    async with (await _pool(client)).acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via, trust_tier) "
            "VALUES ($1,'Mod','test','moderator')", oid)
    client.cookies.set("session", issue_session(oid))
    return oid


async def _scored(client, *, dog, cat, key="k"):
    sid, pid = uuid7(), uuid7()
    async with (await _pool(client)).acquire() as c:
        oid = uuid7()
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')",
            oid)
        await c.execute(
            "INSERT INTO sightings (id, observer_id, captured_at, animal_confidence) "
            "VALUES ($1,$2,now(),$3)", sid, oid, max(dog, cat))
        await c.execute(
            "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,$3)", pid, sid, key)
        await c.execute(
            "INSERT INTO detections (photo_id, model, dog, cat) VALUES ($1,'yolo26x',$2,$3)",
            pid, dog, cat)
    return sid


async def test_the_queue_is_least_animal_like_first(app_client):
    await _moderator(app_client)
    mid = await _scored(app_client, dog=0.40, cat=0.00, key="b")
    low = await _scored(app_client, dog=0.02, cat=0.01, key="a")
    high = await _scored(app_client, dog=0.95, cat=0.00, key="c")

    r = await app_client.get("/moderation/animals")
    assert r.status_code == 200, r.text
    ids = [i["sighting_id"] for i in r.json()["items"]]
    assert ids == [str(low), str(mid), str(high)]


async def test_it_reports_dog_and_cat_separately(app_client):
    """Akash's ask was to confirm the photos being taken out have no *dog* in
    them. A bare 0.82 cannot answer that -- it might have been a cat."""
    await _moderator(app_client)
    sid = await _scored(app_client, dog=0.03, cat=0.82)

    item = (await app_client.get("/moderation/animals")).json()["items"][0]
    assert item["sighting_id"] == str(sid)
    assert item["dog"] == pytest.approx(0.03, abs=1e-6)
    assert item["cat"] == pytest.approx(0.82, abs=1e-6)


async def test_the_queue_works_while_the_filter_is_inert(app_client):
    """Review comes before the threshold, not after it."""
    from app.config import settings

    assert settings.animal_confidence_min == 0.0
    await _moderator(app_client)
    await _scored(app_client, dog=0.01, cat=0.00)
    assert len((await app_client.get("/moderation/animals")).json()["items"]) == 1


async def test_ruling_records_the_verdict_and_empties_it_from_the_queue(app_client):
    mod = await _moderator(app_client)
    sid = await _scored(app_client, dog=0.02, cat=0.00)

    r = await app_client.post(f"/sighting/{sid}/animal", data={"verdict": "animal"})
    assert r.status_code == 200, r.text

    async with (await _pool(app_client)).acquire() as c:
        row = await c.fetchrow(
            "SELECT animal_override, animal_reviewed_at, animal_reviewed_by, "
            "       review_status, reviewed_at "
            "FROM sightings WHERE id=$1", sid)
    assert row["animal_override"] is True
    assert row["animal_reviewed_at"] is not None
    assert row["animal_reviewed_by"] == mod
    # The two review states are independent questions about the same photo.
    assert row["review_status"] == "valid"
    assert row["reviewed_at"] is None

    assert (await app_client.get("/moderation/animals")).json()["items"] == []


async def test_no_animal_is_recorded_too(app_client):
    await _moderator(app_client)
    sid = await _scored(app_client, dog=0.02, cat=0.00)
    r = await app_client.post(f"/sighting/{sid}/animal", data={"verdict": "no_animal"})
    assert r.status_code == 200

    async with (await _pool(app_client)).acquire() as c:
        assert await c.fetchval(
            "SELECT animal_override FROM sightings WHERE id=$1", sid) is False


async def test_a_non_moderator_gets_nothing(app_client):
    """Same shape as the reported queue: a 404, so a non-moderator sees what
    they would see for a route that does not exist."""
    oid = uuid7()
    async with (await _pool(app_client)).acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')",
            oid)
    app_client.cookies.set("session", issue_session(oid))

    assert (await app_client.get("/moderation/animals")).status_code == 404
```

Check `require_moderator` in `app/auth/deps.py` for the actual status it raises and assert that, not 404, if it differs.

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd backend && uv run pytest tests/test_moderation_animals.py -v`
Expected: FAIL — 404 for `/moderation/animals` (route not defined).

- [ ] **Step 3: Add the endpoints**

Append to `backend/app/routes/moderation.py`:

```python
@router.get("/moderation/animals")
async def animal_queue(
    _mod: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    """Photos the detector thinks have no animal in them, least likely first.

    THIS IS ALSO HOW THE THRESHOLD GETS CHOSEN
    ------------------------------------------
    A histogram of scores says where they cluster; it cannot say where the
    detector starts being wrong. Walking this list from the bottom does: you
    stop being able to say "no animal" at some point, and that point is
    `animal_confidence_min`. So the review pass that checks the cleanup is the
    same pass that produces the number, and it runs while the filter is still
    inert and nothing has been hidden from anyone.

    Dog and cat are reported separately rather than as the max the sighting
    was scored with. The product call is that any animal counts, but the
    question a moderator is actually answering is "is there a dog in this",
    and 0.82 alone cannot distinguish a dog from a cat.

    Not folded into `/moderation/queue`: that one aggregates reports with a
    HAVING clause, and a second source would make both unreadable.
    """
    rows = await conn.fetch(
        """
        SELECT s.id,
               s.captured_at,
               s.animal_confidence,
               o.display_name AS observer,
               pd.dog, pd.cat, pd.s3_key
        FROM sightings s
        LEFT JOIN observers o ON o.id = s.observer_id
        -- One representative photo and its scores, in a lateral rather than a
        -- join: a clip yields up to twelve frames and would multiply the row.
        -- The photo shown is the one that scored highest, so a moderator
        -- rules against the sighting's best evidence rather than its worst.
        LEFT JOIN LATERAL (
            SELECT ph.s3_key, dd.dog, dd.cat
            FROM photos ph
            JOIN detections dd ON dd.photo_id = ph.id AND dd.model = $1
            WHERE ph.sighting_id = s.id
            ORDER BY greatest(dd.dog, dd.cat) DESC
            LIMIT 1
        ) pd ON TRUE
        WHERE s.animal_confidence IS NOT NULL
          AND s.animal_override IS NULL
        ORDER BY s.animal_confidence ASC, s.captured_at DESC
        LIMIT $2
        """,
        DETECTOR_NAME,
        MAX_QUEUE,
    )
    keys = [thumb_key(r["s3_key"]) for r in rows if r["s3_key"]]
    urls = await storage.urls(keys)
    thumbs = iter(urls)
    return {
        "items": [
            {
                "sighting_id": str(r["id"]),
                "captured_at": r["captured_at"],
                "observer": r["observer"],
                "animal_confidence": r["animal_confidence"],
                "dog": r["dog"],
                "cat": r["cat"],
                "thumb_url": next(thumbs) if r["s3_key"] else None,
            }
            for r in rows
        ]
    }


@router.post("/sighting/{sighting_id}/animal")
async def rule_on_animal(
    sighting_id: UUID,
    verdict: Literal["animal", "no_animal"] = Form(...),
    moderator_id: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
):
    """A person's answer to "is there an animal in this", which beats the model's.

    Written to `animal_override`, never to `review_status`. That column and
    `reviewed_at` mean "a person ruled on a report" (0010), and a verdict about
    what is in the frame is a different question -- the same photo can carry
    both answers. Keeping them apart is also what lets this ruling survive a
    threshold retune and the next detector swap, so the corpus is reviewed
    once rather than after every change.

    Reversible by ruling again, and it deletes nothing.
    """
    override = verdict == "animal"
    updated = await conn.fetchval(
        "UPDATE sightings SET animal_override = $2, animal_reviewed_at = now(), "
        "animal_reviewed_by = $3, updated_at = now() WHERE id = $1 RETURNING id",
        sighting_id,
        override,
        moderator_id,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such sighting")
    logger.info(
        "sighting=%s ruled %s by moderator=%s", sighting_id, verdict, moderator_id
    )
    return {"status": "ok", "animal_override": override}
```

Add `from app.detect_reid import DETECTOR_NAME` to the module imports. `MAX_QUEUE`, `thumb_key`, `get_storage`, `require_moderator`, `Literal`, `Form` and `HTTPException` are all already imported by this file for the reported queue — confirm rather than re-adding.

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `cd backend && uv run pytest tests/test_moderation_animals.py tests/test_moderation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/moderation.py backend/tests/test_moderation_animals.py
git commit -m "feat(moderation): a queue of what the detector says is not an animal

Sorted least-animal-like first, which is not just a safety net over the
threshold -- it is how the threshold gets chosen. A histogram says where
scores cluster; walking this list says where the detector starts being
wrong. And it works while the filter is still inert, so review comes
before anything is hidden."
```

---

### Task 8: The moderator's second queue in the UI

**Files:**
- Modify: `frontend/src/api.ts` (after `reviewSighting`, ~L430)
- Modify: `frontend/src/screens/Moderation.tsx`
- Create: `frontend/src/screens/Moderation.animals.test.tsx`

**Interfaces:**
- Consumes: `GET /moderation/animals`, `POST /sighting/{id}/animal` (Task 7).
- Produces: `FlaggedItem`, `getFlaggedQueue()`, `ruleOnAnimal()` in `api.ts`.

The screen is reached from `Dex.tsx`'s moderator-only `flags` view. Add a sub-toggle inside `Moderation.tsx` rather than a new top-level tab — the nav already has five entries.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/screens/Moderation.animals.test.tsx`:

```tsx
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getModerationQueue: vi.fn(),
  reviewSighting: vi.fn(),
  getFlaggedQueue: vi.fn(),
  ruleOnAnimal: vi.fn(),
}));

import {
  getFlaggedQueue,
  getModerationQueue,
  ruleOnAnimal,
  type FlaggedItem,
} from "../api";
import Moderation from "./Moderation";

afterEach(cleanup);

function flagged(over: Partial<FlaggedItem> = {}): FlaggedItem {
  return {
    sighting_id: "s1",
    captured_at: "2026-08-01T10:00:00Z",
    observer: "Priya",
    animal_confidence: 0.02,
    dog: 0.02,
    cat: 0.01,
    thumb_url: "https://example.test/a_thumb.webp",
    ...over,
  };
}

/** The flagged queue lives behind a toggle in the same screen as the reported
 * one, so every test here has to switch to it first. */
async function openFlagged() {
  vi.mocked(getModerationQueue).mockResolvedValue({ items: [] });
  render(<Moderation onUnauthorized={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: /NOT ANIMALS/ }));
}

describe("Moderation — the flagged queue", () => {
  it("says so plainly when nothing is flagged", async () => {
    vi.mocked(getFlaggedQueue).mockResolvedValue({ items: [] });
    await openFlagged();
    expect(await screen.findByText(/NOTHING FLAGGED/)).toBeInTheDocument();
  });

  it("shows dog and cat separately, not the max", async () => {
    // The question being answered is "is there a DOG in this". A bare 0.82
    // cannot answer it -- that might have been a cat.
    vi.mocked(getFlaggedQueue).mockResolvedValue({
      items: [flagged({ dog: 0.03, cat: 0.82, animal_confidence: 0.82 })],
    });
    await openFlagged();
    expect(await screen.findByText(/DOG 0\.03/)).toBeInTheDocument();
    expect(screen.getByText(/CAT 0\.82/)).toBeInTheDocument();
  });

  it("lists the least animal-like first, in the order the API gave them", async () => {
    // The order is the instrument: you walk it from the top until the photos
    // start being real dogs, and that is where the threshold goes. Re-sorting
    // client-side would break that, so assert the API order is preserved.
    vi.mocked(getFlaggedQueue).mockResolvedValue({
      items: [
        flagged({ sighting_id: "low", dog: 0.01, cat: 0.0, observer: "Least" }),
        flagged({ sighting_id: "mid", dog: 0.4, cat: 0.0, observer: "Middle" }),
      ],
    });
    await openFlagged();
    await screen.findByText(/Least/);
    const shown = screen.getAllByText(/logged by/).map((n) => n.textContent);
    expect(shown[0]).toMatch(/Least/);
    expect(shown[1]).toMatch(/Middle/);
  });

  it("keeps one the moderator says is real, and drops the card", async () => {
    // Dropped locally rather than refetched, matching the reported queue: the
    // list must not reorder under someone working down it.
    vi.mocked(getFlaggedQueue).mockResolvedValue({ items: [flagged()] });
    vi.mocked(ruleOnAnimal).mockResolvedValue(undefined);
    await openFlagged();

    await userEvent.click(await screen.findByRole("button", { name: /THERE IS AN ANIMAL/ }));
    expect(ruleOnAnimal).toHaveBeenCalledWith("s1", "animal");
    await waitFor(() => expect(screen.queryByText(/logged by Priya/)).toBeNull());
  });

  it("confirms one the detector was right about", async () => {
    vi.mocked(getFlaggedQueue).mockResolvedValue({ items: [flagged()] });
    vi.mocked(ruleOnAnimal).mockResolvedValue(undefined);
    await openFlagged();

    await userEvent.click(await screen.findByRole("button", { name: /^NO ANIMAL$/ }));
    expect(ruleOnAnimal).toHaveBeenCalledWith("s1", "no_animal");
    await waitFor(() => expect(screen.queryByText(/logged by Priya/)).toBeNull());
  });
});
```

Note the `/^NO ANIMAL$/` anchor in the last test: without it the matcher also
hits the `NOT ANIMALS` toggle and the empty state.

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd frontend && npm test -- Moderation.animals`
Expected: FAIL — `getFlaggedQueue` is not exported.

- [ ] **Step 3: Add the API functions**

In `frontend/src/api.ts`, after `reviewSighting`:

```ts
export interface FlaggedItem {
  sighting_id: string;
  captured_at: string;
  /** Who logged it. User-supplied at /join, so untrusted text. */
  observer: string | null;
  /** max(dog, cat) over the sighting's photos, under the current detector. */
  animal_confidence: number | null;
  /** Reported separately because "is there a dog in this" and "is there an
   * animal in this" are different questions, and the max cannot tell them
   * apart. */
  dog: number | null;
  cat: number | null;
  thumb_url: string | null;
}

/** Moderators only. Least animal-like first — which is also how the hiding
 * threshold gets chosen: walk it from the top until the photos start being
 * real dogs. */
export async function getFlaggedQueue(): Promise<{ items: FlaggedItem[] }> {
  const res = await fetch(`${API_BASE}/moderation/animals`, { credentials: "include" });
  return handle<{ items: FlaggedItem[] }>(res);
}

/** A person's verdict, which outranks the model's and survives a later change
 * to the threshold or the detector. Writes nothing to `review_status`: being
 * reported and having no animal in it are different questions. */
export async function ruleOnAnimal(
  sightingId: string,
  verdict: "animal" | "no_animal",
): Promise<void> {
  const form = new FormData();
  form.append("verdict", verdict);
  const res = await fetch(`${API_BASE}/sighting/${sightingId}/animal`, {
    method: "POST",
    credentials: "include",
    body: form,
  });
  await handle<unknown>(res);
}
```

- [ ] **Step 4: Add the sub-queue to `Moderation.tsx`**

Add a `queue` state (`"reported" | "flagged"`), a two-button toggle above the list reusing the existing `.dex-tabs`-style classes from `Dex.tsx`, and a second render branch. The flagged card reuses `.match-card`, `.mod-head`, `.mod-thumb`, `.mod-meta` and `.match-actions`, showing:

```
{new Date(item.captured_at).toLocaleString()}
{item.observer ? `logged by ${item.observer}` : "observer unknown"}
DOG {item.dog?.toFixed(2) ?? "—"} · CAT {item.cat?.toFixed(2) ?? "—"}
```

with buttons `THERE IS AN ANIMAL` → `ruleOnAnimal(id, "animal")` and `NO ANIMAL` → `ruleOnAnimal(id, "no_animal")`, each dropping the card from local state on success exactly as `decide` does today. Extend the screen's docstring to say why there are two queues and that the flagged one is how the threshold is chosen.

Empty state: `NOTHING FLAGGED — / RUN THE RESCORE FIRST`.

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `cd frontend && npm test -- Moderation && npx tsc --noEmit && npm run build`
Expected: PASS, clean typecheck, successful build.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api.ts frontend/src/screens/Moderation.tsx frontend/src/screens/Moderation.animals.test.tsx
git commit -m "feat(ui): a second moderator queue for detector-flagged photos

Dog and cat shown separately, because the question being answered is 'is
there a dog in this' and the max of the two cannot answer it."
```

---

### Task 9: Telling the contributor

**Files:**
- Modify: `backend/app/routes/dex.py:20-60`
- Modify: `backend/tests/test_dex.py`
- Modify: `frontend/src/api.ts:34-46` (the `Sighting` interface)
- Modify: `frontend/src/screens/Dex.tsx:216-226`
- Modify: `frontend/src/screens/Dex.test.tsx` (or create `Dex.offmap.test.tsx` if no suitable file exists)

**Interfaces:**
- Consumes: `animal_present()` (Task 4).
- Produces: `/dex` items gain `on_map: bool` and `off_map_reason: "reported" | "hidden" | "no_animal" | null`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_dex.py`:

```python
async def test_dex_says_why_a_sighting_is_off_the_shared_map(authed_client, monkeypatch):
    """Yours stays in your dex whatever happens to it -- but a sighting that
    quietly stops appearing on the shared map with no explanation anywhere is
    the bad experience #67 set out to avoid."""
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    client, oid = authed_client
    sid = await _post(client)  # reuse this file's existing helper

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE sightings SET animal_confidence = 0.02 WHERE id = $1", sid)

    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["id"] == str(sid)
    assert item["on_map"] is False
    assert item["off_map_reason"] == "no_animal"


async def test_a_report_outranks_the_detector_in_the_explanation(authed_client):
    """Both can be true at once. A person acting is the more useful thing to
    be told, so it is the reason reported."""
    client, oid = authed_client
    sid = await _post(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE sightings SET review_status='pending', animal_confidence=0.02 "
            "WHERE id=$1", sid)

    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["off_map_reason"] == "reported"


async def test_a_normal_sighting_is_on_the_map(authed_client):
    client, oid = authed_client
    await _post(client)
    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["on_map"] is True
    assert item["off_map_reason"] is None
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd backend && uv run pytest tests/test_dex.py -v`
Expected: FAIL — `KeyError: 'on_map'`.

- [ ] **Step 3: Add the fields**

In `backend/app/routes/dex.py`, add `from app.aggregates import animal_present` and add to the SELECT list (the query already aliases `sightings` as `s`):

```python
            f"{animal_present()} AS animal_ok,"
```

— which means the query string becomes an f-string; double any existing literal braces if present (there are none today).

In the row-assembly block, replace the `"review_status": row["review_status"],` line with:

```python
                # Yours stays in your own dex whatever its status -- but you
                # should be told when one has been taken off the shared map,
                # rather than wondering why nobody can see it. Two independent
                # reasons that can both be true; a person acting is the more
                # useful one to hear, so it wins.
                "review_status": row["review_status"],
                "on_map": _on_map(row),
                "off_map_reason": _off_map_reason(row),
```

and add above the route:

```python
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
```

- [ ] **Step 4: Frontend — type and copy**

In `frontend/src/api.ts`, add to `Sighting`:

```ts
  /** Present on /dex only. Whether this appears on the shared map at all. */
  on_map?: boolean;
  /** Why it does not: a person reported it, a moderator hid it, or the
   * detector found no animal in the frame. */
  off_map_reason?: "reported" | "hidden" | "no_animal" | null;
```

In `frontend/src/screens/Dex.tsx`, replace the `review_status`-driven block at L216-226 with one driven by `off_map_reason`, keeping the existing comment and `.under-review` class:

```tsx
                {s.off_map_reason && (
                  <div className="under-review">
                    {s.off_map_reason === "reported"
                      ? "REPORTED · UNDER REVIEW"
                      : s.off_map_reason === "hidden"
                        ? "HIDDEN BY A MODERATOR"
                        : "NOT ON THE SHARED MAP · NO ANIMAL DETECTED"}
                  </div>
                )}
```

Add a frontend test asserting all three strings render for the three reasons, and that nothing renders when `off_map_reason` is `null`.

- [ ] **Step 5: Run everything**

Run: `cd backend && uv run pytest tests/test_dex.py -v` then `cd ../frontend && npm test -- Dex && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/dex.py backend/tests/test_dex.py frontend/src/api.ts frontend/src/screens/Dex.tsx frontend/src/screens/Dex*.test.tsx
git commit -m "feat(dex): say why a sighting is not on the shared map

A sighting quietly vanishing because a model disagreed is the bad
experience this was supposed to avoid. Two reasons can be true at once;
a person having acted is the more useful thing to be told."
```

---

### Task 10: Delete the YOLOv8n detector

**Files:**
- Modify: `backend/app/detect.py`
- Delete: `backend/app/ml/yolov8n.onnx`
- Modify: `backend/app/ml/NOTICE.md`
- Modify: `backend/scripts/fetch_models.py:46-47`
- Modify: `backend/tests/test_detect.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app/detect.py` exporting only `load_upright`.

`detect.py`'s scorer is dead — `analyse()` and everything downstream use `detect_reid` (YOLO26x). What survives is `load_upright`, which `detect_reid.py`, `embed.py` and `analyse.py` all import.

`DOG_CONF_THRESHOLD = 0.25` is the reason this is in the plan rather than left alone: it is **v8n-calibrated**, and v8n scored a real dog at 0.021. Leaving it in the tree is how it becomes the 26x threshold by inheritance.

- [ ] **Step 1: Check nothing still imports the dead symbols**

Run:

```bash
cd backend && grep -rn "DOG_CONF_THRESHOLD\|detect.dog_confidence\|yolov8n" app scripts tests ../docker ../Dockerfile ../deploy 2>/dev/null
```

Expected: only `app/detect.py`, `app/ml/NOTICE.md`, `scripts/fetch_models.py`, and `tests/test_detect.py`. **If anything else appears — a Dockerfile COPY, a deploy script, a CI step — fix that too before continuing.**

- [ ] **Step 2: Rewrite `detect.py`**

Replace the whole file with:

```python
"""Decoding a photo the way the rest of the pipeline expects it.

This file used to hold a YOLOv8n dog-presence scorer. It is gone: detection
moved to `detect_reid` (YOLO26x) and `analyse.analyse` is the one pass every
caller uses. Its `DOG_CONF_THRESHOLD = 0.25` went with it, deliberately --
that number was calibrated against a model that scored a clearly visible dog
at 0.021, and leaving it in the tree is how it becomes the new detector's
threshold by inheritance. The current one is `settings.animal_confidence_min`,
chosen against rescored numbers (#67).

What is left is the one thing every path shares.
"""

import io

from PIL import Image, ImageOps


def load_upright(image_bytes: bytes) -> Image.Image:
    """Decode to RGB with EXIF orientation applied.

    Phone cameras store portrait shots rotated with an orientation tag;
    feeding those to the detector sideways measurably drops confidence, so
    this must match what `photos.process_photo` does before it hashes and
    stores the same pixels.
    """
    return ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
```

- [ ] **Step 3: Move the letterbox tests**

`tests/test_detect.py` imports `_letterbox` from `app.detect`. Move those assertions to `tests/test_detect_reid.py` against `detect_reid._letterbox` — the one actually in the path — adjusting for its different return type (`(batch, scale, pad_x, pad_y)` rather than a bare array). Keep the `load_upright` tests in `test_detect.py`.

- [ ] **Step 4: Remove the model and its references**

```bash
git rm backend/app/ml/yolov8n.onnx
```

In `backend/scripts/fetch_models.py`, remove the `yolov8n.onnx` entry from the size map at L46-47 (keep `miewid_msv3.onnx` and `yolo26x.onnx`). In `backend/app/ml/NOTICE.md`, remove the YOLOv8n attribution paragraph, keeping YOLO26x's.

- [ ] **Step 5: Run the whole backend suite**

Run: `cd backend && uv run pytest -q`
Expected: PASS, no import errors.

- [ ] **Step 6: Commit**

```bash
git add -A backend/app/detect.py backend/app/ml backend/scripts/fetch_models.py backend/tests/test_detect.py backend/tests/test_detect_reid.py
git commit -m "chore: delete the YOLOv8n detector and its 0.25 threshold

Dead since analyse() -- but DOG_CONF_THRESHOLD is the reason to remove it
rather than leave it. It is calibrated against a model that scored a real
dog at 0.021, and a stale constant sitting in the tree is exactly how it
becomes the new detector's threshold by inheritance."
```

---

## After the plan: what Akash does

Do not do these. Report them, and stop.

1. Deploy, then run `uv run python scripts/rescore_photos.py --dry-run`, then for real (with `--embed`), from `/app/backend` inside the container. Single-digit minutes.
2. Run `--histogram` and put the output **and a recommended threshold** in the PR description.
3. Tell Akash the flagged queue is ready: Dex → FLAGS → NOT ANIMALS, least animal-like first. **This is the checkpoint he asked to be brought in at.**
4. Request review from **@aswin-dot-R** on the threshold recommendation.
5. Only then set `animal_confidence_min`. Dropping `sightings.dog_confidence` is a separate migration after that.

## Interaction with PR #64

#64 edits `_analyse_and_save` and `_save_dog_confidence` in `routes/sighting.py` and adds `sightings.processing_state`. Build on `main`; whoever merges second reconciles. The reconciliation is one edit: the GPU worker's completion handler calls `save_detection` and `recompute_animal_confidence` from `app/scoring.py`, with the `dog_confidence`/`cat_confidence` it already carries in `CompletedFrame`. A note is already on the PR saying so.
