"""Public liveness and authenticated, bounded operational diagnostics."""
import asyncio
import hmac
import os
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
from app.moderator_alerts import delivery_configured, mail_status
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
# Counts only: no references, contacts or content. An unsent acknowledgement
# counts even after closure, matching the moderator queue.
GRIEVANCE_SQL = """
    SELECT count(*) FILTER (WHERE g.resolved_at IS NULL) AS open,
           count(*) FILTER (WHERE (g.resolved_at IS NULL AND (now() >= g.resolve_due_at
                    OR (g.removed_at IS NULL AND g.removal_due_at IS NOT NULL AND now() >= g.removal_due_at)))
                OR (m.sent_at IS NULL AND now() >= g.acknowledge_due_at)) AS overdue
    FROM grievances g LEFT JOIN moderation_mail m ON m.id = g.acknowledgement_mail_id
    WHERE g.resolved_at IS NULL OR m.sent_at IS NULL
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
            mail = await mail_status(conn)
            grievances = dict(await conn.fetchrow(GRIEVANCE_SQL))
            size = await conn.fetchval("SELECT pg_database_size(current_database())")
            return {"queues": queues, "beats": beats, "errors": int(count), "mail": mail,
                    "grievances": grievances, "db_size_bytes": int(size)}


async def database_write_check(pool):
    """The read-only probe cannot see a read-only (standby/recovery,
    default_transaction_read_only) or full database. Upsert one fixed row in a
    read-write transaction with the same short timeout."""
    import asyncpg

    try:
        async with asyncio.timeout(settings.health_check_timeout_s):
            async with pool.acquire() as conn, conn.transaction():
                await conn.fetchval("SELECT set_config('statement_timeout', $1, true)",
                                    str(int(settings.health_check_timeout_s * 1000)))
                await conn.execute(
                    "INSERT INTO health_write_probe (id, touched_at) VALUES (1, now()) "
                    "ON CONFLICT (id) DO UPDATE SET touched_at = EXCLUDED.touched_at")
        return {"status": "ok"}
    except asyncpg.ReadOnlySQLTransactionError:
        return {"status": "read_only"}
    except asyncpg.DiskFullError:
        return {"status": "full"}
    except Exception:
        return {"status": "unavailable"}


def database_disk_check(size_bytes):
    result = {"db_size_bytes": size_bytes}
    status = "disabled"
    limit = settings.health_db_size_max_bytes
    if limit and size_bytes is not None:
        status = "low" if size_bytes >= limit else "ok"
        result["db_size_max_bytes"] = limit
    path = settings.health_db_disk_path
    if path:
        try:
            stat = os.statvfs(path)
            free = stat.f_bavail * stat.f_frsize
            total = stat.f_blocks * stat.f_frsize
            ratio = free / total if total else 0.0
            result.update(free_bytes=free, total_bytes=total, free_ratio=round(ratio, 4),
                          min_free_ratio=settings.health_db_disk_min_free_ratio)
            if ratio < settings.health_db_disk_min_free_ratio:
                status = "low"
            elif status == "disabled":
                status = "ok"
        except OSError:
            status = "unavailable"
    result["status"] = status
    return result


def moderation_mail_check(mail):
    """Alert/acknowledgement delivery, visible outside the moderator tab.

    Unconfigured delivery is degraded: alerts and grievance acknowledgements
    would silently wait. Counts and ages only; never recipients or content.
    """
    if mail is None:
        return {"status": "unknown"}
    age = mail["oldest_unsent_age_s"]
    status = ("unconfigured" if not delivery_configured() else
              "failed" if mail["recently_undeliverable_mail"] else
              "stale" if age is not None and age >= settings.health_moderation_mail_stale_s else "ok")
    return {"status": status, "pending": mail["pending_mail"], "retrying": mail["failed_mail"],
            "undeliverable": mail["undeliverable_mail"],
            "recently_undeliverable": mail["recently_undeliverable_mail"],
            "oldest_unsent_age_s": age, "stale_after_s": settings.health_moderation_mail_stale_s}


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


async def restore_drill_check(storage):
    required = settings.health_restore_drill_required
    stale_after = settings.health_restore_drill_stale_s
    try:
        async with asyncio.timeout(settings.health_check_timeout_s):
            drill = await storage.restore_drill_health()
    except Exception:
        return {"status": "unavailable" if required else "disabled"}
    if drill is None:
        return {"status": "missing" if required else "disabled", "age_s": None, "stale_after_s": stale_after}
    status = ("invalid" if not drill["valid"] else
              "stale" if drill["age_s"] >= stale_after else "ok")
    return {"status": status if required else "disabled", "age_s": drill["age_s"],
            "manifest_compared": drill["manifest_compared"], "stale_after_s": stale_after}


@router.get("/health/detailed", dependencies=[Depends(require_health_token)])
async def detailed_health(request: Request, storage=Depends(get_storage)):
    async def database():
        try:
            return await database_checks(request.app.state.pool)
        except Exception:
            return None

    snapshot, object_storage, database_backup, restore_drill, database_write = await asyncio.gather(
        database(), storage_check(storage), backup_check(storage), restore_drill_check(storage),
        database_write_check(request.app.state.pool))
    required = expected_capabilities()
    checks = {"database": {"status": "unavailable" if snapshot is None else "ok"},
              "object_storage": object_storage, "database_backup": database_backup,
              "restore_drill": restore_drill, "database_write": database_write,
              "database_disk": database_disk_check(snapshot["db_size_bytes"] if snapshot else None)}
    checks["local_reid_models"] = (local_reid_health()
        if not settings.media_jobs_enabled or settings.media_cpu_fallback_enabled else {"status": "disabled"})
    checks["moderation_mail"] = moderation_mail_check(snapshot["mail"] if snapshot else None)
    if snapshot is None:
        checks["grievances"] = {"status": "unknown"}
    else:
        g = snapshot["grievances"]
        checks["grievances"] = {"status": "overdue" if g["overdue"] else "ok",
                                "open": g["open"], "overdue": g["overdue"]}
    if snapshot is None:
        queues = {name: {"status": "unknown"} for name in QUEUE_NAMES}
        gpu = {"status": "unknown" if required else "disabled"}
        error_check = {"status": "unknown" if settings.ops_error_monitoring_enabled else "disabled"}
    else:
        queues, beats, count = snapshot["queues"], snapshot["beats"], snapshot["errors"]
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
async def client_error(request: Request, body: ClientError, observer_id: UUID = Depends(require_observer)):
    if not settings.ops_error_monitoring_enabled:
        raise HTTPException(404, "not found")
    async with request.app.state.pool.acquire() as conn:
        await check(conn, f"client-errors:obs:{observer_id}", Limit(10, 900))
    # Validate the two fixed client categories but persist a single aggregate.
    # Never retain the observer, payload, browser, URL, message or stack.
    errors.record("client_error")
