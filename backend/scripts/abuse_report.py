"""Read-only operational report; uses the configured application database."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg

from app.abuse import snapshot
from app.config import settings


async def report(hours: int) -> None:
    conn = await asyncpg.connect(settings.database_url)
    try:
        async with conn.transaction(readonly=True):
            print(json.dumps(await snapshot(conn, hours), indent=2))
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, choices=range(1, 169))
    args = parser.parse_args()
    asyncio.run(report(args.hours))
