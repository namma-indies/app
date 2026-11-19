from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json

from botocore.exceptions import ClientError

import pytest

from app.storage.s3 import storage_from_settings


class Body:
    def __init__(self, raw):
        self.raw = raw
    async def __aenter__(self):
        # aiobotocore's wrapper context yields the underlying aiohttp response,
        # whose read() does not accept a size. Bounded reads use the wrapper.
        return object()
    async def __aexit__(self, *args):
        pass
    async def read(self, maximum):
        return self.raw[:maximum]


class Client:
    def __init__(self, pages=None):
        self.pages = pages or []
        self.header = b"PGDMP"
        self.puts = []
        self.gets = []
    async def head_bucket(self, **kwargs):
        pass
    async def put_object(self, **kwargs):
        self.puts.append(kwargs)
    async def get_object(self, **kwargs):
        self.gets.append(kwargs)
        return {"Body": Body(self.header if kwargs.get("Range") else b"ok")}
    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self
    async def paginate(self, **kwargs):
        assert kwargs["Prefix"] == "backups/"
        for page in self.pages:
            yield page


def storage(monkeypatch, client):
    instance = storage_from_settings()
    @asynccontextmanager
    async def context():
        yield client
    monkeypatch.setattr(instance, "_client", context)
    return instance


async def test_health_exercises_write_and_read_on_a_fixed_non_media_object(monkeypatch):
    client = Client()
    await storage(monkeypatch, client).check_health()
    assert client.puts[0]["Key"] == client.gets[0]["Key"] == "monitoring/storage-probe"
    assert client.puts[0]["Body"] == b"ok"


def marker(**over):
    stamp = datetime.now(timezone.utc).replace(microsecond=0)
    body = {"version": 1, "key": f"backups/indiedex-{stamp:%Y%m%dT%H%M%SZ}.dump", "size": 5000,
            "created_at": stamp.isoformat(), "pgdmp_verified": True, **over}
    return json.dumps(body).encode()


class MarkerClient(Client):
    def __init__(self, raw=None, missing=False):
        super().__init__()
        self.raw, self.missing = raw, missing
    async def get_object(self, **kwargs):
        self.gets.append(kwargs)
        if self.missing:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": Body(self.raw)}
    def get_paginator(self, name):
        raise AssertionError("the freshness check must not list backups")


async def test_backup_probe_reads_only_the_latest_marker(monkeypatch):
    client = MarkerClient(marker())
    result = await storage(monkeypatch, client).backup_health()
    assert result["valid"] is True and 0 <= result["age_s"] < 10
    assert [g["Key"] for g in client.gets] == ["backups/latest.json"]


async def test_missing_marker_is_missing_and_bad_markers_are_invalid(monkeypatch):
    assert await storage(monkeypatch, MarkerClient(missing=True)).backup_health() is None
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert (await storage(monkeypatch, MarkerClient(marker(created_at=old))).backup_health())["age_s"] > 200000
    for bad in (marker(pgdmp_verified=False), marker(size=10), marker(key="backups/other.dump"),
                marker(created_at="2026-01-01T00:00:00"), b"not json", b"{" + b" " * 5000 + b"}",
                marker(created_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())):
        assert (await storage(monkeypatch, MarkerClient(bad)).backup_health())["valid"] is False
