import asyncio
from http.cookies import SimpleCookie

import asyncpg
import pytest
from pydantic import SecretStr, ValidationError

from app.auth import store_review
from app.config import Settings, settings
from app.ids import uuid7
from app.security import issue_session, read_session

CODE = "review-only-test-code-" * 3


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "store_review_enabled", True)
    monkeypatch.setattr(settings, "store_review_secret", SecretStr(CODE))


def cookies(response):
    jar = SimpleCookie()
    for value in response.headers.get_list("set-cookie"):
        jar.load(value)
    return {"Cookie": "; ".join(f"{name}={value.value}" for name, value in jar.items())}, jar


async def signin(client, code=CODE):
    return await client.post("/auth/reviewer", data={"secret": code}, headers={"Accept": "application/json"})


async def test_default_off_and_empty_secret_cannot_sign_in(app_client):
    assert not Settings(_env_file=None).store_review_enabled
    response = await app_client.get("/auth/options")
    assert response.json() == {"store_reviewer_enabled": False}
    assert response.headers["cache-control"] == "no-store"
    assert (await signin(app_client, "")).status_code in (404, 422)
    assert (await signin(app_client)).status_code == 404


@pytest.mark.parametrize("secret", ["", "short", "न" * 64, "x" * 513])
def test_enabling_requires_a_usable_secret_and_errors_do_not_echo_it(secret):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, store_review_enabled=True, store_review_secret=secret)
    if len(secret) > 1:
        assert secret not in str(exc.value)
    assert "STORE_REVIEW_SECRET" in str(exc.value)


async def test_reviewers_need_no_inbox_and_reuse_one_account(app_client, enabled):
    first = await signin(app_client)
    second = await signin(app_client)
    assert first.status_code == second.status_code == 200
    headers, jar = cookies(first)
    _, again = cookies(second)
    assert read_session(jar["session"].value) == read_session(again["session"].value)
    assert (await app_client.get("/me", headers=headers)).json()["is_moderator"] is False
    async with app_client._transport.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM observers")
        assert row["email"] is None
        assert row["store_reviewer"] is True
        assert row["trust_tier"] != "moderator"
        assert await conn.fetchval("SELECT count(*) FROM login_tokens") == 0
    for name in ("session", store_review.COOKIE):
        assert jar[name]["secure"] and jar[name]["httponly"]
        assert jar[name]["samesite"] == "none"
        assert int(jar[name]["max-age"]) == store_review.MAX_AGE_S


async def test_concurrent_reviewers_create_exactly_one_account(app_client, enabled, monkeypatch):
    monkeypatch.setattr(settings, "rl_store_review_times", 20)
    responses = await asyncio.gather(*(signin(app_client) for _ in range(8)))
    assert all(r.status_code == 200 for r in responses)
    ids = {read_session(cookies(r)[1]["session"].value) for r in responses}
    assert len(ids) == 1
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM observers WHERE store_reviewer") == 1
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO observers (id,store_reviewer) VALUES ($1,true)", uuid7())


async def test_demo_cannot_moderate_even_if_promoted_accidentally(app_client, enabled):
    response = await signin(app_client)
    headers, jar = cookies(response)
    oid = read_session(jar["session"].value)
    async with app_client._transport.app.state.pool.acquire() as conn:
        await conn.execute("UPDATE observers SET trust_tier='moderator' WHERE id=$1", oid)
    assert (await app_client.get("/me", headers=headers)).json()["is_moderator"] is False
    assert (await app_client.get("/moderation/queue", headers=headers)).status_code == 404
    assert (await app_client.get("/moderation/abuse", headers=headers)).status_code == 404


async def test_primary_session_alone_cannot_open_demo_account(app_client, enabled):
    response = await signin(app_client)
    _, jar = cookies(response)
    oid = read_session(jar["session"].value)
    assert (await app_client.get("/me", headers={"Cookie": f"session={issue_session(oid)}"})).status_code == 401


async def test_disable_and_rotation_revoke_existing_sessions(app_client, enabled, monkeypatch):
    response = await signin(app_client)
    headers, jar = cookies(response)
    oid = read_session(jar["session"].value)
    assert (await app_client.get("/me", headers=headers)).status_code == 200
    monkeypatch.setattr(settings, "store_review_enabled", False)
    assert (await app_client.get("/me", headers=headers)).status_code == 401
    monkeypatch.setattr(settings, "store_review_enabled", True)
    monkeypatch.setattr(settings, "store_review_secret", SecretStr("a-different-review-test-code" * 3))
    assert (await app_client.get("/me", headers=headers)).status_code == 401
    next_response = await signin(app_client, "a-different-review-test-code" * 3)
    assert next_response.status_code == 200
    assert read_session(cookies(next_response)[1]["session"].value) == oid


async def test_expired_review_credential_is_rejected(app_client, enabled, monkeypatch):
    from itsdangerous import TimestampSigner
    timestamp = TimestampSigner.get_timestamp
    with monkeypatch.context() as patch:
        patch.setattr(TimestampSigner, "get_timestamp", lambda self: timestamp(self) - store_review.MAX_AGE_S - 1)
        response = await signin(app_client)
    assert (await app_client.get("/me", headers=cookies(response)[0])).status_code == 401


