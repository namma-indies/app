"""A photo with no animal in it must not be proposed as the same animal as
something else.

The box threshold (REID_CONF_THRESHOLD = 0.10) is independent of the map
threshold, so a sighting can be too low for the map and still have an
embedding. `match.py`'s own comment says a `same` verdict folds content into
an identity that no later decision can unpick -- so this is the one place
where getting it wrong is not reversible.
"""

import numpy as np
import pytest

from app.embed import EMBED_DIM, MODEL_NAME
from app.ids import uuid7

pytestmark = pytest.mark.asyncio

LAT, LNG = 12.97, 77.59


def _vec(seed: float) -> np.ndarray:
    """A deterministic, real-shaped (but not L2-normalised) vector. Every
    sighting below shares the same direction, so cosine similarity between any
    two of them is ~1.0 -- what matters here is which candidates survive the
    animal filter, not ranking."""
    return np.full(EMBED_DIM, seed, dtype=np.float32)


def _lit(vec: np.ndarray) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"


async def _observer(conn):
    oid = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')", oid)
    return oid


async def _embedded_sighting(conn, oid, *, conf, vec_seed=0.1):
    """A sighting with one photo and a real-shaped vector, scored `conf`."""
    sid, pid = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geog, geo_source, "
        "                       animal_confidence) "
        "VALUES ($1,$2,now(), ST_SetSRID(ST_MakePoint(77.59,12.97),4326)::geography, "
        "        'device_gps', $3)",
        sid, oid, conf)
    await conn.execute(
        "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,'k')", pid, sid)
    await conn.execute(
        "INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
        "VALUES ($1,$2,$3,$4,$5::vector)",
        uuid7(), pid, MODEL_NAME, EMBED_DIM, _lit(_vec(vec_seed)))
    return sid


async def test_a_low_scoring_sighting_is_not_a_candidate(migrated_db, monkeypatch):
    from app.config import settings
    from app.matching import find_candidates

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    oid = await _observer(migrated_db)
    query = await _embedded_sighting(migrated_db, oid, conf=0.95)
    furniture = await _embedded_sighting(migrated_db, oid, conf=0.05)
    real = await _embedded_sighting(migrated_db, oid, conf=0.88)

    hits = await find_candidates(
        migrated_db, _vec(0.1), lat=LAT, lng=LNG, radius_m=2000.0,
        exclude_sighting_id=query, limit=10,
    )
    ids = {h.sighting_id for h in hits}
    assert furniture not in ids
    assert real in ids


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

    hits = await find_candidates(
        migrated_db, _vec(0.1), lat=LAT, lng=LNG, radius_m=2000.0,
        exclude_sighting_id=query, limit=10,
    )
    ids = {h.sighting_id for h in hits}
    assert rescued in ids
