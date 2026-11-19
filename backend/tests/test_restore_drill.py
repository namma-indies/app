from datetime import datetime, timezone
import json

import pytest

from scripts import restore_db_drill as drill
from scripts.ops_common import OpsError

NOW = datetime(2026, 11, 3, 8, tzinfo=timezone.utc)
MANIFEST = {"version": 1, "created_at": "2026-11-03T07:00:00+00:00", "schema_head": "synthetic-revision",
            "counts": {"observers": 50, "sightings": 1000, "captures": 900, "photos": 1200,
                       "individuals": 300, "confirmations": 400, "embeddings": 1100}}


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
        assert args[0] == "get-object"
        from pathlib import Path
        if args[4].endswith(".json"):
            assert "--if-match" not in args and args[4] == backup(stamp)["Key"].removesuffix(".dump") + ".json"
            Path(args[-1]).write_text(json.dumps({**MANIFEST, "key": backup(stamp)["Key"], "size": 1024}))
            return {}
        assert "--if-match" in args
        Path(args[-1]).write_bytes(b"PGDMP" + b"0" * 1019)
        return {}
    monkeypatch.setattr(drill, "aws_command", aws)
    manifest = drill.fetch_latest(tmp_path / "backup.dump", max_age_hours=36)
    assert len(calls) == 4 and manifest["counts"]["sightings"] == 1000


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


def _fetch_with(monkeypatch, tmp_path, manifest_body):
    monkeypatch.setenv("RESTORE_BACKUP_BUCKET", "synthetic")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    def aws(args, **kwargs):
        if args[0] == "list-objects-v2":
            return {"Contents": [backup(stamp, size=1024)], "IsTruncated": False}
        from pathlib import Path
        if args[4].endswith(".json"):
            if manifest_body is None:
                assert kwargs.get("missing_ok")
                return None
            Path(args[-1]).write_bytes(manifest_body(backup(stamp)["Key"]))
            return {}
        Path(args[-1]).write_bytes(b"PGDMP" + b"0" * 1019)
        return {}
    monkeypatch.setattr(drill, "aws_command", aws)
    return lambda **kw: drill.fetch_latest(tmp_path / "backup.dump", max_age_hours=36, **kw)


def test_missing_manifest_fails_unless_explicitly_allowed(monkeypatch, tmp_path):
    fetch = _fetch_with(monkeypatch, tmp_path, None)
    with pytest.raises(OpsError, match="manifest_missing"):
        fetch()
    assert fetch(require_manifest=False) is None


@pytest.mark.parametrize("body", [
    lambda key: b"not json",
    lambda key: json.dumps({**MANIFEST, "key": "backups/indiedex-20200101T000000Z.dump", "size": 1024}).encode(),
    lambda key: json.dumps({**MANIFEST, "key": key, "size": 999}).encode(),
    lambda key: json.dumps({**MANIFEST, "key": key, "size": 1024, "counts": {"sightings": 1}}).encode(),
])
def test_a_manifest_for_another_dump_or_malformed_is_refused(monkeypatch, tmp_path, body):
    with pytest.raises(OpsError, match="manifest_invalid"):
        _fetch_with(monkeypatch, tmp_path, body)()


def _sanity_matching(docker, manifest):
    docker.sanity.update(manifest["counts"], revision=manifest["schema_head"])


def test_restore_matching_the_manifest_passes_within_tolerance(archive, docker):
    _sanity_matching(docker, MANIFEST)
    docker.sanity["sightings"] += 10  # max(slack 10, 1% of 1000 = 10)
    drill.restore(archive, manifest=MANIFEST)
    assert docker.calls[-1][1] == "rm"


@pytest.mark.parametrize("change,code", [({"sightings": 950}, "manifest_count_mismatch"),
                                         ({"embeddings": 1}, "manifest_count_mismatch"),
                                         ({"observers": 70}, "manifest_count_mismatch"),
                                         ({"revision": "synthetic-older"}, "manifest_schema_mismatch")])
def test_restore_that_lost_rows_or_has_another_schema_fails(archive, docker, monkeypatch, change, code):
    monkeypatch.setattr(drill, "known_revisions", lambda: {"synthetic-revision", "synthetic-older"})
    _sanity_matching(docker, MANIFEST)
    docker.sanity.update(change)
    with pytest.raises(OpsError, match=code):
        drill.restore(archive, manifest=MANIFEST)
    assert docker.calls[-1][1] == "rm"


def test_pass_marker_records_only_times(monkeypatch):
    monkeypatch.setenv("RESTORE_BACKUP_BUCKET", "synthetic")
    written = {}
    def aws(args, **kwargs):
        assert args[:5] == ["put-object", "--bucket", "synthetic", "--key", "monitoring/restore-drill.json"]
        from pathlib import Path
        written.update(json.loads(Path(args[args.index("--body") + 1]).read_text()))
        return {}
    monkeypatch.setattr(drill, "aws_command", aws)
    drill.record_pass(MANIFEST)
    assert set(written) == {"version", "passed_at", "backup_created_at", "manifest_compared"}
    assert written["manifest_compared"] is True and written["backup_created_at"] == MANIFEST["created_at"]
    def denied(args, **kwargs):
        raise OpsError("object_store_command_failed")
    monkeypatch.setattr(drill, "aws_command", denied)
    with pytest.raises(OpsError, match="pass_marker_write_failed"):
        drill.record_pass(None)


def test_local_archive_compares_a_manifest_file(archive, docker, monkeypatch, tmp_path, capsys):
    raw = {"schema_head": MANIFEST["schema_head"], "counts": MANIFEST["counts"]}
    path = tmp_path / "counts.json"
    path.write_text(json.dumps(raw))
    _sanity_matching(docker, MANIFEST)
    monkeypatch.setattr(drill.sys, "argv", ["restore_db_drill", "--archive", str(archive), "--manifest", str(path)])
    assert drill.main() == 0
    assert "manifest counts" in capsys.readouterr().out
    monkeypatch.setattr(drill.sys, "argv", ["restore_db_drill", "--manifest", str(path)])
    assert drill.main() == 1
    monkeypatch.setattr(drill.sys, "argv", ["restore_db_drill", "--archive", str(archive), "--record-pass"])
    assert drill.main() == 1
