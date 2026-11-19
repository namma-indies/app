"""External launch checker. Stdlib + AWS CLI; never import application .env.

State is a private S3 object, not an Actions cache or a public repository issue.
It contains fixed check codes and notification times only. Run from the default
branch with the private settings listed in docs/ops/launch-monitoring.md.
"""
import argparse
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ops_common import OpsError, aws_command

MAX_BODY = 512 * 1024
STATE_KEY = "monitoring/launch-state.json"
QUEUES = ("reid", "transcription", "suggestion", "description",
          "description_backfill", "suggestion_backfill")
DEPENDENCIES = ("database", "object_storage", "database_backup", "local_reid_models", "gpu_worker", "errors")
CHECKS = ("app", "api", "detailed_health", *DEPENDENCIES, *("queue_" + q for q in QUEUES))
STATUSES = {"ok", "unavailable", "unknown", "invalid", "unauthorized", "disabled",
            "paused", "stale", "missing", "unconfigured", "elevated"}
HEALTHY = {"ok", "disabled", "paused"}


def secure_url(value, *, base=False):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or "\\" in value or any(ord(c) <= 32 for c in value)
                or (base and (parsed.path not in ("", "/") or parsed.query))):
            raise ValueError
        parsed.port
    except (ValueError, TypeError):
        raise OpsError("configuration_invalid") from None
    return value.rstrip("/") if base else value


@dataclass(frozen=True)
class Config:
    app_url: str = field(repr=False)
    api_url: str = field(repr=False)
    token: str = field(repr=False)
    webhook: str = field(repr=False)
    bucket: str = field(repr=False)
    endpoint: str = field(default="", repr=False)
    reminder_s: int = 21600

    @classmethod
    def from_env(cls):
        try:
            result = cls(
                secure_url(os.environ["MONITOR_APP_URL"], base=True),
                secure_url(os.environ["MONITOR_API_URL"], base=True),
                os.environ["HEALTH_CHECK_TOKEN"],
                secure_url(os.environ["MONITOR_WEBHOOK_URL"]),
                os.environ["MONITOR_STATE_BUCKET"],
                secure_url(os.environ["MONITOR_STATE_ENDPOINT"], base=True)
                    if os.environ.get("MONITOR_STATE_ENDPOINT") else "",
                int(os.environ.get("MONITOR_REMINDER_S") or "21600"),
            )
            if (not result.token or not result.bucket or result.reminder_s < 600
                    or any(ord(c) < 33 or ord(c) > 126 for c in result.token)):
                raise ValueError
            return result
        except (ValueError, KeyError):
            raise OpsError("configuration_invalid") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, *, token=None, payload=None, sample=False):
    headers = {"Accept-Encoding": "identity", "User-Agent": "IndieDex-launch-monitor"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if sample:
        headers["Range"] = "bytes=0-1023"
    req = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                  headers=headers)
    opener = build_opener(NoRedirect(), ProxyHandler({}))
    try:
        try:
            response = opener.open(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            body = response.read(1024 if sample else MAX_BODY + 1)
            if ((not sample and len(body) > MAX_BODY)
                    or response.headers.get("Content-Encoding", "identity") != "identity"):
                raise OpsError("http_response_invalid")
            return response.status, response.headers, body
    except OpsError:
        raise
    except Exception:
        raise OpsError("http_unavailable") from None


class AppPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = False
        self.modules = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and attrs.get("id") == "root":
            self.root = True
        if tag == "script" and attrs.get("type") == "module" and attrs.get("src"):
            self.modules.append(attrs["src"])


def app_status(base):
    try:
        status, headers, body = request(base + "/")
        if status != 200:
            return "unavailable"
        if "text/html" not in headers.get("Content-Type", ""):
            return "invalid"
        page = AppPage()
        page.feed(body.decode("utf-8"))
        if not page.root or not page.modules:
            return "invalid"
        asset = urljoin(base + "/", page.modules[0])
        # Never fetch a foreign origin named by an HTML response.
        secure_url(asset)
        if urlsplit(asset).netloc != urlsplit(base).netloc:
            return "invalid"
        status, headers, body = request(asset, sample=True)
        if status not in (200, 206):
            return "unavailable"
        if (not body.strip() or b"<html" in body[:100].lower()
                or not any(t in headers.get("Content-Type", "") for t in
                           ("text/javascript", "application/javascript"))):
            return "invalid"
        return "ok"
    except Exception:
        return "unavailable"


def api_checks(base, token):
    checks = {name: "unknown" for name in ("api", "detailed_health", *DEPENDENCIES,
                                          *("queue_" + q for q in QUEUES))}
    try:
        status, _, body = request(base + "/health")
        checks["api"] = "ok" if status == 200 and json.loads(body) == {"status": "ok"} else "invalid"
    except Exception:
        checks["api"] = "unavailable"
    try:
        status, _, body = request(base + "/health/detailed", token=token)
        if status in (401, 403, 404):
            checks["detailed_health"] = "unauthorized"
            return checks
        if status not in (200, 503):
            raise ValueError
        report = json.loads(body)
        for name in DEPENDENCIES:
            code = report["checks"][name]["status"]
            if code not in STATUSES:
                raise ValueError
            checks[name] = code
        for name in QUEUES:
            code = report["queues"][name]["status"]
            if code not in STATUSES:
                raise ValueError
            checks["queue_" + name] = code
        healthy = all(checks[name] in HEALTHY for name in (*DEPENDENCIES, *("queue_" + q for q in QUEUES)))
        if report["status"] != ("ok" if healthy else "degraded") or status != (200 if healthy else 503):
            raise ValueError
        checks["detailed_health"] = "ok"
    except OpsError:
        checks["detailed_health"] = "unavailable"
    except Exception:
        checks["detailed_health"] = "invalid"
    return checks


class S3State:
    def __init__(self, bucket, endpoint=""):
        self.bucket, self.endpoint = bucket, endpoint

    def load(self):
        with tempfile.TemporaryDirectory(prefix="launch-state-") as directory:
            path = Path(directory) / "state.json"
            result = aws_command(["get-object", "--bucket", self.bucket, "--key", STATE_KEY,
                                  str(path)], endpoint=self.endpoint, missing_ok=True)
            if result is None:
                return {"version": 1, "checks": {}, "last_notified": 0}
            try:
                if path.stat().st_size > 64 * 1024:
                    raise ValueError
                state = json.loads(path.read_text())
                if (state["version"] != 1 or not isinstance(state["checks"], dict)
                        or not set(state["checks"]).issubset(CHECKS)
                        or not all(code in STATUSES for code in state["checks"].values())
                        or type(state["last_notified"]) is not int or state["last_notified"] < 0):
                    raise ValueError
                return state
            except (OSError, ValueError, KeyError, TypeError):
                raise OpsError("state_invalid") from None

    def save(self, state):
        with tempfile.TemporaryDirectory(prefix="launch-state-") as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state, sort_keys=True))
            path.chmod(0o600)
            aws_command(["put-object", "--bucket", self.bucket, "--key", STATE_KEY,
                         "--body", str(path), "--content-type", "application/json"], endpoint=self.endpoint)


