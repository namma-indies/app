"""Deleting an account (#179): in the app, on the web, and the purge after.

The test that matters most is the second: from the moment someone asks, what
they logged is gone from every surface anyone else can read; after the purge
it is gone from every table, and what is left of them is an anonymous
tombstone that other people's records can still point at.
"""
from datetime import timedelta
from uuid import UUID

import pytest

from app import accounts
from app.accounts import OBSERVER_COLUMNS, OBSERVER_REFERENCES
from app.config import settings
from app.deps import get_storage
from app.erasure import REFERENCES
from app.ids import uuid7
from app.media_purges import settled
from tests.test_dogs_route import _post, _second_observer
from tests.test_erasure import _as, _mentions, _pool, _rich_world, _surfaces


@pytest.fixture
def mail(monkeypatch):
    """Every account mail sent, as (to, subject, text)."""
    sent = []

    async def capture(to, message):
        sent.append((to, message.subject, message.text))
    from app.routes import account
    monkeypatch.setattr(account, "send", capture)
    return sent


async def _purge_now(pool, storage, observer_id):
    async with pool.acquire() as c:
        await c.execute("UPDATE account_deletions SET purge_after = now() - interval '1 second' WHERE observer_id=$1",
                        observer_id)
    assert await accounts.purge_one(pool, storage) == observer_id
    await settled()


# --- the schema guard -----------------------------------------------------------

@pytest.mark.asyncio
async def test_every_reference_to_an_observer_and_every_column_is_classified(migrated_db):
    refs = {(r["tbl"], r["col"]) for r in await migrated_db.fetch(
        """SELECT src.relname AS tbl, att.attname AS col FROM pg_constraint con
           JOIN pg_class src ON src.oid = con.conrelid
           JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
           WHERE con.contype = 'f' AND con.confrelid = 'observers'::regclass""")}
    assert refs == set(OBSERVER_REFERENCES), (
        f"classify in app.accounts.OBSERVER_REFERENCES: {sorted(refs - set(OBSERVER_REFERENCES))}; "
        f"no longer in the schema: {sorted(set(OBSERVER_REFERENCES) - refs)}")
    columns = {r["column_name"] for r in await migrated_db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'observers'")}
    assert columns == set(OBSERVER_COLUMNS), (
        f"classify in app.accounts.OBSERVER_COLUMNS: {sorted(columns - set(OBSERVER_COLUMNS))}")


# --- in the app -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_asking_hides_everything_at_once_and_the_purge_erases_it(authed_client, mail):
    client, owner = authed_client
    w = await _rich_world(client, owner)
    pool, storage = await _pool(client), get_storage()
    async with pool.acquire() as c:
        await c.execute("UPDATE observers SET email='owner@example.org' WHERE id=$1", owner)
        # A verdict the owner gave on the viewer's pair: theirs, about the viewer's dog.
        await c.execute("""INSERT INTO confirmations (id, sighting_id, observer_id, verdict)
                           VALUES ($1,$2,$3,'different')""", uuid7(), UUID(w["theirs"]), owner)
        await c.execute("INSERT INTO individual_names (id, individual_id, name, proposed_by) VALUES ($1,$2,'Kaju',$3)",
                        uuid7(), w["dog"], owner)
    before = await _surfaces(client, w["viewer"], w["moderator"])
    assert _mentions(before["dogs"], "Owner") and _mentions(before["map"], w["mine"])

    _as(client, owner)
    assert (await client.post("/account/delete", json={"confirm": "nope"})).status_code == 422
    r = await client.post("/account/delete", json={"confirm": " Delete "})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["can_cancel"]) == ("pending_deletion", True)
    (to, subject, text), = mail
    assert to == "owner@example.org" and "will be deleted" in subject and "Keep my account" in text

    # Hidden from everyone, at once.
    pending = await _surfaces(client, w["viewer"], w["moderator"])
    for name, surface in pending.items():
        assert not _mentions(surface, w["mine"]), f"{name} still shows a pending account's sighting"
        assert not _mentions(surface, "Owner"), f"{name} still names a pending account"
    # The owner's session reaches only /account.
    _as(client, owner)
    r = await client.get("/dex")
    assert (r.status_code, r.json()["detail"]) == (403, "account_pending_deletion")
    assert (await client.get("/account")).json()["status"] == "pending_deletion"
    # Asking twice changes nothing, and sends nothing more.
    assert (await client.post("/account/delete", json={"confirm": "delete"})).status_code == 200
    assert len(mail) == 1

    # The grace period has not passed: nothing is purged.
    assert await accounts.purge_one(pool, storage) is None
    await _purge_now(pool, storage, owner)

    after = await _surfaces(client, w["viewer"], w["moderator"])
    for name, surface in after.items():
        assert not _mentions(surface, w["mine"]) and not _mentions(surface, "Owner"), name
    (dog,) = after["dogs"]
    assert (dog["id"], dog["sighting_count"]) == (str(w["dog"]), 1)          # the viewer's dog stays (Q3)
    async with pool.acquire() as c:
        for (table, column), how in OBSERVER_REFERENCES.items():
            n = await c.fetchval(f"SELECT count(*) FROM {table} WHERE {column}=$1", owner)
            if how == "erased":
                assert n == 0, f"{table}.{column} still has the deleted account's rows"
        for (table, column, target) in REFERENCES:
            if target in ("sightings", "captures"):
                assert await c.fetchval(f"SELECT count(*) FROM {table} WHERE {column}=$1", UUID(w["mine"])) == 0
        tomb = dict(await c.fetchrow("SELECT * FROM observers WHERE id=$1", owner))
        for column, how in OBSERVER_COLUMNS.items():
            if how == "emptied":
                assert tomb[column] is None, f"the tombstone keeps {column}"
        assert tomb["social_consent"] is False and tomb["deleted_at"] is not None
        # Their verdict on the viewer's pair stays, unnamed (Q5); their name claim goes (Q4).
        assert await c.fetchval("SELECT count(*) FROM confirmations WHERE observer_id=$1", owner) == 1
        assert await c.fetchval("SELECT count(*) FROM individual_names") == 0
        assert await c.fetchval("SELECT status FROM account_deletions WHERE observer_id=$1", owner) == "done"
        assert await c.fetchval("SELECT count(*) FROM media_purges") == 0
    assert await storage.list_keys(f"sightings/{w['mine']}/") == []

    # The cookie that still says who they were opens nothing.
    _as(client, owner)
    assert (await client.get("/account")).status_code == 401
    assert (await client.get("/dex")).status_code == 401
    # Signing in again with the same address is a new, empty account.
    from app.auth.email_login import get_or_create_observer_by_email
    async with pool.acquire() as c:
        again = await get_or_create_observer_by_email(c, email="owner@example.org")
    assert again != owner


