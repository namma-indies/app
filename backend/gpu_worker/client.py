"""Outbound-only, serial DGX worker. No database or bucket credentials are used.

Inject an adapter exposing ``analyse_photo(bytes)`` and ``extract_clip(bytes)``
(the GpuInference interface). Bootstrap/model pin verification belongs to the
entrypoint; ``serve(adapter)`` loads HTTP configuration and handles SIGTERM.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import signal
from typing import Protocol, Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

PIPELINE_VERSION = "stored-webp-q90-yolo26x-miewid-msv3-v1"
MODEL_NAME = "miewid-msv3"


class WorkerError(Exception):
    """Only fixed, credential-free classifications cross the failure API."""
    code = "worker_error"
    retryable = True


class ProtocolError(WorkerError):
    code = "invalid_protocol"
    retryable = False


class MediaLimit(WorkerError):
    code = "media_limit"
    retryable = False


class LeaseLost(WorkerError):
    code = "lease_lost"


class RemoteError(WorkerError):
    code = "remote_unavailable"


class CompletionUncertain(WorkerError):
    code = "completion_unconfirmed"


def https_url(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\\" in value
                or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
            raise ValueError
        return parsed.hostname.lower(), parsed.port or 443
    except (ValueError, TypeError):
        raise ProtocolError() from None


@dataclass(frozen=True)
class Config:
    api_url: str
    token: str = field(repr=False)
    storage_origins: tuple[str, ...] = ()
    owner: str = field(default_factory=lambda: "dgx-" + uuid4().hex)
    request_timeout: float = 30
    job_timeout: float = 300
    heartbeat_interval: float = 15
    idle_delay: float = 3
    error_delay: float = 5
    max_backoff: float = 60
    completion_attempts: int = 3
    retry_delay: float = 0.5
    max_photo_bytes: int = 32 * 1024 * 1024
    max_clip_bytes: int = 100 * 1024 * 1024
    max_json_bytes: int = 1024 * 1024

    def __post_init__(self):
        https_url(self.api_url)
        parsed = urlsplit(self.api_url)
        if parsed.path not in ("", "/") or parsed.query:
            raise ProtocolError()
        if not self.token or any(ord(c) < 33 or ord(c) > 126 for c in self.token):
            raise ProtocolError()
        if not self.storage_origins:
            raise ProtocolError()
        for origin in self.storage_origins:
            https_url(origin)
            parsed = urlsplit(origin)
            if parsed.path not in ("", "/") or parsed.query:
                raise ProtocolError()
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", self.owner):
            raise ProtocolError()
        for name in ("request_timeout", "job_timeout", "heartbeat_interval", "idle_delay",
                     "error_delay", "max_backoff", "retry_delay", "max_photo_bytes",
                     "max_clip_bytes", "max_json_bytes", "completion_attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ProtocolError()
        for name in ("max_photo_bytes", "max_clip_bytes", "max_json_bytes", "completion_attempts"):
            if not isinstance(getattr(self, name), int):
                raise ProtocolError()
        if self.completion_attempts > 5:
            raise ProtocolError()

    @classmethod
    def from_env(cls):
        token, token_file = os.getenv("MEDIA_GPU_TOKEN"), os.getenv("MEDIA_GPU_TOKEN_FILE")
        if bool(token) == bool(token_file):
            raise ProtocolError()
        if token_file:
            try:
                with Path(token_file).open() as stream:
                    token = stream.read(4097).strip()
                if len(token) > 4096:
                    raise ProtocolError()
            except OSError:
                raise ProtocolError() from None
        return cls(api_url=os.getenv("MEDIA_GPU_API_URL", ""), token=token or "",
                   storage_origins=tuple(x.strip() for x in
                       os.getenv("MEDIA_GPU_STORAGE_ORIGINS", "").split(",") if x.strip()))


class Adapter(Protocol):
    def analyse_photo(self, raw: bytes) -> Any: ...
    def extract_clip(self, raw: bytes) -> list[Any]: ...


@dataclass
class Lease:
    job: dict
    deadline: float
    lost: bool = False
    completing: bool = False

    def check(self):
        if self.lost or asyncio.get_running_loop().time() >= self.deadline:
            self.lost = True
            raise LeaseLost()


def expiry_deadline(value: str, started: float) -> float:
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise LeaseLost()
        # Conservatively subtract request elapsed time a second time, rather
        # than let slow responses extend a lease locally beyond the server.
        return started + remaining
    except (ValueError, TypeError, AttributeError):
        raise ProtocolError() from None


class Worker:
    def __init__(self, config: Config, adapter: Adapter, *,
                 api_transport: httpx.AsyncBaseTransport | None = None,
                 storage_transport: httpx.AsyncBaseTransport | None = None):
        self.config, self.adapter = config, adapter
        # HTTPX logs complete presigned query strings even at INFO. This
        # dedicated worker intentionally emits no HTTP-library diagnostics.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).disabled = True
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)
        self.api = httpx.AsyncClient(transport=api_transport, trust_env=False,
                                    timeout=config.request_timeout, follow_redirects=False)
        self.storage = httpx.AsyncClient(transport=storage_transport, trust_env=False,
                                        timeout=config.request_timeout, follow_redirects=False)
        self.stop = asyncio.Event()
        self._serial = asyncio.Lock()
        self._native_tasks: set[asyncio.Task] = set()
        self._storage_origins = {https_url(x) for x in config.storage_origins}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.api.aclose()
        await self.storage.aclose()

    async def _api(self, path: str, body: dict | bytes) -> dict:
        content = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
        async with asyncio.timeout(self.config.request_timeout):
            async with self.api.stream("POST", self.config.api_url.rstrip("/") +
                    "/internal/media-jobs/" + path, content=content,
                    headers={"Authorization": "Bearer " + self.config.token,
                             "Content-Type": "application/json", "Accept-Encoding": "identity"},
                    follow_redirects=False) as response:
                if response.status_code == 409:
                    raise LeaseLost()
                if response.status_code >= 500 or response.status_code == 429:
                    raise RemoteError()
                if not 200 <= response.status_code < 300:
                    raise ProtocolError()
                raw = await self._read(response, self.config.max_json_bytes)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise ProtocolError() from None

    @staticmethod
    async def _read(response, maximum):
        length = response.headers.get("content-length")
        if length is not None:
            try:
                if not 0 <= int(length) <= maximum:
                    raise MediaLimit()
            except ValueError:
                raise ProtocolError() from None
        # Forbid compression to bound both wire bytes and decoded memory.
        if response.headers.get("content-encoding", "identity") != "identity":
            raise ProtocolError()
        raw = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(raw) + len(chunk) > maximum:
                raise MediaLimit()
            raw.extend(chunk)
        return bytes(raw)

    def _storage_url(self, value):
        if https_url(value) not in self._storage_origins:
            raise ProtocolError()
        return value

    async def _download(self, source, maximum, lease):
        lease.check()
        async with asyncio.timeout(self.config.request_timeout):
            async with self.storage.stream("GET", self._storage_url(source["url"]),
                    headers={"Accept-Encoding": "identity"}, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise RemoteError() if response.status_code >= 400 else ProtocolError()
                raw = await self._read(response, maximum)
        lease.check()
        if not raw:
            raise MediaLimit()
        return raw

    async def _upload(self, url, raw, lease):
        lease.check()
        async with asyncio.timeout(self.config.request_timeout):
            async with self.storage.stream("PUT", self._storage_url(url), content=raw,
                    headers={"Content-Type": "image/webp", "Content-Length": str(len(raw))},
                    follow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    raise RemoteError() if response.status_code >= 400 else ProtocolError()
        lease.check()

    async def _thread(self, method, raw, lease):
        lease.check()
        task = asyncio.create_task(asyncio.to_thread(method, raw))
        self._native_tasks.add(task)
        def finished(done):
            self._native_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Python cannot stop an executing native/CUDA thread. Never claim
            # another job in this process while abandoned inference may run.
            self.stop.set()
            raise
        lease.check()
        return result

    async def _heartbeat(self, lease):
        while True:
            lease.check()
            delay = min(self.config.heartbeat_interval,
                        (lease.deadline - asyncio.get_running_loop().time()) / 3)
            await asyncio.sleep(delay)
            lease.check()
            started = asyncio.get_running_loop().time()
            try:
                async with asyncio.timeout(lease.deadline - asyncio.get_running_loop().time()):
                    reply = await self._api(lease.job["id"] + "/heartbeat",
                                            {"lease_token": lease.job["lease_token"]})
                lease.check()
                lease.deadline = expiry_deadline(reply["lease_expires_at"], started)
            except Exception:
                # An unconfirmed renewal is not evidence of lease ownership.
                lease.lost = True
                raise LeaseLost() from None

    @staticmethod
    def _frame(metadata, analysis, photo_id, index=None):
        vector = None if analysis.vector is None else [float(x) for x in analysis.vector]
        bbox = None if analysis.bbox is None else list(analysis.bbox)
        width, height, phash = (metadata[k] for k in ("width", "height", "phash"))
        if (not isinstance(width, int) or not isinstance(height, int)
                or not 0 < width <= 16384 or not 0 < height <= 16384
                or not re.fullmatch("[0-9a-f]{16}", phash)):
            raise ProtocolError()
        if (bbox is None) != (vector is None):
            raise ProtocolError()
        if vector is not None:
            if (len(vector) != 2152 or not all(math.isfinite(x) for x in vector)
                    or abs(math.sqrt(sum(x*x for x in vector)) - 1) > .01):
                raise ProtocolError()
            if (len(bbox) != 4 or not all(isinstance(x, int) for x in bbox)
                    or not (0 <= bbox[0] < bbox[2] <= width and 0 <= bbox[1] < bbox[3] <= height)):
                raise ProtocolError()
        dog, cat = float(analysis.dog_confidence), float(analysis.cat_confidence)
        if not 0 <= dog <= 1 or not 0 <= cat <= 1:
            raise ProtocolError()
        return dict(photo_id=str(UUID(photo_id)), index=index, width=width, height=height,
                    phash=phash, dog_confidence=dog, cat_confidence=cat, bbox=bbox, vector=vector)

    async def _process(self, lease):
        job = lease.job
        frames = []
        sources = job["sources"]
        if job["pipeline_version"] != PIPELINE_VERSION or job["model"] != MODEL_NAME:
            raise ProtocolError()
        if job["kind"] == "photo":
            if not 1 <= len(sources) <= 24:
                raise ProtocolError()
            for source in sources:
                raw = await self._download(source, self.config.max_photo_bytes, lease)
                analysis = await self._thread(self.adapter.analyse_photo, raw, lease)
                frames.append(self._frame(source, analysis, source["photo_id"]))
        elif job["kind"] == "video":
            if len(sources) != 1 or sources[0]["photo_id"] is not None:
                raise ProtocolError()
            raw = await self._download(sources[0], self.config.max_clip_bytes, lease)
            processed = await self._thread(self.adapter.extract_clip, raw, lease)
            if not 1 <= len(processed) <= min(12, job["max_frames"]):
                raise MediaLimit()
            specs = []
            for index, photo in enumerate(processed):
                if (not isinstance(photo.original, bytes) or not isinstance(photo.thumbnail, bytes)
                        or not 0 < len(photo.original) <= 20 * 1024 * 1024
                        or not 0 < len(photo.thumbnail) <= 1024 * 1024):
                    raise MediaLimit()
                specs.append(dict(index=index, original_bytes=len(photo.original),
                                  thumbnail_bytes=len(photo.thumbnail)))
            analyses = [await self._thread(self.adapter.analyse_photo, photo.original, lease)
                        for photo in processed]
            lease.check()
            reply = await self._api(job["id"] + "/urls", {"lease_token": job["lease_token"], "frames": specs})
            uploads = reply["uploads"]
            if len(uploads) != len(processed) or {u["index"] for u in uploads} != set(range(len(processed))):
                raise ProtocolError()
            for slot in sorted(uploads, key=lambda x: x["index"]):
                if slot["content_type"] != "image/webp":
                    raise ProtocolError()
                index = slot["index"]
                photo = processed[index]
                frame = self._frame(vars(photo), analyses[index], slot["photo_id"], index)
                await self._upload(slot["original_url"], photo.original, lease)
                await self._upload(slot["thumbnail_url"], photo.thumbnail, lease)
                frames.append(frame)
        else:
            raise ProtocolError()
        if len({f["photo_id"] for f in frames}) != len(frames):
            raise ProtocolError()
        payload = json.dumps(dict(lease_token=job["lease_token"], pipeline_version=PIPELINE_VERSION,
                                  model=MODEL_NAME, frames=frames), allow_nan=False).encode()
        # Encode once: ambiguous transport errors may replay only this exact
        # completion, never a regenerated analysis or a subsequent fail call.
        lease.completing = True
        for attempt in range(self.config.completion_attempts):
            lease.check()
            try:
                reply = await self._api(job["id"] + "/complete", payload)
                if reply.get("status") != "done":
                    raise ProtocolError()
                return
            except (httpx.TransportError, TimeoutError, RemoteError):
                if attempt + 1 == self.config.completion_attempts:
                    raise CompletionUncertain() from None
                await asyncio.sleep(self.config.retry_delay * 2**attempt)

    async def run_once(self) -> bool:
        async with self._serial:
            if self.stop.is_set() or self._native_tasks:
                return False
            started = asyncio.get_running_loop().time()
            reply = await self._api("claim", {"owner": self.config.owner})
            job = reply["job"]
            if job is None:
                return False
            job_id = str(UUID(job["id"]))
            if job_id != job["id"] or not isinstance(job["lease_token"], str) or not 32 <= len(job["lease_token"]) <= 128:
                raise ProtocolError()
            lease = Lease(job, expiry_deadline(job["lease_expires_at"], started))
            work = asyncio.create_task(self._process(lease))
            heartbeat = asyncio.create_task(self._heartbeat(lease))
            try:
                async with asyncio.timeout(self.config.job_timeout):
                    done, _ = await asyncio.wait((work, heartbeat), return_when=asyncio.FIRST_COMPLETED)
                    if work in done:
                        await work
                    else:
                        await heartbeat
            except (LeaseLost, CompletionUncertain):
                pass
            except asyncio.CancelledError:
                self.stop.set()
                raise
            except Exception as error:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                # Do not include str(error), HTTP bodies, URLs, or tracebacks.
                if not lease.completing and not lease.lost and asyncio.get_running_loop().time() < lease.deadline:
                    code = error.code if isinstance(error, WorkerError) else (
                        "processing_timeout" if isinstance(error, TimeoutError) else "processing_failed")
                    retryable = error.retryable if isinstance(error, WorkerError) else not isinstance(error, ValueError)
                    with suppress(Exception):
                        await self._api(job_id + "/fail", dict(lease_token=job["lease_token"],
                                                              error=code, retryable=retryable))
            finally:
                work.cancel()
                heartbeat.cancel()
                await asyncio.gather(work, heartbeat, return_exceptions=True)
            return True

    async def run(self):
        errors = 0
        while not self.stop.is_set():
            try:
                busy = await self.run_once()
                errors = 0
                delay = 0 if busy else self.config.idle_delay
            except Exception:
                errors = min(errors + 1, 8)
                delay = min(self.config.max_backoff, self.config.error_delay * 2**(errors - 1))
            # A lease renewal is not evidence that native inference made progress.
            # The separate process watchdog observes completed claim loops only.
            progress = os.getenv("MEDIA_GPU_PROGRESS_FILE")
            if progress:
                Path(progress).touch()
            if delay:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), delay * random.uniform(0.8, 1.2))


async def serve(adapter: Adapter, config: Config | None = None):
    """SIGTERM stops claims; the current job drains within its job timeout.

    Native GPU hangs require a supervisor's hard kill after the grace period:
    Python cannot forcibly terminate a thread executing a CUDA call.
    """
    async with Worker(config or Config.from_env(), adapter) as worker:
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGINT)
        for sig in signals:
            loop.add_signal_handler(sig, worker.stop.set)
        try:
            await worker.run()
        finally:
            for sig in signals:
                loop.remove_signal_handler(sig)
