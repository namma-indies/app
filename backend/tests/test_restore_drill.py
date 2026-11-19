from datetime import datetime, timezone
import json

import pytest

from scripts import restore_db_drill as drill
from scripts.ops_common import OpsError

NOW = datetime(2026, 11, 3, 8, tzinfo=timezone.utc)


def backup(stamp="20261103T070000Z", size=5000):
    return {"Key": "backups/indiedex-" + stamp + ".dump", "Size": size, "ETag": '"synthetic"'}


def test_selects_latest_matching_backup_without_falling_back_to_a_damaged_one():
    older, latest = backup("20261102T070000Z"), backup()
    assert drill.newest_backup([latest, older, {"Key": "backups/other.txt"}], now=NOW, max_age_hours=36) == latest
    with pytest.raises(OpsError, match="backup_invalid"):
        drill.newest_backup([older, backup(size=0)], now=NOW, max_age_hours=36)


@pytest.mark.parametrize("objects,code", [([], "backup_missing"), ([backup("20261001T000000Z")], "backup_not_fresh"), ([backup("20261104T000000Z")], "backup_not_fresh")])
def test_missing_stale_and_future_backups_are_refused(objects, code):
    with pytest.raises(OpsError, match=code):
        drill.newest_backup(objects, now=NOW, max_age_hours=36)


def test_paginates_and_fetches_the_selected_version_only(monkeypatch, tmp_path):
    monkeypatch.setenv("RESTORE_BACKUP_BUCKET", "synthetic")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    calls = []
    def aws(args, **kwargs):
        calls.append(args)
        if args[0] == "list-objects-v2":
            if "--continuation-token" in args:
                return {"Contents": [backup(stamp, size=1024)], "IsTruncated": False}
            return {"Contents": [], "IsTruncated": True, "NextContinuationToken": "synthetic-page"}
        assert args[0] == "get-object" and "--if-match" in args
        from pathlib import Path
        Path(args[-1]).write_bytes(b"PGDMP" + b"0" * 1019)
        return {}
    monkeypatch.setattr(drill, "aws_command", aws)
    drill.fetch_latest(tmp_path / "backup.dump", max_age_hours=36)
    assert len(calls) == 3


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "synthetic.dump"
    path.write_bytes(b"PGDMP" + b"0" * 2000)
    return path


class Docker:
    def __init__(self):
        self.calls = []
        self.fail_restore = False
        self.schema_only = False
        self.sanity = dict(extensions=True, one_revision=True, revision="synthetic-revision", sightings=1,
                           photos=1, individuals=1, confirmations=1, embeddings=1,
                           vectors_valid=True, constraints_valid=True, photo_owners_valid=True)
    def command(self, args, **kwargs):
        self.calls.append(args)
        if "pg_restore" in args:
            if "--list" in args:
                return b"" if self.schema_only else b"\n".join(
                    ("123; 0 0 TABLE DATA public " + table + " postgres").encode() for table in drill.CORE_TABLES)
            if self.fail_restore:
                raise OpsError("drill_command_failed")
        if "psql" in args:
            return json.dumps(self.sanity).encode()
        return b""


@pytest.fixture
def docker(monkeypatch):
    fake = Docker()
    monkeypatch.setattr(drill, "command", fake.command)
    monkeypatch.setattr(drill, "known_revisions", lambda: {"synthetic-revision"})
    return fake


def test_restores_only_into_a_generated_networkless_tmpfs_container(archive, docker):
    drill.restore(archive)
    run = docker.calls[0]
    assert run[run.index("--network") + 1] == "none"
    assert "--tmpfs" in run and "--label" in run
    assert "-p" not in run and "--publish" not in run and "--volume" not in run
    name = run[run.index("--name") + 1]
    assert name.startswith("indiedex-restore-drill-")
    assert docker.calls[-1] == ["docker", "rm", "--force", "--volumes", name]


def test_partial_restore_failure_still_discards_the_database(archive, docker):
    docker.fail_restore = True
    with pytest.raises(OpsError):
        drill.restore(archive)
    assert docker.calls[-1][:4] == ["docker", "rm", "--force", "--volumes"]


def test_schema_only_archive_is_not_a_database_backup(archive, docker):
    docker.schema_only = True
    with pytest.raises(OpsError, match="archive_missing_table_data"):
        drill.restore(archive)
    assert docker.calls[-1][1] == "rm"


@pytest.mark.parametrize("key,value", [("embeddings", 0), ("sightings", 0), ("extensions", False), ("vectors_valid", False),
                                       ("photo_owners_valid", False), ("one_revision", False), ("revision", "unknown")])
def test_sanity_failure_is_not_reported_as_a_success(archive, docker, key, value):
    docker.sanity[key] = value
    with pytest.raises(OpsError, match="restore_sanity_failed"):
        drill.restore(archive)
    assert docker.calls[-1][1] == "rm"


@pytest.mark.parametrize("contents", [b"", b"PGDMP", b"pg_dump: synthetic error\n" * 100])
def test_invalid_archive_never_starts_a_container(archive, docker, contents):
    archive.write_bytes(contents)
    with pytest.raises(OpsError, match="archive_invalid"):
        drill.restore(archive)
    assert not docker.calls


def test_no_live_database_destination_is_accepted(monkeypatch, capsys):
    monkeypatch.setattr(drill.sys, "argv", ["restore_db_drill", "--database-url", "postgresql://synthetic"])
    with pytest.raises(SystemExit) as error:
        drill.main()
    assert error.value.code == 2


def test_tool_failure_does_not_leak_raw_sql_or_identifiers(monkeypatch, capsys):
    monkeypatch.setattr(drill.sys, "argv", ["restore_db_drill"])
    def failed(*args, **kwargs):
        raise RuntimeError("synthetic-private-data")
    monkeypatch.setattr(drill, "fetch_latest", failed)
    assert drill.main() == 1
    assert capsys.readouterr().err == "restore_drill: failed\n"
