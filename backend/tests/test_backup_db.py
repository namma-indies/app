"""What the backup refuses to call a backup.

Everything the app knows lives in one Docker volume on one box. The photos are
in S3 and the app's IAM cannot delete them; the rows are not, and the rows are
the part that cannot be reconstructed -- which animal, seen by whom, and every
human verdict in `confirmations`, which is also the calibration set the
thresholds are meant to be fitted against.

The failure mode a backup job actually has is not erroring. It is succeeding
while storing something that is not a database, and nobody noticing until a
restore is the only option left. These pin the checks that stop that.
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "backup_db", Path(__file__).resolve().parents[1] / "scripts" / "backup_db.py"
)
backup_db = importlib.util.module_from_spec(_spec)
sys.modules["backup_db"] = backup_db
_spec.loader.exec_module(backup_db)


def _dump(tmp_path, body: bytes) -> str:
    p = tmp_path / "test.dump"
    p.write_bytes(body)
    return str(p)


def test_a_real_archive_passes(tmp_path):
    path = _dump(tmp_path, b"PGDMP" + b"\x00" * 4000)
    assert backup_db.check_dump(path) == 4005


def test_an_empty_file_is_not_a_database(tmp_path):
    with pytest.raises(SystemExit) as e:
        backup_db.check_dump(_dump(tmp_path, b""))
    assert "not a database" in str(e.value)


def test_a_truncated_dump_is_refused(tmp_path):
    """A dump cut off mid-write is still a file, and a file is what a size-only
    check would happily upload."""
    with pytest.raises(SystemExit):
        backup_db.check_dump(_dump(tmp_path, b"PGDMP" + b"\x00" * 10))


def test_an_error_message_is_not_uploaded_as_a_backup(tmp_path):
    """The case this exists for: pg_dump failing and its complaint landing in
    the redirect. Plenty of bytes, plausible size, not a database."""
    body = b"pg_dump: error: connection to server failed\n" * 200
    with pytest.raises(SystemExit) as e:
        backup_db.check_dump(_dump(tmp_path, body))
    assert "PGDMP" in str(e.value)


def test_plain_sql_format_is_refused(tmp_path):
    """`-Fc` is not a preference. A plain-SQL dump cannot have its table of
    contents verified without restoring it, which is the check the box runs
    before this ever sees the file."""
    body = b"--\n-- PostgreSQL database dump\n--\n" + b"CREATE TABLE x();\n" * 100
    with pytest.raises(SystemExit):
        backup_db.check_dump(_dump(tmp_path, body))


def test_keys_sort_chronologically_as_strings():
    """`--list` sorts lexically and reports the last as newest, so the naming
    has to make those the same thing."""
    early = backup_db._key(datetime(2026, 9, 4, 19, 30, tzinfo=timezone.utc))
    later = backup_db._key(datetime(2026, 12, 25, 4, 5, tzinfo=timezone.utc))
    next_year = backup_db._key(datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc))
    assert early < later < next_year
    assert sorted([next_year, early, later])[-1] == next_year


def test_keys_are_utc_and_prefixed():
    key = backup_db._key(datetime(2026, 9, 4, 19, 30, 5, tzinfo=timezone.utc))
    assert key == "backups/indiedex-20260904T193005Z.dump"
    assert key.startswith(backup_db.PREFIX)


def test_backups_live_under_their_own_prefix():
    """Retention is an S3 lifecycle rule on this prefix, because the app's IAM
    has no DeleteObject and must not gain it -- that is what keeps a compromised
    app from erasing the photo corpus. A lifecycle rule needs the backups to be
    separable from the photos, which `sightings/` are not."""
    assert backup_db.PREFIX == "backups/"
    assert not backup_db._key().startswith("sightings/")


async def test_upload_writes_the_latest_marker_after_a_verified_upload(tmp_path, monkeypatch):
    import json

    class Storage:
        def __init__(self):
            self.objects = {}
        async def put_file(self, key, path, content_type):
            self.objects[key] = Path(path).read_bytes()
            return len(self.objects[key])
        async def list_keys(self, prefix):
            return [k for k in self.objects if k.startswith(prefix)]
        async def put(self, key, data, content_type):
            assert content_type == "application/json"
            self.objects[key] = data

    fake = Storage()
    monkeypatch.setattr(backup_db, "storage_from_settings", lambda: fake)
    assert await backup_db.upload(_dump(tmp_path, b"PGDMP" + b"\x00" * 4000)) == 0
    (dump_key,) = [k for k in fake.objects if k.endswith(".dump")]
    marker = json.loads(fake.objects["backups/latest.json"])
    assert marker["key"] == dump_key and marker["size"] == 4005 and marker["pgdmp_verified"] is True
    assert dump_key.endswith(datetime.fromisoformat(marker["created_at"]).strftime("%Y%m%dT%H%M%SZ.dump"))


async def test_a_vanished_upload_writes_no_marker(tmp_path, monkeypatch):
    class Storage:
        puts = []
        async def put_file(self, key, path, content_type):
            return 4005
        async def list_keys(self, prefix):
            return []
        async def put(self, key, data, content_type):
            self.puts.append(key)

    fake = Storage()
    monkeypatch.setattr(backup_db, "storage_from_settings", lambda: fake)
    with pytest.raises(SystemExit):
        await backup_db.upload(_dump(tmp_path, b"PGDMP" + b"\x00" * 4000))
    assert fake.puts == []


def test_counts_file_is_validated_and_becomes_a_manifest(tmp_path):
    import json

    good = {"schema_head": "0046_moderation_fixes", "counts": {t: 3 for t in backup_db.MANIFEST_TABLES}}
    path = tmp_path / "counts.json"
    path.write_text(json.dumps(good))
    counts = backup_db.read_counts(str(path))
    when = datetime(2026, 11, 19, 19, 30, tzinfo=timezone.utc)
    key = backup_db._key(when)
    manifest = json.loads(backup_db.manifest(key, 5000, when, counts))
    assert manifest["key"] == key and manifest["schema_head"] == "0046_moderation_fixes"
    assert manifest["counts"]["sightings"] == 3 and manifest["created_at"].startswith("2026-11-19T19:30")
    assert backup_db.manifest_key(key) == key.removesuffix(".dump") + ".json"
    for bad in ({**good, "counts": {"sightings": 3}}, {**good, "schema_head": ""},
                {**good, "counts": {**good["counts"], "photos": -1}}):
        path.write_text(json.dumps(bad))
        with pytest.raises(SystemExit):
            backup_db.read_counts(str(path))
    path.write_text("ERROR: relation does not exist")
    with pytest.raises(SystemExit):
        backup_db.read_counts(str(path))


async def test_upload_with_counts_writes_manifest_before_the_marker(tmp_path, monkeypatch):
    import json

    order = []
    class Storage:
        async def put_file(self, key, path, content_type):
            order.append(key)
            return 4005
        async def list_keys(self, prefix):
            return order[:1]
        async def put(self, key, data, content_type):
            order.append(key)

    monkeypatch.setattr(backup_db, "storage_from_settings", lambda: Storage())
    counts = tmp_path / "counts.json"
    counts.write_text(json.dumps({"schema_head": "x", "counts": {t: 0 for t in backup_db.MANIFEST_TABLES}}))
    assert await backup_db.upload(_dump(tmp_path, b"PGDMP" + b"\x00" * 4000), str(counts)) == 0
    assert order[0].endswith(".dump") and order[1] == backup_db.manifest_key(order[0])
    assert order[2] == "backups/latest.json"
