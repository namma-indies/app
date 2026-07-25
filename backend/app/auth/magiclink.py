import argparse
import asyncio
from datetime import datetime, timezone
from uuid import UUID

import asyncpg
from itsdangerous import BadData, URLSafeTimedSerializer

from app.config import settings
from app.ids import uuid7


class MagicLinkProvider:
    """Auth provider that resolves an observer_id from a signed, time-limited
    magic-link token. Implements the `AuthProvider` seam."""

    MAX_AGE_S = 60 * 60 * 24 * 7  # link valid 7 days

    def _serializer(self) -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(settings.magic_link_secret, salt="magic-link")

    def mint(self, observer_id: UUID) -> str:
        return self._serializer().dumps(str(observer_id))

    def verify(self, token: str, max_age_s: int | None = None) -> UUID | None:
        max_age = self.MAX_AGE_S if max_age_s is None else max_age_s
        try:
            # Load without itsdangerous's own max_age check (which has
            # only second-level resolution) and enforce expiry ourselves
            # with sub-second precision, so `max_age_s=0` reliably rejects
            # a token verified immediately after minting.
            raw, timestamp = self._serializer().loads(
                token, max_age=None, return_timestamp=True
            )
        except (BadData, ValueError):
            return None
        age_s = (datetime.now(timezone.utc) - timestamp).total_seconds()
        if age_s < 0 or age_s >= max_age:
            return None
        try:
            return UUID(raw)
        except ValueError:
            return None

    async def resolve(self, conn: asyncpg.Connection, *, token: str) -> UUID:
        observer_id = self.verify(token)
        if observer_id is None:
            raise ValueError("invalid or expired magic-link token")
        exists = await conn.fetchval(
            "SELECT count(*) FROM observers WHERE id = $1", observer_id
        )
        if not exists:
            raise ValueError("observer not found")
        return observer_id


async def create_observer(
    conn: asyncpg.Connection, *, display_name: str, created_via: str
) -> UUID:
    observer_id = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1, $2, $3)",
        observer_id,
        display_name,
        created_via,
    )
    return observer_id


async def create_observer_and_link(
    conn: asyncpg.Connection, *, display_name: str, base_url: str
) -> str:
    observer_id = await create_observer(
        conn, display_name=display_name, created_via="magic_link"
    )
    token = MagicLinkProvider().mint(observer_id)
    return f"{base_url}/auth/magic-link/consume?token={token}"


async def consume(conn: asyncpg.Connection, token: str) -> UUID | None:
    provider = MagicLinkProvider()
    try:
        return await provider.resolve(conn, token=token)
    except ValueError:
        return None


async def _mint_cli(display_name: str, base_url: str) -> None:
    conn = await asyncpg.connect(settings.database_url)
    try:
        link = await create_observer_and_link(
            conn, display_name=display_name, base_url=base_url
        )
        print(link)
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Magic-link auth CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mint_parser = subparsers.add_parser("mint", help="Create an observer and mint a magic link")
    mint_parser.add_argument("label", help="Display name for the new observer")
    mint_parser.add_argument("--base-url", default="http://localhost:8000")

    args = parser.parse_args()

    if args.command == "mint":
        asyncio.run(_mint_cli(args.label, args.base_url))
