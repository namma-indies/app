"""Public liveness and authenticated, bounded operational diagnostics."""
import asyncio
import hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from app.config import settings
from app.auth.deps import require_observer
from app.deps import get_storage
from app.media_jobs import require_gpu
from app.model_health import local_reid_health
from app.operational_errors import WINDOW_S, errors
from app.ratelimit import Limit, check

router = APIRouter()

# Include running jobs too: a hung or abandoned lease must not look drained.
# Deliberately throttled backfills have their own rows and age threshold.
QUEUE_SQL = """
    SELECT 'reid' AS name, count(*) FILTER (WHERE status='pending') AS pending,
           count(*) FILTER (WHERE status='running') AS running,
           extract(epoch FROM now()-min(created_at)) AS oldest_age_s
    FROM jobs WHERE status IN ('pending','running')
        AND (sighting_id IS NOT NULL OR capture_id IS NOT NULL)
    UNION ALL
    SELECT 'transcription', count(*) FILTER (WHERE status='pending'),
           count(*) FILTER (WHERE status='running'), extract(epoch FROM now()-min(created_at))
    FROM transcription_jobs WHERE status IN ('pending','running')
    UNION ALL
    SELECT 'suggestion', count(*) FILTER (WHERE suggest_status='pending'),
           count(*) FILTER (WHERE suggest_status='running'), extract(epoch FROM now()-min(created_at))
    FROM voice_transcripts WHERE outcome='transcribed' AND suggest_status IN ('pending','running')
    UNION ALL
    SELECT 'description', count(*) FILTER (WHERE status='pending'),
           count(*) FILTER (WHERE status='running'), extract(epoch FROM now()-min(created_at))
    FROM photo_descriptions WHERE NOT backfill AND status IN ('pending','running')
    UNION ALL
    SELECT 'description_backfill', count(*) FILTER (WHERE status='pending'),
           count(*) FILTER (WHERE status='running'), extract(epoch FROM now()-min(created_at))
    FROM photo_descriptions WHERE backfill AND status IN ('pending','running')
    UNION ALL
    SELECT 'suggestion_backfill', count(*) FILTER (WHERE status='pending'),
           count(*) FILTER (WHERE status='running'), extract(epoch FROM now()-min(created_at))
    FROM transcript_backfill WHERE status IN ('pending','running')
"""
QUEUE_NAMES = ("reid", "transcription", "suggestion", "description",
               "description_backfill", "suggestion_backfill")


async def require_health_token(authorization: str | None = Header(None)):
    expected = settings.health_check_token
    if not expected:
        raise HTTPException(404, "not found", headers={"Cache-Control": "no-store"})
    supplied = (authorization or "").removeprefix("Bearer ")
    if (not authorization or not authorization.startswith("Bearer ")
            or not hmac.compare_digest(supplied.encode(), expected.encode())):
        raise HTTPException(401, "authentication required",
                            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})


@router.get("/health")
async def health():
    return {"status": "ok"}


def expected_capabilities():
    required = []
    if settings.media_jobs_enabled:
        required.append("reid")
        if settings.multi_animal_enabled:
            required.append("multi_animal")
    if settings.transcription_enabled:
        required.append("transcription")
    if settings.suggestions_enabled and settings.suggest_via == "worker":
        required.append("suggestions")
    return required


def queue_enabled(name):
    return {
        "reid": settings.media_jobs_enabled,
        "transcription": settings.media_jobs_enabled and settings.transcription_enabled,
        "suggestion": settings.suggestions_enabled,
        "description": settings.describe_enabled and settings.suggestions_enabled
                       and settings.suggest_via == "worker",
        "description_backfill": settings.describe_enabled and settings.suggestions_enabled
                       and settings.suggest_via == "worker" and settings.describe_backfill_per_hour > 0,
        "suggestion_backfill": settings.suggestions_enabled and settings.suggest_via == "worker"
                       and settings.transcript_backfill_per_hour > 0,
    }[name]


def queue_check(row):
    name = row["name"]
    age = max(0, int(row["oldest_age_s"])) if row["oldest_age_s"] is not None else None
    threshold = (settings.health_backfill_stale_s if name.endswith("_backfill")
                 else settings.health_queue_stale_s)
    return {
        "status": ("paused" if not queue_enabled(name) else
                   "stale" if age is not None and age >= threshold else "ok"),
        "pending": row["pending"], "running": row["running"],
        "oldest_age_s": age, "stale_after_s": threshold,
    }


