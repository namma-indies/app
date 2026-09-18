#!/usr/bin/env python3
"""Prepare a source-only release locally, or verify/launch it on the provider.

Prepare AFTER the reviewed source is committed (no commit is made by this tool):
  python deploy/gpu_release.py prepare --repo CHECKOUT --revision FULL_COMMIT --output NEW_DIRECTORY
Copies only the allowlisted Git blobs from that commit; never working-tree bytes.
Prints the manifest SHA256 for source_manifest_sha256. Transfer this directory to
an immutable versioned release_dir on the runtime PVC; never update it in place.

Provision a versioned isolated venv with provider Python 3.12 (no system packages):
  python3 -m venv /data/PRIVATE_VERSIONED_VENV
  /data/PRIVATE_VERSIONED_VENV/bin/python -m pip install -r RELEASE/requirements.txt
  /data/PRIVATE_VERSIONED_VENV/bin/python -m pip check
Capture stdout BYTES from `.../bin/python -m pip freeze --all` and record their
SHA256 as venv_freeze_sha256. Do not install CPU onnxruntime or torch. Existing
validated venv may be reused only with its observed fingerprint and correct
permissions. This is environment inventory, NOT cryptographic verification of
venv package contents, nor a reproducible transitive dependency lock. The PVC
provisioner and its other writers remain trusted; restrict them operationally.
In particular .pth startup code, pip itself, native libraries and normal venv
symlink targets execute before/without content verification: modified code can
preserve (or forge) the same freeze output. Read-only subpath mounts do not stop
other PVC writers changing those bytes after a check. A package hash manifest
alone would still leave that race; a verified private runtime snapshot or an
operator-enforced immutable versioned venv is needed before claiming integrity.
Do not reject the ordinary bin/python and lib64 symlinks as a substitute.

The renderer mounts this stdlib verifier from a ConfigMap. Provider Python runs
it with -I -S before any release/venv imports. Source is copied to private scratch
while verifying its pinned manifest, then executed only from that verified copy.
No source symlinks, hardlinks, unlisted files (including pyc) or path traversal.
The manifest binds source_revision and requirements.txt to the release hash.
Root-owned 0700 directories are NOT nonroot-ready. Explicitly provision parent
traversal, source/model read access and venv execution for configured uid/gid;
verify as that UID before rollout. No automatic chmod/chown touches the PVC.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
FILES = {
    **{f"gpu_worker/{name}": f"backend/gpu_worker/{name}" for name in (
        "__init__.py", "__main__.py", "client.py", "clip.py", "inference.py", "supervise.py", "multi.py")},
    **{f"app/{name}": f"backend/app/{name}" for name in (
        "__init__.py", "analyse.py", "detect.py", "detect_reid.py", "embed.py", "photos.py", "video.py", "config.py", "tracking.py")},
    "requirements.txt": "backend/gpu_worker/requirements.txt",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def valid_hash(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
        raise ValueError("invalid digest")
    return value


def prepare(repo, revision, output):
    valid_hash(revision, 40)
    blobs = {}
    for name, source in FILES.items():
        listing = subprocess.check_output(["git", "-C", str(repo), "ls-tree", revision, "--", source])
        if not listing.startswith(b"100644 blob "):
            raise ValueError("release needs committed regular source files")
        blobs[name] = subprocess.check_output(["git", "-C", str(repo), "show", f"{revision}:{source}"])
    manifest = json.dumps({"version": 1, "source_revision": revision,
                           "files": {name: digest(raw) for name, raw in blobs.items()}},
                          sort_keys=True, indent=2).encode() + b"\n"
    output.mkdir(mode=0o755, parents=False, exist_ok=False)
    for name, raw in blobs.items():
        target = output / name
        target.parent.mkdir(mode=0o755, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o644)
    (output / "manifest.json").write_bytes(manifest)
    (output / "manifest.json").chmod(0o644)
    return digest(manifest)


def open_root(root):
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("invalid release path")
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(root_fd, name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or str(path) != name:
        raise ValueError("unsafe release file")
    directory = os.dup(root_fd)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 * 1024:
                raise ValueError("unsafe release file")
            raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("oversized release file")
            return raw
    finally:
        os.close(directory)


def tree_files(fd, prefix=""):
    found = set()
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        relative = prefix + name
        if stat.S_ISDIR(info.st_mode):
            if relative not in {"app", "gpu_worker"}:
                raise ValueError("unexpected release directory")
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                found.update(tree_files(child, relative + "/"))
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            found.add(relative)
        else:
            raise ValueError("unsafe release entry")
    return found


def verify_release(root, expected, revision, destination):
    valid_hash(expected)
    valid_hash(revision, 40)
    fd = open_root(root)
    try:
        raw = read_file(fd, "manifest.json")
        if digest(raw) != expected:
            raise ValueError("manifest digest mismatch")
        manifest = json.loads(raw)
        if (set(manifest) != {"version", "source_revision", "files"}
                or manifest["version"] != 1 or manifest["source_revision"] != revision
                or set(manifest["files"]) != set(FILES)):
            raise ValueError("invalid source manifest")
        if tree_files(fd) != set(FILES) | {"manifest.json"}:
            raise ValueError("release tree mismatch")
        for name, expected_file in manifest["files"].items():
            valid_hash(expected_file)
            content = read_file(fd, name)
            if digest(content) != expected_file:
                raise ValueError("source digest mismatch")
            target = destination / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o400)
    finally:
        os.close(fd)


def launch():
    env = os.environ.copy()
    root = Path("/scratch/verified-release")
    # emptyDir survives container restarts; do not accumulate source snapshots.
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    verify_release(env["MEDIA_GPU_RELEASE_DIR"], env["MEDIA_GPU_SOURCE_MANIFEST_SHA256"],
                   env["MEDIA_GPU_SOURCE_REVISION"], root)
    venv = Path(env["MEDIA_GPU_VENV_DIR"])
    os.close(open_root(venv))
    python = str(venv / "bin/python")
    # Execute only the operator-trusted venv, with no release on its import path.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # -I ignores PYTHONDONTWRITEBYTECODE; -B is needed on read-only venv mounts.
    proof = subprocess.check_output([python, "-B", "-I", "-m", "pip", "freeze", "--all"],
                                    env=env, cwd="/scratch", timeout=60, stderr=subprocess.DEVNULL)
    if digest(proof) != valid_hash(env["MEDIA_GPU_VENV_FREEZE_SHA256"]):
        raise ValueError("venv inventory mismatch")
    check = ('import sys,importlib.util; assert sys.version_info[:2]==(3,12); '
             'assert sys.prefix != sys.base_prefix; '
             'assert importlib.util.find_spec("torch") is None; '
             'import importlib.metadata as m; '
             'assert "onnxruntime" not in {d.metadata["Name"].lower() for d in m.distributions()}')
    subprocess.run([python, "-B", "-I", "-c", check], check=True, env=env, cwd="/scratch",
                   timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if "include-system-site-packages = false" not in (venv / "pyvenv.cfg").read_text().lower():
        raise ValueError("venv isolation required")
    env["PYTHONPATH"] = str(root)
    env["PATH"] = str(venv / "bin") + os.pathsep + env.get("PATH", "")
    os.chdir(root)
    os.execve(python, [python, "-m", "gpu_worker.supervise"], env)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("prepare")
    create.add_argument("--repo", type=Path, required=True)
    create.add_argument("--revision", required=True)
    create.add_argument("--output", type=Path, required=True)
    sub.add_parser("launch")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            print(prepare(args.repo, args.revision, args.output))
        else:
            launch()
    except Exception:
        print("gpu release: verification/provisioning failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
