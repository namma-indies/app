import asyncio
import hashlib
import itertools

import asyncpg
import pytest
from fastapi import HTTPException
from itsdangerous import TimestampSigner

from app.auth import proof
from app.config import Settings, settings
from app.ratelimit import Limit, check, check_backoff, key_hash, record_failure

JSON = {"Accept": "application/json"}


def solve(work):
    for n in itertools.count():
        digest = hashlib.sha256(f"{work['challenge']}:{n}".encode()).digest()
        if not int.from_bytes(digest, "big") >> (256 - work["difficulty"]):
            return str(n)


async def test_many_connections_share_one_budget_and_reconnecting_keeps_it(migrated_db):
    pool = await asyncpg.create_pool(settings.test_database_url, min_size=1, max_size=8)
    async def attempt():
        async with pool.acquire() as conn:
            try:
                await check(conn, "shared", Limit(3, 86400))
                return True
            except HTTPException as exc:
                assert exc.status_code == 429
                return False
    try:
        assert sum(await asyncio.gather(*(attempt() for _ in range(20)))) == 3
    finally:
        await pool.close()
    fresh = await asyncpg.connect(settings.test_database_url)
    try:
        with pytest.raises(HTTPException):
            await check(fresh, "shared", Limit(3, 86400))
        hashes = await fresh.fetch("SELECT key_hash FROM rate_limit_buckets")
        assert len(hashes) == 1 and len(hashes[0]["key_hash"]) == 32
    finally:
        await fresh.close()


async def test_backoff_doubles_caps_and_does_not_extend_when_checked(migrated_db):
    waits = []
    for n in range(1, 14):
        try:
            await record_failure(migrated_db, "failures")
        except HTTPException as exc:
            waits.append(int(exc.headers["Retry-After"]))
            until = await migrated_db.fetchval("SELECT blocked_until FROM auth_failures")
            with pytest.raises(HTTPException):
                await check_backoff(migrated_db, "failures")
            assert await migrated_db.fetchval("SELECT blocked_until FROM auth_failures") == until
        await migrated_db.execute("UPDATE auth_failures SET blocked_until=now()-interval '1 second'")
    assert waits[:3] == [5, 10, 20]
    assert waits[-1] == 900
    await migrated_db.execute("UPDATE auth_failures SET expires_at=now()-interval '1 second'")
    await record_failure(migrated_db, "failures")
    assert await migrated_db.fetchval("SELECT failures FROM auth_failures") == 1


async def test_locked_state_survives_a_new_connection(migrated_db):
    for _ in range(2):
        await record_failure(migrated_db, "source")
    with pytest.raises(HTTPException):
        await record_failure(migrated_db, "source")
    other = await asyncpg.connect(settings.test_database_url)
    try:
        with pytest.raises(HTTPException) as exc:
            await check_backoff(other, "source")
        assert int(exc.value.headers["Retry-After"]) > 0
    finally:
        await other.close()


async def test_passcode_requires_work_even_with_the_right_secret(app_client):
    response = await app_client.post("/auth/join", data={"name": "Tester", "passcode": settings.join_passcode}, headers=JSON)
    assert response.status_code == 400
    assert "session=" not in response.headers.get("set-cookie", "")


async def test_non_ascii_wrong_passcode_is_a_normal_failure(app_client, passcode_form):
    form = await passcode_form(app_client, name="Tester", passcode="नहीं")
    response = await app_client.post("/auth/join", data=form, headers=JSON)
    assert response.status_code == 401


async def test_one_proof_cannot_create_two_accounts_concurrently(app_client, passcode_form):
    form = await passcode_form(app_client, name="Tester", passcode=settings.join_passcode)
    replies = await asyncio.gather(*(app_client.post("/auth/join", data=form, headers=JSON) for _ in range(2)))
    assert sorted(r.status_code for r in replies) == [200, 400]
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM observers") == 1


