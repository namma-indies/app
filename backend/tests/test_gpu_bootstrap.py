"""Synthetic bootstrap coverage: no GPU, model weights, database, or HTTP."""
import asyncio
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from gpu_worker import __main__ as bootstrap


@pytest.fixture
def configured(monkeypatch, tmp_path):
    root = tmp_path.resolve()
    paths = {}
    for name in ("DETECTOR", "EMBEDDER"):
        path = root / f"{name.lower()}.onnx"
        raw = f"synthetic {name}".encode()
        path.write_bytes(raw)
        monkeypatch.setenv(f"MEDIA_GPU_{name}_PATH", str(path))
        monkeypatch.setenv(f"MEDIA_GPU_{name}_SHA256", hashlib.sha256(raw).hexdigest())
        paths[name] = path
    monkeypatch.setenv("MEDIA_GPU_API_URL", "https://api.example.test")
    monkeypatch.setenv("MEDIA_GPU_STORAGE_ORIGINS", "https://storage.example.test")
    monkeypatch.setenv("MEDIA_GPU_TOKEN", "synthetic-secret")
    monkeypatch.delenv("MEDIA_GPU_TOKEN_FILE", raising=False)
    return paths


@pytest.fixture
def runtime(monkeypatch):
    from gpu_worker import client

    calls = {"construct": [], "serve": []}
    adapter = object()
    fake = ModuleType("gpu_worker.inference")

    class GpuInference:
        @classmethod
        def from_paths(cls, detector, embedder):
            calls["construct"].append((detector, embedder))
            calls["bytes"] = (detector.read_bytes(), embedder.read_bytes())
            return adapter

    async def serve(received, config=None):
        assert received is adapter
        assert config.token == "synthetic-secret"
        calls["serve"].append(received)

    fake.GpuInference = GpuInference
    monkeypatch.setitem(sys.modules, "gpu_worker.inference", fake)
    monkeypatch.setattr(client, "serve", serve)
    return calls, fake


def test_verified_snapshots_are_loaded_then_cleaned(configured, runtime):
    calls, _ = runtime
    asyncio.run(bootstrap.run())
    assert calls["bytes"] == (b"synthetic DETECTOR", b"synthetic EMBEDDER")
    assert len(calls["construct"]) == len(calls["serve"]) == 1
    detector, embedder = calls["construct"][0]
    assert detector != configured["DETECTOR"]
    assert embedder != configured["EMBEDDER"]
    assert not detector.exists() and not embedder.exists()


@pytest.mark.parametrize("name", ["DETECTOR", "EMBEDDER"])
@pytest.mark.parametrize("field", ["PATH", "SHA256"])
def test_missing_model_configuration_prevents_sessions(configured, runtime, monkeypatch,
                                                       name, field):
    monkeypatch.delenv(f"MEDIA_GPU_{name}_{field}")
    assert bootstrap.main() == 1
    assert not runtime[0]["construct"]
    assert not runtime[0]["serve"]


@pytest.mark.parametrize("digest", ["", "not-a-hash", "a" * 63, "g" * 64, "0" * 64])
def test_invalid_or_mismatched_hash_prevents_sessions(configured, runtime, monkeypatch, digest):
    monkeypatch.setenv("MEDIA_GPU_EMBEDDER_SHA256", digest)
    assert bootstrap.main() == 1
    assert not runtime[0]["construct"]


def test_uppercase_hash_is_accepted(configured, runtime, monkeypatch):
    key = "MEDIA_GPU_DETECTOR_SHA256"
    monkeypatch.setenv(key, os.environ[key].upper())
    assert bootstrap.main() == 0


@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "symlink", "fifo",
                                   "socket", "hardlink", "parent_symlink", "relative", "parent"])
def test_unsafe_model_paths_prevent_sessions(configured, runtime, monkeypatch, kind):
    source = configured["DETECTOR"]
    path = source.parent / "unsafe"
    if kind == "empty":
        path.touch()
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(source)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        # No bound sockets are needed to exercise the special-file mode guard.
        path.write_bytes(b"synthetic socket inode")
        monkeypatch.setattr(bootstrap.os, "fstat", lambda fd: SimpleNamespace(
            st_mode=stat.S_IFSOCK, st_nlink=1, st_size=32))
    elif kind == "hardlink":
        os.link(source, path)
    elif kind == "parent_symlink":
        path.symlink_to(source.parent, target_is_directory=True)
        path = path / source.name
    elif kind == "relative":
        path = Path("detector.onnx")
    elif kind == "parent":
        path = source.parent / ".." / source.parent.name / source.name
    monkeypatch.setenv("MEDIA_GPU_DETECTOR_PATH", str(path))
    assert bootstrap.main() == 1
    assert not runtime[0]["construct"]


def test_snapshot_is_unchanged_if_source_is_replaced(configured, runtime, monkeypatch):
    _, fake = runtime
    original = fake.GpuInference.from_paths

    def replace_original(detector, embedder):
        configured["DETECTOR"].write_bytes(b"replacement after verification")
        assert detector.read_bytes() == b"synthetic DETECTOR"
        return original(detector, embedder)

    monkeypatch.setattr(fake.GpuInference, "from_paths", replace_original)
    assert bootstrap.main() == 0


def test_invalid_http_configuration_prevents_sessions(configured, runtime, monkeypatch):
    monkeypatch.setenv("MEDIA_GPU_API_URL", "https://user:secret@api.example.test")
    assert bootstrap.main() == 1
    assert not runtime[0]["construct"]


def test_initialization_logs_and_exceptions_are_sanitized(configured, runtime, monkeypatch, capfd):
    _, fake = runtime
    secret = "https://secret.example.test/?token=do-not-print"
    snapshots = []

    def fail(detector, embedder):
        snapshots.extend([detector, embedder])
        print(secret)
        print(secret, file=sys.stderr)
        os.write(2, secret.encode())
        raise RuntimeError(secret)

    monkeypatch.setattr(fake.GpuInference, "from_paths", fail)
    assert bootstrap.main() == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert "gpu_worker:" in captured.err
    assert not runtime[0]["serve"]
    assert all(not path.exists() for path in snapshots)


def test_runtime_exception_is_sanitized(configured, runtime, monkeypatch, capsys):
    from gpu_worker import client

    async def fail(*args, **kwargs):
        raise RuntimeError("https://secret.example.test/?token=do-not-print")

    monkeypatch.setattr(client, "serve", fail)
    assert bootstrap.main() == 1
    captured = capsys.readouterr()
    assert "secret.example" not in captured.err
    assert "Traceback" not in captured.err


def test_module_entrypoint_fails_cleanly_without_configuration():
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEDIA_GPU_")}
    backend = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(backend)
    result = subprocess.run([sys.executable, "-m", "gpu_worker"], env=env,
                            cwd=backend, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("gpu_worker:")
    assert "Traceback" not in result.stderr


def test_worker_manifest_has_complete_gpu_runtime_dependencies():
    requirements = (Path(__file__).resolve().parents[1] /
                    "gpu_worker" / "requirements.txt").read_text()
    lines = [line for line in requirements.splitlines() if line and not line.startswith("#")]
    assert "onnxruntime-gpu==1.26.0" in lines
    for package in ("httpx", "pydantic", "pydantic-settings", "numpy", "Pillow", "ImageHash",
                    "imageio", "imageio-ffmpeg"):
        assert any(line.startswith(package + "==") for line in lines)
    assert not any(line.startswith(("torch", "onnxruntime=", "onnxruntime>", "onnxruntime<"))
                   for line in lines)
