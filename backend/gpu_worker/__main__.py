"""Run from backend with ``python -m gpu_worker`` in the separate GPU environment.

Required: MEDIA_GPU_{DETECTOR,EMBEDDER}_{PATH,SHA256}, MEDIA_GPU_API_URL,
MEDIA_GPU_STORAGE_ORIGINS, and exactly one of MEDIA_GPU_TOKEN / TOKEN_FILE.
With MEDIA_GPU_TRANSCRIBE=1, also MEDIA_GPU_WHISPER_{BIN,MODEL,VAD}_{PATH,SHA256}:
a statically linked whisper-server, ggml-large-v3 and a silero VAD model. The
binary and VAD are snapshotted like the ONNX models; large-v3 is verified in
place, like the Qwen weights below, and kept resident in whisper-server.
With MEDIA_GPU_SUGGEST=1, also MEDIA_GPU_LLAMA_{BIN,MODEL,MMPROJ}_{PATH,SHA256}:
llama-server and a Qwen3.8 GGUF with its vision projector. At ~18 GB those two
are verified in place instead of copied: hashed through a descriptor opened
without following symlinks, which is then handed to llama-server, so the bytes
it maps are the bytes that were hashed (barring an in-place rewrite by a PVC
writer, which remain trusted as everywhere else here).
Models must be nonempty, singly-linked regular files under absolute,
symlink-free paths. Private verified snapshots prevent path replacement between
verification and ORT loading; allow temporary disk space for both model files.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
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


def _verified_descriptor(spec: tuple[Path, str], stack: ExitStack) -> int:
    """Hash a pinned file through a descriptor kept open for the process's life."""
    path, expected = spec
    source = stack.enter_context(_open_model(path))
    actual = hashlib.file_digest(source, "sha256").hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise BootstrapError("model_hash_mismatch")
    return source.fileno()


def _snapshot(spec: tuple[Path, str], destination: Path, *, executable: bool = False) -> None:
    path, expected = spec
    with _open_model(path) as source, destination.open("xb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
    with destination.open("rb") as snapshot:
        actual = hashlib.file_digest(snapshot, "sha256").hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise BootstrapError("model_hash_mismatch")
    destination.chmod(0o500 if executable else 0o400)


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
    with ExitStack() as stack:
        with _quiet_startup():
            detector_spec = _model_spec("DETECTOR")
            embedder_spec = _model_spec("EMBEDDER")
            from gpu_worker.client import Config, serve

            config = Config.from_env()
            whisper_specs = ({name: _model_spec("WHISPER_" + name) for name in ("BIN", "MODEL", "VAD")}
                             if config.transcribe else None)
            llama_specs = ({name: _model_spec("LLAMA_" + name) for name in ("BIN", "MODEL", "MMPROJ")}
                           if config.suggest else None)
            with tempfile.TemporaryDirectory(prefix="gpu-models-") as directory:
                root = Path(directory)
                detector, embedder = root / "detector.onnx", root / "embedder.onnx"
                _snapshot(detector_spec, detector)
                _snapshot(embedder_spec, embedder)
                # Importing application settings can itself raise with secret values.
                # Both hashes must pass before importing or constructing ORT sessions.
                from gpu_worker.inference import GpuInference

                adapter = GpuInference.from_paths(detector, embedder)
            transcriber = None
            if whisper_specs is not None:
                # Kept resident like the local Qwen: whisper-server loads
                # large-v3 onto the slice once, here, from the descriptor that
                # was hashed, so no job ever waits on a model load.
                from gpu_worker.transcribe import Transcriber

                root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="gpu-whisper-")))
                binary, vad = root / "whisper-server", root / "vad.bin"
                _snapshot(whisper_specs["BIN"], binary, executable=True)
                _snapshot(whisper_specs["VAD"], vad)
                weights = _verified_descriptor(whisper_specs["MODEL"], stack)
                transcriber = Transcriber(binary, f"/proc/self/fd/{weights}", vad, pass_fds=(weights,))
                transcriber.start()
                stack.callback(transcriber.stop)
            local_model = None
            if llama_specs is not None:
                from gpu_worker.llm import LocalModel

                root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="gpu-llama-")))
                binary = root / "llama-server"
                _snapshot(llama_specs["BIN"], binary, executable=True)
                weights = _verified_descriptor(llama_specs["MODEL"], stack)
                projector = _verified_descriptor(llama_specs["MMPROJ"], stack)
                local_model = LocalModel(binary, f"/proc/self/fd/{weights}", f"/proc/self/fd/{projector}",
                                         pass_fds=(weights, projector))
                # Loading ~18 GB onto the slice; inside the supervisor's 420 s
                # startup allowance.
                local_model.start()
                stack.callback(local_model.stop)
        await serve(adapter, config=config, transcriber=transcriber, model=local_model)


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
