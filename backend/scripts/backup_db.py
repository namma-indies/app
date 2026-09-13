"""Take a database backup and put it in object storage.

WHY THIS EXISTS
---------------
Everything the app knows lives in one Docker volume on one Lightsail box.
Photos are safe -- they are in S3, and the app's IAM has no DeleteObject -- but
photos are not the database. Sightings, individuals, confirmations and every
embedding are only here. The Seoul rollback box was deleted on 2026-08-16, so
there is no second copy of any of it, and the runbook names "Postgres backups"
as the restore path for an incident without anything creating them.

A photo without its sighting is an anonymous JPEG of a dog. The rows are the
part that cannot be reconstructed: which animal, seen by whom, where, and every
human verdict in `confirmations` -- which is also the calibration set the
matching thresholds are supposed to be fitted against.

WHAT IT DOES
------------
Reads a `pg_dump -Fc` archive from a path (produced by remote-backup-db.sh,
which runs pg_dump inside the db container where the binary lives) and streams
it to `backups/` in the same bucket as the photos.

Custom format, not plain SQL: it restores selectively, in parallel, and
`pg_restore --list` can verify the table of contents without restoring
anything. That verification runs before the upload, so a truncated dump is
never stored as though it were a backup.

RETENTION IS NOT HANDLED HERE, DELIBERATELY
-------------------------------------------
The app's IAM user holds List, Get and Put but no DeleteObject, which is a
property worth keeping: it is why a compromised app cannot erase the photo
corpus. That also means this script cannot prune old backups, and it should not
be given the permission to. Expire them with an S3 lifecycle rule on the
`backups/` prefix instead -- server-side, needs no credential here, and cannot
be turned against the photos.

Usage, on the box:

    uv run python scripts/backup_db.py /tmp/indiedex.dump
    uv run python scripts/backup_db.py --list
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.storage.s3 import storage_from_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backup")

PREFIX = "backups/"

# A pg_dump custom-format archive starts with the magic "PGDMP". Checking it
# here means a backup that is actually an error message -- pg_dump writing a
# failure to stdout, a redirect that captured the wrong stream -- is refused
# rather than uploaded and counted as a backup.
MAGIC = b"PGDMP"

# Smaller than this and something went wrong. An empty schema-only dump of this
# database is already several KB, so a file under 1 KB is not a database.
MIN_BYTES = 1024


def _key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{PREFIX}indiedex-{now:%Y%m%dT%H%M%SZ}.dump"


def check_dump(path: str) -> int:
    """Refuse anything that is not plausibly a pg_dump archive. Returns size."""
    size = os.path.getsize(path)
    if size < MIN_BYTES:
        raise SystemExit(f"refusing to upload {path}: {size} bytes is not a database")
    with open(path, "rb") as fh:
        head = fh.read(len(MAGIC))
    if head != MAGIC:
        raise SystemExit(
            f"refusing to upload {path}: does not start with {MAGIC!r}, so it is "
            "not a pg_dump custom-format archive. Check what the dump step wrote."
        )
    return size


async def upload(path: str) -> int:
    size = check_dump(path)
    storage = storage_from_settings()
    key = _key()
    log.info("backup: %s -> s3://%s/%s (%.1f MB)",
             path, settings.s3_bucket, key, size / 1_000_000)
    await storage.put_file(key, path, "application/octet-stream")

    # Read the size back rather than trusting the write. A backup nobody has
    # ever confirmed is there is a belief, not a backup.
    keys = await storage.list_keys(key)
    if key not in keys:
        raise SystemExit(f"upload reported success but {key} is not in the bucket")
    log.info("backup: stored %s", key)
    return 0


async def list_backups() -> int:
    storage = storage_from_settings()
    keys = sorted(await storage.list_keys(PREFIX))
    if not keys:
        log.warning("no backups in s3://%s/%s -- nothing has ever run",
                    settings.s3_bucket, PREFIX)
        return 1
    for k in keys:
        log.info("  %s", k)
    log.info("%d backup(s); newest %s", len(keys), keys[-1])
    return 0


async def fetch(key: str, dest: str) -> int:
    """Pull a backup back down, so a restore is a command rather than a project.

    A backup procedure that has a documented restore nobody can run is the same
    as no backup. This is the half that gets exercised on the worst day.
    """
    storage = storage_from_settings()
    if not key.startswith(PREFIX):
        key = PREFIX + key.lstrip("/")
    log.info("restore: s3://%s/%s -> %s", settings.s3_bucket, key, dest)
    size = await storage.get_file(key, dest)
    log.info("restore: %.1f MB", size / 1_000_000)
    # The same check the upload made, on the way back. A bucket can hand back a
    # zero-length object for a key that exists.
    check_dump(dest)
    log.info("restore: archive looks intact -- now run pg_restore, see "
             "deploy/BACKUPS.md")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", help="the pg_dump -Fc archive to upload")
    ap.add_argument("--list", action="store_true", help="list what is stored")
    ap.add_argument("--fetch", metavar="KEY",
                    help="download a stored backup instead of making one")
    ap.add_argument("--to", default="/tmp/restore.dump",
                    help="where --fetch writes (default: /tmp/restore.dump)")
    args = ap.parse_args()
    if args.list:
        raise SystemExit(asyncio.run(list_backups()))
    if args.fetch:
        raise SystemExit(asyncio.run(fetch(args.fetch, args.to)))
    if not args.path:
        ap.error("give a dump path, or --list, or --fetch KEY")
    raise SystemExit(asyncio.run(upload(args.path)))
