"""Self-hosted error counts. Never retain messages, requests, IDs or stack locals.

The database retains minute buckets for 14 days. A bounded in-memory buffer
keeps request handling independent of the database; at most 15 seconds of
unflushed counts can be lost on a hard process exit. Existing application logs
remain on the host, rather than being forwarded to a monitoring vendor.
"""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import logging
from threading import Lock

from app.config import settings

WINDOW_S = 900
RETENTION_DAYS = 14
FLUSH_S = 15


class ErrorCounts(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self._counts = Counter()
        self._lock = Lock()

    def record(self, kind):
        if not settings.ops_error_monitoring_enabled:
            return
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        with self._lock:
            # Even during a prolonged DB outage, memory use is bounded.
            self._prune(minute)
            self._counts[minute, kind] += 1

    def _prune(self, now):
        for key in list(self._counts):
            if key[0] < now - timedelta(seconds=WINDOW_S):
                del self._counts[key]

    def emit(self, record):
        if record.levelno >= logging.ERROR or record.exc_info:
            self.record("application_error")

    def pending(self):
        with self._lock:
            self._prune(datetime.now(timezone.utc))
            return sum(self._counts.values())

    def clear(self):
        with self._lock:
            self._counts.clear()

    async def flush_to_db(self, pool):
        with self._lock:
            batch, self._counts = self._counts, Counter()
        try:
            async with asyncio.timeout(settings.health_check_timeout_s):
                async with pool.acquire() as conn, conn.transaction():
                    await conn.executemany(
                        """INSERT INTO operational_errors (minute, kind, count) VALUES ($1, $2, $3)
                           ON CONFLICT (minute, kind) DO UPDATE
                           SET count=operational_errors.count + EXCLUDED.count""",
                        [(minute, kind, count) for (minute, kind), count in batch.items()],
                    )
                    await conn.execute(
                        "DELETE FROM operational_errors WHERE minute < now() - $1 * interval '1 day'",
                        RETENTION_DAYS,
                    )
        except (Exception, asyncio.CancelledError):
            with self._lock:
                self._counts.update(batch)
                self._prune(datetime.now(timezone.utc))
            raise


errors = ErrorCounts()


async def flush_loop(pool):
    while True:
        try:
            await errors.flush_to_db(pool)
        except Exception:
            # Outside app.* so a failed flush never creates more error events.
            logging.getLogger("operational_monitor").warning("error_counter_flush_failed")
        await asyncio.sleep(FLUSH_S)


class ErrorCaptureMiddleware:
    """Count handled 5xx responses, unhandled errors and failing background tasks."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in ("/health", "/health/detailed"):
            return await self.app(scope, receive, send)
        counted = False

        async def observed_send(message):
            nonlocal counted
            if message["type"] == "http.response.start" and message["status"] >= 500:
                errors.record("http_5xx")
                counted = True
            if (message["type"] == "http.response.start" and 200 <= message["status"] < 300
                    and scope.get("method") == "POST" and scope["path"].endswith("/fail")
                    and scope["path"].startswith(("/internal/media-jobs/", "/internal/capture-jobs/",
                                                 "/internal/transcription-jobs/", "/internal/suggestion-jobs/"))):
                # Failed attempts can become terminal before an age threshold.
                # Observe only the accepted route/status, never its job/payload.
                errors.record("application_error")
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except Exception:
            if not counted:
                errors.record("http_5xx")
            # A fixed diagnostic, with no exception text or request context.
            logging.getLogger("operational_monitor").error("unhandled_application_error")
            raise
