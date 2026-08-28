import httpx
import pytest

from app.storage.s3 import storage_from_settings


@pytest.fixture
def storage():
    return storage_from_settings()


@pytest.mark.asyncio
async def test_put_then_presigned_url_roundtrips_bytes():
    s = storage_from_settings()
    await s.ensure_bucket()
    key = "test/hello.txt"
    payload = b"woof-woof-123"
    await s.put(key, payload, "text/plain")
    url = await s.url(key, expires_s=120)
    async with httpx.AsyncClient() as c:
        r = await c.get(url)
    assert r.status_code == 200
    assert r.content == payload


# --- batched presigning ------------------------------------------------------
# `url()` opens a fresh aioboto3 client per call. Presigning itself is a local
# HMAC (no network), but client construction is not free: measured 2.86 ms per
# presign that way against 0.20 ms sharing one client across the batch. /map
# presigns one URL per sighting and /dex two per photo, so at /map's default
# limit of 2000 that is ~5.7 s of CPU per request on the box serving live
# traffic, versus ~0.4 s.


@pytest.mark.asyncio
async def test_urls_returns_one_presigned_url_per_key(storage):
    keys = [f"sightings/a/{i}_thumb.webp" for i in range(5)]
    urls = await storage.urls(keys)
    assert len(urls) == len(keys)
    for key, url in zip(keys, urls):
        assert url.startswith("http")
        assert key.split("/")[-1] in url


@pytest.mark.asyncio
async def test_urls_opens_exactly_one_client_for_the_whole_batch(storage):
    """The point of the method. Without this, the loop is the cost."""
    opened = 0
    real_client = storage._client

    def counting_client(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_client(*args, **kwargs)

    storage._client = counting_client
    try:
        await storage.urls([f"sightings/a/{i}.webp" for i in range(25)])
    finally:
        storage._client = real_client
    assert opened == 1, f"opened {opened} clients for one batch"


@pytest.mark.asyncio
async def test_urls_of_nothing_makes_no_client_at_all(storage):
    opened = 0
    real_client = storage._client

    def counting_client(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_client(*args, **kwargs)

    storage._client = counting_client
    try:
        assert await storage.urls([]) == []
    finally:
        storage._client = real_client
    assert opened == 0


@pytest.mark.asyncio
async def test_urls_matches_url_for_the_same_key(storage):
    """Batching must not change what a client receives -- same public host, so
    the SigV4 host signature still matches what the browser requests."""
    key = "sightings/a/b_thumb.webp"
    single = await storage.url(key)
    batched = (await storage.urls([key]))[0]
    assert single.split("?")[0] == batched.split("?")[0]
