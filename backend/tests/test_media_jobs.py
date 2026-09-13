"""Protocol tests do not touch a database; integration tests require an explicit
local disposable DSN and never use the repository's truncating fixtures.
"""
import io
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from pydantic import ValidationError

from app import media_jobs as jobs
from app.config import settings


def frame(**updates):
    return jobs.FrameResult.model_validate({
        "photo_id": uuid4(), "width": 100, "height": 80,
        "phash": "0123456789abcdef", "dog_confidence": 0.1,
        "cat_confidence": 0.0, **updates,
    })


def completion(frames=None, **updates):
    return jobs.Completion.model_validate({
        "lease_token": "x" * 43, "pipeline_version": jobs.PIPELINE_VERSION,
        "model": jobs.MODEL_NAME, "frames": frames or [frame()], **updates,
    })


@pytest.mark.parametrize("updates", [
    {"dog_confidence": float("nan")}, {"cat_confidence": float("inf")},
    {"dog_confidence": 1.1}, {"width": 0}, {"phash": "invalid"},
    {"bbox": [0, 0, 10, 10]},
    {"bbox": [0, 0, 101, 10], "vector": [1.0] + [0.0] * 2151},
    {"bbox": [0, 0, 10, 10], "vector": [0.0] * 2152},
    {"bbox": [0, 0, 10, 10], "vector": [1.0]},
    {"bbox": [0, 0, 10, 10], "vector": [float("nan")] * 2152},
])
def test_analysis_rejects_invalid_data(updates):
    with pytest.raises(ValidationError):
        frame(**updates)


def test_valid_unit_embedding_and_no_animal():
    assert frame().vector is None
    assert frame(bbox=(0, 0, 10, 10), vector=[1.0] + [0.0] * 2151).vector[0] == 1


def test_digest_ignores_lease_and_frame_order_but_not_analysis():
    a, b = frame(), frame()
    body = completion([a, b])
    assert jobs.completion_digest(body) == jobs.completion_digest(completion([b, a], lease_token="y" * 43))
    assert jobs.completion_digest(body) != jobs.completion_digest(completion([a.model_copy(update={"dog_confidence": 0.2}), b]))


def test_duplicate_ids_and_pipeline_mismatch_rejected():
    a = frame()
    for kwargs in ({"frames": [a, a]}, {"pipeline_version": "legacy"}, {"model": "other"}):
        with pytest.raises(ValidationError):
            completion(**kwargs)


@pytest.mark.asyncio
async def test_gpu_auth_fails_closed_and_has_no_claimed_cpu_capability(monkeypatch):
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    monkeypatch.setattr(settings, "media_gpu_token", "secret")
    for header in (None, "Bearer wrong", "secret", "bearer secret", "Bearer sécret"):
        with pytest.raises(HTTPException) as exc:
            await jobs.require_gpu(header)
        assert exc.value.status_code == 401
    await jobs.require_gpu("Bearer secret")
    monkeypatch.setattr(settings, "media_gpu_token", "")
    with pytest.raises(HTTPException):
        await jobs.require_gpu("Bearer ")
    monkeypatch.setattr(settings, "media_gpu_token", "secret")
    monkeypatch.setattr(settings, "media_jobs_enabled", False)
    with pytest.raises(HTTPException):
        await jobs.require_gpu("Bearer secret")
    with pytest.raises(ValidationError):
        jobs.Claim(owner="gpu", capability="gpu")


