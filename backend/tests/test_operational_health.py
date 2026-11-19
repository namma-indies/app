import asyncio
from contextlib import asynccontextmanager
import logging

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.deps import get_storage
from app.ids import uuid7
from app.operational_errors import ErrorCaptureMiddleware, errors


class HealthyStorage:
    async def check_health(self):
        pass

    async def backup_health(self):
        return {"age_s": 3600, "valid": True}


@pytest_asyncio.fixture
async def health_client(app_client, monkeypatch):
    for name, value in {
        "health_check_token": "synthetic-health-token", "media_gpu_token": "synthetic-worker-token",
        "media_jobs_enabled": True, "multi_animal_enabled": True, "transcription_enabled": True,
        "suggestions_enabled": True, "suggest_via": "worker", "describe_enabled": True,
        "ops_error_monitoring_enabled": False,
        "media_cpu_fallback_enabled": False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    app = app_client._transport.app
    app.dependency_overrides[get_storage] = HealthyStorage
    async with app.state.pool.acquire() as conn:
        await conn.execute("TRUNCATE worker_heartbeats, operational_errors")
    errors.clear()
    yield app_client
    errors.clear()
    app.dependency_overrides.pop(get_storage, None)


async def detail(client):
    return await client.get("/health/detailed", headers={"Authorization": "Bearer synthetic-health-token"})


async def worker_beat(client, **kwargs):
    return await client.post("/internal/worker-health",
        headers={"Authorization": "Bearer synthetic-worker-token"},
        json={"transcription": True, "suggestions": True, "multi_animal": True, **kwargs})


async def sighting(conn):
    oid, sid = uuid7(), uuid7()
    await conn.execute("INSERT INTO observers (id, display_name) VALUES ($1, 'Synthetic')", oid)
    await conn.execute("INSERT INTO sightings (id, observer_id, captured_at, geo_source) VALUES ($1,$2,now(),'none')", sid, oid)
    return sid


async def test_public_health_is_minimal_without_auth_or_dependencies(health_client):
    response = await health_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_local_model_details_are_private_and_follow_the_cpu_path(health_client, monkeypatch, tmp_path):
    from app import model_health
    monkeypatch.setattr(settings, "media_cpu_fallback_enabled", True)
    monkeypatch.setattr(model_health, "MODEL_DIR", tmp_path)
    await worker_beat(health_client)
    assert (await detail(health_client)).json()["checks"]["local_reid_models"]["status"] == "unavailable"
    for name in model_health.MODEL_NAMES:
        (tmp_path / name).write_bytes(b"synthetic presence only")
    assert (await detail(health_client)).status_code == 200
    assert (await health_client.get("/health")).json() == {"status": "ok"}


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic synthetic-health-token", "Bearer synthetic-worker-token"])
async def test_detailed_health_rejects_wrong_credentials(health_client, authorization):
    response = await health_client.get("/health/detailed", headers={"Authorization": authorization} if authorization else {})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert "checks" not in response.json()


async def test_a_query_string_token_does_not_authenticate(health_client):
    response = await health_client.get("/health/detailed?token=synthetic-health-token")
    assert response.status_code == 401


async def test_detailed_health_is_disabled_until_a_token_is_set(health_client, monkeypatch):
    monkeypatch.setattr(settings, "health_check_token", "")
    assert (await detail(health_client)).status_code == 404


async def test_worker_auth_and_capabilities_are_independent_of_health_auth(health_client):
    bad = await health_client.post("/internal/worker-health", json={}, headers={"Authorization": "Bearer synthetic-health-token"})
    assert bad.status_code == 401
    assert (await worker_beat(health_client, transcription="true")).status_code == 422
    assert (await worker_beat(health_client)).status_code == 200
    response = await detail(health_client)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["checks"]["gpu_worker"]["status"] == "ok"


async def test_idle_worker_loss_and_missing_model_capabilities_are_visible(health_client):
    assert (await worker_beat(health_client, transcription=False)).status_code == 200
    response = await detail(health_client)
    assert response.status_code == 503
    caps = response.json()["checks"]["gpu_worker"]["capabilities"]
    assert caps["transcription"] == {"status": "missing", "age_s": None}
    async with health_client._transport.app.state.pool.acquire() as conn:
        await conn.execute("UPDATE worker_heartbeats SET seen_at=now()-interval '20 minutes'")
    caps = (await detail(health_client)).json()["checks"]["gpu_worker"]["capabilities"]
    assert caps["reid"]["status"] == "stale"
    assert caps["reid"]["age_s"] >= 1200


async def test_all_four_live_queues_report_oldest_pending_or_running_age_without_payloads(health_client):
    await worker_beat(health_client)
    async with health_client._transport.app.state.pool.acquire() as conn:
        sid = await sighting(conn)
        await conn.execute("""INSERT INTO jobs (id,kind,sighting_id,pipeline_version,status,created_at,lease_expires_at)
            VALUES ($1,'photo',$2,'synthetic','running',now()-interval '1 hour',now()-interval '1 minute')""", uuid7(), sid)
        await conn.execute("""INSERT INTO transcription_jobs (id,capture_id,source,s3_key,pipeline_version,created_at)
            VALUES ($1,$2,'note','captures/synthetic-private-key','synthetic',now()-interval '1 hour')""", uuid7(), sid)
        await conn.execute("""INSERT INTO voice_transcripts (id,capture_id,source,pipeline_version,model,outcome,language,text,created_at)
            VALUES ($1,$2,'note','synthetic','synthetic','transcribed','en','synthetic-private-transcript',now()-interval '1 hour')""", uuid7(), sid)
        await conn.execute("""INSERT INTO photo_descriptions (id,sighting_id,capture_id,created_at)
            VALUES ($1,$2,$2,now()-interval '1 hour')""", uuid7(), sid)
    response = await detail(health_client)
    assert response.status_code == 503
    queues = response.json()["queues"]
    for name in ("reid", "transcription", "suggestion", "description"):
        assert queues[name]["status"] == "stale"
        assert queues[name]["oldest_age_s"] >= 3600
    assert queues["reid"]["running"] == 1
    for secret in (str(sid), "synthetic-private-key", "synthetic-private-transcript", "synthetic-health-token"):
        assert secret not in response.text


async def test_intentionally_paused_backfill_does_not_page(health_client):
    await worker_beat(health_client)
    async with health_client._transport.app.state.pool.acquire() as conn:
        sid = await sighting(conn)
        await conn.execute("""INSERT INTO photo_descriptions (id,sighting_id,capture_id,backfill,created_at)
            VALUES ($1,$2,$2,true,now()-interval '7 days')""", uuid7(), sid)
    response = await detail(health_client)
    assert response.status_code == 200
    assert response.json()["queues"]["description_backfill"]["status"] == "paused"


async def test_enabled_backfill_is_monitored_separately(health_client, monkeypatch):
    monkeypatch.setattr(settings, "describe_backfill_per_hour", 10)
    await worker_beat(health_client)
    async with health_client._transport.app.state.pool.acquire() as conn:
        sid = await sighting(conn)
        await conn.execute("""INSERT INTO photo_descriptions (id,sighting_id,capture_id,backfill,created_at)
            VALUES ($1,$2,$2,true,now()-interval '2 days')""", uuid7(), sid)
    queues = (await detail(health_client)).json()["queues"]
    assert queues["description"]["status"] == "ok"
    assert queues["description_backfill"]["status"] == "stale"


async def test_database_pool_exhaustion_is_bounded_and_other_probes_still_return(health_client, monkeypatch):
    monkeypatch.setattr(settings, "health_check_timeout_s", .1)
    class HungPool:
        @asynccontextmanager
        async def acquire(self):
            await asyncio.sleep(20)
            yield
    app, original = health_client._transport.app, health_client._transport.app.state.pool
    app.state.pool = HungPool()
    try:
        async with asyncio.timeout(1):
            response = await detail(health_client)
    finally:
        app.state.pool = original
    report = response.json()
    assert response.status_code == 503
    assert report["checks"]["database"]["status"] == "unavailable"
    assert report["checks"]["object_storage"]["status"] == "ok"
    assert report["queues"]["reid"]["status"] == "unknown"


async def test_storage_timeout_is_bounded_and_does_not_expose_exceptions(health_client, monkeypatch):
    monkeypatch.setattr(settings, "health_check_timeout_s", .1)
    class HungStorage:
        async def check_health(self):
            await asyncio.sleep(20)
    health_client._transport.app.dependency_overrides[get_storage] = HungStorage
    async with asyncio.timeout(1):
        response = await detail(health_client)
    assert response.status_code == 503
    assert response.json()["checks"]["object_storage"] == {"status": "unavailable"}


@pytest.mark.parametrize("backup,status", [(None, "missing"), ({"age_s": 1, "valid": False}, "invalid"),
                                           ({"age_s": 200000, "valid": True}, "stale")])
async def test_missing_damaged_or_old_backup_is_an_incident(health_client, backup, status):
    class BackupStorage(HealthyStorage):
        async def backup_health(self):
            return backup
    health_client._transport.app.dependency_overrides[get_storage] = BackupStorage
    assert (await detail(health_client)).json()["checks"]["database_backup"]["status"] == status


async def test_error_counts_are_persisted_once_and_survive_memory_reset(health_client, monkeypatch):
    monkeypatch.setattr(settings, "ops_error_monitoring_enabled", True)
    await worker_beat(health_client)
    for _ in range(5):
        errors.record("http_5xx")
    assert (await detail(health_client)).json()["checks"]["errors"]["status"] == "elevated"
    await errors.flush_to_db(health_client._transport.app.state.pool)
    errors.clear()
    report = (await detail(health_client)).json()
    assert report["checks"]["errors"]["count"] == 5
    # Health's own degraded 503s must not feed the counter.
    assert errors.pending() == 0


async def test_client_error_ingest_requires_auth_and_stores_only_a_count(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "ops_error_monitoring_enabled", True)
    errors.clear()
    for _ in range(10):
        assert (await client.post("/monitoring/client-error", json={"kind": "error"})).status_code == 204
    assert (await client.post("/monitoring/client-error", json={"kind": "error"})).status_code == 429
    assert errors.pending() == 10
    assert all(kind == "client_error" for _, kind in errors._counts)
    await errors.flush_to_db(client._transport.app.state.pool)
    errors.clear()
    client.cookies.clear()
    assert (await client.post("/monitoring/client-error", json={"kind": "error"})).status_code == 401


async def test_client_error_ingest_refuses_extra_context_and_is_off_by_default(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "ops_error_monitoring_enabled", False)
    assert (await client.post("/monitoring/client-error", json={"kind": "error"})).status_code == 404
    monkeypatch.setattr(settings, "ops_error_monitoring_enabled", True)
    errors.clear()
    assert (await client.post("/monitoring/client-error", json={"kind": "error", "message": "private"})).status_code == 422
    assert errors.pending() == 0


async def test_failed_flush_keeps_the_bounded_buffer_for_retry(monkeypatch):
    monkeypatch.setattr(settings, "ops_error_monitoring_enabled", True)
    errors.clear()
    errors.record("application_error")
    with pytest.raises(AttributeError):
        await errors.flush_to_db(object())
    assert errors.pending() == 1
    errors.clear()


async def test_accepted_gpu_failures_are_counted_without_inspecting_payloads(monkeypatch):
    monkeypatch.setattr(settings, "ops_error_monitoring_enabled", True)
    errors.clear()
    app = FastAPI()
    app.add_middleware(ErrorCaptureMiddleware)
    @app.post("/internal/media-jobs/synthetic/fail")
    async def fail():
        return {"status": "failed"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.post("/internal/media-jobs/synthetic/fail")).status_code == 200
    assert errors.pending() == 1
    errors.clear()


async def test_self_hosted_capture_handles_both_handled_and_unhandled_5xx(monkeypatch):
    monkeypatch.setattr(settings, "ops_error_monitoring_enabled", True)
    errors.clear()
    app = FastAPI()
    app.add_middleware(ErrorCaptureMiddleware)
    @app.get("/handled")
    async def handled():
        return JSONResponse({}, status_code=500)
    @app.get("/unhandled")
    async def unhandled():
        raise ValueError("synthetic-private-body")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://test") as client:
        assert (await client.get("/handled")).status_code == 500
        assert (await client.get("/unhandled")).status_code == 500
    assert errors.pending() == 2
    # The error handler never copies the message or exception into its buffer.
    errors.emit(logging.LogRecord("app.synthetic", logging.ERROR, "", 0, "private", (), None))
    assert errors.pending() == 3
    assert all(kind in ("http_5xx", "application_error") for _, kind in errors._counts)
    errors.clear()
