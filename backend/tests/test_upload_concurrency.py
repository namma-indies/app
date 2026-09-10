"""Concurrent clip uploads must not exhaust the connection pool.

THE BUG THIS PINS
-----------------
`create_sighting` takes a connection for the whole request through
`conn=Depends(get_conn)`. The clip branch then used to take a SECOND one from
the same pool to write `clip_s3_key`:

    async with request.app.state.pool.acquire() as conn:      # a second one
        await conn.execute("UPDATE sightings SET clip_s3_key = ...")

So every in-flight clip upload needed two of the pool's connections at once.
Once as many clips were in flight as the pool had connections, all of them held
one and all of them waited for another that could never be released.
`asyncpg.Pool.acquire()` has no default timeout, so they waited forever.

Measured on a 20-core dev box against db_pool_max=30, before the fix:

    15 concurrent clips   19.4 s, all 15 succeeded
    30 concurrent clips   ZERO succeeded, API permanently wedged
    then 40 photo uploads ZERO succeeded -- the pool was gone

Postgres showed 30 connections, every one idle and checked out. `/health` kept
returning 200 throughout, because it touches no database, which is exactly how
a wedged box passes its own health check.

WHY THE TEST IS SHAPED LIKE THIS
--------------------------------
Reproducing it needs concurrency ABOVE the pool size, not above some absolute
number. The pool here is small, so a handful of concurrent clips is enough --
the same collision as thirty against thirty, at a size a test suite can afford.
"""

import asyncio
import tempfile

import imageio.v2 as imageio
import numpy as np
import pytest


def _clip(n_frames: int = 6, fps: int = 6) -> bytes:
    """A tiny mp4 with genuinely varying frames."""
    frames = []
    for i in range(n_frames):
        f = np.zeros((64, 64, 3), dtype=np.uint8)
        f[:, :, 0] = (np.arange(64) + i * 30) % 256
        f[:, :, 1] = (np.arange(64)[:, None] + i * 20) % 256
        f[i % 56 : i % 56 + 8, i % 56 : i % 56 + 8] = 255
        frames.append(f)
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        with imageio.get_writer(tmp.name, fps=fps, format="ffmpeg", macro_block_size=1) as w:
            for f in frames:
                w.append_data(f)
        tmp.seek(0)
        return tmp.read()


async def _pool_size(client) -> int:
    return client._transport.app.state.pool.get_max_size()


@pytest.mark.asyncio
async def test_concurrent_clip_uploads_do_not_exhaust_the_pool(authed_client):
    """More simultaneous clips than the pool has connections.

    With the double-acquire this deadlocks and the whole API stops answering
    anything that needs a database -- not just uploads. The timeout is what
    makes the failure a test result instead of a hung suite.
    """
    client, _ = authed_client
    pool_max = await _pool_size(client)
    # Comfortably past the point where two-connections-per-request collides.
    n = pool_max + 2
    clip = _clip()

    async def upload(i: int):
        return await client.post(
            "/sighting",
            files={"video": (f"c{i}.mp4", clip, "video/mp4")},
            data={
                "geo_source": "none",
                "captured_at": "2026-09-10T10:00:00Z",
                "client_token": f"conc-{i}",
            },
        )

    results = await asyncio.wait_for(
        asyncio.gather(*(upload(i) for i in range(n)), return_exceptions=True),
        timeout=180,
    )

    codes = [r.status_code if hasattr(r, "status_code") else repr(r) for r in results]
    assert all(c == 201 for c in codes), f"{codes.count(201)}/{n} succeeded: {codes}"


@pytest.mark.asyncio
async def test_the_api_still_answers_after_a_burst(authed_client):
    """The part that made this severe rather than slow.

    A deadlocked pool never recovers: connections are held by requests that
    will never finish, so every later request -- photos, the dex, anything
    touching the database -- hangs too. A burst must leave the API usable.
    """
    client, _ = authed_client
    clip = _clip()
    pool_max = await _pool_size(client)

    async def upload(i: int):
        return await client.post(
            "/sighting",
            files={"video": (f"b{i}.mp4", clip, "video/mp4")},
            data={
                "geo_source": "none",
                "captured_at": "2026-09-10T10:00:00Z",
                "client_token": f"burst-{i}",
            },
        )

    await asyncio.wait_for(
        asyncio.gather(*(upload(i) for i in range(pool_max + 2)), return_exceptions=True),
        timeout=180,
    )

    # A plain read, which needs one connection. Under the bug this hung forever.
    r = await asyncio.wait_for(client.get("/dex"), timeout=30)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_a_clip_upload_holds_only_one_connection(authed_client):
    """The mechanism, asserted directly rather than inferred from a burst.

    Watches how far the pool is drawn down while exactly one clip is in
    flight. Two would mean the second acquire is back, and the burst tests
    above would then only fail at a size that depends on the pool setting.
    """
    client, _ = authed_client
    pool = client._transport.app.state.pool
    clip = _clip()

    free_before = pool.get_idle_size()
    peak_in_use = 0
    done = False

    async def watch():
        nonlocal peak_in_use
        while not done:
            in_use = free_before - pool.get_idle_size()
            peak_in_use = max(peak_in_use, in_use)
            await asyncio.sleep(0.01)

    watcher = asyncio.create_task(watch())
    r = await client.post(
        "/sighting",
        files={"video": ("one.mp4", clip, "video/mp4")},
        data={
            "geo_source": "none",
            "captured_at": "2026-09-10T10:00:00Z",
            "client_token": "single",
        },
    )
    done = True
    await watcher

    assert r.status_code == 201
    assert peak_in_use <= 1, (
        f"one clip upload drew {peak_in_use} connections at once; the request "
        "path must never hold two, or concurrency above the pool size deadlocks"
    )
