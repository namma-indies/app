# Email + Allowlist Sign-In Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an allowlisted `@dognosis.tech` address sign in by emailed magic link, resolving to a stable observer identity.

**Architecture:** A second door on `/join` alongside the passcode gate. Email is normalized and allowlist-checked, an observer is **looked up or created** by address (the fix for unstable identity), a single-use token row is issued, and Resend mails the link. Both doors mint the same session cookie.

**Tech Stack:** FastAPI, asyncpg, Alembic (raw SQL migrations), pytest/pytest-asyncio, Resend REST API via httpx.

**Spec:** `docs/specs/2026-07-27-email-allowlist-auth-design.md`

## Global Constraints

- Python `>=3.12`. Package manager is `uv`; run everything as `uv run ...` from `backend/`.
- Migrations are **raw SQL via `op.execute`**, not Alembic ops. Revision ids are `NNNN_snake_name`. Every migration has a working `downgrade()`.
- Tests needing Postgres request `migrated_db`, `app_client`, or `authed_client`. Pure-unit tests must request none of them, so they run without Postgres.
- `asyncio_mode = "auto"` is set; `@pytest.mark.asyncio` is still written explicitly to match existing files.
- Never log or persist a raw login token. Store only its SHA-256.
- The passcode path in `routes/join.py` keeps its create-a-new-observer-every-time behaviour. Do not "fix" it.
- Run the full suite with `uv run pytest -q` before each commit.

---

### Task 1: Migration — `observers.email` and `login_tokens`

**Files:**
- Create: `backend/migrations/versions/0003_email_auth.py`
- Modify: `backend/tests/conftest.py:66-69` and `:88-91` (add `login_tokens` to both TRUNCATE lists)
- Test: `backend/tests/test_migration.py`

