"""Fixed-window counters, and the two ways they go wrong.

The first is memory: a map keyed by client IP with no bound is itself the
denial-of-service. The second is the key: if a client can choose it, the
limiter is decoration. Both have tests here.
"""

import pytest
from starlette.requests import Request

from app.config import settings
from app.ratelimit import Limit, check, client_key, reset


def _request(headers=None, client=("10.0.0.1", 1234)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": client, "method": "GET", "path": "/"})


@pytest.fixture(autouse=True)
def _clean():
    reset()
    yield
    reset()


def test_allows_up_to_the_limit_then_refuses():
    limit = Limit(times=3, window_s=60)
    for _ in range(3):
        check("k", limit)
    with pytest.raises(Exception) as exc:
        check("k", limit)
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) > 0


def test_keys_have_independent_budgets():
    limit = Limit(times=1, window_s=60)
    check("a", limit)
    check("b", limit)
    with pytest.raises(Exception):
        check("a", limit)


def test_the_store_stays_bounded_under_many_keys():
    """Otherwise the limiter is the attack: one request per forged key."""
    from app.ratelimit import _counter

    limit = Limit(times=10, window_s=60)
    for i in range(_counter._max_keys + 500):
        check(f"key-{i}", limit)
    assert len(_counter._hits) <= _counter._max_keys


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


def test_disabling_the_limiter_is_a_single_switch(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    limit = Limit(times=1, window_s=60)
    for _ in range(5):
        check("k", limit)


async def test_the_email_endpoint_stops_sending_after_the_limit(app_client, monkeypatch):
    """The endpoint this change exists for: it had no throttle at all."""
    monkeypatch.setattr(settings, "rl_email_times", 2)
    sent = []

    class _Sender:
        async def send(self, address, link):
            sent.append(address)

    monkeypatch.setattr("app.routes.join.get_sender", lambda: _Sender())

    for _ in range(2):
        resp = await app_client.post(
            "/auth/email", data={"email": "akash@dognosis.tech"},
            headers={"accept": "application/json"},
        )
        assert resp.status_code == 200
    resp = await app_client.post(
        "/auth/email", data={"email": "akash@dognosis.tech"},
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 429
    assert len(sent) == 2


async def test_stats_is_limited_per_observer(authed_client, monkeypatch):
    monkeypatch.setattr(settings, "rl_stats_times", 2)
    client, _ = authed_client   # the fixture yields (client, observer_id)
    for _ in range(2):
        assert (await client.get("/stats")).status_code == 200
    assert (await client.get("/stats")).status_code == 429
