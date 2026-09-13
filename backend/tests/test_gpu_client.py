"""HTTP-only worker contract tests: no CUDA, S3, or database connections."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from gpu_worker.client import Config, MODEL_NAME, PIPELINE_VERSION, ProtocolError, Worker


TOKEN = "worker-secret-do-not-log"
LEASE = "lease-secret-" + "x" * 32
SIGNED = "https://objects.example/source?signature=storage-secret"


def expiry(seconds=60):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def job(kind="photo"):
    source = dict(photo_id=str(uuid4()), url=SIGNED, width=20, height=10, phash="0123456789abcdef")
    if kind == "video":
        source = dict(photo_id=None, url=SIGNED)
    return dict(id=str(uuid4()), sighting_id=str(uuid4()), kind=kind,
                pipeline_version=PIPELINE_VERSION, model=MODEL_NAME,
                lease_token=LEASE, lease_expires_at=expiry(), sources=[source], max_frames=12)


def config(**kwargs):
    return replace(Config("https://api.example", TOKEN, ("https://objects.example",),
                          heartbeat_interval=.015, retry_delay=.001, idle_delay=.01), **kwargs)


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.vector = [1.0] + [0.0] * 2151

    def analyse_photo(self, raw):
        self.calls.append(("analyse", raw, threading.get_ident()))
        return SimpleNamespace(dog_confidence=.8, cat_confidence=.1,
                               bbox=(0, 0, 10, 10), vector=self.vector)

    def extract_clip(self, raw):
        self.calls.append(("extract", raw, threading.get_ident()))
        return [SimpleNamespace(original=b"stored-webp", thumbnail=b"thumb-webp", width=20,
                                height=10, phash="0123456789abcdef", content_type="image/webp")]


class FakeServer:
    def __init__(self, claimed=None):
        self.job = claimed if claimed is not None else job()
        self.api_calls = []
        self.storage_calls = []
        self.completions = []
        self.failures = []
        self.heartbeat_status = 200
        self.complete_errors = 0
        self.claimed = False

    async def api(self, request):
        assert request.url.host == "api.example"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        body = json.loads(request.content)
        route = request.url.path.rsplit("/", 1)[-1]
        self.api_calls.append((route, body))
        if route == "claim":
            result = None if self.claimed else self.job
            self.claimed = True
            return httpx.Response(200, json={"job": result})
        assert body["lease_token"] == LEASE
        if route == "heartbeat":
            return httpx.Response(self.heartbeat_status, json={"lease_expires_at": expiry()})
        if route == "urls":
            return httpx.Response(200, json={"sources": self.job["sources"], "uploads": [
                dict(index=spec["index"], photo_id=str(uuid4()),
                     original_url="https://objects.example/frame?signature=output-secret",
                     thumbnail_url="https://objects.example/thumb?signature=thumb-secret",
                     content_type="image/webp") for spec in body["frames"]]})
        if route == "complete":
            self.completions.append(request.content)
            # Validate against the actual backend schema, not a copied fixture.
            from app.media_jobs import Completion
            Completion.model_validate(body)
            if self.complete_errors:
                self.complete_errors -= 1
                raise httpx.ReadError("https://secret.example/?token=" + TOKEN)
            return httpx.Response(200, json={"status": "done", "outcome": "ready", "replayed": False})
        if route == "fail":
            self.failures.append(body)
            return httpx.Response(200, json={"status": "pending"})
        raise AssertionError(route)

    async def storage(self, request):
        assert request.url.host == "objects.example"
        assert "authorization" not in request.headers
        self.storage_calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=b"input-media")
        assert request.method == "PUT"
        assert request.headers["content-type"] == "image/webp"
        assert request.headers["content-length"] == str(len(request.content))
        return httpx.Response(200)

    def worker(self, adapter=None, cfg=None):
        return Worker(cfg or config(), adapter or FakeAdapter(),
                      api_transport=httpx.MockTransport(self.api),
                      storage_transport=httpx.MockTransport(self.storage))


async def test_photo_preserves_source_metadata_and_2152_vector():
    server, adapter = FakeServer(), FakeAdapter()
    async with server.worker(adapter) as worker:
        assert await worker.run_once()
        assert not await worker.run_once()
    body = json.loads(server.completions[0])
    frame = body["frames"][0]
    assert frame["photo_id"] == server.job["sources"][0]["photo_id"]
    assert (frame["width"], frame["height"], frame["phash"], frame["index"]) == (
        20, 10, "0123456789abcdef", None)
    assert len(frame["vector"]) == 2152
    assert adapter.calls[0][1] == b"input-media"
    assert adapter.calls[0][2] != threading.get_ident()
    assert not server.failures


async def test_video_cpu_extract_then_stored_webp_inference_and_exact_upload_headers():
    server, adapter = FakeServer(job("video")), FakeAdapter()
    async with server.worker(adapter) as worker:
        assert await worker.run_once()
    assert [x[0] for x in adapter.calls] == ["extract", "analyse"]
    assert adapter.calls[1][1] == b"stored-webp"
    assert all(x[2] != threading.get_ident() for x in adapter.calls)
    uploads = [r for r in server.storage_calls if r.method == "PUT"]
    assert [r.content for r in uploads] == [b"stored-webp", b"thumb-webp"]
    slots = next(body for route, body in server.api_calls if route == "urls")
    assert slots["frames"] == [dict(index=0, original_bytes=11, thumbnail_bytes=10)]
    frame = json.loads(server.completions[0])["frames"][0]
    assert frame["index"] == 0
    assert frame["photo_id"]
    assert not server.failures


@pytest.mark.parametrize("attempts", [1, 2, 3])
async def test_completion_retries_identical_payload_bounded_without_fail(attempts):
    server = FakeServer()
    server.complete_errors = attempts
    async with server.worker() as worker:
        await worker.run_once()
    assert len(server.completions) == min(attempts + 1, 3)
    assert len(set(server.completions)) == 1
    assert not server.failures


class BlockingAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def analyse_photo(self, raw):
        self.entered.set()
        assert self.release.wait(3)
        return super().analyse_photo(raw)


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


async def test_heartbeat_renews_while_inference_is_off_event_loop():
    server, adapter = FakeServer(), BlockingAdapter()
    async with server.worker(adapter) as worker:
        task = asyncio.create_task(worker.run_once())
        try:
            await wait_until(lambda: sum(r == "heartbeat" for r, _ in server.api_calls) >= 2)
            assert adapter.entered.is_set()
            assert not task.done()
        finally:
            adapter.release.set()
        await task
    assert len(server.completions) == 1


@pytest.mark.parametrize("status", [409, 503, 401])
async def test_lost_or_unconfirmed_lease_cancels_work_never_completes(status):
    server, adapter = FakeServer(), BlockingAdapter()
    server.heartbeat_status = status
    async with server.worker(adapter) as worker:
        try:
            await worker.run_once()
            assert worker.stop.is_set()
            assert not await worker.run_once()
            assert not server.completions and not server.failures
        finally:
            adapter.release.set()
            await wait_until(lambda: not worker._native_tasks)
    assert sum(r == "claim" for r, _ in server.api_calls) == 1


async def test_timeout_classified_and_native_thread_prevents_next_claim():
    server, adapter = FakeServer(), BlockingAdapter()
    async with server.worker(adapter, config(job_timeout=.03)) as worker:
        try:
            await worker.run_once()
            assert worker.stop.is_set()
            assert not await worker.run_once()
            assert not server.completions
            assert server.failures[0]["error"] == "processing_timeout"
        finally:
            adapter.release.set()
            await wait_until(lambda: not worker._native_tasks)


async def test_external_cancellation_never_completes_or_claims_again():
    server, adapter = FakeServer(), BlockingAdapter()
    async with server.worker(adapter) as worker:
        task = asyncio.create_task(worker.run_once())
        try:
            await wait_until(adapter.entered.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert worker.stop.is_set()
            assert not await worker.run_once()
            assert not server.completions and not server.failures
        finally:
            adapter.release.set()
            await wait_until(lambda: not worker._native_tasks)


async def test_stop_drains_current_job_without_claiming_another():
    server, adapter = FakeServer(), BlockingAdapter()
    async with server.worker(adapter) as worker:
        task = asyncio.create_task(worker.run())
        try:
            await wait_until(adapter.entered.is_set)
            worker.stop.set()
        finally:
            adapter.release.set()
        await task
    assert len(server.completions) == 1
    assert sum(r == "claim" for r, _ in server.api_calls) == 1


@pytest.mark.parametrize("url", ["http://objects.example/a", "https://evil.example/a",
    "https://objects.example.evil/a", "https://user:pass@objects.example/a",
    "https://objects.example/a#frag", "https://objects.example:444/a", "file:///tmp/a",
    "https://objects.example/a\n", "https://objects.example\\@evil.example/a"])
async def test_unsafe_source_url_rejected_before_storage_request(url):
    server = FakeServer()
    server.job["sources"][0]["url"] = url
    async with server.worker() as worker:
        await worker.run_once()
    assert not server.storage_calls
    assert not server.completions
    assert server.failures[0]["error"] == "invalid_protocol"


@pytest.mark.parametrize("which", ["api", "storage"])
async def test_redirects_never_followed(which):
    server = FakeServer()
    requests = []
    async def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://evil.example/steal"})
    if which == "api":
        server.api = redirect
    else:
        server.storage = redirect
    async with server.worker() as worker:
        if which == "api":
            with pytest.raises(ProtocolError):
                await worker.run_once()
        else:
            await worker.run_once()
    assert len(requests) == 1
    assert not server.completions


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"a" * 4
        yield b"b" * 8


@pytest.mark.parametrize("declared", [True, False])
async def test_download_limit_applies_with_and_without_content_length(declared):
    server = FakeServer()
    async def oversized(request):
        return httpx.Response(200, headers={"content-length": "12"} if declared else {}, stream=Chunks())
    server.storage = oversized
    async with server.worker(cfg=config(max_photo_bytes=8)) as worker:
        await worker.run_once()
    assert not server.completions
    assert server.failures[0]["error"] == "media_limit"
    assert server.failures[0]["retryable"] is False


async def test_exception_messages_never_cross_fail_api_or_logs(caplog):
    class Broken(FakeAdapter):
        def analyse_photo(self, raw):
            raise RuntimeError(TOKEN + LEASE + SIGNED)
    server = FakeServer()
    async with server.worker(Broken()) as worker:
        await worker.run_once()
    assert server.failures == [dict(lease_token=LEASE, error="processing_failed", retryable=True)]
    assert TOKEN not in caplog.text and LEASE not in caplog.text and SIGNED not in caplog.text


@pytest.mark.parametrize("vector", [[1.0], [float("nan")] * 2152, [1.0] * 2152])
async def test_invalid_embedding_never_completed(vector):
    server, adapter = FakeServer(), FakeAdapter()
    adapter.vector = vector
    async with server.worker(adapter) as worker:
        await worker.run_once()
    assert not server.completions
    assert server.failures[0]["retryable"] is False


async def test_no_animal_result_is_valid_completion():
    class NoAnimal(FakeAdapter):
        def analyse_photo(self, raw):
            return SimpleNamespace(dog_confidence=0, cat_confidence=0, bbox=None, vector=None)
    server = FakeServer()
    async with server.worker(NoAnimal()) as worker:
        await worker.run_once()
    assert json.loads(server.completions[0])["frames"][0]["vector"] is None


def test_configuration_env_file_and_unique_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_GPU_API_URL", "https://api.example")
    monkeypatch.setenv("MEDIA_GPU_STORAGE_ORIGINS", "https://objects.example")
    monkeypatch.setenv("MEDIA_GPU_TOKEN", TOKEN)
    monkeypatch.delenv("MEDIA_GPU_TOKEN_FILE", raising=False)
    first = Config.from_env()
    assert first.owner != Config.from_env().owner
    assert TOKEN not in repr(first)
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    monkeypatch.setenv("MEDIA_GPU_TOKEN_FILE", str(path))
    with pytest.raises(ProtocolError):
        Config.from_env()
    monkeypatch.delenv("MEDIA_GPU_TOKEN")
    assert Config.from_env().token == TOKEN


@pytest.mark.parametrize("url", ["http://api.example", "https://u:p@api.example", "https://api.example/prefix"])
def test_api_origin_must_be_https_without_credentials_or_path(url):
    with pytest.raises(ProtocolError):
        config(api_url=url)


async def test_idle_backoff_is_interruptible_and_does_not_spin():
    server = FakeServer()
    server.claimed = True
    async with server.worker(cfg=config(idle_delay=10)) as worker:
        task = asyncio.create_task(worker.run())
        await wait_until(lambda: bool(server.api_calls))
        worker.stop.set()
        await asyncio.wait_for(task, .1)
    assert len(server.api_calls) == 1


async def test_timeout_during_completion_never_sends_fail():
    server = FakeServer()
    original = server.api
    async def slow_complete(request):
        if request.url.path.endswith("/complete"):
            server.completions.append(request.content)
            await asyncio.sleep(1)
        return await original(request)
    server.api = slow_complete
    async with server.worker(cfg=config(job_timeout=.04)) as worker:
        await worker.run_once()
    assert len(server.completions) == 1
    assert not server.failures


async def test_untrusted_upload_slot_never_receives_any_data():
    server = FakeServer(job("video"))
    original = server.api
    async def unsafe_slot(request):
        response = await original(request)
        if request.url.path.endswith("/urls"):
            body = response.json()
            body["uploads"][0]["original_url"] = "https://evil.example/steal"
            return httpx.Response(200, json=body)
        return response
    server.api = unsafe_slot
    async with server.worker() as worker:
        await worker.run_once()
    assert all(request.method == "GET" for request in server.storage_calls)
    assert not server.completions
    assert server.failures[0]["error"] == "invalid_protocol"


async def test_hung_heartbeat_stops_at_local_expiry_without_completion():
    server, adapter = FakeServer(), BlockingAdapter()
    server.job["lease_expires_at"] = expiry(.07)
    original = server.api
    async def hang(request):
        if request.url.path.endswith("/heartbeat"):
            await asyncio.sleep(1)
        return await original(request)
    server.api = hang
    async with server.worker(adapter) as worker:
        try:
            await asyncio.wait_for(worker.run_once(), .4)
            assert worker.stop.is_set()
            assert not server.failures and not server.completions
        finally:
            adapter.release.set()
            await wait_until(lambda: not worker._native_tasks)


async def test_concurrent_run_once_calls_are_serialized():
    server, adapter = FakeServer(), BlockingAdapter()
    async with server.worker(adapter) as worker:
        first = asyncio.create_task(worker.run_once())
        second = asyncio.create_task(worker.run_once())
        try:
            await wait_until(adapter.entered.is_set)
            assert sum(r == "claim" for r, _ in server.api_calls) == 1
        finally:
            adapter.release.set()
        assert await first
        assert not await second
    assert len(server.completions) == 1


async def test_expired_claim_never_downloads_or_completes():
    from gpu_worker.client import LeaseLost
    server = FakeServer()
    server.job["lease_expires_at"] = expiry(-1)
    async with server.worker() as worker:
        with pytest.raises(LeaseLost):
            await worker.run_once()
    assert not server.storage_calls and not server.completions
