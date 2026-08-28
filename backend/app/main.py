import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import effective_dsn
from app.deps import get_storage
from app.routes.auth import router as auth_router
from app.routes.dex import router as dex_router
from app.routes.join import router as join_router
from app.routes.sighting import router as sighting_router
from app.routes.map import router as map_router
from app.routes.match import router as match_router
from app.routes.photo_metadata import router as photo_metadata_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # asyncpg defaults to max_size=10, and this pool is shared by two very
    # different consumers: request handlers (which hold a connection for the
    # whole request via the get_conn dependency) and the background tasks that
    # embed and match afterwards. Concurrent uploads exhaust it silently --
    # measured at 16 at once, exactly 10 were served and 6 were never handed to
    # the app at all, sitting suspended on pool.acquire() until the client gave
    # up. They do not appear in a faulthandler dump either, because a suspended
    # coroutine has no thread stack, which is what made this look like a hang
    # with no cause.
    app.state.pool = await asyncpg.create_pool(
        effective_dsn(),
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )
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
# The native iOS app's webview loads from capacitor://localhost -- a real
# cross-origin request the web PWA (same-origin) never had to make. Only
# that scheme is allowed; the web app doesn't need CORS at all.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["capacitor://localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(join_router)
app.include_router(dex_router)
app.include_router(sighting_router)
app.include_router(map_router)
app.include_router(match_router)
app.include_router(photo_metadata_router)


@app.get("/health")
async def health():
    """Liveness plus whether re-identification can actually run.

    The deploy workflow's existing check is "does the site return 200", which
    proves the app booted and nothing more. Both ML background tasks swallow a
    missing model and let the sighting save, so a box without the ONNX files
    serves perfectly good 200s while quietly embedding nothing. Reporting model
    presence here gives that check something falsifiable to look at.
    """
    from pathlib import Path

    ml = Path(__file__).resolve().parent / "ml"
    models = {
        name: (ml / name).exists()
        for name in ("miewid_msv3.onnx", "yolo26x.onnx")
    }
    return {
        "status": "ok",
        "models": models,
        # Explicit rather than inferred: "degraded" means the app is up but
        # re-ID is silently off, which is exactly the state worth alerting on.
        "reid": "ready" if all(models.values()) else "degraded",
    }


@app.get("/.well-known/apple-app-site-association")
async def apple_app_site_association():
    # Proves to iOS that this app owns this domain, so a magic-link email
    # opens directly in the native app instead of Safari (which can't share
    # the app's session cookie anyway -- separate cookie storage entirely).
    # Scoped to /auth/* only: a shared sighting or dex link shouldn't yank
    # someone with the app installed out of their browser.
    return JSONResponse(
        {
            "applinks": {
                "details": [
                    {
                        "appIDs": ["8365J7N4CZ.org.nammaindies.app"],
                        "components": [{"/": "/auth/*"}],
                    }
                ]
            }
        }
    )


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