async def database_checks(pool):
    async with asyncio.timeout(settings.health_check_timeout_s):
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.fetchval("SELECT set_config('statement_timeout', $1, true)",
                                str(int(settings.health_check_timeout_s * 1000)))
            await conn.fetchval("SELECT 1")
            queues = {row["name"]: queue_check(row) for row in await conn.fetch(QUEUE_SQL)}
            beats = {row["capability"]: max(0, int(row["age_s"])) for row in await conn.fetch(
                "SELECT capability, extract(epoch FROM now()-seen_at) AS age_s FROM worker_heartbeats")}
            count = 0
            if settings.ops_error_monitoring_enabled:
                count = await conn.fetchval(
                    "SELECT coalesce(sum(count),0) FROM operational_errors "
                    "WHERE minute >= now() - $1 * interval '1 second'", WINDOW_S)
            return queues, beats, int(count)


async def storage_check(storage):
    try:
        async with asyncio.timeout(settings.health_check_timeout_s):
            await storage.check_health()
        return {"status": "ok"}
    except Exception:
        return {"status": "unavailable"}


async def backup_check(storage):
    try:
        async with asyncio.timeout(settings.health_check_timeout_s):
            backup = await storage.backup_health()
        status = ("missing" if backup is None else "invalid" if not backup["valid"] else
                  "stale" if backup["age_s"] >= settings.health_backup_stale_s else "ok")
        return {"status": status, "age_s": backup["age_s"] if backup else None,
                "stale_after_s": settings.health_backup_stale_s}
    except Exception:
        return {"status": "unavailable"}


@router.get("/health/detailed", dependencies=[Depends(require_health_token)])
async def detailed_health(request: Request, storage=Depends(get_storage)):
    async def database():
        try:
            return await database_checks(request.app.state.pool)
        except Exception:
            return None

    snapshot, object_storage, database_backup = await asyncio.gather(
        database(), storage_check(storage), backup_check(storage))
    required = expected_capabilities()
    checks = {"database": {"status": "unavailable" if snapshot is None else "ok"},
              "object_storage": object_storage, "database_backup": database_backup}
    checks["local_reid_models"] = (local_reid_health()
        if not settings.media_jobs_enabled or settings.media_cpu_fallback_enabled else {"status": "disabled"})
    if snapshot is None:
        queues = {name: {"status": "unknown"} for name in QUEUE_NAMES}
        gpu = {"status": "unknown" if required else "disabled"}
        error_check = {"status": "unknown" if settings.ops_error_monitoring_enabled else "disabled"}
    else:
        queues, beats, count = snapshot
        capabilities = {name: {"status": ("missing" if name not in beats else
                                "stale" if beats[name] >= settings.health_worker_stale_s else "ok"),
                               "age_s": beats.get(name)} for name in required}
        gpu = {"status": ("disabled" if not required else
                           "unconfigured" if not settings.media_jobs_enabled or not settings.media_gpu_token else
                           "unavailable" if any(c["status"] != "ok" for c in capabilities.values()) else "ok"),
               "capabilities": capabilities, "stale_after_s": settings.health_worker_stale_s}
        count += errors.pending()
        error_check = {"status": ("disabled" if not settings.ops_error_monitoring_enabled else
                                  "elevated" if count >= settings.ops_error_alert_threshold else "ok"),
                       "count": count, "window_s": WINDOW_S,
                       "alert_at": settings.ops_error_alert_threshold}
    checks["gpu_worker"] = gpu
    checks["errors"] = error_check
    okay = {"ok", "disabled", "paused"}
    healthy = all(c["status"] in okay for c in [*checks.values(), *queues.values()])
    return JSONResponse({"status": "ok" if healthy else "degraded", "checks": checks, "queues": queues},
                        status_code=200 if healthy else 503, headers={"Cache-Control": "no-store"})


class WorkerHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcription: StrictBool = False
    suggestions: StrictBool = False
    multi_animal: StrictBool = False


@router.post("/internal/worker-health", dependencies=[Depends(require_gpu)])
async def worker_health(body: WorkerHeartbeat, request: Request):
    capabilities = ["reid", *(name for name, enabled in body.model_dump().items() if enabled)]
    async with asyncio.timeout(settings.health_check_timeout_s):
        async with request.app.state.pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO worker_heartbeats (capability, seen_at) VALUES ($1, now())
                   ON CONFLICT (capability) DO UPDATE SET seen_at=EXCLUDED.seen_at""",
                [(name,) for name in capabilities],
            )
    return {"status": "ok"}


class ClientError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["error", "unhandled_rejection"]


@router.post("/monitoring/client-error", status_code=204)
async def client_error(body: ClientError, observer_id: UUID = Depends(require_observer)):
    if not settings.ops_error_monitoring_enabled:
        raise HTTPException(404, "not found")
    check(f"client-errors:obs:{observer_id}", Limit(10, 900))
    # Validate the two fixed client categories but persist a single aggregate.
    # Never retain the observer, payload, browser, URL, message or stack.
    errors.record("client_error")
