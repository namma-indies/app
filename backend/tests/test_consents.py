"""Agreement before creation, verified email receipts, and version-change gates."""
import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from app import accounts, consent
from app.auth.email_login import absorb_passcode_observers, get_or_create_observer_by_email
from app.auth.logintoken import issue
from app.config import Settings, settings
from app.deps import get_storage
from app.security import issue_session


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    for key, value in {
        "consent_enabled": True, "terms_version": "terms-one", "privacy_version": "privacy-one",
        "community_guidelines_version": "guidelines-one", "consent_minimum_age": None,
        "email_allowlist_domains": "example.invalid", "email_allowlist_addresses": "",
        "public_base_url": "http://test",
    }.items():
        monkeypatch.setattr(settings, key, value)
    sent = []

    class Sender:
        async def send(self, address, link):
            sent.append((address, link))

    from app.routes import join
    monkeypatch.setattr(join, "get_sender", lambda: Sender())
    return sent


def payload(**changes):
    value = {
        "terms_version": settings.terms_version,
        "privacy_version": settings.privacy_version,
        "community_guidelines_version": settings.community_guidelines_version,
        "agree_terms_and_guidelines": True, "acknowledge_privacy": True,
        "age_confirmed": settings.consent_minimum_age is not None,
    }
    if settings.consent_minimum_age is not None:
        value["minimum_age"] = settings.consent_minimum_age
    return {**value, **changes}


def pool(client):
    return client._transport.app.state.pool


async def counts(client):
    async with pool(client).acquire() as conn:
        row = await conn.fetchrow("""SELECT (SELECT count(*) FROM observers) AS accounts,
            (SELECT count(*) FROM login_tokens) AS tokens,
            (SELECT count(*) FROM account_consents) AS receipts""")
        return tuple(row.values())


def as_account(client, observer_id):
    client.cookies.set("session", issue_session(observer_id))


def link_path(link):
    value = urlsplit(link)
    return value.path + "?" + value.query


async def join(client, fields=None, *, name="Tester"):
    return await client.post("/auth/join", headers={"Accept": "application/json"},
                             data={"name": name, "passcode": settings.join_passcode,
                                   **(payload() if fields is None else fields)})


async def signup(client, fields=None, *, address="person@example.invalid"):
    return await client.post("/auth/email", headers={"Accept": "application/json"},
                             data={"email": address, "create_account": "true",
                                   **(payload() if fields is None else fields)})


def test_configuration_defaults_off_and_requires_all_versions():
    defaults = Settings(_env_file=None)
    assert defaults.consent_enabled is False and defaults.consent_minimum_age is None
    with pytest.raises(ValidationError, match="consent requires current"):
        Settings(_env_file=None, consent_enabled=True, terms_version="one", privacy_version="one")
    for age in (0, -1, 121):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, consent_minimum_age=age)


@pytest.mark.parametrize("door", ["passcode", "email"])
@pytest.mark.parametrize("change", [
    {"agree_terms_and_guidelines": False}, {"acknowledge_privacy": False},
    {"terms_version": "old"}, {"privacy_version": "old"}, {"community_guidelines_version": "old"},
])
async def test_missing_or_stale_acceptance_never_creates_an_account(app_client, policy, door, change):
    result = await (join(app_client, payload(**change)) if door == "passcode"
                    else signup(app_client, payload(**change)))
    assert result.status_code == 428
    assert result.json()["consent_required"] is True
    assert result.json()["consent"]["terms_version"] == "terms-one"
    assert await counts(app_client) == (0, 0, 0)
    assert policy == [] and "session=" not in result.headers.get("set-cookie", "")


async def test_passcode_receipt_commits_with_creation(app_client):
    before = datetime.now(timezone.utc)
    response = await join(app_client)
    assert response.status_code == 200
    assert await counts(app_client) == (1, 0, 1)
    async with pool(app_client).acquire() as conn:
        receipt = await conn.fetchrow("SELECT * FROM account_consents")
    assert receipt["source"] == "passcode" and receipt["minimum_age"] == 0
    assert before <= receipt["accepted_at"] <= datetime.now(timezone.utc)
    as_account(app_client, receipt["observer_id"])
    assert (await app_client.get("/dex")).status_code == 200


