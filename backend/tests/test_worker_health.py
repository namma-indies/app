import json

import httpx
import pytest

from gpu_worker.client import Config, Worker


@pytest.mark.parametrize("health_status", [200, 404, 500])
async def test_loaded_worker_reports_capabilities_and_telemetry_failure_never_stops_claims(health_status):
    calls = []
    async def api(request):
        assert request.headers["authorization"] == "Bearer synthetic-worker-token"
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/internal/worker-health":
            return httpx.Response(health_status, json={"status": "ok"})
        assert request.url.path.endswith("/claim")
        worker.stop.set()
        return httpx.Response(200, json={"job": None})
    config = Config("https://api.example.test", "synthetic-worker-token", ("https://objects.example.test",),
                    transcribe=True, suggest=True, multi_animal=True)
    async with Worker(config, object(), transcriber=object(), model=object(),
                      api_transport=httpx.MockTransport(api)) as worker:
        await worker.run()
    assert calls[0] == ("/internal/worker-health", {"transcription": True, "suggestions": True, "multi_animal": True})
    assert any(path == "/internal/capture-jobs/claim" for path, _ in calls)
    assert any(path == "/internal/transcription-jobs/claim" for path, _ in calls)
    assert any(path == "/internal/suggestion-jobs/claim" for path, _ in calls)


async def test_unloaded_optional_models_are_not_advertised():
    report = []
    async def api(request):
        if request.url.path == "/internal/worker-health":
            report.append(json.loads(request.content))
        else:
            worker.stop.set()
        return httpx.Response(200, json={"job": None})
    async with Worker(Config("https://api.example.test", "synthetic", ("https://objects.example.test",)), object(),
                      api_transport=httpx.MockTransport(api)) as worker:
        await worker.run()
    assert report == [{"transcription": False, "suggestions": False, "multi_animal": False}]
