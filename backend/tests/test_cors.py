import httpx
import pytest


@pytest.mark.asyncio
async def test_allows_the_native_app_origin_with_credentials():
    # A CORS preflight is answered entirely by CORSMiddleware before the
    # request ever reaches a route handler, so this doesn't need a DB pool.
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.options(
            "/dex",
            headers={
                "Origin": "capacitor://localhost",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert res.headers["access-control-allow-origin"] == "capacitor://localhost"
    assert res.headers["access-control-allow-credentials"] == "true"


@pytest.mark.asyncio
async def test_rejects_an_unrecognized_origin():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.options(
            "/dex",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-origin" not in res.headers
