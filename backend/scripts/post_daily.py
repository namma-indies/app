"""Post today's @nammaindies sighting, if there is one. Run by the systemd timer
`deploy/social-post.timer` at 09:00 IST; safe to run by hand at any time --
a second run on the same day posts nothing.

    uv run python scripts/post_daily.py

With `SOCIAL_POSTING_ENABLED` unset or false this is a dry run: it prints what
would have gone out and touches nothing, which is how the first run after
setup is checked before turning posting on.

Exit status is what systemd records, so it means something: 0 for every quiet
outcome (posted, dry run, nothing queued, already posted today), 1 when a post
failed or the run refused to post because an earlier one was never confirmed
-- both need a person, and a failed unit is where they will look.
"""

import asyncio
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import effective_dsn  # noqa: E402
from app.social import RunResult, run_daily  # noqa: E402
from app.storage.s3 import storage_from_settings  # noqa: E402
from app.x_client import x_client_from_settings  # noqa: E402

NEEDS_A_PERSON = {"failed", "blocked_by_uncertain"}


def today_in(tz: str, now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date()


def exit_code(result: RunResult) -> int:
    return 1 if result.outcome in NEEDS_A_PERSON else 0


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    enabled = settings.social_posting_enabled
    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    try:
        result = await run_daily(
            pool, storage_from_settings(), x_client_from_settings() if enabled else None,
            today=today_in(settings.social_timezone), enabled=enabled)
    finally:
        await pool.close()
    print(f"{result.outcome} pick={result.pick_id} x_post_id={result.x_post_id} "
          f"detail={result.detail!r}")
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