async def test_work_is_bound_to_the_client_and_rejects_tampering(migrated_db):
    work = proof.issue("client-a")
    solution = solve(work)
    with pytest.raises(HTTPException):
        await proof.consume(migrated_db, "client-b", work["challenge"], solution)
    with pytest.raises(HTTPException):
        await proof.consume(migrated_db, "client-a", work["challenge"] + "tampered", solution)
    await proof.consume(migrated_db, "client-a", work["challenge"], solution)
    with pytest.raises(HTTPException):
        await proof.consume(migrated_db, "client-a", work["challenge"], solution)


async def test_expired_work_is_refused(migrated_db, monkeypatch):
    timestamp = TimestampSigner.get_timestamp
    with monkeypatch.context() as patch:
        patch.setattr(TimestampSigner, "get_timestamp", lambda self: timestamp(self) - 601)
        work = proof.issue("client")
    with pytest.raises(HTTPException):
        await proof.consume(migrated_db, "client", work["challenge"], solve(work))


@pytest.mark.parametrize("path", ["/auth/email/consume", "/auth/magic-link/consume"])
async def test_invalid_signin_tokens_get_a_plain_window_not_backoff(app_client, monkeypatch, path):
    """Tokens are high-entropy: backoff would only lock out a carrier NAT."""
    monkeypatch.setattr(settings, "rl_consume_times", 5)
    replies = [await app_client.get(path, params={"token": "invalid"}, headers=JSON) for _ in range(6)]
    assert [r.status_code for r in replies] == [401] * 5 + [429]
    assert int(replies[-1].headers["Retry-After"]) > 0
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM auth_failures") == 0


async def test_parallel_wrong_guesses_are_bounded_by_the_budget(app_client, monkeypatch, passcode_form):
    """The budget is charged before the compare, atomically, so N parallel
    guesses cannot all be compared."""
    monkeypatch.setattr(settings, "rl_join_times", 3)
    forms = [await passcode_form(app_client, name="x", passcode="wrong") for _ in range(10)]
    replies = await asyncio.gather(*(app_client.post("/auth/join", data=f, headers=JSON) for f in forms))
    assert sorted(r.status_code for r in replies) == [401] * 3 + [429] * 7


async def test_the_global_wrong_passcode_budget_pauses_the_door(app_client, monkeypatch, passcode_form, caplog):
    monkeypatch.setattr(settings, "rl_join_global_wrong_times", 2)
    for _ in range(2):
        form = await passcode_form(app_client, name="x", passcode="wrong")
        assert (await app_client.post("/auth/join", data=form, headers=JSON)).status_code == 401
    assert "global wrong-passcode budget reached" in caplog.text
    form = await passcode_form(app_client, name="Tester", passcode=settings.join_passcode)
    refused = await app_client.post("/auth/join", data=form, headers=JSON)
    assert refused.status_code == 429
    assert "email" in refused.json()["error"]
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM observers") == 0
        assert await conn.fetchval("SELECT sum(count) FROM abuse_hourly WHERE event='passcode_lockouts'") == 1


async def test_correct_passcodes_do_not_spend_the_global_budget(app_client, monkeypatch, passcode_form):
    monkeypatch.setattr(settings, "rl_join_global_wrong_times", 1)
    for i in range(3):
        form = await passcode_form(app_client, name=f"tester{i}", passcode=settings.join_passcode)
        assert (await app_client.post("/auth/join", data=form, headers=JSON)).status_code == 200


async def test_successful_joins_are_capped_per_source_per_day(app_client, monkeypatch, passcode_form):
    monkeypatch.setattr(settings, "rl_join_success_ip_day_times", 2)
    codes = []
    for i in range(3):
        form = await passcode_form(app_client, name=f"tester{i}", passcode=settings.join_passcode)
        codes.append((await app_client.post("/auth/join", data=form, headers=JSON)).status_code)
    assert codes == [200, 200, 429]


