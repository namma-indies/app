from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_launch as monitor
from scripts.ops_common import OpsError


def config():
    return monitor.Config("https://app.example.test", "https://api.example.test",
                          "synthetic-health-secret", "https://alerts.example.test/private-token", "synthetic-state")


class State:
    def __init__(self):
        self.value = {"version": 1, "checks": {}, "last_notified": 0}
        self.writes = 0
    def load(self):
        return deepcopy(self.value)
    def save(self, state):
        self.value = deepcopy(state)
        self.writes += 1


class Services:
    def __init__(self):
        self.app = 200
        self.asset = 200
        self.asset_type = "text/javascript"
        self.module = "/assets/app.js"
        self.public_health = {"status": "ok"}
        self.detail_code = None
        self.queue = "ok"
        self.age = 0
        self.webhook = 200
        self.notifications = []
        self.requests = []
    def request(self, url, *, token=None, payload=None, sample=False):
        self.requests.append((url, token))
        if url == config().webhook:
            self.notifications.append(payload)
            return self.webhook, {}, b""
        if url == config().app_url + "/":
            return self.app, {"Content-Type": "text/html"}, (
                '<div id="root"></div><script type="module" src="' + self.module + '"></script>').encode()
        if url.endswith("/assets/app.js"):
            assert sample is True
            return self.asset, {"Content-Type": self.asset_type}, b"console.log('synthetic');"
        if url.endswith("/health"):
            return 200, {}, json.dumps(self.public_health).encode()
        assert url.endswith("/health/detailed")
        assert token == config().token
        if self.detail_code is not None:
            return self.detail_code, {}, b"{}"
        report = {"status": "ok" if self.queue == "ok" else "degraded",
                  "checks": {key: {"status": "ok"} for key in monitor.DEPENDENCIES},
                  "queues": {key: {"status": "ok", "oldest_age_s": self.age} for key in monitor.QUEUES}}
        report["queues"]["transcription"]["status"] = self.queue
        return 200 if self.queue == "ok" else 503, {}, json.dumps(report).encode()


@pytest.fixture
def services(monkeypatch):
    result = Services()
    monkeypatch.setattr(monitor, "request", result.request)
    return result


def test_incident_dedup_reminder_change_and_recovery(services):
    state = State()
    monitor.run(config(), state=state, now=1000)
    assert not services.notifications
    services.queue = "stale"
    monitor.run(config(), state=state, now=1100)
    assert "incident" in services.notifications[-1]["text"]
    for now in (1200, 1800, 22000):
        services.age += 600
        monitor.run(config(), state=state, now=now)
    assert len(services.notifications) == 1  # Ages never change incident identity.
    monitor.run(config(), state=state, now=22700)
    assert "reminder" in services.notifications[-1]["text"]
    services.app = 503
    monitor.run(config(), state=state, now=22800)
    assert "changed" in services.notifications[-1]["text"]
    services.app, services.queue = 200, "ok"
    monitor.run(config(), state=state, now=22900)
    assert "recovery" in services.notifications[-1]["text"]
    monitor.run(config(), state=state, now=23000)
    assert len(services.notifications) == 4
    serialized = json.dumps(services.notifications) + json.dumps(state.value)
    assert "https://" not in serialized and "synthetic-health-secret" not in serialized


def test_initial_incident_alerts_and_failed_delivery_is_retried(services):
    services.app, services.webhook = 503, 500
    state = State()
    with pytest.raises(OpsError, match="notification_failed"):
        monitor.run(config(), state=state, now=1000)
    assert state.value["checks"] == {} and state.value["last_notified"] == 0
    services.webhook = 200
    monitor.run(config(), state=state, now=1100)
    assert state.value["last_notified"] == 1100
    assert "incident" in services.notifications[-1]["text"]


def test_broken_state_store_never_sends_an_untracked_alert(services):
    class BrokenState:
        def load(self):
            raise OpsError("object_store_command_failed")
    with pytest.raises(OpsError):
        monitor.run(config(), state=BrokenState())
    assert not services.requests


