import httpx
import pytest


@pytest.mark.asyncio
async def test_apple_app_site_association_scopes_only_auth_links():
    # Universal Links must be scoped, not "/*" -- otherwise every shared
    # sighting/dex link would also yank a user with the app installed out
    # of their browser and into the app, which nobody asked for. Only the
    # auth-continuation links (magic-link emails) should do that.
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/.well-known/apple-app-site-association")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    body = res.json()
    detail = body["applinks"]["details"][0]
    assert detail["appIDs"] == ["8365J7N4CZ.org.nammaindies.app"]
    assert detail["components"] == [{"/": "/auth/*"}]
