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


def test_today_is_counted_in_ist():
    from datetime import datetime, timezone
    # 20:00 UTC on the 24th is already the 25th in Bengaluru.
    assert str(post_daily.today_in("Asia/Kolkata",
                                   datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc))) == "2026-10-25"
