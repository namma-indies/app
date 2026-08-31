"""A human verdict must survive re-resolution.

`POST /proposal` is the only path that sets match_status='confirmed', because
auto_merge_min is deliberately unreachable. So a confirmed sighting represents
the scarcest thing the system holds: a person looking at two photographs and
deciding. Re-running the model must never overwrite that.

This was not hypothetical. `backfill_embeddings.py --resolve` re-resolves
existing sightings, and it is exactly what you run after a model change or a
threshold change -- both of which move scores, which is the case that erased
the verdict.
"""

import numpy as np
import pytest

from app.embed import EMBED_DIM, MODEL_NAME
from app.ids import uuid7
from app.matching import resolve_sighting


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _lit(v) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in v) + "]"


async def _observer(conn):
    oid = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','t')", oid)
    return oid


async def _sighting(conn, oid, *, individual_id=None, status="unmatched", seed=1,
                    vec=None):
    sid = uuid7()
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geo_source, "
        "individual_id, match_status) VALUES ($1,$2,now(),'none',$3,$4)",
        sid, oid, individual_id, status)
    pid = uuid7()
    await conn.execute(
        "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,'k.webp')", pid, sid)
    await conn.execute(
        f"INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
        f"VALUES ($1,$2,$3,$4,$5::vector({EMBED_DIM}))",
        uuid7(), pid, MODEL_NAME, EMBED_DIM,
        _lit(_unit(seed) if vec is None else vec))
    return sid


async def _resolve(conn, sid, propose_min=0.5):
    return await resolve_sighting(conn, sid, auto_merge_min=1.01,
                                  propose_min=propose_min, radius_m=1000,
                                  max_candidates=5, new_uuid=uuid7)


@pytest.mark.asyncio
async def test_a_confirmed_sighting_keeps_its_dog(migrated_db):
    """The bug: with nothing above the bar, resolution set individual_id=NULL
    and match_status='unmatched', erasing a human's decision."""
    conn = migrated_db
    oid = await _observer(conn)
    iid = uuid7()
    await conn.execute(
        "INSERT INTO individuals (id, created_by, status) VALUES ($1,'feeder','active')", iid)
    sid = await _sighting(conn, oid, individual_id=iid, status="confirmed")

    await _resolve(conn, sid)

    row = await conn.fetchrow(
        "SELECT individual_id, match_status FROM sightings WHERE id=$1", sid)
    assert row["individual_id"] == iid, "the human's verdict was erased"
    assert row["match_status"] == "confirmed"


@pytest.mark.asyncio
async def test_it_survives_a_threshold_that_would_find_nothing(migrated_db):
    """The realistic trigger. Raising the bar is a config change someone makes
    deliberately; it must not quietly unlink every confirmed dog on the next
    backfill."""
    conn = migrated_db
    oid = await _observer(conn)
    iid = uuid7()
    await conn.execute(
        "INSERT INTO individuals (id, created_by, status) VALUES ($1,'feeder','active')", iid)
    sid = await _sighting(conn, oid, individual_id=iid, status="confirmed")

    await _resolve(conn, sid, propose_min=0.99)

    assert (await conn.fetchval(
        "SELECT individual_id FROM sightings WHERE id=$1", sid)) == iid


@pytest.mark.asyncio
async def test_re_resolving_is_still_idempotent_for_unmatched(migrated_db):
    """The guard must not freeze everything else. An unmatched sighting is
    still re-decidable, which is the whole point of a re-run."""
    conn = migrated_db
    oid = await _observer(conn)
    sid = await _sighting(conn, oid)

    out = await _resolve(conn, sid)
    assert out.status == "unmatched"
    row = await conn.fetchrow("SELECT match_status FROM sightings WHERE id=$1", sid)
    assert row["match_status"] == "unmatched"


@pytest.mark.asyncio
async def test_a_proposed_sighting_can_still_change_its_mind(migrated_db):
    """Only 'confirmed' is settled. A pending proposal is the model's opinion,
    and re-running is how it gets updated -- freezing that too would make a
    re-embed pointless."""
    conn = migrated_db
    oid = await _observer(conn)
    base = _unit(7)
    # A deliberately middling similarity (~0.6), so one threshold sits below it
    # and another above. Identical vectors score 1.0 and clear almost any bar,
    # which is what made the first version of this test assert the wrong thing.
    blended = base * 0.6 + _unit(8) * 0.8
    blended /= np.linalg.norm(blended)
    await _sighting(conn, oid, vec=base)
    b = await _sighting(conn, oid, vec=blended)

    out = await _resolve(conn, b, propose_min=0.4)
    assert out.status == "proposed", f"expected a proposal, got {out.status}"

    # Raise the bar above that pair's score: the proposal must clear.
    out = await _resolve(conn, b, propose_min=0.9)
    assert out.status == "unmatched", f"expected it to clear, got {out.status}"
    n = await conn.fetchval(
        "SELECT count(*) FROM match_proposals WHERE sighting_id=$1 AND status='pending'", b)
    assert n == 0