**Interfaces:**
- Produces: table `login_tokens(token_hash text PK, observer_id uuid NOT NULL, expires_at timestamptz NOT NULL, used_at timestamptz, created_at timestamptz NOT NULL DEFAULT now())`; column `observers.email text` with a unique index `ux_observers_email`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_migration.py`:

```python
@pytest.mark.asyncio
async def test_observers_has_unique_email(migrated_db):
    col = await migrated_db.fetchval(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='observers' AND column_name='email'")
    assert col == 'text'
    idx = await migrated_db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename='observers'")
    assert any('email' in r['indexdef'] and 'UNIQUE' in r['indexdef'].upper() for r in idx)


@pytest.mark.asyncio
async def test_login_tokens_table_shape(migrated_db):
    cols = {r['column_name']: r['is_nullable'] for r in await migrated_db.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name='login_tokens'")}
    assert cols == {
        'token_hash': 'NO', 'observer_id': 'NO', 'expires_at': 'NO',
        'used_at': 'YES', 'created_at': 'NO',
    }


@pytest.mark.asyncio
async def test_login_tokens_cascade_on_observer_delete(migrated_db):
    from app.ids import uuid7
    oid = uuid7()
    await migrated_db.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'X','test')", oid)
    await migrated_db.execute(
        "INSERT INTO login_tokens (token_hash, observer_id, expires_at) "
        "VALUES ('deadbeef', $1, now() + interval '1 hour')", oid)
    await migrated_db.execute("DELETE FROM observers WHERE id = $1", oid)
    assert await migrated_db.fetchval("SELECT count(*) FROM login_tokens") == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_migration.py -q`
Expected: FAIL — `email` column absent, `login_tokens` missing (the shape test asserts `cols == {}` mismatch).

- [ ] **Step 3: Write the migration**

Create `backend/migrations/versions/0003_email_auth.py`:

```python
"""email sign-in: stable observer identity + single-use login tokens

The passcode gate creates a new observer on every join, so one person becomes
many identities and their sightings split. Email is a real key, so observers
can be looked up by it instead of minted blind. Login tokens are stored as
SHA-256 hashes and burned on use.

Revision ID: 0003_email_auth
Revises: 0002_dog_confidence_label
Create Date: 2026-07-27

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_email_auth"
down_revision: Union[str, None] = "0002_dog_confidence_label"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plaintext, not hashed: we need the address to send to it, so a hash
    # sitting beside the plaintext would buy nothing. Revisit at open signup.
    op.execute("ALTER TABLE observers ADD COLUMN email text;")
    # Partial unique: passcode observers have no email and must not collide
    # with each other on NULL.
    op.execute(
        "CREATE UNIQUE INDEX ux_observers_email ON observers (email) "
        "WHERE email IS NOT NULL;"
    )
    # token_hash is the PK: we look up only by hash, never by raw token.
    op.execute(
        """
        CREATE TABLE login_tokens (
            token_hash text PRIMARY KEY,
            observer_id uuid NOT NULL REFERENCES observers(id) ON DELETE CASCADE,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    # Sweeping expired tokens is the only query that isn't by PK.
    op.execute("CREATE INDEX ix_login_tokens_expires_at ON login_tokens (expires_at);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS login_tokens;")
    op.execute("DROP INDEX IF EXISTS ux_observers_email;")
    op.execute("ALTER TABLE observers DROP COLUMN IF EXISTS email;")
```

- [ ] **Step 4: Add `login_tokens` to both conftest TRUNCATE lists**

In `backend/tests/conftest.py`, both TRUNCATE statements (in `migrated_db` and `app_client`) currently read:

```python
"TRUNCATE observers, sightings, photos, embeddings, individuals, "
"match_proposals, confirmations, clinical_records RESTART IDENTITY CASCADE"
```

Change both to:

```python
"TRUNCATE observers, sightings, photos, embeddings, individuals, "
"match_proposals, confirmations, clinical_records, login_tokens "
"RESTART IDENTITY CASCADE"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_migration.py -q`
Expected: PASS.

- [ ] **Step 6: Verify the downgrade actually works**

Run:
```bash
cd backend && ALEMBIC_DB_URL="$(uv run python -c 'from app.config import settings; print(settings.test_database_url_sync)')" \
  uv run alembic downgrade 0002_dog_confidence_label && \
  ALEMBIC_DB_URL="$(uv run python -c 'from app.config import settings; print(settings.test_database_url_sync)')" \
  uv run alembic upgrade head
```
Expected: both complete without error.

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0003_email_auth.py backend/tests/test_migration.py backend/tests/conftest.py
git commit -m "feat(auth): schema for email identity and single-use login tokens"
```

---

### Task 2: Email normalization and allowlist

**Files:**
- Create: `backend/app/auth/allowlist.py`
- Modify: `backend/app/config.py:14` (add two settings beside `join_passcode`)
- Test: `backend/tests/test_allowlist.py`

**Interfaces:**
- Produces: `normalize_email(raw: str) -> str` (trims, lowercases; returns `""` if not a plausible address) and `is_allowed(email: str) -> bool`. Both pure — no DB, no I/O.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_allowlist.py`:

```python
import pytest

from app.auth.allowlist import is_allowed, normalize_email


@pytest.mark.parametrize("raw,expected", [
    ("  Akash@Dognosis.TECH ", "akash@dognosis.tech"),
    ("a@b.co", "a@b.co"),
    ("no-at-sign", ""),
    ("", ""),
    ("two@at@signs.com", ""),
    ("trailing@", ""),
    ("@leading.com", ""),
])
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


def test_domain_is_allowed(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", "dognosis.tech")
    monkeypatch.setattr(settings, "email_allowlist_addresses", "")
    assert is_allowed("akash@dognosis.tech") is True
    assert is_allowed("someone@gmail.com") is False


def test_individual_address_is_allowed(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", "dognosis.tech")
    monkeypatch.setattr(settings, "email_allowlist_addresses", "friend@gmail.com")
    assert is_allowed("friend@gmail.com") is True
    assert is_allowed("other@gmail.com") is False


def test_allowlist_entries_are_whitespace_and_case_tolerant(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", " Dognosis.tech , example.org ")
    monkeypatch.setattr(settings, "email_allowlist_addresses", " Friend@Gmail.com ")
    assert is_allowed("x@example.org") is True
    assert is_allowed("friend@gmail.com") is True


def test_empty_allowlist_allows_nobody(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", "")
    monkeypatch.setattr(settings, "email_allowlist_addresses", "")
    assert is_allowed("akash@dognosis.tech") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_allowlist.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth.allowlist'`.

- [ ] **Step 3: Add the settings**

In `backend/app/config.py`, directly below the `join_passcode` line, add:

```python
    # Comma-separated. Empty allowlist allows nobody -- fail closed.
    email_allowlist_domains: str = "dognosis.tech"
    email_allowlist_addresses: str = ""
```

- [ ] **Step 4: Write the implementation**

Create `backend/app/auth/allowlist.py`:

```python
from app.config import settings


def normalize_email(raw: str) -> str:
    """Trim and lowercase an address. Returns "" for anything that isn't a
    plausible `local@domain`, so callers can treat falsy as invalid."""
    email = raw.strip().lower()
    local, sep, domain = email.partition("@")
    if not sep or not local or not domain or "@" in domain:
        return ""
    return email


def _split(csv: str) -> set[str]:
    return {part.strip().lower() for part in csv.split(",") if part.strip()}


def is_allowed(email: str) -> bool:
    """True if the address is on the pilot allowlist. An empty allowlist
    allows nobody -- a misconfigured env var must not open the door."""
    if not email:
        return False
    domain = email.rpartition("@")[2]
    return (
        email in _split(settings.email_allowlist_addresses)
        or domain in _split(settings.email_allowlist_domains)
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_allowlist.py -q`
Expected: PASS (all cases).

- [ ] **Step 6: Commit**

```bash
git add backend/app/auth/allowlist.py backend/app/config.py backend/tests/test_allowlist.py
git commit -m "feat(auth): email normalization and fail-closed allowlist"
```

---

### Task 3: Look up or create an observer by email

**Files:**
- Create: `backend/app/auth/email_login.py`
- Test: `backend/tests/test_email_login.py`

**Interfaces:**
- Consumes: `app.ids.uuid7`.
- Produces: `async get_or_create_observer_by_email(conn, *, email: str, display_name: str | None = None) -> UUID`. Sets `created_via='email'` on creation.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_email_login.py`:

```python
import pytest

from app.auth.email_login import get_or_create_observer_by_email


@pytest.mark.asyncio
async def test_creates_observer_on_first_sight(migrated_db):
    oid = await get_or_create_observer_by_email(
        migrated_db, email="akash@dognosis.tech", display_name="Akash")
    row = await migrated_db.fetchrow(
        "SELECT email, display_name, created_via FROM observers WHERE id = $1", oid)
    assert row["email"] == "akash@dognosis.tech"
    assert row["display_name"] == "Akash"
    assert row["created_via"] == "email"


@pytest.mark.asyncio
async def test_same_email_resolves_to_same_observer(migrated_db):
    """The whole point: a returning person is one identity, not many."""
    first = await get_or_create_observer_by_email(migrated_db, email="a@dognosis.tech")
    second = await get_or_create_observer_by_email(migrated_db, email="a@dognosis.tech")
    assert first == second
    assert await migrated_db.fetchval("SELECT count(*) FROM observers") == 1


@pytest.mark.asyncio
async def test_existing_display_name_is_not_overwritten(migrated_db):
    oid = await get_or_create_observer_by_email(
        migrated_db, email="a@dognosis.tech", display_name="Akash")
    await get_or_create_observer_by_email(
        migrated_db, email="a@dognosis.tech", display_name="Someone Else")
    name = await migrated_db.fetchval(
        "SELECT display_name FROM observers WHERE id = $1", oid)
    assert name == "Akash"


@pytest.mark.asyncio
async def test_falls_back_to_local_part_when_no_display_name(migrated_db):
    oid = await get_or_create_observer_by_email(migrated_db, email="priya@dognosis.tech")
    name = await migrated_db.fetchval(
        "SELECT display_name FROM observers WHERE id = $1", oid)
    assert name == "priya"


@pytest.mark.asyncio
async def test_passcode_observers_are_untouched(migrated_db):
    """A NULL-email passcode observer must not be matched or clobbered."""
    from app.auth.magiclink import create_observer
    pc = await create_observer(migrated_db, display_name="Field Tester", created_via="passcode")
    oid = await get_or_create_observer_by_email(migrated_db, email="a@dognosis.tech")
    assert oid != pc
    assert await migrated_db.fetchval("SELECT count(*) FROM observers") == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_email_login.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth.email_login'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/auth/email_login.py`:

```python
from uuid import UUID

import asyncpg

from app.ids import uuid7


async def get_or_create_observer_by_email(
    conn: asyncpg.Connection, *, email: str, display_name: str | None = None
) -> UUID:
    """Resolve an email to a stable observer, creating one on first sight.

    This is the fix for split identity: the passcode gate mints a new observer
    per join, so one person becomes many. An address is a real key, so we look
    up by it. An existing display_name is never overwritten -- the name someone
    chose is theirs, and a later sign-in shouldn't rename them.
    """
    existing = await conn.fetchval("SELECT id FROM observers WHERE email = $1", email)
    if existing is not None:
        return existing

    observer_id = uuid7()
    name = (display_name or "").strip() or email.partition("@")[0]
    # ON CONFLICT covers two simultaneous first-time sign-ins racing on the
    # unique index; the loser reads back the winner's row.
    row = await conn.fetchrow(
        """
        INSERT INTO observers (id, email, display_name, created_via)
        VALUES ($1, $2, $3, 'email')
        ON CONFLICT (email) WHERE email IS NOT NULL DO NOTHING
        RETURNING id
        """,
        observer_id,
        email,
        name,
    )
    if row is not None:
        return row["id"]
    return await conn.fetchval("SELECT id FROM observers WHERE email = $1", email)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_email_login.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/auth/email_login.py backend/tests/test_email_login.py
git commit -m "feat(auth): resolve observers by email instead of minting per join"
```

---

### Task 4: Single-use login tokens

**Files:**
- Create: `backend/app/auth/logintoken.py`
- Test: `backend/tests/test_logintoken.py`

**Interfaces:**
- Consumes: the `login_tokens` table from Task 1.
- Produces: `async issue(conn, observer_id: UUID, ttl_s: int = 1800) -> str` returning the **raw** token for the URL; `async consume(conn, raw: str) -> UUID | None`. Module constant `DEFAULT_TTL_S = 1800`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_logintoken.py`:

```python
import pytest

from app.auth.logintoken import consume, issue


@pytest.mark.asyncio
async def test_issued_token_consumes_to_its_observer(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    raw = await issue(migrated_db, oid)
    assert await consume(migrated_db, raw) == oid


@pytest.mark.asyncio
async def test_token_is_single_use(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    raw = await issue(migrated_db, oid)
    assert await consume(migrated_db, raw) == oid
    assert await consume(migrated_db, raw) is None


@pytest.mark.asyncio
async def test_expired_token_is_rejected(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    raw = await issue(migrated_db, oid, ttl_s=-1)
    assert await consume(migrated_db, raw) is None


@pytest.mark.asyncio
async def test_garbage_token_is_rejected(migrated_db):
    assert await consume(migrated_db, "not-a-real-token") is None
    assert await consume(migrated_db, "") is None


@pytest.mark.asyncio
async def test_raw_token_is_never_stored(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    raw = await issue(migrated_db, oid)
    stored = await migrated_db.fetchval("SELECT token_hash FROM login_tokens")
    assert stored != raw
    assert len(stored) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_tokens_are_distinct(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    assert await issue(migrated_db, oid) != await issue(migrated_db, oid)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_logintoken.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth.logintoken'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/auth/logintoken.py`:

```python
import hashlib
import secrets
from uuid import UUID

import asyncpg

DEFAULT_TTL_S = 30 * 60  # emailed links are short-lived; the session cookie is the long-lived thing


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue(
    conn: asyncpg.Connection, observer_id: UUID, ttl_s: int = DEFAULT_TTL_S
) -> str:
    """Mint a single-use login token. Returns the raw token for the URL; only
    its SHA-256 is persisted, so a database leak yields nothing usable."""
    raw = secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO login_tokens (token_hash, observer_id, expires_at) "
        "VALUES ($1, $2, now() + make_interval(secs => $3))",
        _hash(raw),
        observer_id,
        ttl_s,
    )
    return raw


async def consume(conn: asyncpg.Connection, raw: str) -> UUID | None:
    """Burn a token and return its observer, or None if it is unknown,
    expired, or already used.

    Single statement on purpose: the UPDATE ... WHERE used_at IS NULL is what
    makes single-use atomic. Checking then updating would let two concurrent
    clicks both succeed.
    """
    if not raw:
        return None
    return await conn.fetchval(
        """
        UPDATE login_tokens SET used_at = now()
        WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now()
        RETURNING observer_id
        """,
        _hash(raw),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_logintoken.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/auth/logintoken.py backend/tests/test_logintoken.py
git commit -m "feat(auth): single-use, hash-at-rest login tokens"
```

---

### Task 5: Resend sender behind a seam

**Files:**
- Create: `backend/app/email/__init__.py`, `backend/app/email/sender.py`
- Modify: `backend/pyproject.toml:5-18` (move `httpx` from dev group to main dependencies), `backend/app/config.py`
- Test: `backend/tests/test_email_sender.py`

**Interfaces:**
- Produces: `class LoginEmail` protocol with `async send(to: str, link: str) -> None`; `ResendSender`; `ConsoleSender`; `get_sender() -> LoginEmail` choosing by whether `settings.resend_api_key` is set.

**Note:** `httpx` is currently a dev-only dependency but is needed at runtime for the Resend call. Move it into `[project].dependencies`, keeping the same `>=0.28.0` floor. It stays available to tests either way.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_email_sender.py`:

```python
import pytest

from app.email.sender import ConsoleSender, ResendSender, get_sender


def test_get_sender_is_console_without_api_key(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "resend_api_key", "")
    assert isinstance(get_sender(), ConsoleSender)


def test_get_sender_is_resend_with_api_key(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    assert isinstance(get_sender(), ResendSender)


@pytest.mark.asyncio
async def test_console_sender_prints_link_and_does_not_raise(capsys):
    await ConsoleSender().send("a@dognosis.tech", "https://x/consume?token=abc")
    out = capsys.readouterr().out
    assert "a@dognosis.tech" in out
    assert "https://x/consume?token=abc" in out


@pytest.mark.asyncio
async def test_resend_sender_posts_expected_payload(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "email_from", "IndieDex <hello@nammaindies.org>")
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    import app.email.sender as sender_mod
    monkeypatch.setattr(sender_mod.httpx, "AsyncClient", FakeClient)

    await ResendSender().send("a@dognosis.tech", "https://x/consume?token=abc")

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_test_key"
    assert captured["json"]["to"] == ["a@dognosis.tech"]
    assert captured["json"]["from"] == "IndieDex <hello@nammaindies.org>"
    assert "https://x/consume?token=abc" in captured["json"]["html"]
    assert "https://x/consume?token=abc" in captured["json"]["text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_email_sender.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.email'`.

- [ ] **Step 3: Move `httpx` to runtime dependencies**

In `backend/pyproject.toml`, add `"httpx>=0.28.0",` to `[project].dependencies` (alphabetically, after `fastapi`) and delete the `"httpx>=0.28.0",` line from `[dependency-groups].dev`. Then run `uv sync`.

- [ ] **Step 4: Add the settings**

In `backend/app/config.py`, below the allowlist settings from Task 2, add:

```python
    resend_api_key: str = ""  # empty => links are printed to the log, not emailed
    email_from: str = "IndieDex <hello@nammaindies.org>"
    public_base_url: str = "http://localhost:8000"
```

- [ ] **Step 5: Write the implementation**

Create `backend/app/email/__init__.py` (empty file), and `backend/app/email/sender.py`:

```python
from typing import Protocol

import httpx

from app.config import settings

SUBJECT = "Your IndieDex sign-in link"


def _bodies(link: str) -> tuple[str, str]:
    text = (
        "Tap to sign in to IndieDex:\n\n"
        f"{link}\n\n"
        "This link works once and expires in 30 minutes.\n"
        "If you didn't ask for it, you can ignore this email."
    )
    html = (
        '<div style="font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#2e2016">'
        "<p>Tap to sign in to IndieDex:</p>"
        f'<p><a href="{link}" style="background:#c15f3c;color:#fff;padding:12px 20px;'
        'border-radius:10px;text-decoration:none;display:inline-block">Sign in</a></p>'
        "<p style=\"color:#6b5844;font-size:14px\">This link works once and expires in "
        "30 minutes. If you didn't ask for it, you can ignore this email.</p>"
        "</div>"
    )
    return text, html


class LoginEmail(Protocol):
    async def send(self, to: str, link: str) -> None: ...


class ConsoleSender:
    """Used whenever RESEND_API_KEY is unset -- local dev and tests. Prints the
    link instead of sending, so the flow is exercisable without a mail account."""

    async def send(self, to: str, link: str) -> None:
        print(f"[login-email] to={to} link={link}", flush=True)


class ResendSender:
    async def send(self, to: str, link: str) -> None:
        text, html = _bodies(link)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.email_from,
                    "to": [to],
                    "subject": SUBJECT,
                    "text": text,
                    "html": html,
                },
            )
            resp.raise_for_status()


def get_sender() -> LoginEmail:
    return ResendSender() if settings.resend_api_key else ConsoleSender()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_email_sender.py -q`
Expected: PASS (4 tests).

- [ ] **Step 7: Commit**

```bash
git add backend/app/email/ backend/app/config.py backend/pyproject.toml backend/uv.lock backend/tests/test_email_sender.py
git commit -m "feat(email): Resend sender with a console fallback for dev"
```

---

### Task 6: `/join` email form and the two routes

**Files:**
- Modify: `backend/app/routes/join.py` (add email section to `_page`, add two routes)
- Test: `backend/tests/test_join_email.py`

**Interfaces:**
- Consumes: `normalize_email`/`is_allowed` (Task 2), `get_or_create_observer_by_email` (Task 3), `issue`/`consume` (Task 4), `get_sender` (Task 5), and the existing `set_session_cookie`.
- Produces: `POST /auth/email` (form field `email`) and `GET /auth/email/consume?token=...`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_join_email.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_join_email.py -q`
Expected: FAIL — no `email` input on `/join`, `/auth/email` returns 405/404.

- [ ] **Step 3: Add the email section to the page**

In `backend/app/routes/join.py`, replace the `_page` signature and its form markup. The function becomes:

```python
def _page(*, error: str | None = None, name: str = "", notice: str | None = None) -> str:
    """Server-rendered gate with two doors. Email is the identified path for the
    internal team; the passcode below it is the anonymous path for field testers
    recruited over WhatsApp, who have no company address.

    Everything interpolated here is attacker-controlled -- `name` comes straight
    off the passcode form and `error` embeds the submitted address -- so every
    value is escaped. This closes a pre-existing reflected XSS in the name field.
    """
    if notice:
        banner = f'<p class="ok">{escape(notice)}</p>'
    elif error:
        banner = f'<p class="err">{escape(error)}</p>'
    else:
        banner = '<p class="sub">Closed pilot — sign in with your work email, or use the shared passcode.</p>'
```

Add `from html import escape` to the imports, and change the name input in the
passcode form to `value="{escape(name)}"`.

The CSS moves out of the f-string into a module-level constant, so its braces
stop needing `{{`/`}}` escaping. Add this **above** `_page`, and delete the old
inline `<style>` contents:

```python
_STYLE = """
  :root { --terra:#c15f3c; --terra-dark:#a44a2b; --cream:#f4ead9; --ink:#2e2016; --line:#d8c4a6; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100dvh; display:grid; place-items:center; padding:24px;
         background:var(--cream); color:var(--ink);
         font:16px/1.5 ui-rounded,-apple-system,"Segoe UI",Roboto,sans-serif; }
  .card { width:100%; max-width:360px; background:#fffdf8; border:1px solid var(--line);
          border-radius:16px; padding:28px 24px; box-shadow:0 8px 30px rgba(46,32,22,.12); }
  h1 { margin:0 0 2px; font-size:1.4rem; letter-spacing:.02em; }
  .paw { font-size:1.6rem; }
  .sub { margin:.2rem 0 1.3rem; color:#6b5844; font-size:.92rem; }
  .err { margin:.2rem 0 1.3rem; color:var(--terra-dark); font-size:.92rem; font-weight:600; }
  .ok { margin:.2rem 0 1.3rem; color:#3d7a4a; font-size:.92rem; font-weight:600; }
  label { display:block; font-size:.8rem; font-weight:600; color:#6b5844; margin:0 0 6px; }
  input { width:100%; padding:12px 14px; margin-bottom:16px; font-size:1rem;
          border:1px solid var(--line); border-radius:10px; background:#fff; color:var(--ink); }
  input:focus { outline:2px solid var(--terra); border-color:var(--terra); }
  button { width:100%; padding:13px; font-size:1rem; font-weight:700; color:#fff; cursor:pointer;
           background:var(--terra); border:0; border-radius:10px; }
  button:active { background:var(--terra-dark); }
  .divider { display:flex; align-items:center; gap:10px; margin:22px 0 18px;
             color:#6b5844; font-size:.78rem; }
  .divider::before, .divider::after { content:""; flex:1; height:1px; background:var(--line); }
  .alt { background:none; color:var(--terra); border:1px solid var(--line); }
"""
```

Replace the single `<form class="card" ...>` element with a card containing both forms:

```python
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Join IndieDex</title>
<style>
{_STYLE}
</style>
</head>
<body>
  <div class="card">
    <div class="paw">🐾</div>
    <h1>IndieDex</h1>
    {banner}
    <form method="post" action="/auth/email">
      <label for="email">Work email</label>
      <input id="email" name="email" type="email" placeholder="you@dognosis.tech" required autocomplete="email">
      <button type="submit">Email me a link</button>
    </form>
    <div class="divider">or</div>
    <form method="post" action="/auth/join">
      <label for="name">Your name</label>
      <input id="name" name="name" value="{escape(name)}" placeholder="e.g. Priya" required autocomplete="name">
      <label for="passcode">Passcode</label>
      <input id="passcode" name="passcode" type="password" placeholder="shared code" required autocomplete="off">
      <button type="submit" class="alt">Join with passcode</button>
    </form>
  </div>
</body>
</html>"""
```

Note the outer element is now a `<div class="card">` holding two `<form>`s — the
card was previously the form itself, so that change is load-bearing, not
cosmetic.

- [ ] **Step 4: Add the two routes**

Add these imports at the top of `backend/app/routes/join.py`:

```python
from app.auth.allowlist import is_allowed, normalize_email
from app.auth.email_login import get_or_create_observer_by_email
from app.auth.logintoken import consume as consume_login_token
from app.auth.logintoken import issue as issue_login_token
from app.email.sender import get_sender
```

Append to the end of the file:

```python
@router.post("/auth/email")
async def email_submit(email: str = Form(...), conn=Depends(get_conn)):
    address = normalize_email(email)
    if not address:
        return HTMLResponse(_page(error="That doesn't look like an email address."), status_code=400)
    if not is_allowed(address):
        # Told plainly, not silently swallowed: the allowlist is a domain, not
        # a secret, and silent failure just generates "did it send?" pings.
        return HTMLResponse(
            _page(error=f"{address} isn't on the pilot list yet — ask Akash to add you."),
            status_code=403,
        )
    observer_id = await get_or_create_observer_by_email(conn, email=address)
    token = await issue_login_token(conn, observer_id)
    link = f"{settings.public_base_url}/auth/email/consume?token={token}"
    await get_sender().send(address, link)
    return HTMLResponse(_page(notice=f"Check your email — a sign-in link is on its way to {address}."))


@router.get("/auth/email/consume")
async def email_consume(token: str, conn=Depends(get_conn)):
    observer_id = await consume_login_token(conn, token)
    if observer_id is None:
        return HTMLResponse(
            _page(error="That link has expired or was already used. Request a new one."),
            status_code=401,
        )
    resp = RedirectResponse(url="/", status_code=303)
    set_session_cookie(resp, observer_id)
    return resp
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_join_email.py -q`
Expected: PASS (8 tests).

- [ ] **Step 6: Run the whole suite**

Run: `cd backend && uv run pytest -q`
Expected: PASS — including the pre-existing `test_auth_route.py`, which must be unaffected.

- [ ] **Step 7: Commit**

```bash
git add backend/app/routes/join.py backend/tests/test_join_email.py
git commit -m "feat(auth): email sign-in door on /join alongside the passcode gate"
```

---

### Task 7: Operations documentation

**Files:**
- Modify: `docs/OPERATIONS.md` (the "Auth & testers" section and its rollout ladder)

**Interfaces:**
- Consumes: everything above. No code.

- [ ] **Step 1: Update the rollout ladder**

In `docs/OPERATIONS.md`, mark stage 1 shipped and record the new env vars. Under "Auth & testers", add a third bullet beside the passcode and magic-link ones:

```markdown
- **Email + allowlist (`/join`)** — internal testers enter a work address; if it
  matches the allowlist they're emailed a single-use link (30 min). Unlike the
  passcode door, this resolves to a **stable observer** — signing in again
  reuses the same identity, so sightings stay attributed to one person.
  Observers tagged `created_via='email'`.
  - Allowlist and sender live in the box `.env`:
    ```
    EMAIL_ALLOWLIST_DOMAINS=dognosis.tech
    EMAIL_ALLOWLIST_ADDRESSES=
    RESEND_API_KEY=re_...
    EMAIL_FROM=IndieDex <hello@nammaindies.org>
    PUBLIC_BASE_URL=https://app.nammaindies.org
    ```
  - **With `RESEND_API_KEY` unset the link is printed to the container log
    instead of emailed** (`docker compose logs app | grep login-email`). That's
    the intended dev behaviour and a usable fallback if Resend is down.
  - Add someone outside the domain: append to `EMAIL_ALLOWLIST_ADDRESSES`
    (comma-separated) and `up -d app`.
```

- [ ] **Step 2: Record the identity caveat**

Add below that bullet:

```markdown
> **The two doors mean different things.** Passcode observers are anonymous and
> *unstable* — `routes/join.py` mints a new one on every join, so clearing
> cookies or reinstalling the PWA splits a person's sightings across identities.
> That is deliberate: a typed name is not a key, and matching on it would let
> anyone assume a colleague's identity. Email observers are stable. Existing
> passcode sightings are **not** merged when someone later signs in by email.
```

- [ ] **Step 3: Note the DNS and single-use dependencies**

Append to the rollout ladder section:

```markdown
Sending domain `nammaindies.org` is on Cloudflare in the **NI account**
(`Nammaindies@gmail.com's Account`), separate from the Dognosis one; zone
`a62218d755169b97600f578f3d0010e8`. DKIM/SPF/DMARC records are added there. The
API token is the `nammaindies-narrow` account token (Zone Read + DNS Write),
stored in Bitwarden as `CLOUDFLARE_API_TOKEN_DNS`.

Login links are **single-use**, which is safe because `dognosis.tech` is Google
Workspace — Gmail proxies images but does not pre-click links. Microsoft 365
Safe Links *does* pre-click and would burn tokens before the human clicks. **If
the allowlist ever gains a Microsoft-hosted domain, revisit single-use.**
```

- [ ] **Step 4: Commit**

```bash
git add docs/OPERATIONS.md
git commit -m "docs: email sign-in operations, allowlist env, identity caveat"
```

---

## Deployment (after Task 7, once Resend is set up)

Not a code task — the operator runs this.

1. Resend: add `nammaindies.org`, copy the DKIM/SPF/MX records.
2. Add those records to the Cloudflare zone, plus DMARC:
   `_dmarc.nammaindies.org TXT "v=DMARC1; p=none;"`
3. Wait for Resend to verify the domain.
4. On the box: append `RESEND_API_KEY`, `EMAIL_FROM`, `PUBLIC_BASE_URL`,
   `EMAIL_ALLOWLIST_DOMAINS` to `.env`, then
   `sudo docker compose -f docker-compose.prod.yml up -d --build`.
5. **Send yourself a test link before telling the team.** A brand-new sending
   domain mailing Google Workspace can land in spam at first; check the spam
   folder explicitly.
