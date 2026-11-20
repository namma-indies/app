"""Bring the store-review door back after the review account was deleted.

Store reviewers are asked to test account deletion, and do. Once the review
account is erased (or banned), the reviewer door refuses to mint a
replacement on its own -- by design, so a ban or a deletion cannot be undone
by signing in again -- and answers 503 until this is run.

Default: erase whatever the review account still holds, exactly as an
account deletion would, retire it, and create a fresh, empty reviewer
identity. Existing reviewer sessions stop working; the reviewer code is
unchanged (rotate STORE_REVIEW_SECRET separately if it should be).

    --keep-content   only cancel a deletion that is still in its grace
                     period; keep the account and its uploads
    --allow-banned   reset even if a moderator banned the review account

Usage, from /app/backend inside the container:

    uv run python scripts/reset_store_reviewer.py
    uv run python scripts/reset_store_reviewer.py --keep-content
"""

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import effective_dsn  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-content", action="store_true",
                    help="only cancel a scheduled deletion; keep the account and its uploads")
    ap.add_argument("--allow-banned", action="store_true", help="reset even a banned review account")
    args = ap.parse_args()
    from app.auth.store_review import ResetRefused, reset
    from app.deps import get_storage
    from app.media_purges import settled
    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    try:
        outcome = await reset(pool, get_storage(), keep_content=args.keep_content,
                              allow_banned=args.allow_banned)
        await settled()
    except ResetRefused as exc:
        print(f"NOT RESET: {exc}", file=sys.stderr)
        return 1
    finally:
        await pool.close()
    print({"created": "No review account existed; created one.",
           "reset": "Review account erased and replaced with a fresh one.",
           "kept": "Scheduled deletion cancelled; the review account is kept.",
           "unchanged": "The review account is in use and not scheduled for deletion; nothing to do."}[outcome])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
