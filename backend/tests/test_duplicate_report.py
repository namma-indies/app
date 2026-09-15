"""The legacy report must not turn matching metadata into deletion advice."""

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "find_duplicate_sightings.py"
spec = importlib.util.spec_from_file_location("duplicate_report", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_null_hashes_are_not_duplicate_evidence():
    assert "WHERE p.phash IS NOT NULL" in report.SQL
    assert "WHERE p.phash IS NOT NULL" in report.DETAIL_SQL


async def test_report_does_not_recommend_identity_merge(monkeypatch, capsys):
    from datetime import datetime, timezone

    class Connection:
        async def fetch(self, sql):
            return [{
                "copies": 2,
                "display_name": "Synthetic observer",
                "observer_id": "observer",
                "captured_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "first_seen": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "spread": "0:00:01",
                "identities": 0,
                "sighting_ids": ["sighting-a", "sighting-b"],
            }]

        async def fetchval(self, sql):
            return 2

        async def close(self):
            pass

    async def connect(dsn):
        return Connection()

    monkeypatch.setattr(report.asyncpg, "connect", connect)
    monkeypatch.setattr(report.sys, "argv", [str(SCRIPT)])
    assert await report.main() == 0
    text = capsys.readouterr().out
    assert "not a deletion count" in text
    assert "no in-app undo" in text
    assert "merge each pair" not in text
