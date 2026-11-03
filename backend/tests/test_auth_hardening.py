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
async def test_invalid_signin_tokens_trigger_backoff(app_client, path):
    replies = [await app_client.get(path, params={"token": "invalid"}, headers=JSON) for _ in range(4)]
    assert [r.status_code for r in replies] == [401, 401, 429, 429]
    assert int(replies[-1].headers["Retry-After"]) > 0


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
