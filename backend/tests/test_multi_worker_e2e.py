"""Real capture routes and job transactions with synthetic inference and object storage."""
import io
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
import numpy as np
import pytest
from PIL import Image

from app.config import settings
from app.deps import get_storage
from gpu_worker.client import Config, Worker
from gpu_worker.inference import CUDA, GpuInference, Identity


class MemoryObjects:
    def __init__(self):
        self.objects = {}

    async def ensure_bucket(self):
        pass

    async def put(self, key, data, content_type):
        self.objects[key] = (data, content_type)

    async def get(self, key):
        return self.objects[key][0]

    async def url(self, key, expires_s=3600):
        return "https://objects.example.test/" + key

    async def urls(self, keys, expires_s=3600):
        return [await self.url(key, expires_s) for key in keys]

    async def put_url(self, key, size, expires_s):
        return await self.url(key)

    async def publish_checked(self, source, target, size):
        data, mime = self.objects[source]
        assert len(data) == size and mime == "image/webp"
        self.objects[target] = (data, mime)

    async def request(self, request):
        key = unquote(urlsplit(str(request.url)).path.lstrip("/"))
        if request.method == "PUT":
            await self.put(key, await request.aread(), request.headers["content-type"])
            return httpx.Response(200)
        if key not in self.objects:
            return httpx.Response(404)
        data, mime = self.objects[key]
        return httpx.Response(200, content=data, headers={"content-type": mime})


class Session:
    def __init__(self, detector=False):
        self.detector = detector
        self.calls = 0

    def disable_fallback(self):
        pass

    def get_providers(self):
        return [CUDA]

    def get_inputs(self):
        return [SimpleNamespace(name="pixels")]

    def run(self, _, feeds):
        self.calls += 1
        if self.detector:
            return [np.array([[[40, 100, 240, 500, .85, 16],
                              [400, 200, 500, 400, .9, 16]]], dtype=np.float32)]
        vector = np.zeros((1, 2152), dtype=np.float32)
        vector[0, (self.calls - 1) % 2] = 1
        return [vector]


