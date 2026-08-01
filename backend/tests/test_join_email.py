import pytest

CAPTURED: list[tuple[str, str]] = []


class CapturingSender:
    async def send(self, to: str, link: str) -> None:
        CAPTURED.append((to, link))


@pytest.fixture(autouse=True)
def _capture_email(monkeypatch):
    CAPTURED.clear()
    import app.routes.join as join_mod
    monkeypatch.setattr(join_mod, "get_sender", lambda: CapturingSender())
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", "dognosis.tech")
    monkeypatch.setattr(settings, "email_allowlist_addresses", "")
    monkeypatch.setattr(settings, "public_base_url", "http://test")
    yield


@pytest.mark.asyncio
async def test_join_page_offers_both_doors(app_client):
    r = await app_client.get("/join")
    assert r.status_code == 200
    assert 'name="email"' in r.text
    assert 'name="passcode"' in r.text


@pytest.mark.asyncio
async def test_allowlisted_email_is_sent_a_link(app_client):
    r = await app_client.post("/auth/email", data={"email": "Akash@Dognosis.tech"})
    assert r.status_code == 200
    assert "check your email" in r.text.lower()
    assert len(CAPTURED) == 1
    to, link = CAPTURED[0]
    assert to == "akash@dognosis.tech"
    assert link.startswith("http://test/auth/email/consume?token=")


@pytest.mark.asyncio
async def test_non_allowlisted_email_is_told_so_and_sends_nothing(app_client):
    r = await app_client.post("/auth/email", data={"email": "someone@gmail.com"})
    assert r.status_code == 403
    assert "isn't on the pilot list" in r.text
    assert CAPTURED == []


@pytest.mark.asyncio
async def test_malformed_email_is_rejected(app_client):
    r = await app_client.post("/auth/email", data={"email": "not-an-email"})
    assert r.status_code == 400
    assert CAPTURED == []


@pytest.mark.asyncio
async def test_full_round_trip_sets_a_session(app_client):
    await app_client.post("/auth/email", data={"email": "akash@dognosis.tech"})
    _, link = CAPTURED[0]
    r = await app_client.get(link.replace("http://test", ""), follow_redirects=False)
    assert r.status_code == 303
    assert "session=" in r.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_link_cannot_be_reused(app_client):
    await app_client.post("/auth/email", data={"email": "akash@dognosis.tech"})
    _, link = CAPTURED[0]
    path = link.replace("http://test", "")
    assert (await app_client.get(path, follow_redirects=False)).status_code == 303
    assert (await app_client.get(path, follow_redirects=False)).status_code == 401


@pytest.mark.asyncio
async def test_signing_in_twice_reuses_one_observer(app_client):
    """The identity fix, end to end."""
    pool = app_client._transport.app.state.pool
    for _ in range(2):
        await app_client.post("/auth/email", data={"email": "akash@dognosis.tech"})
    async with pool.acquire() as c:
        count = await c.fetchval("SELECT count(*) FROM observers WHERE email IS NOT NULL")
    assert count == 1


@pytest.mark.asyncio
async def test_reflected_input_is_html_escaped(app_client):
    """Both doors echo user input back into the page; neither may inject."""
    r = await app_client.post(
        "/auth/email", data={"email": "<script>alert(1)</script>@evil.com"})
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text

    r = await app_client.post(
        "/auth/join", data={"name": "<img src=x onerror=alert(1)>", "passcode": "wrong"})
    assert "<img src=x" not in r.text


@pytest.mark.asyncio
async def test_passcode_door_still_works(app_client):
    from app.config import settings
    r = await app_client.post(
        "/auth/join",
        data={"name": "Field Tester", "passcode": settings.join_passcode},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "session=" in r.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_passcode_sightings_carry_over_on_first_email_signin(app_client):
    """A tester who logged under the passcode door with their work email as
    their name keeps that history when they later sign in properly."""
    from app.ids import uuid7
    pool = app_client._transport.app.state.pool

    async with pool.acquire() as c:
        old = uuid7()
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) "
            "VALUES ($1, 'akash@dognosis.tech', 'passcode')", old)
        await c.execute(
            "INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2, now())",
            uuid7(), old)

    await app_client.post("/auth/email", data={"email": "akash@dognosis.tech"})
    _, link = CAPTURED[0]
    r = await app_client.get(link.replace("http://test", ""), follow_redirects=False)
    assert r.status_code == 303

    async with pool.acquire() as c:
        new = await c.fetchval(
            "SELECT id FROM observers WHERE email = 'akash@dognosis.tech'")
        assert await c.fetchval(
            "SELECT count(*) FROM sightings WHERE observer_id = $1", new) == 1
        assert await c.fetchval(
            "SELECT deleted_at FROM observers WHERE id = $1", old) is not None


@pytest.mark.asyncio
async def test_no_carry_over_merely_from_requesting_a_link(app_client):
    """Typing an address proves nothing -- absorption waits for the click."""
    from app.ids import uuid7
    pool = app_client._transport.app.state.pool
    async with pool.acquire() as c:
        old = uuid7()
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) "
            "VALUES ($1, 'akash@dognosis.tech', 'passcode')", old)
        await c.execute(
            "INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2, now())",
            uuid7(), old)

    await app_client.post("/auth/email", data={"email": "akash@dognosis.tech"})

    async with pool.acquire() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM sightings WHERE observer_id = $1", old) == 1
        assert await c.fetchval(
            "SELECT deleted_at FROM observers WHERE id = $1", old) is None


@pytest.mark.asyncio
async def test_send_failure_shows_a_message_instead_of_a_stack_trace(app_client, monkeypatch):
    """A failing sender must not surface as a 500 with a traceback.

    This cost two days once: SES rejected an unverified recipient, the
    exception went unhandled, and the page just died -- so it looked like the
    button was broken rather than the mail being refused.
    """
    class ExplodingSender:
        async def send(self, to: str, link: str) -> None:
            raise RuntimeError("SES said no")

    import app.routes.join as join_mod
    monkeypatch.setattr(join_mod, "get_sender", lambda: ExplodingSender())

    r = await app_client.post("/auth/email", data={"email": "akash@dognosis.tech"})

    assert r.status_code == 500
    assert "Traceback" not in r.text
    assert "SES said no" not in r.text          # never leak internals to the page
    assert "couldn't send" in r.text.lower()
