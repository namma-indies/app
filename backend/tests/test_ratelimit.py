"""Shared budgets, expiry and correctly keyed public and authenticated routes."""

import pytest
from starlette.requests import Request

from app.config import settings
from app.ratelimit import Limit, check, client_key


def _request(headers=None, client=("10.0.0.1", 1234)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": client, "method": "GET", "path": "/"})


async def test_allows_up_to_the_limit_then_refuses(migrated_db):
    limit = Limit(times=3, window_s=60)
    for _ in range(3):
        await check(migrated_db, "k", limit)
    with pytest.raises(Exception) as exc:
        await check(migrated_db, "k", limit)
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) > 0


async def test_keys_have_independent_budgets(migrated_db):
    limit = Limit(times=1, window_s=60)
    await check(migrated_db, "a", limit)
    await check(migrated_db, "b", limit)
    with pytest.raises(Exception):
        await check(migrated_db, "a", limit)


async def test_expired_keys_are_swept_without_evicting_live_budgets(migrated_db):
    from app.ratelimit import cleanup
    await check(migrated_db, "live", Limit(1, 3600))
    await migrated_db.execute("INSERT INTO rate_limit_buckets VALUES (decode('aa','hex'),1,now()-interval '1 second')")
    await cleanup(migrated_db)
    assert await migrated_db.fetchval("SELECT count(*) FROM rate_limit_buckets") == 1
    with pytest.raises(Exception):
        await check(migrated_db, "live", Limit(1, 3600))


def test_client_ip_ignores_headers_when_no_proxy_is_trusted(monkeypatch):
    """Locally and in tests there is no proxy, so a header is just something
    the caller typed. Trusting it would let one client be every client."""
    monkeypatch.setattr(settings, "trust_proxy_header", False)
    request = _request({"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "1.2.3.4"})
    assert client_key(request) == "10.0.0.1"


def test_client_ip_uses_the_header_the_proxy_controls(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_header", True)
    assert client_key(_request({"X-Real-IP": "203.0.113.9"})) == "203.0.113.9"


def test_trusting_the_header_still_falls_back_when_there_is_none(monkeypatch):
    """Why trusting it can be the default. With no proxy in front there is no
    header to read, so dev and tests get the peer address without configuring
    anything -- and the deployed topology, where `request.client.host` is
    Caddy's container IP and identical for every user, gets the real client."""
    monkeypatch.setattr(settings, "trust_proxy_header", True)
    assert client_key(_request()) == "10.0.0.1"


def test_without_the_header_every_caller_shares_one_key(monkeypatch):
    """The failure this default prevents. Behind a proxy the peer address is
    the proxy, so an IP-keyed limiter becomes one global bucket -- the sixth
    person to request a login link in the window gets a 429 because five
    strangers already did."""
    monkeypatch.setattr(settings, "trust_proxy_header", False)
    proxy_ip = ("172.18.0.4", 5555)
    a = client_key(_request({"X-Real-IP": "203.0.113.9"}, client=proxy_ip))
    b = client_key(_request({"X-Real-IP": "198.51.100.7"}, client=proxy_ip))
    assert a == b == "172.18.0.4"


async def test_disabling_the_limiter_is_a_single_switch(monkeypatch, migrated_db):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    limit = Limit(times=1, window_s=60)
    for _ in range(5):
        await check(migrated_db, "k", limit)


async def test_the_email_endpoint_stops_sending_after_the_limit(app_client, monkeypatch):
    """The endpoint this change exists for: it had no throttle at all."""
    monkeypatch.setattr(settings, "rl_email_addr_times", 2)
    monkeypatch.setattr(settings, "auth_email_cooldown_s", 0)
    monkeypatch.setattr(settings, "email_allowlist_domains", "example.test")
    sent = []

    class _Sender:
        async def send(self, address, link):
            sent.append(address)

    monkeypatch.setattr("app.routes.join.get_sender", lambda: _Sender())

    for _ in range(2):
        resp = await app_client.post(
            "/auth/email", data={"email": "alex@example.test"},
            headers={"accept": "application/json"},
        )
        assert resp.status_code == 200
    resp = await app_client.post(
        "/auth/email", data={"email": "alex@example.test"},
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 429
    assert len(sent) == 2


async def test_many_people_may_join_from_one_carrier_nat(app_client, monkeypatch, passcode_form):
    """Field testers recruited over WhatsApp are on mobile data, and Indian
    carriers put many subscribers behind one public address. Counting correct
    passcodes would make an onboarding push look like an attack."""
    monkeypatch.setattr(settings, "rl_join_times", 2)
    for i in range(6):
        resp = await app_client.post(
            "/auth/join",
            data=await passcode_form(app_client, name=f"tester{i}", passcode=settings.join_passcode),
            headers={"accept": "application/json"},
        )
        assert resp.status_code == 200, f"tester {i} was turned away"


async def test_wrong_passcodes_still_run_out(app_client, monkeypatch, passcode_form):
    """The budget exists; it is just spent by guesses rather than by people."""
    monkeypatch.setattr(settings, "rl_join_times", 2)
    for _ in range(2):
        resp = await app_client.post(
            "/auth/join",
            data=await passcode_form(app_client, name="x", passcode="wrong"),
            headers={"accept": "application/json"},
        )
        assert resp.status_code == 401
    resp = await app_client.post(
        "/auth/join",
        data=await passcode_form(app_client, name="x", passcode="wrong"),
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 429


async def test_one_inbox_is_protected_even_from_many_sources(app_client, monkeypatch):
    """The per-address budget is the anti-mail-bomb control, and it is tighter
    than the per-IP one on purpose -- an IP is not a person, an address is."""
    monkeypatch.setattr(settings, "rl_email_addr_times", 2)
    monkeypatch.setattr(settings, "rl_email_ip_times", 100)
    monkeypatch.setattr(settings, "auth_email_cooldown_s", 0)
    monkeypatch.setattr(settings, "email_allowlist_domains", "example.test")

    class _Sender:
        async def send(self, address, link):
            pass

    monkeypatch.setattr("app.routes.join.get_sender", lambda: _Sender())

    for _ in range(2):
        resp = await app_client.post(
            "/auth/email", data={"email": "alex@example.test"},
            headers={"accept": "application/json"},
        )
        assert resp.status_code == 200
    resp = await app_client.post(
        "/auth/email", data={"email": "alex@example.test"},
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 429


async def test_stats_is_limited_per_observer(authed_client, monkeypatch):
    monkeypatch.setattr(settings, "rl_stats_times", 2)
    client, _ = authed_client   # the fixture yields (client, observer_id)
    for _ in range(2):
        assert (await client.get("/stats")).status_code == 200
    assert (await client.get("/stats")).status_code == 429