@pytest.mark.asyncio
async def test_keep_my_account_brings_everything_back_unchanged(authed_client, mail):
    client, owner = authed_client
    viewer = await _second_observer(client, "Viewer")
    mine = await _post(client)
    _as(client, owner)
    assert (await client.post("/account/delete", json={"confirm": "delete"})).status_code == 200
    _as(client, viewer)
    assert mine not in {s["id"] for s in (await client.get("/map")).json()["sightings"]}
    _as(client, owner)
    r = await client.post("/account/delete/cancel")
    assert (r.status_code, r.json()["status"]) == (200, "active")
    assert mine in {s["id"] for s in (await client.get("/dex")).json()["sightings"]}
    _as(client, viewer)
    assert mine in {s["id"] for s in (await client.get("/map")).json()["sightings"]}
    pool = await _pool(client)
    assert await accounts.purge_one(pool, get_storage()) is None


@pytest.mark.asyncio
async def test_once_the_purge_has_started_it_cannot_be_cancelled(authed_client, mail):
    client, owner = authed_client
    pool = await _pool(client)
    assert (await client.post("/account/delete", json={"confirm": "delete"})).status_code == 200
    async with pool.acquire() as c:
        await c.execute("UPDATE account_deletions SET status='purging' WHERE observer_id=$1", owner)
    assert (await client.post("/account/delete/cancel")).status_code == 409
    assert (await client.get("/account")).json()["can_cancel"] is False


@pytest.mark.asyncio
async def test_a_purge_that_stops_halfway_carries_on_where_it_stopped(authed_client, mail, monkeypatch):
    client, owner = authed_client
    pool, storage = await _pool(client), get_storage()
    for _ in range(3):
        await _post(client)
    assert (await client.post("/account/delete", json={"confirm": "delete"})).status_code == 200
    from app import erasure
    real = erasure.erase
    calls = []

    async def fail_on_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("the box restarted")
        return await real(*args, **kwargs)
    monkeypatch.setattr(accounts, "erase", fail_on_second)
    async with pool.acquire() as c:
        await c.execute("UPDATE account_deletions SET purge_after = now() - interval '1 second'")
    assert await accounts.purge_one(pool, storage) is None
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT status, last_error, attempts FROM account_deletions")
        assert (row["status"], row["last_error"], row["attempts"]) == ("purging", "RuntimeError", 1)
        assert await c.fetchval("SELECT count(*) FROM captures WHERE observer_id=$1", owner) == 2
        assert await accounts.purge_one(pool, storage) is None                   # waits out its retry delay
        await c.execute("UPDATE account_deletions SET lease_until = now() - interval '1 second'")
    assert await accounts.purge_one(pool, storage) == owner
    await settled()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM captures WHERE observer_id=$1", owner) == 0
        assert await c.fetchval("SELECT status FROM account_deletions") == "done"
    await accounts.erase_account(pool, storage, owner)                          # again: nothing to do, no error


# --- on the web ------------------------------------------------------------------------

async def _link(mail) -> str:
    (_, _, text), = mail
    return next(line for line in text.splitlines() if "/account/delete/confirm?token=" in line).split("?token=")[1]