async def test_a_stale_but_genuine_proof_does_not_count_toward_backoff(app_client, monkeypatch):
    """Expired, issued to another address, or already spent: ask for a new
    proof, never lock the source out."""
    monkeypatch.setattr(settings, "auth_ip_backoff_after", 1)
    timestamp = TimestampSigner.get_timestamp
    with monkeypatch.context() as patch:
        patch.setattr(TimestampSigner, "get_timestamp", lambda self: timestamp(self) - 601)
        old = (await app_client.get("/auth/join/challenge")).json()
    elsewhere = proof.issue("198.51.100.7")
    for work in (old, elsewhere):
        form = {"name": "x", "passcode": "wrong", "challenge": work["challenge"], "solution": solve(work)}
        reply = await app_client.post("/auth/join", data=form, headers=JSON)
        assert reply.status_code == 400
        assert "expired" in reply.json()["error"]
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM auth_failures") == 0
    # A forged proof still counts.
    reply = await app_client.post("/auth/join", data={"name": "x", "passcode": "wrong",
                                  "challenge": "forged", "solution": "1"}, headers=JSON)
    assert reply.status_code == 429


async def test_ip_keyed_backoff_waits_for_the_ip_threshold(migrated_db, monkeypatch):
    monkeypatch.setattr(settings, "auth_ip_backoff_after", 4)
    for _ in range(3):
        await record_failure(migrated_db, "ip", after=settings.auth_ip_backoff_after)
    with pytest.raises(HTTPException):
        await record_failure(migrated_db, "ip", after=settings.auth_ip_backoff_after)


def test_ip_backoff_threshold_defaults_high():
    assert Settings(_env_file=None).auth_ip_backoff_after == 10


async def test_an_address_not_on_the_list_is_not_a_failed_signin(app_client, monkeypatch):
    monkeypatch.setattr(settings, "email_allowlist_domains", "example.test")
    monkeypatch.setattr(settings, "auth_ip_backoff_after", 1)
    for _ in range(3):
        reply = await app_client.post("/auth/email", data={"email": "someone@elsewhere.test"}, headers=JSON)
        assert reply.status_code == 403
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM auth_failures") == 0
        assert await conn.fetchval("SELECT count(*) FROM abuse_hourly WHERE event='failed_signins'") == 0


async def test_only_refused_credentials_are_audited_as_failed_signins(app_client, passcode_form):
    await app_client.post("/auth/email", data={"email": "not-an-address"}, headers=JSON)  # 400
    await app_client.post("/auth/reviewer", data={"secret": "x"}, headers=JSON)  # door off: 404
    await app_client.post("/auth/join", data={"name": "x", "passcode": "y"}, headers=JSON)  # no proof: 400
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM abuse_hourly WHERE event='failed_signins'") == 0
    form = await passcode_form(app_client, name="x", passcode="wrong")
    assert (await app_client.post("/auth/join", data=form, headers=JSON)).status_code == 401
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT sum(count) FROM abuse_hourly WHERE event='failed_signins'") == 1


async def test_address_cooldown_applies_across_sources_and_email_case(app_client, monkeypatch):
    monkeypatch.setattr(settings, "email_allowlist_domains", "example.test")
    sent = []
    class Sender:
        async def send(self, address, link):
            sent.append(address)
    monkeypatch.setattr("app.routes.join.get_sender", Sender)
    first = await app_client.post("/auth/email", data={"email": "Person@Example.test"}, headers={**JSON, "X-Real-IP": "192.0.2.1"})
    second = await app_client.post("/auth/email", data={"email": "person@example.test"}, headers={**JSON, "X-Real-IP": "192.0.2.2"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert "wait" in second.json()["error"].lower()
    assert int(second.headers["Retry-After"]) > 0
    assert sent == ["person@example.test"]
    async with app_client._transport.app.state.pool.acquire() as conn:
        hashes = await conn.fetch("SELECT key_hash FROM rate_limit_buckets")
        assert all(len(row["key_hash"]) == 32 for row in hashes)


def test_open_signup_is_still_off_by_default():
    assert Settings(_env_file=None).email_open_signup is False
