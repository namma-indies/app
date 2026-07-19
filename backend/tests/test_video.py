import io
import tempfile

import imageio.v2 as imageio
import numpy as np
import pytest


def _make_video(frames: list[np.ndarray], fps: int = 6) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        with imageio.get_writer(tmp.name, fps=fps, format="ffmpeg", macro_block_size=1) as writer:
            for frame in frames:
                writer.append_data(frame)
        tmp.seek(0)
        return tmp.read()


def _varied_frames(n: int = 12, size: tuple[int, int] = (64, 64)) -> list[np.ndarray]:
    """Frames whose content changes noticeably from one to the next (moving
    block on a gradient background) so phash dedup keeps several but not all."""
    h, w = size
    frames = []
    for i in range(n):
        base = np.zeros((h, w, 3), dtype=np.uint8)
        # gradient background that shifts each frame
        shift = (i * 20) % 256
        base[:, :, 0] = (np.arange(w) + shift) % 256
        base[:, :, 1] = (np.arange(h)[:, None] + shift) % 256
        # a moving colored block
        bx = (i * (w // n)) % (w - 8)
        by = (i * (h // n)) % (h - 8)
        base[by : by + 8, bx : bx + 8] = [255, 0, 0]
        frames.append(base)
    return frames


def _frames_with_duplicates(n_unique: int = 3, repeats: int = 4, size=(64, 64)) -> list[np.ndarray]:
    """Several visually distinct frames, each repeated multiple times in a
    row, so dedup must collapse the repeats."""
    h, w = size
    uniques = []
    for i in range(n_unique):
        base = np.zeros((h, w, 3), dtype=np.uint8)
        base[:, :] = [(i * 90) % 256, (i * 50) % 256, (i * 130) % 256]
        bx = (i * (w // n_unique)) % (w - 8)
        base[10:20, bx : bx + 8] = [255, 255, 255]
        uniques.append(base)
    frames = []
    for u in uniques:
        frames.extend([u] * repeats)
    return frames


@pytest.mark.asyncio
async def test_video_sighting_creates_multiple_frames(authed_client):
    client, _ = authed_client
    video_bytes = _make_video(_varied_frames(12))
    r = await client.post(
        "/sighting",
        files={"video": ("clip.mp4", video_bytes, "video/mp4")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        n = await c.fetchval("SELECT count(*) FROM photos WHERE sighting_id=$1", sid)
    assert 1 < n <= 12


@pytest.mark.asyncio
async def test_video_frames_are_diverse_not_all(authed_client):
    client, _ = authed_client
    # 3 unique looks, each repeated 4x -> 12 raw frames but should collapse
    # to close to 3 kept frames (near-duplicates removed).
    video_bytes = _make_video(_frames_with_duplicates(n_unique=3, repeats=4))
    r = await client.post(
        "/sighting",
        files={"video": ("clip.mp4", video_bytes, "video/mp4")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        n = await c.fetchval("SELECT count(*) FROM photos WHERE sighting_id=$1", sid)
    # 12 raw sampled frames collapsed down substantially due to dedup
    assert n < 12


@pytest.mark.asyncio
async def test_video_not_stored_in_s3(authed_client):
    client, _ = authed_client
    video_bytes = _make_video(_varied_frames(12))
    r = await client.post(
        "/sighting",
        files={"video": ("clip.mp4", video_bytes, "video/mp4")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["sighting_id"]

    from app.deps import get_storage

    storage = get_storage()
    keys = await storage.list_keys(f"sightings/{sid}/")
    assert keys, "expected stored objects for the sighting"
    assert all(k.endswith(".jpg") for k in keys)
