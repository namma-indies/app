import json
import sys

from app import model_health


def test_presence_check_is_fail_closed_and_does_not_load_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(model_health, "MODEL_DIR", tmp_path)
    assert model_health.local_reid_health()["status"] == "unavailable"
    for name in model_health.MODEL_NAMES:
        (tmp_path / name).write_bytes(b"synthetic presence only")
    assert model_health.local_reid_health()["status"] == "ok"
    (tmp_path / model_health.MODEL_NAMES[0]).write_bytes(b"")
    assert model_health.local_reid_health()["status"] == "unavailable"


def test_local_cli_checks_api_liveness_without_a_health_token(monkeypatch, capsys):
    monkeypatch.setattr(model_health, "local_reid_health", lambda: {"status": "ok", "models": {}})
    monkeypatch.setattr(sys, "argv", ["model_health", "--live"])
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, maximum):
            return b'{"status":"ok"}'
    monkeypatch.setattr(model_health, "urlopen", lambda *args, **kwargs: Response())
    assert model_health.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic-private-detail")
    monkeypatch.setattr(model_health, "urlopen", unavailable)
    assert model_health.main() == 1
    assert "synthetic-private-detail" not in capsys.readouterr().out
