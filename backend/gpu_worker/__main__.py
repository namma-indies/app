"""Run from backend with ``python -m gpu_worker`` in the separate GPU environment.

Required: MEDIA_GPU_{DETECTOR,EMBEDDER}_{PATH,SHA256}, MEDIA_GPU_API_URL,
MEDIA_GPU_STORAGE_ORIGINS, and exactly one of MEDIA_GPU_TOKEN / TOKEN_FILE.
Models must be nonempty, singly-linked regular files under absolute,
symlink-free paths. Private verified snapshots prevent path replacement between
verification and ORT loading; allow temporary disk space for both model files.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile


class BootstrapError(RuntimeError):
    """A startup precondition failed; never attach environment values."""


def _model_spec(name: str) -> tuple[Path, str]:
    raw = os.environ.get(f"MEDIA_GPU_{name}_PATH", "")
    digest = os.environ.get(f"MEDIA_GPU_{name}_SHA256", "")
    path = Path(raw)
    if (not raw or not path.is_absolute() or ".." in path.parts
            or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
        raise BootstrapError("invalid_model_configuration")
    return path, digest.lower()


@contextmanager
def _open_model(path: Path):
    # Walking with dir_fd rejects symlinks in parents as well as the final name,
    # without a check/open race. O_NONBLOCK avoids hanging on a substituted FIFO.
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    descriptor = None
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size == 0:
            raise BootstrapError("invalid_model_file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            yield source
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _snapshot(spec: tuple[Path, str], destination: Path) -> None:
    path, expected = spec
    with _open_model(path) as source, destination.open("xb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
    with destination.open("rb") as snapshot:
        actual = hashlib.file_digest(snapshot, "sha256").hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise BootstrapError("model_hash_mismatch")
    destination.chmod(0o400)


@contextmanager
def _quiet_startup():
    # ORT prints model paths and provider failures from native code, bypassing
    # Python logging. Suppress both native descriptors and Python streams only
    # during synchronous bootstrap; normal operation keeps its safe diagnostics.
    from contextlib import redirect_stderr, redirect_stdout

    with open(os.devnull, "w") as sink:
        saved = [os.dup(fd) for fd in (1, 2)]
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
        finally:
            for fd, original in zip((1, 2), saved):
                os.dup2(original, fd)
                os.close(original)


async def run() -> None:
    with _quiet_startup():
        detector_spec = _model_spec("DETECTOR")
        embedder_spec = _model_spec("EMBEDDER")
        from gpu_worker.client import Config, serve

        config = Config.from_env()
        with tempfile.TemporaryDirectory(prefix="gpu-models-") as directory:
            root = Path(directory)
            detector, embedder = root / "detector.onnx", root / "embedder.onnx"
            _snapshot(detector_spec, detector)
            _snapshot(embedder_spec, embedder)
            # Importing application settings can itself raise with secret values.
            # Both hashes must pass before importing or constructing ORT sessions.
            from gpu_worker.inference import GpuInference

            adapter = GpuInference.from_paths(detector, embedder)
    await serve(adapter, config=config)


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("gpu_worker: startup or runtime failed; check configuration and GPU readiness",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