async def test_passcode_creation_rolls_back_if_receipt_cannot_be_saved(app_client, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("receipt failed")
    monkeypatch.setattr(consent, "record_snapshot", fail)
    with pytest.raises(RuntimeError, match="receipt failed"):
        await join(app_client)
    assert await counts(app_client) == (0, 0, 0)


async def test_email_account_and_token_are_atomic(app_client, monkeypatch, policy):
    async def fail(*args, **kwargs):
        raise RuntimeError("token failed")
    from app.routes import join as join_route
    monkeypatch.setattr(join_route, "issue_login_token", fail)
    with pytest.raises(RuntimeError, match="token failed"):
        await signup(app_client)
    assert await counts(app_client) == (0, 0, 0) and policy == []


async def test_email_receipt_waits_for_its_own_verified_link(app_client, policy):
    response = await signup(app_client)
    assert response.status_code == 200
    assert await counts(app_client) == (1, 1, 0)
    first = link_path(policy[0][1])
    # A later ordinary sign-in is not evidence that the person accepted the
    # legal step attached to the first, unused link.
    await app_client.post("/auth/email", data={"email": "person@example.invalid"})
    second = link_path(policy[1][1])
    assert (await app_client.get(second)).status_code == 303
    assert await counts(app_client) == (1, 2, 0)
    async with pool(app_client).acquire() as conn:
        observer_id = await conn.fetchval("SELECT id FROM observers")
    as_account(app_client, observer_id)
    assert (await app_client.get("/account/consent")).json()["required"] is True
    assert (await app_client.get(first)).status_code == 303
    status = (await app_client.get("/account/consent")).json()
    assert status["required"] is False and status["accepted_at"]
    assert (await app_client.get(first)).status_code == 401
    assert await counts(app_client) == (1, 2, 1)


async def test_email_redemption_and_receipt_roll_back_together(app_client, policy, monkeypatch):
    await signup(app_client)
    path = link_path(policy[0][1])

    async def fail(*args, **kwargs):
        raise RuntimeError("receipt failed")
    with monkeypatch.context() as patch:
        patch.setattr(consent, "record_snapshot", fail)
        with pytest.raises(RuntimeError, match="receipt failed"):
            await app_client.get(path)
    async with pool(app_client).acquire() as conn:
        assert await conn.fetchval("SELECT used_at FROM login_tokens") is None
    assert await counts(app_client) == (1, 1, 0)
    assert (await app_client.get(path)).status_code == 303
    assert await counts(app_client) == (1, 1, 1)


async def test_signup_request_for_existing_email_cannot_accept_for_its_owner(app_client, policy):
    async with pool(app_client).acquire() as conn:
        observer_id = await get_or_create_observer_by_email(conn, email="person@example.invalid")
    await signup(app_client)
    assert await counts(app_client) == (1, 1, 0)
    as_account(app_client, observer_id)
    assert (await app_client.get("/account/consent")).json()["required"] is True
    assert (await app_client.get(link_path(policy[0][1]))).status_code == 303
    assert (await app_client.get("/account/consent")).json()["required"] is True
    assert (await app_client.post("/account/consent", json=payload())).status_code == 200
    assert (await app_client.get("/account/consent")).json()["required"] is False


async def test_known_and_unknown_signin_have_the_same_screen_without_creating_accounts(app_client, policy):
    async with pool(app_client).acquire() as conn:
        await get_or_create_observer_by_email(conn, email="known@example.invalid")
    responses = [await app_client.post("/auth/email", headers={"Accept": "application/json"}, data={"email": email})
                 for email in ("known@example.invalid", "unknown@example.invalid")]
    assert responses[0].status_code == responses[1].status_code == 200
    assert responses[0].json() == responses[1].json()
    assert await counts(app_client) == (1, 1, 0) and len(policy) == 1


async def test_parallel_email_signups_keep_one_account_and_one_receipt(app_client, policy):
    first, second = await asyncio.gather(signup(app_client), signup(app_client))
    assert first.status_code == second.status_code == 200
    assert await counts(app_client) == (1, 2, 0)
    for _, link in policy:
        assert (await app_client.get(link_path(link))).status_code == 303
    assert await counts(app_client) == (1, 2, 1)


async def test_a_policy_changed_during_email_delivery_still_requires_current_acceptance(app_client, policy, monkeypatch):
    await signup(app_client)
    monkeypatch.setattr(settings, "privacy_version", "privacy-two")
    assert (await app_client.get(link_path(policy[0][1]))).status_code == 303
    async with pool(app_client).acquire() as conn:
        old = await conn.fetchrow("SELECT observer_id, privacy_version FROM account_consents")
    assert old["privacy_version"] == "privacy-one"
    as_account(app_client, old["observer_id"])
    assert (await app_client.get("/dex")).status_code == 428
    assert (await app_client.post("/account/consent", json=payload())).status_code == 200
    assert (await app_client.get("/dex")).status_code == 200


@pytest.mark.parametrize("document", ["terms_version", "privacy_version", "community_guidelines_version"])
async def test_version_changes_prompt_once_and_keep_history(authed_client, monkeypatch, document):
    client, observer_id = authed_client
    assert (await client.get("/dex")).status_code == 428
    old_payload = payload()
    accepted = await client.post("/account/consent", json=old_payload)
    assert accepted.status_code == 200 and accepted.json()["required"] is False
    first_time = accepted.json()["accepted_at"]
    again = await client.post("/account/consent", json=old_payload)
    assert again.json()["accepted_at"] == first_time
    assert (await client.get("/account/consent")).json()["required"] is False
    monkeypatch.setattr(settings, document, "revision-two")
    assert (await client.get("/dex")).status_code == 428
    assert (await client.get("/account/consent")).json()["required"] is True
    assert (await client.post("/account/consent", json=old_payload)).status_code == 428
    assert (await client.post("/account/consent", json=payload())).json()["required"] is False
    assert (await client.get("/account/consent")).json()["required"] is False
    async with pool(client).acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM account_consents WHERE observer_id=$1", observer_id) == 2


async def test_concurrent_acceptance_is_idempotent(authed_client):
    client, _ = authed_client
    replies = await asyncio.gather(*(client.post("/account/consent", json=payload()) for _ in range(2)))
    assert all(r.status_code == 200 and r.json()["required"] is False for r in replies)
    assert replies[0].json()["accepted_at"] == replies[1].json()["accepted_at"]
    assert (await counts(client))[2] == 1


async def test_age_line_is_optional_and_rejects_missing_or_stale_assertions(app_client, monkeypatch):
    response = await app_client.get("/join?signup=1")
    assert "I am at least" not in response.text
    monkeypatch.setattr(settings, "consent_minimum_age", 18)
    assert "I am at least 18 years old" in (await app_client.get("/join?signup=1")).text
    assert (await signup(app_client, payload(age_confirmed=False))).status_code == 428
    assert (await join(app_client, payload(minimum_age=16))).status_code == 428
    assert await counts(app_client) == (0, 0, 0)
    assert (await signup(app_client)).status_code == 200


async def test_raising_age_asks_existing_accounts_again(authed_client, monkeypatch):
    client, _ = authed_client
    await client.post("/account/consent", json=payload())
    monkeypatch.setattr(settings, "consent_minimum_age", 18)
    assert (await client.get("/account/consent")).json()["required"] is True
    assert (await client.post("/account/consent", json=payload(age_confirmed=False))).status_code == 428
    assert (await client.post("/account/consent", json=payload())).json()["required"] is False
    monkeypatch.setattr(settings, "consent_minimum_age", 16)
    assert (await client.get("/account/consent")).json()["required"] is False


async def test_server_forms_show_unchecked_agreement_before_creation(app_client):
    initial = await app_client.get("/join")
    assert 'href="/join?signup=1"' in initial.text
    assert 'name="agree_terms_and_guidelines"' in initial.text  # passcode is always creation
    email = await app_client.get("/join?signup=1")
    assert email.text.count('name="agree_terms_and_guidelines"') == 2
    assert email.text.count('name="acknowledge_privacy"') == 2
    assert " checked" not in email.text
    assert email.headers["cache-control"] == "no-store"
    rejected = await app_client.post("/auth/email", data={"email": "person@example.invalid", "create_account": "true"})
    assert rejected.status_code == 428 and "Before creating your account" in rejected.text
    assert 'value="person@example.invalid"' in rejected.text
    assert await counts(app_client) == (0, 0, 0)


async def test_policy_versions_are_escaped_in_server_forms(app_client, monkeypatch):
    monkeypatch.setattr(settings, "terms_version", '\"><script>alert(1)</script>')
    html = (await app_client.get("/join?signup=1")).text
    assert '<script>alert(1)</script>' not in html
    assert "&lt;script&gt;" in html


async def test_public_configuration_has_no_account_data_but_receipts_require_a_session(app_client):
    public = await app_client.get("/auth/consent")
    assert public.status_code == 200 and public.json() == consent.current_policy()
    assert public.headers["cache-control"] == "no-store"
    assert (await app_client.get("/account/consent")).status_code == 401
    assert (await app_client.post("/account/consent", json=payload())).status_code == 401


async def test_cross_site_simple_requests_cannot_accept_for_an_account(authed_client):
    client, _ = authed_client
    form = await client.post("/account/consent", data=payload())
    no_type = await client.post("/account/consent", content=json.dumps(payload()))
    plain = await client.post("/account/consent", content=json.dumps(payload()), headers={"Content-Type": "text/plain"})
    assert form.status_code in (415, 422) and plain.status_code in (415, 422)
    assert no_type.status_code in (415, 422)
    assert (await counts(client))[2] == 0
    assert (await client.post("/account/consent", json=payload())).status_code == 200


async def test_stale_consent_does_not_block_deletion_or_logout(authed_client):
    client, _ = authed_client
    assert (await client.get("/dex")).status_code == 428
    assert (await client.get("/account")).status_code == 200
    assert (await client.post("/account/delete", json={"confirm": "delete"})).status_code == 200
    assert (await client.get("/account/consent")).status_code == 200
    assert (await client.post("/auth/logout", headers={"Accept": "application/json"})).status_code == 200


async def test_suspension_keeps_its_own_restriction_and_account_access(authed_client):
    client, observer_id = authed_client
    async with pool(client).acquire() as conn:
        await conn.execute("UPDATE observers SET banned_at=now() WHERE id=$1", observer_id)
    response = await client.get("/dex")
    assert response.status_code == 403 and response.json()["detail"] == "account_suspended"
    assert (await client.get("/account")).status_code == 200


async def test_account_erasure_removes_receipts_and_pending_agreements(authed_client):
    client, observer_id = authed_client
    await client.post("/account/consent", json=payload())
    async with pool(client).acquire() as conn, conn.transaction():
        await issue(conn, observer_id, signup_consent=consent.validate_submission(consent.ConsentSubmission(**payload())).model_dump_json())
        await accounts.request(conn, observer_id, "app")
        await conn.execute("UPDATE account_deletions SET purge_after=now() - interval '1 second' WHERE observer_id=$1", observer_id)
    assert await accounts.purge_one(pool(client), get_storage()) == observer_id
    assert await counts(client) == (1, 0, 0)


async def test_passcode_carry_over_cannot_supply_verified_email_consent(app_client):
    await join(app_client, name="person@example.invalid")
    async with pool(app_client).acquire() as conn:
        target = await get_or_create_observer_by_email(conn, email="person@example.invalid")
        assert await absorb_passcode_observers(conn, target=target, email="person@example.invalid") == 1
        assert await consent.needs_consent(conn, target) is True
        assert await conn.fetchval("SELECT count(*) FROM account_consents") == 0


async def test_disabled_consent_preserves_existing_signup_contract(app_client, monkeypatch):
    monkeypatch.setattr(settings, "consent_enabled", False)
    assert (await join(app_client, {})).status_code == 200
    assert await counts(app_client) == (1, 0, 0)
    async with pool(app_client).acquire() as conn:
        observer_id = await conn.fetchval("SELECT id FROM observers")
    as_account(app_client, observer_id)
    assert (await app_client.get("/dex")).status_code == 200