def test_read_only_state_store_never_repeats_uncheckpointed_notifications(services):
    services.app = 503
    class ReadOnlyState(State):
        def save(self, state):
            raise OpsError("object_store_command_failed")
    for now in (1000, 1600):
        with pytest.raises(OpsError):
            monitor.run(config(), state=ReadOnlyState(), now=now)
    assert not services.notifications


@pytest.mark.parametrize("status", [401, 403, 404])
def test_health_auth_failure_is_an_incident(services, status):
    services.detail_code = status
    state = State()
    monitor.run(config(), state=state, now=1000)
    assert state.value["checks"]["detailed_health"] == "unauthorized"
    assert "incident" in services.notifications[-1]["text"]


def test_broken_js_bundle_is_detected_even_when_html_and_api_are_up(services):
    services.asset = 404
    monitor.run(config(), state=State(), now=1000)
    assert "app: unavailable" in services.notifications[-1]["text"]


def test_spa_fallback_cannot_masquerade_as_javascript(services):
    services.asset_type = "text/html"
    assert monitor.app_status(config().app_url) == "invalid"


def test_html_cannot_make_the_checker_fetch_a_foreign_origin(services):
    services.module = "https://elsewhere.example.test/assets/app.js"
    assert monitor.app_status(config().app_url) == "invalid"
    assert len(services.requests) == 1


def test_worker_token_is_never_sent_to_frontend_or_notification_destination(services):
    monitor.run(config(), state=State(), now=1000)
    assert [url for url, token in services.requests if token] == [config().api_url + "/health/detailed"]


def test_missing_or_inconsistent_health_schema_fails_closed(services):
    services.public_health = {"status": "ok", "models": {"unexpected": True}}
    assert monitor.api_checks(config().api_url, config().token)["api"] == "invalid"
    services.queue = "invented"
    assert monitor.api_checks(config().api_url, config().token)["detailed_health"] == "invalid"


@pytest.mark.parametrize("url", ["http://app.example.test", "https://user:pass@app.example.test", "https://app.example.test/#fragment", "https://app.example.test/\n"])
def test_monitor_urls_require_https_without_userinfo_or_control_characters(url):
    with pytest.raises(OpsError, match="configuration_invalid"):
        monitor.secure_url(url, base=True)


def test_state_missing_and_invalid_are_distinct(monkeypatch):
    monkeypatch.setattr(monitor, "aws_command", lambda *args, **kwargs: None)
    assert monitor.S3State("synthetic").load()["checks"] == {}
    def invalid(args, **kwargs):
        Path(args[-1]).write_text('{"version":1,"checks":{"private-host":"ok"},"last_notified":0}')
        return {}
    monkeypatch.setattr(monitor, "aws_command", invalid)
    with pytest.raises(OpsError, match="state_invalid"):
        monitor.S3State("synthetic").load()


def test_s3_errors_never_print_bucket_names_or_credentials(monkeypatch):
    from scripts import ops_common
    monkeypatch.setattr(ops_common.subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(returncode=1, stdout=b"", stderr=b"AccessDenied synthetic-private-bucket secret"))
    with pytest.raises(OpsError) as error:
        ops_common.aws_command(["get-object"], missing_ok=True)
    assert str(error.value) == "object_store_command_failed"


def test_large_bundle_probe_reads_a_bounded_prefix_even_if_range_is_ignored(monkeypatch):
    class Response:
        status = 200
        headers = {"Content-Type": "text/javascript", "Content-Length": "2000000"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, maximum):
            assert maximum == 1024
            return b"a" * maximum
    class Opener:
        def open(self, req, timeout):
            assert req.get_header("Range") == "bytes=0-1023"
            assert not req.get_header("Authorization")
            return Response()
    monkeypatch.setattr(monitor, "build_opener", lambda *args: Opener())
    assert len(monitor.request("https://app.example.test/asset.js", sample=True)[2]) == 1024


def test_top_level_failure_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr(monitor.sys, "argv", ["check_launch"])
    monkeypatch.setattr(monitor.Config, "from_env", lambda: config())
    def failed(*args, **kwargs):
        raise ValueError("synthetic-private-url-and-token")
    monkeypatch.setattr(monitor, "run", failed)
    assert monitor.main() == 1
    assert capsys.readouterr().err == "launch_monitor: check_failed\n"