@pytest.mark.asyncio
async def test_the_web_page_reveals_nothing_about_who_has_an_account(app_client, mail):
    page = await app_client.get("/account/delete")
    assert page.status_code == 200 and "Delete your account" in page.text and "noindex" in page.text
    pool = await _pool(app_client)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO observers (id, display_name, email, created_via) VALUES ($1,'A','a@example.org','email')",
                        uuid7())
    known = await app_client.post("/account/delete/request", data={"email": "A@example.org"})
    unknown = await app_client.post("/account/delete/request", data={"email": "nobody@example.org"})
    assert known.status_code == unknown.status_code == 200
    assert known.text.replace("a@example.org", "X") == unknown.text.replace("nobody@example.org", "X")
    assert [to for to, _, _ in mail] == ["a@example.org"]
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM observers") == 1          # nothing is created


@pytest.mark.asyncio
async def test_following_the_link_deletes_nothing_and_the_button_schedules_it(app_client, mail):
    pool = await _pool(app_client)
    oid = uuid7()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO observers (id, display_name, email, created_via) VALUES ($1,'A','a@example.org','email')",
                        oid)
    await app_client.post("/account/delete/request", data={"email": "a@example.org"})
    token = await _link(mail)
    # A mail scanner opens the link; a person reads the page.
    for _ in range(2):
        page = await app_client.get(f"/account/delete/confirm?token={token}")
        assert page.status_code == 200 and "Delete my account" in page.text
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT deletion_requested_at FROM observers WHERE id=$1", oid) is None
    # The deletion link cannot sign anyone in.
    assert (await app_client.get(f"/auth/email/consume?token={token}")).status_code == 401
    done = await app_client.post("/account/delete/confirm", data={"token": token})
    assert done.status_code == 200 and "will be deleted on" in done.text
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT via FROM account_deletions WHERE observer_id=$1", oid) == "web"
    assert mail[-1][0] == "a@example.org" and "will be deleted" in mail[-1][1]
    # Used once.
    assert (await app_client.post("/account/delete/confirm", data={"token": token})).status_code == 401
    assert (await app_client.get(f"/account/delete/confirm?token={token}")).status_code == 401


@pytest.mark.asyncio
async def test_a_sign_in_link_cannot_delete_an_account(app_client):
    from app.auth.logintoken import issue
    pool = await _pool(app_client)
    oid = uuid7()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO observers (id, display_name, email, created_via) VALUES ($1,'A','a@example.org','email')",
                        oid)
        token = await issue(c, oid)
    assert (await app_client.get(f"/account/delete/confirm?token={token}")).status_code == 401
    assert (await app_client.post("/account/delete/confirm", data={"token": token})).status_code == 401
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM account_deletions") == 0


# --- after a restore ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_restored_account_is_erased_again_from_the_ledger(authed_client, monkeypatch):
    import sys
    from datetime import datetime, timezone
    from pathlib import Path
    from app.erasure import Erased, write_ledger
    client, owner = authed_client
    pool, storage = await _pool(client), get_storage()
    for key in await storage.list_keys("erasures/"):
        await storage.delete(key)
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    mine = await _post(client)
    await write_ledger(storage, Erased("account", owner))          # as if erased after the backup was taken
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import replay_erasures
    assert await replay_erasures.run(pool, storage, since=start, apply=True) == (1, 1, 1)
    await settled()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT deleted_at IS NOT NULL FROM observers WHERE id=$1", owner)
        assert not await c.fetchval("SELECT EXISTS(SELECT 1 FROM sightings WHERE id=$1)", UUID(mine))
    assert await replay_erasures.run(pool, storage, since=start, apply=True) == (1, 0, 0)
    for key in await storage.list_keys("erasures/"):
        await storage.delete(key)



@pytest.mark.asyncio
async def test_a_passcode_account_being_deleted_is_not_carried_into_an_email_one(app_client, mail):
    """A tester joined with the passcode under their email address, asked to
    delete that account, then signed in by email: the old uploads must not move
    out of the purge into the email account."""
    from app.auth.logintoken import issue
    from app.security import issue_session
    pool, storage = await _pool(app_client), get_storage()
    passcode, email_account = uuid7(), uuid7()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO observers (id, display_name, created_via) VALUES ($1,'t@example.org','passcode')",
                        passcode)
        await c.execute("INSERT INTO observers (id, display_name, email, created_via) VALUES ($1,'T','t@example.org','email')",
                        email_account)
    app_client.cookies.set("session", issue_session(passcode))
    mine = await _post(app_client)
    assert (await app_client.post("/account/delete", json={"confirm": "delete"})).status_code == 200
    async with pool.acquire() as c:
        token = await issue(c, email_account)
    app_client.cookies.clear()
    assert (await app_client.get(f"/auth/email/consume?token={token}")).status_code == 303
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT observer_id FROM sightings WHERE id=$1", UUID(mine)) == passcode
    await _purge_now(pool, storage, passcode)
    async with pool.acquire() as c:
        assert not await c.fetchval("SELECT EXISTS(SELECT 1 FROM sightings WHERE id=$1)", UUID(mine))
