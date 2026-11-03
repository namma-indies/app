"""Restore the newest backup into disposable Postgres; never a live database.

Requires Docker and the AWS CLI. No application configuration or .env is read.
There is deliberately no database URL, host, port or container-name argument.
Use --archive for synthetic/local verification without any S3 access.
"""
import argparse
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ops_common import OpsError, aws_command

BACKEND = Path(__file__).resolve().parents[1]
BACKUP_KEY = re.compile(r"backups/indiedex-(\d{8}T\d{6}Z)\.dump\Z")
CORE_TABLES = ("observers", "sightings", "captures", "photos", "individuals",
               "confirmations", "embeddings", "alembic_version")


def newest_backup(objects, *, now, max_age_hours):
    candidates = [obj for obj in objects if BACKUP_KEY.fullmatch(obj.get("Key", ""))]
    if not candidates:
        raise OpsError("backup_missing")
    latest = max(candidates, key=lambda obj: obj["Key"])
    if latest.get("Size", 0) < 1024:
        raise OpsError("backup_invalid")
    stamp = datetime.strptime(BACKUP_KEY.fullmatch(latest["Key"])[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    age_s = (now - stamp).total_seconds()
    if age_s < -300 or age_s > max_age_hours * 3600:
        raise OpsError("backup_not_fresh")
    return latest


def fetch_latest(destination, *, max_age_hours):
    bucket = os.environ.get("RESTORE_BACKUP_BUCKET", "")
    endpoint = os.environ.get("RESTORE_BACKUP_ENDPOINT", "")
    if not bucket:
        raise OpsError("backup_configuration_missing")
    if endpoint:
        from scripts.check_launch import secure_url
        secure_url(endpoint, base=True)
    objects, continuation = [], None
    while True:
        args = ["list-objects-v2", "--bucket", bucket, "--prefix", "backups/", "--no-paginate"]
        if continuation:
            args += ["--continuation-token", continuation]
        page = aws_command(args, endpoint=endpoint)
        objects.extend(page.get("Contents", []))
        if not page.get("IsTruncated"):
            break
        following = page.get("NextContinuationToken")
        if not following or following == continuation:
            raise OpsError("backup_inventory_invalid")
        continuation = following
    latest = newest_backup(objects, now=datetime.now(timezone.utc), max_age_hours=max_age_hours)
    # No bucket, object key, inventory or response is printed to Actions logs.
    destination.touch(mode=0o600)
    args = ["get-object", "--bucket", bucket, "--key", latest["Key"]]
    if latest.get("ETag"):
        args += ["--if-match", latest["ETag"]]
    aws_command([*args, str(destination)], endpoint=endpoint)
    if destination.stat().st_size != latest["Size"]:
        raise OpsError("backup_size_mismatch")


def command(args, *, stdin=None, timeout=60):
    try:
        result = subprocess.run(args, stdin=stdin, capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise OpsError("drill_command_failed") from None
    if result.returncode:
        # pg_restore/psql can print COPY rows and identifying values on error.
        # Keep all raw tool output out of public logs, including on failures.
        raise OpsError("drill_command_failed")
    return result.stdout


def known_revisions():
    revisions = set()
    for path in (BACKEND / "migrations" / "versions").glob("*.py"):
        for node in ast.parse(path.read_text()).body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            if any(isinstance(target, ast.Name) and target.id == "revision" for target in targets):
                revisions.add(ast.literal_eval(node.value))
    return revisions


def check_archive(path):
    if path.stat().st_size < 1024:
        raise OpsError("archive_invalid")
    with path.open("rb") as source:
        if source.read(5) != b"PGDMP":
            raise OpsError("archive_invalid")


def restore(archive, *, image="indiedex-restore-db", minimum_sightings=1,
            minimum_embeddings=1, data_mib=2048):
    check_archive(archive)
    name = "indiedex-restore-drill-" + uuid4().hex
    created = False
    try:
        # No host mounts, published ports, network, credentials, or durable DB
        # volume. Even SQL functions in an archive are confined to this sandbox.
        # The AO label also allows session cleanup if the invoking worker dies.
        command(["docker", "run", "--detach", "--pull", "never", "--name", name,
                 "--label", "ao.session=" + os.environ.get("AO_SESSION_ID", "restore-drill"),
                 "--network", "none", "--security-opt", "no-new-privileges:true",
                 "--pids-limit", "256", "--memory", f"{data_mib + 1024}m",
                 "--tmpfs", f"/var/lib/postgresql/data:rw,nosuid,size={data_mib}m",
                 "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
                 "--env", "POSTGRES_DB=restore_drill", image])
        created = True
        deadline = time.monotonic() + 60
        while True:
            try:
                command(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1",
                         "-U", "postgres", "-d", "restore_drill"])
                break
            except OpsError:
                if time.monotonic() >= deadline:
                    raise OpsError("drill_database_not_ready") from None
                time.sleep(1)
        with archive.open("rb") as source:
            toc = command(["docker", "exec", "-i", name, "pg_restore", "--list"], stdin=source)
        if any((" TABLE DATA public " + table + " ").encode() not in toc for table in CORE_TABLES):
            raise OpsError("archive_missing_table_data")
        with archive.open("rb") as source:
            command(["docker", "exec", "-i", name, "pg_restore", "--exit-on-error",
                     "--single-transaction", "--no-owner", "--no-privileges", "-U", "postgres",
                     "--dbname", "restore_drill"], stdin=source, timeout=600)
        sql = """
            SET statement_timeout = '60s';
            SELECT json_build_object(
                'extensions', (SELECT count(*) = 2 FROM pg_extension WHERE extname IN ('postgis','vector')),
                'one_revision', (SELECT count(*) = 1 FROM alembic_version),
                'revision', (SELECT min(version_num) FROM alembic_version),
                'sightings', (SELECT count(*) FROM sightings),
                'photos', (SELECT count(*) FROM photos),
                'individuals', (SELECT count(*) FROM individuals),
                'confirmations', (SELECT count(*) FROM confirmations),
                'embeddings', (SELECT count(*) FROM embeddings WHERE vec_miew IS NOT NULL),
                'vectors_valid', NOT EXISTS (SELECT 1 FROM embeddings WHERE vec_miew IS NOT NULL AND vector_dims(vec_miew) <> 2152),
                'constraints_valid', NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
                                                WHERE n.nspname='public' AND c.contype='f' AND NOT c.convalidated),
                'photo_owners_valid', NOT EXISTS (
                    SELECT 1 FROM photos p WHERE
                    (p.sighting_id IS NULL AND p.capture_id IS NULL) OR
                    (p.sighting_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sightings s WHERE s.id=p.sighting_id)) OR
                    (p.capture_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM captures c WHERE c.id=p.capture_id)))
            );
        """
        raw = command(["docker", "exec", name, "psql", "-X", "-qAt", "-U", "postgres",
                       "-d", "restore_drill", "-v", "ON_ERROR_STOP=1", "-c", sql])
        try:
            result = json.loads(raw)
            if (not all(result[key] is True for key in ("extensions", "one_revision", "vectors_valid",
                                                      "constraints_valid", "photo_owners_valid"))
                    or result["revision"] not in known_revisions()
                    or result["sightings"] < minimum_sightings
                    or result["embeddings"] < minimum_embeddings):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise OpsError("restore_sanity_failed") from None
    finally:
        # docker run may create a container and then fail. Cleanup is attempted
        # by our generated name even in that case; no existing name is accepted.
        if created:
            command(["docker", "rm", "--force", "--volumes", name])
        else:
            try:
                command(["docker", "rm", "--force", "--volumes", name])
            except OpsError:
                pass


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="local archive (synthetic verification, no S3)")
    parser.add_argument("--image", default="indiedex-restore-db", help="locally built docker/db image")
    parser.add_argument("--max-age-hours", type=int, default=36)
    parser.add_argument("--minimum-sightings", type=nonnegative, default=1)
    parser.add_argument("--minimum-embeddings", type=nonnegative, default=1)
    parser.add_argument("--data-mib", type=int, default=2048, help="throwaway tmpfs size, MiB")
    args = parser.parse_args()
    try:
        if not 1 <= args.max_age_hours <= 8760 or not 256 <= args.data_mib <= 32768:
            raise OpsError("drill_configuration_invalid")
        with tempfile.TemporaryDirectory(prefix="restore-drill-") as directory:
            archive = args.archive or Path(directory) / "backup.dump"
            if args.archive is None:
                fetch_latest(archive, max_age_hours=args.max_age_hours)
            restore(archive, image=args.image, minimum_sightings=args.minimum_sightings,
                    minimum_embeddings=args.minimum_embeddings, data_mib=args.data_mib)
        print("restore_drill: archive, restore, sanity and cleanup verified")
        return 0
    except OpsError as error:
        print("restore_drill: " + str(error), file=sys.stderr)
    except Exception:
        print("restore_drill: failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