@pytest.mark.asyncio
async def test_endpoint_rejects_unauthenticated_before_db(monkeypatch):
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    monkeypatch.setattr(settings, "media_gpu_token", "secret")
    app = FastAPI()
    app.include_router(jobs.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/internal/media-jobs/claim", json={"owner": "gpu"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_bounded_read_accepts_exact_limit_and_rejects_empty_or_oversize():
    from app.routes.sighting import _read_bounded
    assert await _read_bounded(UploadFile(io.BytesIO(b"abcd")), 4) == b"abcd"
    for data, code in ((b"", 422), (b"abcde", 413)):
        with pytest.raises(HTTPException) as exc:
            await _read_bounded(UploadFile(io.BytesIO(data)), 4)
        assert exc.value.status_code == code


class FakeConnection:
    def __init__(self, job=None):
        self.job = job
        self.calls = []

    @asynccontextmanager
    async def transaction(self):
        self.calls.append(("begin", ()))
        try:
            yield
        finally:
            self.calls.append(("end", ()))

    async def execute(self, query, *args):
        self.calls.append((query, args))

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.job

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return False

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return []


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def lease_row(**updates):
    return {"id": uuid4(), "sighting_id": uuid4(), "kind": "photo", "status": "running",
            "lease_token_hash": jobs.token_hash("x" * 43), "lease_owner": "gpu:test",
            "live": True, "pipeline_version": jobs.PIPELINE_VERSION,
            "payload": {}, "attempts": 1, "lease_expires_at": datetime.now(timezone.utc) + timedelta(seconds=60),
            **updates}


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [{"live": False}, {"status": "pending"}, {"lease_owner": "cpu"}, {"lease_token_hash": jobs.token_hash("old")}, {"lease_token_hash": None}])
async def test_lease_fences_stale_and_wrong_capability(updates):
    conn = FakeConnection(lease_row(**updates))
    with pytest.raises(HTTPException) as exc:
        await jobs.locked_lease(conn, conn.job["id"], "x" * 43)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_completion_replay_never_republishes_objects():
    body = completion()
    conn = FakeConnection(lease_row(status="done", live=False,
        completion_digest=jobs.completion_digest(body), terminal_outcome="no_animal"))
    assert (await jobs.complete_job(conn, None, conn.job["id"], body))["replayed"]
    changed = completion([body.frames[0].model_copy(update={"dog_confidence": 0.2})])
    with pytest.raises(HTTPException) as exc:
        await jobs.complete_job(conn, None, conn.job["id"], changed)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_cpu_claim_query_excludes_video_and_uses_skip_locked():
    conn = FakeConnection()
    assert await jobs.claim_job(conn, "cpu", cpu=True) is None
    query, args = next((q, a) for q, a in conn.calls if "SKIP LOCKED" in q)
    assert "kind='photo'" in query and "cpu_eligible_at<=now()" in query
    assert args == (jobs.PIPELINE_VERSION, True)


@pytest.mark.asyncio
async def test_video_ingestion_never_decodes_and_enqueue_is_atomic(monkeypatch):
    from app.routes import sighting
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    def forbidden(*args):
        pytest.fail("video was decoded on the API server")
    monkeypatch.setattr(sighting, "extract_diverse_frames", forbidden)
    conn = FakeConnection()
    storage = SimpleNamespace(put=AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(conn))))
    tasks = BackgroundTasks()
    result = await sighting.create_sighting(
        background_tasks=tasks, request=request, photos=None,
        video=UploadFile(io.BytesIO(b"opaque video bytes")),
        lat=12.9, lng=77.5, geo_accuracy_m=None, client_token=None,
        geo_source="device_gps", captured_at=datetime.now(timezone.utc),
        reported_at=None, note=None, sex=None, ear_notch=None, condition=None,
        override_no_dog=False, observer_id=uuid4(), storage=storage,
    )
    assert result.status_code == 201
    import json
    body = json.loads(result.body)
    assert body["photo_ids"] == [] and body["processing_state"] == "queued"
    assert tasks.tasks == []
    queries = [q for q, _ in conn.calls]
    assert queries[0] == "begin" and queries[-1] == "end"
    assert any("INSERT INTO jobs" in q for q in queries)
    assert any("UPDATE sightings SET clip_s3_key" in q for q in queries)


@pytest.mark.asyncio
async def test_video_storage_failure_does_not_acknowledge_or_enqueue(monkeypatch):
    from app.routes import sighting
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    conn = FakeConnection()
    storage = SimpleNamespace(put=AsyncMock(side_effect=OSError("offline")))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(conn))))
    with pytest.raises(HTTPException) as exc:
        await sighting.create_sighting(
            background_tasks=BackgroundTasks(), request=request, photos=None,
            video=UploadFile(io.BytesIO(b"video")), lat=None, lng=None,
            geo_accuracy_m=None, client_token=None, geo_source="none",
            captured_at=datetime.now(timezone.utc), reported_at=None, note=None,
            sex=None, ear_notch=None, condition=None, override_no_dog=False,
            observer_id=uuid4(), storage=storage,
        )
    assert exc.value.status_code == 503
    assert not conn.calls


