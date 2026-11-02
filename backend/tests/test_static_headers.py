"""Cache headers on the built frontend (issue #175): the files that name the
current build must revalidate; hashed assets may be cached forever."""

import httpx
import pytest
from fastapi import FastAPI

from app.static import FrontendStaticFiles, cache_control_for


@pytest.fixture
def client(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "icons").mkdir()
    for name in ("index.html", "sw.js", "registerSW.js", "manifest.webmanifest",
                 "workbox-abc123.js", "assets/index-AbC.js", "icons/icon-192.png"):
        (tmp_path / name).write_text("x")
    app = FastAPI()
    app.mount("/", FrontendStaticFiles(directory=tmp_path, html=True))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/index.html", "/sw.js", "/registerSW.js", "/manifest.webmanifest"])
async def test_entry_points_revalidate(client, path):
    res = await client.get(path)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/assets/index-AbC.js", "/workbox-abc123.js"])
async def test_hashed_files_are_immutable(client, path):
    res = await client.get(path)
    assert res.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.asyncio
async def test_other_files_untouched_and_conditional_requests_still_304(client):
    res = await client.get("/icons/icon-192.png")
    assert "cache-control" not in res.headers
    first = await client.get("/sw.js")
    again = await client.get("/sw.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_cache_control_for_ignores_lookalikes():
    assert cache_control_for("assets/sw.js") == "public, max-age=31536000, immutable"
    assert cache_control_for("icons/workbox-x.js") is None
