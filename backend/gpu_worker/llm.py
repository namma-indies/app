"""A local vision language model for suggestion jobs: llama-server on loopback.

``LocalModel(binary, model, mmproj).start()`` launches llama-server bound to
127.0.0.1 in its own process group and waits until it answers /health;
``complete(body)`` sends one OpenAI-style chat request and returns the
assistant's message content. It stays up for the worker's lifetime, as
whisper-server does: loading ~18 GB of weights takes far longer than any one
answer, so no request should ever wait on it.

The worker remains outbound-only. The server listens on loopback inside the
pod, takes no API key because nothing outside the pod can reach it, and never
fetches anything itself: the client inlines every image before a request.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

import httpx

# Never hand the worker's API token to the model server.
_SECRET_ENV = ("MEDIA_GPU_TOKEN",)


class LocalModelError(RuntimeError):
    """The model server failed to start, exited, or answered off-contract."""


class LocalModel:
    def __init__(self, binary, model, mmproj, *, pass_fds: tuple[int, ...] = (), port: int = 8091,
                 context: int = 131072, startup_timeout: float = 300, request_timeout: float = 180):
        self.command = [
            str(binary), "-m", str(model), "--mmproj", str(mmproj),
            "--host", "127.0.0.1", "--port", str(port),
            # All layers on the GPU slice; one request at a time, like the
            # rest of this worker. A 128k context fits the slice beside
            # whisper and the ONNX models because the KV cache is 8-bit
            # (which on llama.cpp needs flash attention for V).
            "-ngl", "999", "-c", str(context), "-np", "1",
            "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
            "--jinja", "--no-webui",
        ]
        self.pass_fds = pass_fds
        self.url = f"http://127.0.0.1:{port}"
        self.startup_timeout, self.request_timeout = startup_timeout, request_timeout
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, start_new_session=True, close_fds=True,
            pass_fds=self.pass_fds)
        deadline = time.monotonic() + self.startup_timeout
        with httpx.Client(trust_env=False, timeout=5) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise LocalModelError("model server exited during startup")
                try:
                    if client.get(self.url + "/health").status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(1)
        self.stop()
        raise LocalModelError("model server did not become ready")

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def complete(self, body: dict) -> str:
        if not self.alive():
            raise LocalModelError("model server is not running")
        with httpx.Client(trust_env=False, timeout=self.request_timeout) as client:
            response = client.post(self.url + "/v1/chat/completions", json=body)
        if response.status_code != 200:
            raise LocalModelError("model server refused the request")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LocalModelError("model server answered off-contract") from None
        if not isinstance(content, str):
            raise LocalModelError("model server answered off-contract")
        return content

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=20)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