@pytest.mark.asyncio
async def test_photo_completion_publishes_no_animal_terminal_state():
    body = completion()
    source = body.frames[0]
    class CompletionConnection(FakeConnection):
        async def fetchrow(self, query, *args):
            self.calls.append((query, args))
            if "SELECT * FROM sightings" in query:
                return {"review_status": "valid"}
            return self.job

        async def fetch(self, query, *args):
            return [{"photo_id": source.photo_id, "s3_key": "stored.webp", "width": source.width,
                     "height": source.height, "phash": source.phash}]

        async def fetchval(self, query, *args):
            return True
    conn = CompletionConnection(lease_row())
    result = await jobs.complete_job(conn, None, conn.job["id"], body)
    assert result == {"status": "done", "outcome": "no_animal", "replayed": False}
    assert any("completion_digest" in q and args[2] == "no_animal" for q, args in conn.calls if "UPDATE jobs" in q)
    assert not any("INSERT INTO embeddings" in q for q, _ in conn.calls)
    wrong = completion([source.model_copy(update={"photo_id": uuid4()})])
    with pytest.raises(HTTPException) as exc:
        await jobs.complete_job(conn, None, conn.job["id"], wrong)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_slots_are_lease_scoped_and_photo_jobs_cannot_put(monkeypatch):
    conn = FakeConnection(lease_row(kind="video"))
    conn.fetchval = AsyncMock(return_value="sightings/known/clip.mp4")
    storage = SimpleNamespace(urls=AsyncMock(return_value=["signed-get"]), put_url=AsyncMock(return_value="signed-put"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(conn))))
    body = jobs.URLRequest(lease_token="x" * 43, frames=[jobs.FrameUpload(index=0, original_bytes=100, thumbnail_bytes=20)])
    result = await jobs.refresh_urls(conn.job["id"], body, request, storage)
    assert result["uploads"][0]["photo_id"]
    for call in storage.put_url.call_args_list:
        key, size, ttl = call.args
        assert key.startswith(f"media-staging/{conn.job['id']}/{conn.job['lease_token_hash']}/")
        assert size in (100, 20) and ttl <= 60
    conn.job["kind"] = "photo"
    with pytest.raises(HTTPException) as exc:
        await jobs.refresh_urls(conn.job["id"], body, request, storage)
    assert exc.value.status_code == 422
    with pytest.raises(ValidationError):
        jobs.URLRequest(lease_token="x" * 43, keys=["private/credentials"])


@pytest.mark.asyncio
async def test_staged_publication_checks_size_and_conditionally_copies():
    from app.storage.s3 import S3Storage
    storage = S3Storage(endpoint="http://localhost", bucket="test", access_key="test", secret_key="test", region="test")
    client = SimpleNamespace(head_object=AsyncMock(return_value={"ContentLength": 100, "ContentType": "image/webp", "ETag": "snapshot"}), copy_object=AsyncMock())
    @asynccontextmanager
    async def fake_client():
        yield client
    storage._client = fake_client
    await storage.publish_checked("staging", "final", 100)
    assert client.copy_object.call_args.kwargs["CopySourceIfMatch"] == "snapshot"
    assert client.copy_object.call_args.kwargs["Key"] == "final"
    client.copy_object.reset_mock()
    with pytest.raises(HTTPException):
        await storage.publish_checked("staging", "final", 101)
    client.copy_object.assert_not_called()


@pytest.mark.asyncio
async def test_failure_invalidates_lease_and_retries_with_backoff():
    conn = FakeConnection(lease_row())
    result = await jobs.fail_job(conn, conn.job["id"], jobs.Failure(lease_token="x" * 43, error="model unavailable"))
    assert result["status"] == "queued"
    query, args = next((q, a) for q, a in conn.calls if "UPDATE jobs" in q)
    assert "lease_token_hash=NULL" in query and args[3] == 2
    conn.job["attempts"] = settings.media_max_attempts
    assert (await jobs.fail_job(conn, conn.job["id"], jobs.Failure(lease_token="x" * 43, error="bad media")))["status"] == "failed"


@pytest.mark.asyncio
async def test_cpu_cannot_complete_video_even_with_internal_token():
    conn = FakeConnection(lease_row(kind="video", lease_owner="cpu"))
    with pytest.raises(HTTPException) as exc:
        await jobs.complete_job(conn, None, conn.job["id"], completion(), cpu=True)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_optional_isolated_db_claim_and_reclaim(monkeypatch):
    dsn = os.environ.get("MEDIA_JOBS_TEST_DSN", "")
    if not dsn:
        pytest.skip("set MEDIA_JOBS_TEST_DSN to an explicitly migrated disposable local database")
    parsed = urlparse(dsn)
    assert parsed.hostname in ("localhost", "127.0.0.1", "::1")
    assert parsed.path.startswith("/indiedex_media_jobs_test")
    import asyncpg
    conn = await asyncpg.connect(dsn)
    monkeypatch.setattr(settings, "media_cpu_grace_s", 0)
    transaction = conn.transaction()
    await transaction.start()
    try:
        sid = uuid4()
        await conn.execute("INSERT INTO sightings(id,captured_at,geo_source,clip_s3_key) VALUES($1,now(),'none','clip')", sid)
        await jobs.enqueue(conn, sid, "video")
        await jobs.enqueue(conn, sid, "video")
        assert await conn.fetchval("SELECT count(*) FROM jobs WHERE sighting_id=$1", sid) == 1
        assert await jobs.claim_job(conn, "cpu", cpu=True) is None
        job, token = await jobs.claim_job(conn, "dgx")
        await conn.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE id=$1", job["id"])
        new_job, new_token = await jobs.claim_job(conn, "dgx-two")
        assert new_job["id"] == job["id"] and token != new_token
        with pytest.raises(HTTPException):
            await jobs.locked_lease(conn, job["id"], token)
        await jobs.locked_lease(conn, job["id"], new_token)
    finally:
        await transaction.rollback()
        await conn.close()
