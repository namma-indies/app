"""Suggestions on the DGX worker: the API builds the request, the worker runs it
locally with images inlined, and the API checks the raw answer."""
from dataclasses import replace
import io
import json
import stat
import sys
import textwrap

import httpx
import pytest

from gpu_worker.client import Config, MediaLimit, SUGGEST_ROUTE, Worker
from gpu_worker.llm import LocalModel, LocalModelError
from tests.test_gpu_client import LEASE, TOKEN, FakeAdapter, expiry

def _webp():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (32, 24), (200, 120, 40)).save(out, "WEBP")
    return out.getvalue()


WEBP = _webp()
IMAGE_URL = "https://objects.example/thumb.webp?signature=storage-secret"


def request_with(images):
    content = [{"type": "text", "text": "Detected language: en\nNote:\nThe brown one is Kaalu."}]
    for label in images:
        content += [{"type": "text", "text": f"Animal {label}:"},
                    {"type": "image_url", "image_url": {"url": IMAGE_URL}}]
    return {"messages": [{"role": "system", "content": "prompt"}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": {}}}}


class FakeModel:
    def __init__(self, answer='{"suggestions": []}'):
        self.bodies, self.answer = [], answer

    def complete(self, body):
        self.bodies.append(body)
        return self.answer


class Server:
    def __init__(self, request, image=WEBP):
        self.job = {"id": "01a16ed8-7376-7f11-b7c2-f2803172b790", "kind": "suggest",
                    "lease_token": LEASE, "lease_expires_at": expiry(), "request": request}
        self.image = image
        self.calls, self.completions, self.failures = [], [], []

    async def api(self, request):
        assert request.headers["authorization"] == "Bearer " + TOKEN
        queue, route = request.url.path.split("/")[2], request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((queue, route))
        if route == "claim":
            if queue != "suggestion-jobs":
                return httpx.Response(200, json={"job": None})
            claimed, self.job = self.job, None
            return httpx.Response(200, json={"job": claimed})
        if route == "complete":
            from app.suggestions import Reply
            Reply.model_validate(body)
            self.completions.append(body)
            return httpx.Response(200, json={"status": "done"})
        if route == "fail":
            self.failures.append(body)
            return httpx.Response(200, json={"status": "failed"})
        if route == "heartbeat":
            return httpx.Response(200, json={"lease_expires_at": expiry()})
        raise AssertionError(route)

    async def storage(self, request):
        return httpx.Response(200, content=self.image)

    def worker(self, model):
        cfg = replace(Config("https://api.example", TOKEN, ("https://objects.example",),
                             heartbeat_interval=.015, retry_delay=.001, idle_delay=.01),
                      suggest=model is not None)
        return Worker(cfg, FakeAdapter(), model=model,
                      api_transport=httpx.MockTransport(self.api),
                      storage_transport=httpx.MockTransport(self.storage))


async def test_runs_the_apis_request_locally_with_images_inlined():
    server, model = Server(request_with(["A", "B"])), FakeModel('{"suggestions": [1]}')
    async with server.worker(model) as worker:
        assert await worker.run_once()
    assert server.calls[:2] == [("media-jobs", "claim"), ("suggestion-jobs", "claim")]
    body = model.bodies[0]
    images = [p["image_url"]["url"] for m in body["messages"] if isinstance(m["content"], list)
              for p in m["content"] if p["type"] == "image_url"]
    # Stored photos are WebP, which llama.cpp cannot load: the model gets JPEG.
    assert len(images) == 2 and all(u.startswith("data:image/jpeg;base64,") for u in images)
    assert "storage-secret" not in json.dumps(body)  # the model never sees a fetchable URL
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert server.completions == [{"lease_token": LEASE, "content": '{"suggestions": [1]}'}]


async def test_a_worker_without_a_model_never_asks_for_suggestions():
    server = Server(request_with([]))
    async with server.worker(None) as worker:
        assert not await worker.run_once()
    assert ("suggestion-jobs", "claim") not in server.calls


async def test_refuses_more_images_than_a_capture_can_have():
    server, model = Server(request_with(list("ABCDEFGHI"))), FakeModel()
    async with server.worker(model) as worker:
        assert await worker.run_once()
    assert not model.bodies
    assert server.failures[0]["error"] == MediaLimit.code


async def test_refuses_an_image_that_is_not_one():
    server, model = Server(request_with(["A"]), image=b"<html>login page</html>"), FakeModel()
    async with server.worker(model) as worker:
        assert await worker.run_once()
    assert not model.bodies and server.failures


async def test_refuses_a_webp_that_does_not_decode():
    server, model = Server(request_with(["A"]), image=b"RIFF\x10\x00\x00\x00WEBPVP8 truncated"), FakeModel()
    async with server.worker(model) as worker:
        assert await worker.run_once()
    assert not model.bodies
    assert server.failures[0]["error"] == MediaLimit.code


def test_config_and_model_must_agree():
    cfg = Config("https://api.example", TOKEN, ("https://objects.example",), suggest=True)
    from gpu_worker.client import ProtocolError
    with pytest.raises(ProtocolError):
        Worker(cfg, FakeAdapter())


# ---------------------------------------------------------------------------
# LocalModel, against a stand-in llama-server
# ---------------------------------------------------------------------------

FAKE_SERVER = textwrap.dedent('''\
    #!{python}
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = int(sys.argv[sys.argv.index("--port") + 1])
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404); self.end_headers()
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = {{"choices": [{{"message": {{"content": json.dumps({{"echo": body["model"]}})}}}}]}}
            data = json.dumps(reply).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


def fake_binary(tmp_path, body):
    path = tmp_path / "llama-server"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_local_model_starts_answers_and_stops(tmp_path):
    binary = fake_binary(tmp_path, FAKE_SERVER.format(python=sys.executable))
    model = LocalModel(binary, "model.gguf", "mmproj.gguf", port=18931, startup_timeout=20)
    model.start()
    try:
        assert json.loads(model.complete({"model": "local", "messages": []})) == {"echo": "local"}
    finally:
        model.stop()
    assert not model.alive()
    with pytest.raises(LocalModelError):
        model.complete({})


def test_local_model_that_dies_on_startup_is_reported(tmp_path):
    binary = fake_binary(tmp_path, "#!/bin/sh\nexit 3\n")
    with pytest.raises(LocalModelError):
        LocalModel(binary, "m", "p", port=18932, startup_timeout=10).start()


def test_local_model_listens_on_loopback_only(tmp_path):
    model = LocalModel("llama-server", "m", "p")
    host = model.command[model.command.index("--host") + 1]
    assert host == "127.0.0.1" and model.url.startswith("http://127.0.0.1:")


def test_local_model_runs_one_slot_with_a_long_8bit_context():
    command = LocalModel("llama-server", "m", "p").command
    flag = lambda name: command[command.index(name) + 1]
    assert (flag("-c"), flag("-np"), flag("-fa"), flag("-ctk"), flag("-ctv")) == ("131072", "1", "on", "q8_0", "q8_0")
