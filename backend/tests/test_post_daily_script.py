"""The script the systemd timer runs: exit codes are what systemd sees, so a
failed post must mark the unit failed and every quiet outcome must not."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import post_daily  # noqa: E402
from app.social import RunResult  # noqa: E402


@pytest.mark.parametrize("outcome,code", [
    ("posted", 0), ("dry_run", 0), ("already_posted_today", 0), ("empty", 0),
    ("blocked_by_uncertain", 1), ("failed", 1),
])
def test_exit_code(outcome, code):
    assert post_daily.exit_code(RunResult(outcome)) == code


def _credentials(monkeypatch, value):
    from app.config import settings
    for name in ("x_api_key", "x_api_secret", "x_access_token", "x_access_token_secret"):
        monkeypatch.setattr(settings, name, value)


@pytest.mark.parametrize("configured", [True, False])
@pytest.mark.asyncio
async def test_takedowns_run_with_posting_off_whenever_x_is_configured(monkeypatch, configured):
    """Posting switched off must not leave a deleted photo's post up on X."""
    from app.config import settings
    monkeypatch.setattr(settings, "social_posting_enabled", False)
    _credentials(monkeypatch, "set" if configured else "")
    purged, ran = [], []

    async def fake_purge(pool, client):
        purged.append(client)
        return 1, 0

    async def fake_run(pool, storage, client, *, today, enabled):
        ran.append((client, enabled))
        return RunResult("dry_run")

    class Pool:
        async def close(self):
            pass

    async def create_pool(*a, **k):
        return Pool()

    monkeypatch.setattr(post_daily, "purge_x_owed", fake_purge)
    monkeypatch.setattr(post_daily, "run_daily", fake_run)
    monkeypatch.setattr(post_daily, "storage_from_settings", lambda: None)
    monkeypatch.setattr(post_daily.asyncpg, "create_pool", create_pool)
    assert await post_daily.main() == 0
    assert len(purged) == (1 if configured else 0)
    assert ran == [(None, False)]                  # still a dry run: nothing posted


@pytest.mark.parametrize("configured", [True, False])
@pytest.mark.asyncio
async def test_the_api_tries_a_takedown_at_once_with_posting_off(monkeypatch, configured):
    from app import social
    from app.config import settings
    monkeypatch.setattr(settings, "social_posting_enabled", False)
    _credentials(monkeypatch, "set" if configured else "")
    tried = []

    async def fake_purge(pool, client, post_id):
        tried.append(post_id)
        return True

    monkeypatch.setattr(social, "purge_x_post", fake_purge)
    social.purge_x_later(None, "x-1")
    await social.x_settled()
    assert tried == (["x-1"] if configured else [])


def test_x_is_configured_only_with_all_four_credentials(monkeypatch):
    from app.config import settings
    from app.x_client import x_configured
    _credentials(monkeypatch, "set")
    assert x_configured()
    monkeypatch.setattr(settings, "x_access_token_secret", "")
    assert not x_configured()


def test_today_is_counted_in_ist():
    from datetime import datetime, timezone
    # 20:00 UTC on the 24th is already the 25th in Bengaluru.
    assert str(post_daily.today_in("Asia/Kolkata",
                                   datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc))) == "2026-10-25"
