"""One vector per sighting, averaged over its frames.

A clip sampled at 1 Hz gives several looks at the same animal a second apart --
one view sampled repeatedly, not several views. Their mean is that view with the
per-frame noise averaged down.

These use synthetic vectors, so they assert the arithmetic and the storage
contract, never MiewID's judgement.
"""

import numpy as np
import pytest

from app.embed import EMBED_DIM
from app.ids import uuid7
from app.routes.sighting import _save_mean_vector


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


async def _sighting(pool) -> str:
    sid = uuid7()
    async with pool.acquire() as c:
        oid = uuid7()
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')",
            oid)
        await c.execute(
            "INSERT INTO sightings (id, observer_id, captured_at, geo_source) "
            "VALUES ($1,$2,now(),'none')", sid, oid)
    return sid


async def _stored(pool, sid):
    async with pool.acquire() as c:
        raw = await c.fetchval("SELECT vec_miew::text FROM sightings WHERE id=$1", sid)
    if raw is None:
        return None
    return np.array([float(x) for x in raw.strip("[]").split(",")], dtype=np.float32)


@pytest.mark.asyncio
async def test_stores_the_mean_of_the_frames(app_client):
    pool = app_client._transport.app.state.pool
    sid = await _sighting(pool)
    vecs = [_unit(1), _unit(2), _unit(3), _unit(4), _unit(5)]
    await _save_mean_vector(pool, sid, vecs)

    expected = np.mean(np.stack(vecs), axis=0)
    expected = expected / np.linalg.norm(expected)
    got = await _stored(pool, sid)
    assert got is not None
    assert float(np.dot(got, expected)) > 0.9999


@pytest.mark.asyncio
async def test_the_mean_is_renormalised(app_client):
    """The mean of unit vectors is not a unit vector -- it is shorter the more
    the frames disagree. Everything downstream reads these as unit length, so a
    plain dot product would otherwise be silently scaled."""
    pool = app_client._transport.app.state.pool
    sid = await _sighting(pool)
    # Deliberately disagreeing frames: their raw mean is far from unit length.
    await _save_mean_vector(pool, sid, [_unit(10), _unit(11), _unit(12)])
    got = await _stored(pool, sid)
    assert abs(float(np.linalg.norm(got)) - 1.0) < 1e-3


@pytest.mark.asyncio
async def test_a_single_frame_stores_itself(app_client):
    """A still, or a clip that yielded one usable frame. The mean of one vector
    is that vector, and it must survive the round trip unchanged."""
    pool = app_client._transport.app.state.pool
    sid = await _sighting(pool)
    v = _unit(7)
    await _save_mean_vector(pool, sid, [v])
    assert float(np.dot(await _stored(pool, sid), v)) > 0.9999


@pytest.mark.asyncio
async def test_averaging_denoises_toward_the_true_view(app_client):
    """Why average at all. Five noisy looks at one view should land closer to
    that view than a single look does -- this is the whole claim, so it is
    measured rather than assumed."""
    pool = app_client._transport.app.state.pool
    truth = _unit(21)
    rng = np.random.default_rng(99)
    frames = []
    for _ in range(5):
        noisy = truth + rng.standard_normal(EMBED_DIM).astype(np.float32) * 0.35
        frames.append(noisy / np.linalg.norm(noisy))

    sid = await _sighting(pool)
    await _save_mean_vector(pool, sid, frames)
    mean = await _stored(pool, sid)

    single = float(np.dot(frames[0], truth))
    averaged = float(np.dot(mean, truth))
    assert averaged > single, f"mean {averaged:.3f} should beat one frame {single:.3f}"


@pytest.mark.asyncio
async def test_no_frames_leaves_it_null(app_client):
    """Every frame failed detection, so there is nothing to average. NULL means
    "not matchable yet", which is distinct from a zero vector -- whose cosine
    against anything is undefined."""
    pool = app_client._transport.app.state.pool
    sid = await _sighting(pool)
    await _save_mean_vector(pool, sid, [])
    assert await _stored(pool, sid) is None


@pytest.mark.asyncio
async def test_exactly_cancelling_frames_store_nothing(app_client):
    """A zero-norm mean cannot be normalised. Storing it would put a vector in
    the index that no cosine comparison can rank."""
    pool = app_client._transport.app.state.pool
    sid = await _sighting(pool)
    v = _unit(31)
    await _save_mean_vector(pool, sid, [v, -v])
    assert await _stored(pool, sid) is None


# --- thin evidence, on the mean path ------------------------------------
#
# `suggest_video` asks the contributor for a clip when a proposal rests on too
# few frames. On the mean path the query is a single averaged vector, so
# counting the query vectors would make every clip look thin -- and the app
# would ask someone who had just filmed five seconds of a dog to film a clip.

@pytest.mark.asyncio
async def test_a_clip_is_not_thin_evidence(app_client):
    """The whole point of a clip is that it carries more evidence than a still.
    Judging it by the number of query vectors inverts that, because averaging
    reduces five frames to one."""
    from app.embed import MODEL_NAME
    from app.matching import resolve_sighting

    pool = app_client._transport.app.state.pool
    async with pool.acquire() as conn:
        oid = uuid7()
        await conn.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','t')", oid)
        # Two sightings that will match each other, each backed by five frames.
        ids = []
        base = _unit(41)
        for n in range(2):
            sid = uuid7()
            await conn.execute(
                "INSERT INTO sightings (id, observer_id, captured_at, geo_source) "
                "VALUES ($1,$2,now(),'none')", sid, oid)
            vecs = []
            for f in range(5):
                pid = uuid7()
                await conn.execute(
                    "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,$3)",
                    pid, sid, f"k{n}-{f}.webp")
                v = base + _unit(100 + n * 10 + f) * 0.2
                v = v / np.linalg.norm(v)
                vecs.append(v)
                await conn.execute(
                    f"INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
                    f"VALUES ($1,$2,$3,$4,$5::vector({EMBED_DIM}))",
                    uuid7(), pid, MODEL_NAME, EMBED_DIM,
                    "[" + ",".join(f"{float(x):.7g}" for x in v) + "]")
            await _save_mean_vector(pool, sid, vecs)
            ids.append(sid)

        out = await resolve_sighting(conn, ids[1], auto_merge_min=1.01,
                                     propose_min=0.4, radius_m=1000,
                                     max_candidates=5, new_uuid=uuid7,
                                     thin_evidence_frames=4)

    assert out.status == "proposed", f"expected a proposal, got {out.status}"
    assert out.suggest_video is False, (
        "five embedded frames is not thin evidence -- asking for a clip here "
        "asks the contributor to redo what they just did"
    )