class LoseFirstCompletion(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.transport = httpx.ASGITransport(app=app)
        self.completions = []

    async def handle_async_request(self, request):
        if request.url.path.endswith("/complete"):
            self.completions.append(await request.aread())
            response = await self.transport.handle_async_request(request)
            if len(self.completions) == 1 and response.status_code == 200:
                await response.aclose()
                raise httpx.ReadError("synthetic lost completion acknowledgement", request=request)
            return response
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        await self.transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("media_kind, sample_count", [("photo", 1), ("photo", 2), ("video", 4)])
async def test_multi_worker_lost_response_then_owner_publication(
        authed_client, monkeypatch, tmp_path, media_kind, sample_count):
    client, owner = authed_client
    app = client._transport.app
    objects = MemoryObjects()
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    monkeypatch.setattr(settings, "multi_animal_enabled", True)
    monkeypatch.setattr(settings, "media_gpu_token", "synthetic-worker-token")
    monkeypatch.setattr(settings, "reid_propose_min", 1.01)
    app.dependency_overrides[get_storage] = lambda: objects
    try:
        image = io.BytesIO()
        Image.new("RGB", (640, 640), "white").save(image, "JPEG")
        data = {"client_token": str(uuid4()), "geo_source": "pin", "lat": "12.97", "lng": "77.59",
                "captured_at": "2026-09-01T09:00:00Z", "note": "two animals together"}
        if media_kind == "video":
            import imageio.v2 as imageio
            from gpu_worker.inference import Limits
            path = tmp_path / "short.mp4"
            writer = imageio.get_writer(str(path), fps=2, codec="libx264", macro_block_size=1)
            try:
                for index in range(sample_count):
                    writer.append_data(np.full((640, 640, 3), 80 + index * 10, dtype=np.uint8))
            finally:
                writer.close()
            files = [("video", ("short.mp4", path.read_bytes(), "video/mp4"))]
        else:
            files = [("photos", (f"photo-{i}.jpg", image.getvalue(), "image/jpeg"))
                     for i in range(sample_count)]
        response = await client.post("/capture", data=data, files=files)
        assert response.status_code == 201, response.text
        receipt = response.json()
        capture_id = receipt["capture_id"]
        assert not receipt["sighting_ids"]
        assert (await client.get("/map")).json()["sightings"] == []
        legacy_claim = await client.post("/internal/media-jobs/claim",
            json={"owner": "old-worker"}, headers={"Authorization": "Bearer synthetic-worker-token"})
        assert legacy_claim.status_code == 200 and legacy_claim.json() == {"job": None}
        if media_kind == "video":
            from app.capture_jobs import claim_capture_job
            async with app.state.pool.acquire() as conn:
                assert await claim_capture_job(conn, "cpu", cpu=True) is None
        from app.security import issue_session
        original_session = client.cookies.get("session")
        client.cookies.set("session", issue_session(uuid4()))
        assert (await client.get(f"/capture/{capture_id}")).status_code == 404
        client.cookies.set("session", original_session)

        detector, embedder = Session(detector=True), Session()
        adapter = GpuInference(detector, embedder, Identity("a" * 64, "b" * 64, "synthetic"),
            limits=Limits(max_tracking_frames=sample_count) if media_kind == "video" else None)
        transport = LoseFirstCompletion(app)
        config = Config(api_url="https://api.example.test", token="synthetic-worker-token",
                        storage_origins=("https://objects.example.test",), multi_animal=True,
                        retry_delay=.001)
        async with Worker(config, adapter, api_transport=transport,
                          storage_transport=httpx.MockTransport(objects.request)) as worker:
            assert await worker.run_once()
        assert len(transport.completions) == 2
        assert transport.completions[0] == transport.completions[1]
        assert detector.calls == sample_count
        assert embedder.calls == 2 * sample_count

        status = (await client.get(f"/capture/{capture_id}")).json()
        assert len(status["instances"]) == 2 * sample_count
        if sample_count > 1:
            assert status["processing_state"] == "needs_review"
            assert not status["sighting_ids"]
            assert (await client.get("/map")).json()["sightings"] == []
            # Left and right animal evidence stays separate even across views.
            groups = [[i["instance_id"] for i in status["instances"] if i["bbox"][0] < 300],
                      [i["instance_id"] for i in status["instances"] if i["bbox"][0] >= 300]]
            response = await client.post(f"/capture/{capture_id}/review", json={
                "revision": status["revision"], "publish": True,
                "groups": [{"instance_ids": ids} for ids in groups],
            })
            assert response.status_code == 200, response.text
            status = response.json()
        assert status["processing_state"] == "ready"
        assert len(set(status["sighting_ids"])) == 2
        pins = (await client.get("/map")).json()["sightings"]
        assert len(pins) == 2
        assert len({(pin["lat"], pin["lng"]) for pin in pins}) == 1
        assert all(pin["lat"] == 12.97 and pin["lng"] == 77.59 for pin in pins)
        client.cookies.set("session", issue_session(uuid4()))
        shared_pins = (await client.get("/map")).json()["sightings"]
        assert len(shared_pins) == 2
        assert len({(pin["lat"], pin["lng"]) for pin in shared_pins}) == 1
        assert all((pin["lat"], pin["lng"]) != (12.97, 77.59) for pin in shared_pins)
        client.cookies.set("session", original_session)
        for instance in status["instances"]:
            assert await objects.get(unquote(urlsplit(instance["photo_url"]).path.lstrip("/")))
        replay = await client.post("/capture", data=data, files=files)
        assert replay.status_code == 201 and replay.json()["duplicate"]
        assert replay.json()["sighting_ids"] == status["sighting_ids"]

        async with app.state.pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM jobs WHERE capture_id=$1::uuid", capture_id) == 1
            assert await conn.fetchval("SELECT count(*) FROM animal_instances WHERE capture_id=$1::uuid", capture_id) == 2 * sample_count
            assert await conn.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1::uuid", capture_id) == 2
            assert await conn.fetchval("SELECT count(*) FROM individuals") == 0
            rows = await conn.fetch("""SELECT a.sighting_id, e.vec_miew::text AS vec
                FROM animal_instances a JOIN embeddings e ON e.instance_id=a.id
                WHERE a.capture_id=$1::uuid""", capture_id)
            by_animal = {}
            for row in rows:
                by_animal.setdefault(row["sighting_id"], set()).add(row["vec"])
            assert len(by_animal) == 2
            assert all(len(vectors) == 1 for vectors in by_animal.values())
            assert len(set.union(*by_animal.values())) == 2
        monkeypatch.setattr(settings, "multi_animal_enabled", False)
        disabled_replay = await client.post("/capture", data=data, files=files)
        assert disabled_replay.status_code == 201 and disabled_replay.json()["duplicate"]
        assert (await client.get(f"/capture/{capture_id}")).json()["sighting_ids"] == status["sighting_ids"]
        blocked = await client.post("/capture", data={**data, "client_token": str(uuid4())}, files=files)
        assert blocked.status_code == 404
    finally:
        app.dependency_overrides.pop(get_storage, None)
