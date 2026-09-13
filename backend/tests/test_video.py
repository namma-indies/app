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
async def test_every_sampled_frame_is_kept_for_the_mean(authed_client):
    """Dedup used to collapse near-duplicates, and a test pinned that it ran.

    Reversed on purpose. Frames are now averaged into one vector per sighting,
    and near-duplicates are precisely what cancels the per-frame noise -- with
    dedup at hamming 8, a five-second clip of a sitting dog yielded exactly ONE
    frame (measured), so the "average of five" was averaging one vector.

    `phash_hamming_min` survives as a parameter for any future path that wants
    distinct views rather than a mean; it just no longer defaults to filtering.
    """
    client, _ = authed_client
    # 3 distinct looks, each repeated 7x, at a declared 2 fps so the 1 Hz
    # stride samples about half of them.
    video_bytes = _make_video(_frames_with_duplicates(n_unique=3, repeats=7), fps=2)
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
    # Well above the 3 distinct visuals the old dedup collapsed to: repeats are
    # now kept because the mean wants them.
    assert n > 4, f"expected repeats to be kept for averaging, got {n}"


@pytest.mark.asyncio
async def test_a_long_clip_is_still_capped(authed_client):
    """Dropping the dedup filter leaves `keep` as the only bound on how many
    frames one clip can write. It has to hold, or a long recording turns into
    an unbounded number of stored photos."""
    from app.video import extract_diverse_frames

    # 40 distinct looks at 1 fps -> 40 sampled, well past both caps.
    video_bytes = _make_video(_varied_frames(40), fps=1)
    frames = extract_diverse_frames(video_bytes)
    assert len(frames) <= 12, f"expected the keep cap to hold, got {len(frames)}"

@pytest.mark.asyncio
async def test_video_bad_upload_returns_422(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"video": ("clip.mp4", b"not a video", "video/mp4")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_the_clip_is_stored_beside_its_frames(authed_client):
    """The clip used to be discarded after frame extraction, and a test pinned
    that. Reversed deliberately: keeping the footage is the only way to re-run a
    better detector or a newer embedding model over it later. Once the frames
    were chosen and the clip dropped, every frame not chosen was gone for good.

    Contributor-facing copy changed with it -- the CLIP READY panel no longer
    claims the clip is never stored.
    """
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
    # Frames still go through process_photo, so they land as WebP.
    assert any(k.endswith(".webp") for k in keys)
    assert f"sightings/{sid}/clip.mp4" in keys, "the clip itself is now kept"

    # ...and the row knows where it is, or the object is unreachable.
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        stored = await c.fetchval(
            "SELECT clip_s3_key FROM sightings WHERE id = $1::uuid", sid)
    assert stored == f"sightings/{sid}/clip.mp4"


@pytest.mark.asyncio
async def test_a_photo_sighting_stores_no_clip(authed_client):
    """Only the video path writes one; a still must not leave a null-keyed
    object or a dangling key behind."""
    import io as _io

    from PIL import Image as _Image

    b = _io.BytesIO()
    _Image.new("RGB", (320, 240), (90, 130, 110)).save(b, "JPEG")
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", b.getvalue(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        assert await c.fetchval(
            "SELECT clip_s3_key FROM sightings WHERE id = $1::uuid",
            r.json()["sighting_id"]) is None
