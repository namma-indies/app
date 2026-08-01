import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from app.db import effective_dsn
from app.deps import get_storage
from app.routes.auth import router as auth_router
from app.routes.dex import router as dex_router
from app.routes.join import router as join_router
from app.routes.sighting import router as sighting_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(effective_dsn())
    # ensure_bucket talks to S3 (or a bogus/unreachable endpoint in some
    # deployments). Storage isn't touched again until a request actually
    # needs it, so a failure here shouldn't block the app from booting --
    # log a warning and continue instead of crashing the whole process.
    try:
        await get_storage().ensure_bucket()
    except Exception:
        logger.warning("storage.ensure_bucket failed at startup; continuing", exc_info=True)
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan)
app.include_router(auth_router)
app.include_router(join_router)
app.include_router(dex_router)
app.include_router(sighting_router)


@app.exception_handler(RequestValidationError)
async def log_validation_error(request: Request, exc: RequestValidationError):
    """Record *which* field a 422 rejected, then answer exactly as FastAPI would.

    A 422 is indistinguishable from any other in the access log, so a client
    sending one malformed field looks the same as a client sending none at
    all. That gap made a real field failure -- every capture rejected, the
    app reporting only "couldn't sync" -- impossible to diagnose from the box.

    The submitted values are deliberately not logged: they carry photos and
    location. Field paths and error types are enough to identify the bug.
    """
    # Content-type and length separate the three ways a body goes wrong:
    # never sent (no length), sent unparseable (wrong/absent content-type),
    # or cut off mid-stream (length present but every field missing).
    logger.warning(
        "422 on %s %s: content_type=%r content_length=%r ua=%r errors=%s",
        request.method,
        request.url.path,
        request.headers.get("content-type"),
        request.headers.get("content-length"),
        request.headers.get("user-agent"),
        [
            {"loc": e.get("loc"), "type": e.get("type"), "msg": e.get("msg")}
            for e in exc.errors()
        ],
    )
    return await request_validation_exception_handler(request, exc)

# Serve the built frontend PWA (frontend/dist) if present, e.g. after
# `cd frontend && npm run build`. Mounted last so the API routes above still
# take priority; falls back to index.html for any other path (SPA routing).
# Guarded so tests/dev without a build don't break.
_frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
