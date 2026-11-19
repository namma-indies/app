from contextlib import asynccontextmanager
from datetime import datetime, timezone

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


async def test_backup_probe_pages_and_reads_only_the_latest_archive_header(monkeypatch):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"backups/indiedex-{stamp}.dump"
    client = Client([{"Contents": [{"Key": "backups/indiedex-20260101T000000Z.dump", "Size": 4000}]},
                     {"Contents": [{"Key": key, "Size": 5000}]}])
    result = await storage(monkeypatch, client).backup_health()
    assert result["valid"] is True and 0 <= result["age_s"] < 10
    assert client.gets[0]["Key"] == key
    assert client.gets[0]["Range"] == "bytes=0-4"


async def test_missing_or_corrupt_newest_backup_is_not_masked_by_an_older_one(monkeypatch):
    assert await storage(monkeypatch, Client()).backup_health() is None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = Client([{"Contents": [{"Key": f"backups/indiedex-{stamp}.dump", "Size": 4000}]}])
    client.header = b"ERROR"
    assert (await storage(monkeypatch, client).backup_health())["valid"] is False
