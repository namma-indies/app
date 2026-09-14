"""Offline contract tests; these do not validate a container or CUDA runtime."""
import importlib.util
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

import pytest

from gpu_worker import supervise
from gpu_worker.client import Config, ProtocolError, Worker

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("render_gpu_worker", ROOT / "deploy/render_gpu_worker.py")
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


@pytest.fixture
def config():
    return dict(namespace="synthetic", name="gpu-worker", image="registry.example.test/worker@sha256:" + "a" * 64,
                source_revision="b" * 40, model_pvc="synthetic-models", token_secret="synthetic-token",
                token_key="token", api_url="https://api.example.test", storage_origins=["https://objects.example.test"],
                detector_file="detector.onnx", embedder_file="embedder.onnx",
                detector_sha256="c" * 64, embedder_sha256="d" * 64, gpu_resource="nvidia.com/mig-3g.71gb")


def test_manifest_contract(config):
    result = recipe.render(config)
    cm, deploy = result["items"]
    pod = deploy["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert deploy["spec"]["replicas"] == 1
    assert deploy["spec"]["strategy"] == {"type": "Recreate"}
    assert not pod["automountServiceAccountToken"]
    assert container["securityContext"]["readOnlyRootFilesystem"]
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["resources"]["limits"]["nvidia.com/mig-3g.71gb"] == 1
    assert pod["volumes"][0]["persistentVolumeClaim"]["readOnly"]
    assert pod["volumes"][1]["secret"]["defaultMode"] == 0o440
    assert cm["data"]["MEDIA_GPU_TOKEN_FILE"] == "/run/worker-token/token"
    assert "MEDIA_GPU_TOKEN" not in cm["data"]
    assert cm["data"]["MEDIA_GPU_DETECTOR_PATH"] == "/models/detector.onnx"
    assert "${" not in json.dumps(result)


def test_default_selector_unchanged(config):
    spec = recipe.render(config)["items"][1]["spec"]
    assert spec["selector"] == {"matchLabels": {"app.kubernetes.io/name": config["name"]}}
    assert spec["template"]["metadata"]["labels"] == spec["selector"]["matchLabels"]


@pytest.mark.parametrize("fixture", ["config", "pvc_config"])
def test_preserve_legacy_selector_for_in_place_rollout(request, fixture):
    config = request.getfixturevalue(fixture)
    legacy = {"app": "existing-worker"}
    config["selector_labels"] = legacy
    deployment = recipe.render(config)["items"][1]
    spec = deployment["spec"]
    assert deployment["metadata"]["name"] == config["name"]
    assert spec["replicas"] == 1 and spec["strategy"] == {"type": "Recreate"}
    assert spec["selector"] == {"matchLabels": legacy}
    assert spec["template"]["metadata"]["labels"] == dict(legacy, **{"app.kubernetes.io/name": config["name"]})
    assert legacy == {"app": "existing-worker"}


@pytest.mark.parametrize("labels", [
    {}, None, [], {"": "worker"}, {"app": None}, {"app": True}, {1: "worker"},
    {"app": "${WORKER}"}, {"app": "two words"}, {"app": "-bad"},
    {"app": "a" * 64}, {"a" * 64: "worker"}, {"a/b/c": "worker"},
    {"UPPER.example/key": "worker"}, {"bad..example/key": "worker"},
    {"a" * 64 + ".example/key": "worker"},
    {"a." * 126 + "aa/key": "worker"}, {"/key": "worker"},
    {"app.kubernetes.io/name": "conflicting-name"},
])
def test_reject_invalid_or_conflicting_selector(config, labels):
    config["selector_labels"] = labels
    with pytest.raises(ValueError):
        recipe.render(config)


def test_accept_kubernetes_label_syntax_and_matching_owned_label(config):
    labels = {"example.test/Role_1": "GPU.v1", "empty": "", "app.kubernetes.io/name": config["name"]}
    config["selector_labels"] = labels
    spec = recipe.render(config)["items"][1]["spec"]
    assert spec["selector"]["matchLabels"] == labels
    assert spec["template"]["metadata"]["labels"] == labels


@pytest.mark.parametrize("key", sorted(recipe.REQUIRED))
def test_missing_required(config, key):
    del config[key]
    with pytest.raises(ValueError):
        recipe.render(config)


@pytest.mark.parametrize("key,value", [
    ("namespace", "${NAMESPACE}"), ("name", "REPLACE_ME"), ("image", "worker:latest"),
    ("source_revision", "main"), ("token_secret", ""), ("token_key", ""),
    ("detector_sha256", "not-a-hash"), ("detector_file", "../secret.onnx"),
    ("embedder_file", "/private/model.onnx"), ("gpu_resource", "cpu"),
    ("api_url", "http://api.example.test"), ("api_url", "https://localhost"),
    ("api_url", "https://127.0.0.1"), ("api_url", "https://api.example.test/path"),
    ("api_url", "https://user:secret@api.example.test"), ("api_url", "https://api.example.test?"),
    ("api_url", "https://api.example.test:bad"), ("storage_origins", []),
    ("storage_origins", ["https://*.example.test"]), ("storage_origins", [""]),
    ("storage_origins", ["https://objects.example.test,bad"]), ("uid", 0), ("gid", True),
    ("token", "must-not-be-rendered"), ("extra", "unknown"),
])
def test_bad_configuration(config, key, value):
    config[key] = value
    with pytest.raises(ValueError):
        recipe.render(config)


def test_renderer_failure_does_not_echo_private_input(tmp_path):
    path = tmp_path / "input.json"
    path.write_text('{"token": "DO-NOT-LOG-THIS"}')
    result = subprocess.run([sys.executable, str(ROOT / "deploy/render_gpu_worker.py"), str(path)], capture_output=True, text=True)
    assert result.returncode == 2
    assert not result.stdout
    assert "DO-NOT-LOG-THIS" not in result.stderr


def test_empty_secret_fails_bootstrap(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text(" \n")
    monkeypatch.delenv("MEDIA_GPU_TOKEN", raising=False)
    monkeypatch.setenv("MEDIA_GPU_TOKEN_FILE", str(token))
    monkeypatch.setenv("MEDIA_GPU_API_URL", "https://api.example.test")
    monkeypatch.setenv("MEDIA_GPU_STORAGE_ORIGINS", "https://objects.example.test")
    with pytest.raises(ProtocolError):
        Config.from_env()


def test_watchdog_kills_unresponsive_child(tmp_path):
    started = time.monotonic()
    result = supervise.supervise([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
                                 tmp_path / "progress", timeout=.3, hang_grace=.1, interval=.01)
    assert result == 1
    assert time.monotonic() - started < 5


def test_watchdog_accepts_progress(tmp_path):
    progress = tmp_path / "progress"
    code = "import pathlib,time,sys; p=pathlib.Path(sys.argv[1]); " + "\nfor _ in range(10): p.touch(); time.sleep(.04)"
    assert supervise.supervise([sys.executable, "-c", code, str(progress)], progress,
                               timeout=.25, hang_grace=.1, interval=.01) == 0


def test_watchdog_bounds_shutdown_even_when_child_ignores_term(tmp_path):
    ready = tmp_path / "ready"
    code = ("from pathlib import Path; import signal,time,sys; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path(sys.argv[1]).touch(); time.sleep(60)")
    parent_code = ("from gpu_worker.supervise import supervise; from pathlib import Path; "
                   "import sys; raise SystemExit(supervise(" + repr([sys.executable, "-c", code, str(ready)]) +
                   ", Path(sys.argv[1]), timeout=30, stop_grace=.1, interval=.01))")
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(tmp_path / "progress")],
                              start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        parent.send_signal(signal.SIGTERM)
        assert parent.wait(timeout=5) == 1
    finally:
        if parent.poll() is None:
            os.killpg(parent.pid, signal.SIGKILL)
            parent.wait()


def test_config_change_triggers_rollout(config):
    old = recipe.render(config)["items"][1]["spec"]["template"]["metadata"]["annotations"]
    config["detector_sha256"] = "e" * 64
    new = recipe.render(config)["items"][1]["spec"]["template"]["metadata"]["annotations"]
    assert old["indiedex.org/config-sha256"] != new["indiedex.org/config-sha256"]


def test_watchdog_propagates_exit(tmp_path):
    assert supervise.supervise([sys.executable, "-c", "raise SystemExit(7)"], tmp_path / "progress", interval=.01) == 7


@pytest.mark.asyncio
async def test_claim_loop_writes_progress(monkeypatch, tmp_path):
    progress = tmp_path / "progress"
    monkeypatch.setenv("MEDIA_GPU_PROGRESS_FILE", str(progress))
    config = Config(api_url="https://api.example.test", token="synthetic", storage_origins=("https://objects.example.test",))
    async with Worker(config, object()) as worker:
        async def iteration():
            worker.stop.set()
            return True
        monkeypatch.setattr(worker, "run_once", iteration)
        await worker.run()
    assert progress.exists()


@pytest.fixture
def pvc_config(config):
    return dict(config, runtime_mode="pvc", image=recipe.PVC_IMAGE, runtime_pvc="synthetic-runtime",
                release_dir="/data/releases/revision-1", venv_dir="/data/venvs/validated-1",
                source_manifest_sha256="e" * 64, venv_freeze_sha256="f" * 64)


def test_pvc_uses_existing_image_without_build(pvc_config):
    cm, deployment, bootstrap = recipe.render(pvc_config)["items"]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == recipe.PVC_IMAGE
    assert container["command"] == ["python3", "-I", "-S", "/bootstrap/gpu_release.py", "launch"]
    assert bootstrap["immutable"] is True
    assert "PYTHONPATH" not in cm["data"]
    mounts = [m for m in container["volumeMounts"] if m["name"] == "runtime"]
    assert len(mounts) == 2
    assert all(m["readOnly"] for m in mounts)
    assert {m["subPath"] for m in mounts} == {"releases/revision-1", "venvs/validated-1"}
    assert pod["securityContext"]["runAsUser"] == 10001


@pytest.mark.parametrize("shared_claim", [True, False], ids=["shared-pvc", "distinct-pvcs"])
def test_pvc_mounts_reuse_claim_without_weakening_security(pvc_config, shared_claim):
    baseline = recipe.render(pvc_config)["items"]
    if shared_claim:
        pvc_config["runtime_pvc"] = pvc_config["model_pvc"]
    cm, deployment, bootstrap = recipe.render(pvc_config)["items"]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    baseline_pod = baseline[1]["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert len(volumes) == len(pod["volumes"]) == (5 if shared_claim else 6)
    claims = [v["persistentVolumeClaim"] for v in volumes.values() if "persistentVolumeClaim" in v]
    assert len(claims) == (1 if shared_claim else 2)
    assert len({claim["claimName"] for claim in claims}) == len(claims)
    assert all(claim["readOnly"] is True for claim in claims)
    assert all(m["name"] in volumes for m in container["volumeMounts"])
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert mounts["/models"] == {"name": "models", "mountPath": "/models", "readOnly": True}
    runtime_name = "models" if shared_claim else "runtime"
    for key in ("release_dir", "venv_dir"):
        path = pvc_config[key]
        assert mounts[path] == {
            "name": runtime_name, "mountPath": path, "subPath": path[len("/data/"):], "readOnly": True,
        }
        assert volumes[runtime_name]["persistentVolumeClaim"]["claimName"] == pvc_config["runtime_pvc"]
    if shared_claim:
        assert "runtime" not in volumes
        assert all(m["name"] != "runtime" for m in container["volumeMounts"])
    assert pod["securityContext"] == baseline_pod["securityContext"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["terminationGracePeriodSeconds"] == baseline_pod["terminationGracePeriodSeconds"]
    assert {k: v for k, v in container.items() if k != "volumeMounts"} == {
        k: v for k, v in baseline_pod["containers"][0].items() if k != "volumeMounts"
    }
    assert [v for v in pod["volumes"] if "persistentVolumeClaim" not in v] == [
        v for v in baseline_pod["volumes"] if "persistentVolumeClaim" not in v
    ]
    assert [m for m in container["volumeMounts"] if m["name"] not in ("models", "runtime")] == [
        m for m in baseline_pod["containers"][0]["volumeMounts"] if m["name"] not in ("models", "runtime")
    ]
    assert cm == baseline[0] and bootstrap == baseline[2]
    assert deployment["spec"]["selector"] == baseline[1]["spec"]["selector"]


@pytest.mark.parametrize("key", sorted(recipe.PVC_REQUIRED))
def test_pvc_required_fields(pvc_config, key):
    del pvc_config[key]
    with pytest.raises(ValueError):
        recipe.render(pvc_config)


@pytest.mark.parametrize("key,value", [
    ("runtime_mode", "unknown"), ("release_dir", "/data/../private"),
    ("release_dir", "/data"), ("release_dir", "/other/release"),
    ("release_dir", "/data/releases//revision"), ("release_dir", "/data/venvs"),
    ("release_dir", "/data/venvs/validated-1"), ("venv_dir", "${VENV_DIR}"),
    ("source_manifest_sha256", "latest"), ("venv_freeze_sha256", ""),
    ("image", "nvcr.io/nvidia/pytorch:25.04-py3"),
])
def test_pvc_rejects_unsafe_inputs(pvc_config, key, value):
    pvc_config[key] = value
    with pytest.raises(ValueError):
        recipe.render(pvc_config)


def test_pvc_fingerprint_change_triggers_rollout(pvc_config):
    first = recipe.render(pvc_config)["items"][1]["spec"]["template"]["metadata"]
    pvc_config["venv_freeze_sha256"] = "1" * 64
    second = recipe.render(pvc_config)["items"][1]["spec"]["template"]["metadata"]
    assert first["annotations"]["indiedex.org/config-sha256"] != second["annotations"]["indiedex.org/config-sha256"]


def test_image_mode_rejects_pvc_fields(config):
    config["release_dir"] = "/data/releases/revision-1"
    with pytest.raises(ValueError):
        recipe.render(config)


@pytest.fixture
def release_module():
    spec = importlib.util.spec_from_file_location("gpu_release", ROOT / "deploy/gpu_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source_release(tmp_path, release_module):
    root = tmp_path.resolve() / "release"
    root.mkdir()
    files = {}
    for name in release_module.FILES:
        path = root / name
        path.parent.mkdir(exist_ok=True)
        raw = b"# synthetic source\n"
        path.write_bytes(raw)
        files[name] = release_module.digest(raw)
    manifest = dict(version=1, source_revision="b" * 40, files=files)
    (root / "manifest.json").write_text(json.dumps(manifest))
    destination = tmp_path.resolve() / "verified"
    destination.mkdir()
    return root, manifest, destination


def verify_source(module, source):
    root, manifest, destination = source
    return module.verify_release(root, module.digest((root / "manifest.json").read_bytes()),
                                 "b" * 40, destination)


def test_verified_release_snapshot(release_module, source_release):
    verify_source(release_module, source_release)
    assert set(p.relative_to(source_release[2]).as_posix() for p in source_release[2].rglob("*") if p.is_file()) == set(release_module.FILES)


@pytest.mark.parametrize("mutation", ["extra", "symlink", "parent_symlink", "hardlink", "changed", "traversal", "revision", "pyc"])
def test_release_rejects_unsafe_tree(release_module, source_release, mutation):
    root, manifest, _ = source_release
    source = root / "gpu_worker/client.py"
    if mutation == "extra":
        (root / ".env").write_text("private")
    elif mutation == "pyc":
        (root / "gpu_worker/__pycache__").mkdir()
    elif mutation == "symlink":
        source.unlink()
        source.symlink_to(root / "app/__init__.py")
    elif mutation == "parent_symlink":
        (root / "app").rename(root.parent / "external")
        (root / "app").symlink_to(root.parent / "external", target_is_directory=True)
    elif mutation == "hardlink":
        os.link(source, root.parent / "external.py")
    elif mutation == "changed":
        source.write_text("changed")
    elif mutation == "traversal":
        manifest["files"]["../outside.py"] = "a" * 64
    elif mutation == "revision":
        manifest["source_revision"] = "c" * 40
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)):
        verify_source(release_module, source_release)


def test_verifier_runs_under_base_python_isolation(release_module, source_release):
    root, _, destination = source_release
    command = ("import runpy,sys,pathlib; m=runpy.run_path(sys.argv[1]); "
               "m['verify_release'](pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4],pathlib.Path(sys.argv[5]))")
    result = subprocess.run([sys.executable, "-B", "-I", "-S", "-c", command,
                             str(ROOT / "deploy/gpu_release.py"), str(root),
                             release_module.digest((root / "manifest.json").read_bytes()),
                             "b" * 40, str(destination)], capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (destination / "gpu_worker/supervise.py").is_file()


def test_manifest_pin_checked_before_copy(release_module, source_release):
    root, _, destination = source_release
    with pytest.raises(ValueError):
        release_module.verify_release(root, "0" * 64, "b" * 40, destination)
    assert not list(destination.iterdir())


def test_venv_root_allows_normal_python_symlink(release_module, tmp_path):
    root = tmp_path.resolve() / "venv"
    (root / "bin").mkdir(parents=True)
    (root / "bin/python").symlink_to(sys.executable)
    fd = release_module.open_root(root)
    os.close(fd)
    assert (root / "bin/python").is_symlink()


def test_isolated_venv_probes_disable_bytecode(release_module):
    import inspect
    source = inspect.getsource(release_module.launch)
    assert '[python, "-B", "-I", "-m", "pip", "freeze", "--all"]' in source
    assert '[python, "-B", "-I", "-c", check]' in source


def test_prepare_reads_only_selected_committed_blobs(release_module, monkeypatch, tmp_path):
    commands = []
    def git(command):
        commands.append(command)
        return b"100644 blob synthetic\tfile\n" if "ls-tree" in command else b"# committed\n"
    monkeypatch.setattr(release_module.subprocess, "check_output", git)
    output = tmp_path / "new-release"
    digest = release_module.prepare(tmp_path, "b" * 40, output)
    assert digest == release_module.digest((output / "manifest.json").read_bytes())
    assert len(commands) == 2 * len(release_module.FILES)
    assert not any(".env" in str(command) for command in commands)
    with pytest.raises(FileExistsError):
        release_module.prepare(tmp_path, "b" * 40, output)


def test_source_release_contains_worker_import_closure(release_module, tmp_path):
    # Import only the source allowlist, not the checkout's backend; a newly
    # transitive app import must not silently break the no-build release.
    import shutil
    for name, source in release_module.FILES.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, target)
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import gpu_worker.inference, gpu_worker.client, gpu_worker.__main__", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_image_static_contract():
    dockerfile = (ROOT / "backend/gpu_worker/Dockerfile").read_text()
    assert "FROM nvcr.io/nvidia/pytorch@sha256:" in dockerfile
    assert "python3 -m venv /opt/worker-venv" in dockerfile
    assert "--system-site-packages" not in dockerfile
    assert "COPY backend/app/ " not in dockerfile
    assert "COPY backend/gpu_worker/ " not in dockerfile
    assert '"-m", "gpu_worker.supervise"' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "pip freeze" in dockerfile
    assert "SOURCE_REVISION" in dockerfile
