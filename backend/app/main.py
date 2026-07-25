import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI
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

# Serve the built frontend PWA (frontend/dist) if present, e.g. after
# `cd frontend && npm run build`. Mounted last so the API routes above still
# take priority; falls back to index.html for any other path (SPA routing).
# Guarded so tests/dev without a build don't break.
_frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
