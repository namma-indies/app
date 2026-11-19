"""Private aggregate counters and moderator-only account upload activity.

Counters contain no email, IP, token or photo data. The private spike list
identifies accounts already visible on the moderator's people screen.
"""
import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.auth.deps import require_moderator, require_observer
from app.config import settings
from app.deps import get_conn
from app.ratelimit import Limit, check, stats_limit

logger = logging.getLogger(__name__)
router = APIRouter()


async def record(conn, event: str) -> None:
    await conn.execute("""INSERT INTO abuse_hourly (hour, event)
        VALUES (date_trunc('hour', statement_timestamp(), 'UTC'), $1)
        ON CONFLICT (hour, event) DO UPDATE SET count = abuse_hourly.count + 1""", event)


async def sweep(pool) -> None:
    while True:
        try:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM abuse_hourly WHERE hour < now() - interval '7 days'")
                await conn.execute("DELETE FROM email_send_attempts WHERE attempted_at <= now() - interval '24 hours'")
        except Exception:
            logger.warning("abuse-state cleanup failed; will retry")
        await asyncio.sleep(60)


async def upload_guard(request, call_next):
    """Reject excess uploads before FastAPI reads/spools a multipart body.

    Both windows reserve atomically. A denied request does not spend the other
    window. Connections are returned before parsing, media work or S3 I/O.
    """
    if request.method != "POST" or request.url.path.rstrip("/") not in ("/sighting", "/capture"):
        return await call_next(request)
    try:
        observer = await require_observer(request)
        async with request.app.state.pool.acquire() as conn:
            try:
                async with conn.transaction():
                    await check(conn, f"upload:hour:{observer}", Limit(settings.rl_upload_hour_times, 3600))
                    await check(conn, f"upload:day:{observer}", Limit(settings.rl_upload_day_times, 86400))
            except HTTPException as exc:
                await record(conn, "upload_throttled")
                wait = (exc.headers or {}).get("Retry-After", "60")
                raise HTTPException(429, f"Upload limit reached. Your queued photos are safe. Try again in {wait} seconds.",
                                    headers={"Retry-After": wait}) from None
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return await call_next(request)


async def snapshot(conn, hours: int = 24) -> dict:
    started = await conn.fetchval("SELECT started_at FROM abuse_monitor_state WHERE singleton")
    rows = await conn.fetch("""
        SELECT h.hour,
            coalesce(sum(a.count) FILTER (WHERE a.event='signups'),0)::bigint AS signups,
            coalesce(sum(a.count) FILTER (WHERE a.event='failed_signins'),0)::bigint AS failed_signins,
            coalesce(sum(a.count) FILTER (WHERE a.event='auth_throttled'),0)::bigint AS auth_throttled,
            coalesce(sum(a.count) FILTER (WHERE a.event='uploads'),0)::bigint AS uploads,
            coalesce(sum(a.count) FILTER (WHERE a.event='upload_throttled'),0)::bigint AS upload_throttled,
            coalesce(sum(a.count) FILTER (WHERE a.event='email_throttled'),0)::bigint AS email_throttled,
            coalesce(sum(a.count) FILTER (WHERE a.event='email_failed'),0)::bigint AS email_failed,
            coalesce(sum(a.count) FILTER (WHERE a.event='reviewer_signins'),0)::bigint AS reviewer_signins
        FROM generate_series(
            greatest(date_trunc('hour', now(), 'UTC') - make_interval(hours => $1 - 1),
                     date_trunc('hour', $2::timestamptz, 'UTC')),
            date_trunc('hour', now(), 'UTC'), interval '1 hour') AS h(hour)
        LEFT JOIN abuse_hourly a ON a.hour=h.hour
        GROUP BY h.hour ORDER BY h.hour DESC
    """, hours, started)
    # captures includes the legacy insert trigger: one row per accepted
    # upload, however many animal sightings a multi-animal capture publishes.
    spikes = await conn.fetch("""
        SELECT o.id, o.display_name,
            count(*) FILTER (WHERE c.created_at > now()-interval '1 hour') AS recent,
            count(*) FILTER (WHERE c.created_at <= now()-interval '1 hour') AS previous
        FROM captures c JOIN observers o ON o.id=c.observer_id
        WHERE c.created_at > now()-interval '2 hours'
            AND o.deleted_at IS NULL AND o.deletion_requested_at IS NULL
        GROUP BY o.id
        HAVING count(*) FILTER (WHERE c.created_at > now()-interval '1 hour') >= $1
            AND count(*) FILTER (WHERE c.created_at > now()-interval '1 hour') >=
                2 * greatest(1, count(*) FILTER (WHERE c.created_at <= now()-interval '1 hour'))
        ORDER BY recent DESC, o.id LIMIT 20
    """, settings.abuse_upload_spike_hour)
    capacity = await conn.fetchrow("""SELECT count(*) AS reserved, count(*) FILTER (WHERE accepted) AS accepted
        FROM email_send_attempts WHERE scope=$1 AND attempted_at > now()-interval '24 hours'""", f"ses:{settings.ses_region}")
    retry_at = await conn.fetchval("SELECT next_at FROM email_delivery_state WHERE scope=$1 AND next_at > now()", f"ses:{settings.ses_region}")
    return {
        "collection_started_at": started.isoformat(),
        "hours": [{**dict(r), "hour": r["hour"].isoformat(),
                   "signup_spike": r["signups"] >= settings.abuse_signup_warn_hour,
                   "signin_spike": r["failed_signins"] >= settings.abuse_signin_warn_hour} for r in rows],
        "upload_spikes": [{**dict(r), "id": str(r["id"])} for r in spikes],
        "email_capacity": {"daily_budget": settings.email_daily_budget,
                           "reserved_last_24h": capacity["reserved"], "accepted_last_24h": capacity["accepted"],
                           "send_interval_s": settings.email_send_interval_s,
                           "retry_at": retry_at.isoformat() if retry_at else None},
        "thresholds": {"signups_per_hour": settings.abuse_signup_warn_hour,
                       "failed_signins_per_hour": settings.abuse_signin_warn_hour,
                       "upload_spike_hour": settings.abuse_upload_spike_hour},
    }


@router.get("/moderation/abuse")
async def abuse_signals(hours: int = Query(24, ge=1, le=168),
                        moderator: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    await check(conn, f"abuse:obs:{moderator}", stats_limit())
    return await snapshot(conn, hours)