def notification_event(previous, checks, now, reminder_s):
    bad = any(code not in HEALTHY for code in checks.values())
    previous_bad = any(code not in HEALTHY for code in previous["checks"].values())
    if checks != previous["checks"]:
        if bad:
            return "changed" if previous_bad else "incident"
        if previous_bad:
            return "recovery"
    if bad and now - previous["last_notified"] >= reminder_s:
        return "reminder"
    return None


def notify(webhook, event, checks):
    # Fixed names/codes only: no URLs, ages, counts, headers or response bodies.
    text = "IndieDex launch monitor: " + event + "\n" + "\n".join(
        f"{name}: {code}" for name, code in sorted(checks.items()) if code not in HEALTHY)
    status, _, _ = request(webhook, payload={"text": text})
    if not 200 <= status < 300:
        raise OpsError("notification_failed")


def run(config, *, state=None, now=None):
    state = state or S3State(config.bucket, config.endpoint)
    previous = state.load()  # Do not spam when durable state cannot be read.
    checks = {"app": app_status(config.app_url), **api_checks(config.api_url, config.token)}
    now = int(time.time()) if now is None else now
    event = notification_event(previous, checks, now, config.reminder_s)
    if event:
        # Prove write access before notifying. A read-only state store must
        # not turn a single outage into a new notification every ten minutes.
        state.save(previous)
        notify(config.webhook, event, checks)
    state.save({"version": 1, "checks": checks,
                "last_notified": now if event else previous["last_notified"]})
    print("launch_monitor: " + (event + " notification sent" if event else "unchanged"))
    # Service incidents are delivered by webhook; infrastructure/configuration
    # failures exit nonzero so the workflow itself can be watched separately.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-alert", action="store_true", help="send a fixed test message, without changing state")
    args = parser.parse_args()
    try:
        if args.test_alert:
            notify(secure_url(os.environ["MONITOR_WEBHOOK_URL"]), "test", {})
            print("launch_monitor: test notification sent")
        else:
            run(Config.from_env())
        return 0
    except OpsError as error:
        print("launch_monitor: " + str(error), file=sys.stderr)
    except Exception:
        print("launch_monitor: check_failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