async def test_wrong_codes_run_out_and_audit_never_contains_credentials(app_client, enabled, caplog):
    responses = [await signin(app_client, "wrong-reviewer-code") for _ in range(7)]
    assert [r.status_code for r in responses] == [401] * 5 + [429] * 2
    assert int(responses[-1].headers["Retry-After"]) > 0
    assert "store_review_signin outcome=failed" in caplog.text
    assert CODE not in caplog.text and "wrong-reviewer-code" not in caplog.text


async def test_success_is_logged_and_counted_without_a_secret(app_client, enabled, caplog):
    response = await signin(app_client)
    assert response.status_code == 200
    assert "store_review_signin outcome=success" in caplog.text
    assert CODE not in caplog.text
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT sum(count) FROM abuse_hourly WHERE event='reviewer_signins'") == 1


async def test_logout_clears_both_credentials(app_client, enabled):
    response = await signin(app_client)
    out = await app_client.post("/auth/logout", headers={**cookies(response)[0], "Accept": "application/json"})
    _, cleared = cookies(out)
    assert int(cleared["session"]["max-age"]) == 0
    assert int(cleared[store_review.COOKIE]["max-age"]) == 0


@pytest.mark.parametrize("column", ["banned_at", "deleted_at"])
async def test_ban_or_erasure_does_not_mint_a_replacement_demo(app_client, enabled, column):
    assert (await signin(app_client)).status_code == 200
    async with app_client._transport.app.state.pool.acquire() as conn:
        await conn.execute(f"UPDATE observers SET {column}=now() WHERE store_reviewer")
    assert (await signin(app_client)).status_code == 503
    async with app_client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM observers WHERE store_reviewer") == 1


async def test_successful_signins_do_not_spend_the_attempt_limit(app_client, enabled, monkeypatch):
    monkeypatch.setattr(settings, "rl_store_review_times", 2)
    for _ in range(5):
        assert (await signin(app_client)).status_code == 200
    assert (await signin(app_client, "wrong-reviewer-code")).status_code == 401


async def _upload_as_reviewer(conn):
    oid = await conn.fetchval("SELECT id FROM observers WHERE store_reviewer AND deleted_at IS NULL")
    await conn.execute("INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2,now())", uuid7(), oid)
    return oid


async def test_reset_brings_back_a_door_the_reviewer_deleted(app_client, enabled):
    from app.deps import get_storage
    from app.media_purges import settled
    first = await signin(app_client)
    headers, _ = cookies(first)
    pool = app_client._transport.app.state.pool
    async with pool.acquire() as conn:
        old = await _upload_as_reviewer(conn)
    # The reviewer deletes the account; the grace period ends; it is erased.
    assert (await app_client.post("/account/delete", json={"confirm": "delete"}, headers=headers)).status_code == 200
    from app import accounts
    await accounts.erase_account(pool, get_storage(), old)
    await settled()
    assert (await signin(app_client)).status_code == 503

    assert await store_review.reset(pool, get_storage()) == "reset"
    await settled()
    again = await signin(app_client)
    assert again.status_code == 200
    new = read_session(cookies(again)[1]["session"].value)
    assert new != old
    assert (await app_client.get("/me", headers=headers)).status_code == 401   # old session is gone
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM observers WHERE store_reviewer") == 1
        assert await conn.fetchval("SELECT count(*) FROM sightings") == 0


async def test_reset_erases_a_live_review_account_and_starts_clean(app_client, enabled):
    from app.deps import get_storage
    from app.media_purges import settled
    pool = app_client._transport.app.state.pool
    assert (await signin(app_client)).status_code == 200
    async with pool.acquire() as conn:
        old = await _upload_as_reviewer(conn)
    assert await store_review.reset(pool, get_storage()) == "reset"
    await settled()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT deleted_at IS NOT NULL FROM observers WHERE id=$1", old)
        assert await conn.fetchval("SELECT count(*) FROM sightings") == 0
        assert await conn.fetchval("SELECT id FROM observers WHERE store_reviewer") != old


async def test_reset_can_just_cancel_a_scheduled_deletion(app_client, enabled):
    from app.deps import get_storage
    pool = app_client._transport.app.state.pool
    headers, jar = cookies(await signin(app_client))
    oid = read_session(jar["session"].value)
    async with pool.acquire() as conn:
        await _upload_as_reviewer(conn)
    await app_client.post("/account/delete", json={"confirm": "delete"}, headers=headers)
    assert await store_review.reset(pool, get_storage(), keep_content=True) == "kept"
    assert (await app_client.get("/me", headers=headers)).status_code == 200
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sightings WHERE observer_id=$1", oid) == 1
    assert await store_review.reset(pool, get_storage(), keep_content=True) == "unchanged"


async def test_reset_refuses_a_banned_reviewer_unless_told(app_client, enabled):
    from app.deps import get_storage
    pool = app_client._transport.app.state.pool
    assert (await signin(app_client)).status_code == 200
    async with pool.acquire() as conn:
        await conn.execute("UPDATE observers SET banned_at=now() WHERE store_reviewer")
    with pytest.raises(store_review.ResetRefused):
        await store_review.reset(pool, get_storage())
    assert await store_review.reset(pool, get_storage(), allow_banned=True) == "reset"
    assert (await signin(app_client)).status_code == 200
