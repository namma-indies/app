"""Candidate search, exercised against a real Postgres with pgvector + PostGIS.

`find_candidates` builds its SQL conditionally, which is where its bugs live:
a geo-less query silently dropped two placeholders and Postgres could not infer
their types, so every GPS-denied sighting raised IndeterminateDatatypeError --
the exact case the function documents as supported. These tests pin the shapes
the SQL has to keep producing.

No model is involved: vectors are synthetic, so the tests run everywhere and
assert on retrieval behaviour rather than on MiewID's judgement.
"""

import numpy as np
import pytest

from app.embed import EMBED_DIM, MODEL_NAME
from app.ids import uuid7
from app.matching import find_candidates


def _unit(seed: int) -> np.ndarray:
    """A deterministic L2-normalised vector, as embed.py would produce."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


async def _observer(conn):
    oid = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, trust_tier) VALUES ($1,'t','feeder')",
        oid,
    )
    return oid


async def _sighting(conn, observer_id, vec, *, lat=None, lng=None):
    """A sighting with one photo and one embedding, optionally located."""
    sid, pid, eid = uuid7(), uuid7(), uuid7()
    if lat is None:
        await conn.execute(
            "INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2,now())",
            sid, observer_id,
        )
    else:
        await conn.execute(
            "INSERT INTO sightings (id, observer_id, captured_at, geog) "
            "VALUES ($1,$2,now(), ST_SetSRID(ST_MakePoint($3,$4),4326)::geography)",
            sid, observer_id, lng, lat,
        )
    await conn.execute(
        "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,$3)",
        pid, sid, f"k/{pid}.webp",
    )
    await conn.execute(
        "INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
        "VALUES ($1,$2,$3,$4,$5::vector)",
        eid, pid, MODEL_NAME, EMBED_DIM,
        "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]",
    )
    return sid


@pytest.mark.asyncio
async def test_finds_candidates_without_any_location(migrated_db):
    """A sighting with GPS denied must still match, against a wider pool.

    This raised IndeterminateDatatypeError before the placeholders were
    numbered as they are appended: the geo predicates disappeared from the SQL
    while their parameters stayed in the list, leaving them untypeable.
    """
    obs = await _observer(migrated_db)
    target = _unit(1)
    await _sighting(migrated_db, obs, target, lat=12.97, lng=77.59)
    await _sighting(migrated_db, obs, _unit(2), lat=28.61, lng=77.20)

    got = await find_candidates(
        migrated_db, target, lat=None, lng=None, radius_m=2000.0, limit=5
    )

    assert got, "a sighting with no GPS must still get candidates"
    assert got[0].similarity == pytest.approx(1.0, abs=1e-3)
    assert got[0].distance_m is None, "no query location means no distance"


@pytest.mark.asyncio
async def test_radius_excludes_the_far_away_twin(migrated_db):
    """Vicinity is a filter, not a tiebreak: an identical vector 1,900 km away
    is not a candidate at all."""
    obs = await _observer(migrated_db)
    target = _unit(3)
    near = await _sighting(migrated_db, obs, target, lat=12.9720, lng=77.5950)
    await _sighting(migrated_db, obs, target, lat=28.6139, lng=77.2090)  # Delhi

    got = await find_candidates(
        migrated_db, target, lat=12.9716, lng=77.5946, radius_m=2000.0, limit=5
    )

    assert [c.sighting_id for c in got] == [near]
    assert got[0].distance_m is not None and got[0].distance_m < 2000


@pytest.mark.asyncio
async def test_scores_a_candidate_by_its_best_matching_frame(migrated_db):
    """A clip's frames are one animal from several angles, so a candidate is
    scored on the frame it matches best -- not on whichever frame happened to
    be stored first, which is what made video worth nothing."""
    obs = await _observer(migrated_db)
    first, second = _unit(4), _unit(5)
    match_for_second = await _sighting(migrated_db, obs, second)
    await _sighting(migrated_db, obs, _unit(6))

    # Querying with only the first frame should not surface it well...
    one = await find_candidates(
        migrated_db, first, lat=None, lng=None, radius_m=2000.0, limit=5
    )
    weak = next(c.similarity for c in one if c.sighting_id == match_for_second)

    # ...but querying with both frames scores it on the one that fits.
    both = await find_candidates(
        migrated_db, [first, second], lat=None, lng=None, radius_m=2000.0, limit=5
    )
    assert both[0].sighting_id == match_for_second
    assert both[0].similarity == pytest.approx(1.0, abs=1e-3)
    assert both[0].similarity > weak


@pytest.mark.asyncio
async def test_never_offers_the_sighting_itself(migrated_db):
    obs = await _observer(migrated_db)
    target = _unit(7)
    me = await _sighting(migrated_db, obs, target)
    other = await _sighting(migrated_db, obs, target)

    got = await find_candidates(
        migrated_db, target, lat=None, lng=None, radius_m=2000.0,
        exclude_sighting_id=me, limit=5,
    )
    assert [c.sighting_id for c in got] == [other]


@pytest.mark.asyncio
async def test_one_row_per_candidate_sighting(migrated_db):
    """A sighting with several frames must appear once, not once per frame,
    or a single clip floods the review queue."""
    obs = await _observer(migrated_db)
    target = _unit(8)
    sid = await _sighting(migrated_db, obs, target)
    for extra in (9, 10, 11):
        pid, eid = uuid7(), uuid7()
        await migrated_db.execute(
            "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,$3)",
            pid, sid, f"k/{pid}.webp",
        )
        v = _unit(extra)
        await migrated_db.execute(
            "INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
            "VALUES ($1,$2,$3,$4,$5::vector)",
            eid, pid, MODEL_NAME, EMBED_DIM,
            "[" + ",".join(f"{float(x):.7g}" for x in v) + "]",
        )

    got = await find_candidates(
        migrated_db, target, lat=None, lng=None, radius_m=2000.0, limit=10
    )
    assert [c.sighting_id for c in got].count(sid) == 1


@pytest.mark.asyncio
async def test_empty_query_returns_nothing(migrated_db):
    """A sighting whose every frame failed to embed has nothing to search with,
    and must not be treated as matching everything."""
    obs = await _observer(migrated_db)
    await _sighting(migrated_db, obs, _unit(12))
    assert await find_candidates(
        migrated_db, [], lat=None, lng=None, radius_m=2000.0, limit=5
    ) == []
