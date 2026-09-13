#!/usr/bin/env python3
"""Render only; never contacts Kubernetes or reads a Secret.

Usage: python deploy/render_gpu_worker.py /private/config.json > /private/worker.json
The output is a Kubernetes List (JSON is accepted by kubectl). Keep BOTH files
outside the checkout. Review it before a separately authorized deployment.
Required JSON keys (unknown keys, including token values, are rejected):
  namespace, name, image (registry/repo@sha256:digest), source_revision (40 hex),
  model_pvc, token_secret, token_key, api_url (HTTPS origin),
  storage_origins (nonempty list of exact HTTPS origins; no wildcards),
  detector_file, embedder_file (relative paths within model PVC),
  detector_sha256, embedder_sha256, gpu_resource (nvidia.com/gpu or MIG key).
Optional: uid, gid (positive integers, default 10001), runtime_mode (image or pvc;
image is the backward-compatible default), selector_labels (nonempty label map).
For an in-place Recreate rollout, keep the existing namespace/name and supply
EXACTLY its current spec.selector.matchLabels as selector_labels. Selectors are
immutable: do not add the renderer's own label to a legacy selector. Pod labels
include both the selector and app.kubernetes.io/name=name; a conflicting value
for that owned label is rejected. Selectors using matchExpressions are unsupported
and must not be converted with this recipe. Compare the live selector before any
apply; this offline renderer cannot check it. Omission preserves the new-deployment
default selector. Do not create a second Deployment or delete/recreate the live one.
For an existing deployment, replace its pod template as a whole using a reviewed
JSON Patch replace operation (preserving its selector), rather than strategic-merge
patching container/volume lists: old differently named containers can otherwise
survive alongside this worker. Inspect the resulting spec before rollout.

PVC MODE (no Docker/Podman build or registry push): runtime_mode="pvc" also requires
  runtime_pvc: PVC containing the versioned source release and isolated venv,
  release_dir: absolute /data/... directory produced by gpu_release.py prepare,
  venv_dir: absolute /data/... directory of the provisioned isolated Python venv,
  source_manifest_sha256: SHA256 of release_dir/manifest.json,
  venv_freeze_sha256: SHA256 of exact `venv/bin/python -m pip freeze --all` stdout.
image must be the supported immutable NGC digest (PVC_IMAGE in this script).
The release and venv directories must be disjoint, with no symlink components.
Only these two PVC subpaths are mounted, read-only, at their original /data paths.
Models may share that claim using model_pvc and relative detector/embedder_file.
See gpu_release.py --help for local release preparation and provider venv setup.
The verifier runs from a ConfigMap with base Python -I -S, checks the pinned
manifest/revision/exact tree and copies verified source to private scratch BEFORE
worker imports. The venv inventory fingerprint is NOT a package-content signature.
PVC writers remain trusted; publish NEW versioned directories, never mutate one.
Existing root-private 0700 files cannot run as the default UID: an operator MUST
provision traversal/read/execute permissions first, then test access as that UID.
No root fallback or automatic permission repair is provided.

IMAGE MODE (optional): build the Dockerfile from a git archive of source_revision,
publish, then supply its immutable digest here. Revision annotation is provenance,
not a signature: verify registry provenance separately. No runtime PVC is needed.
Provision the named Secret/key separately with a nonempty worker token, never in
this config or shell history. Bootstrap rejects an empty/invalid mounted token.
Config changes roll the pod via checksum. The client reads its token only at
startup: rotating Secret content also requires an authorized deployment restart.
Provision ONNX files on the PVC with matching hashes, single links, no symlinks;
make them readable/traversable by uid:gid BEFORE deployment. Read-only mounts do
not repair ownership, and fsGroup support depends on the CSI driver. No init
container recursively chowns the shared models. Scratch (4Gi) holds verified
model copies and bounded clips; shm (2Gi) counts against the memory limit.

One serial worker uses Recreate, no service/ingress. The worker renews API leases;
a separate PID-1 watchdog checks completed claim-loop progress, not lease pulses.
After 420s without progress (includes bootstrap / 300s job budget), it SIGTERMs
then SIGKILLs the process group after 35s. Kubernetes is a second 370s shutdown
bound. This handles a stuck native call, not just a responsive Python event loop.
No liveness probe is represented as proof of CUDA health. A fresh image still
needs real GPU startup, model hash, parity, video and killed-worker lease recovery
validation on staging before promotion. NGC base includes torch, but the isolated
worker venv does not install or import it. Licences remain a separate launch gate.
The application allowlist is NOT a network firewall; arrange DNS/TLS egress policy
at the cluster boundary if required (standard NetworkPolicy cannot pin FQDNs).
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import urlsplit

REQUIRED = {
    "namespace", "name", "image", "source_revision", "model_pvc", "token_secret",
    "token_key", "api_url", "storage_origins", "detector_file", "embedder_file",
    "detector_sha256", "embedder_sha256", "gpu_resource",
}


PVC_REQUIRED = {"runtime_pvc", "release_dir", "venv_dir", "source_manifest_sha256", "venv_freeze_sha256"}
PVC_IMAGE = "nvcr.io/nvidia/pytorch@sha256:d1eac6220dd98ef5870b1a76673cfb6f84451135a6d8a174cb92258a6bf4576d"


def runtime_path(value):
    value = text(value)
    path = PurePosixPath(value)
    if (not value.startswith("/data/") or ".." in path.parts or str(path) != value
            or not re.fullmatch(r"/data/[A-Za-z0-9_./-]+", value)):
        raise ValueError("expected canonical versioned /data path")
    return value


def text(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(c in value for c in "${}<>\\")
            or re.search(r"(?i)(REPLACE_ME|CHANGEME|YOUR_|TODO)", value)):
        raise ValueError("invalid or unresolved input")
    return value


def match(value, pattern):
    if not re.fullmatch(pattern, text(value)):
        raise ValueError("invalid input format")
    return value


def selector_labels(value, owned):
    if not isinstance(value, dict) or not value:
        raise ValueError("selector_labels must be a nonempty label map")
    component = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?"
    dns_label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    for key, label in value.items():
        text(key)
        parts = key.split("/")
        if len(parts) > 2 or not re.fullmatch(component, parts[-1]):
            raise ValueError("invalid selector label key")
        if len(parts) == 2 and (len(parts[0]) > 253 or any(
                not re.fullmatch(dns_label, part) for part in parts[0].split("."))):
            raise ValueError("invalid selector label prefix")
        # Kubernetes permits empty label values, but not unresolved placeholders.
        if label != "":
            match(label, component)
        if key in owned and label != owned[key]:
            raise ValueError("selector conflicts with renderer-owned label")
    return dict(value)


def origin(value):
    value = text(value)
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.path not in ("", "/")
            or url.query or url.fragment or "?" in value or "#" in value
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
        raise ValueError("expected exact HTTPS origin")
    host = url.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ("." not in host or host.endswith((".local", ".localhost", ".internal"))
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p)
                       for p in host.split("."))):
            raise ValueError("expected public DNS host") from None
    else:
        if not address.is_global:
            raise ValueError("expected public address")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("invalid port")
    return value.rstrip("/")


def model_file(value):
    value = text(value)
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or str(path) != value
            or not re.fullmatch(r"[A-Za-z0-9_./-]+\.onnx", value)):
        raise ValueError("expected relative ONNX path")
    return "/models/" + value


def render(config):
    if not isinstance(config, dict):
        raise ValueError("invalid configuration")
    mode = config.get("runtime_mode", "image")
    if mode not in ("image", "pvc"):
        raise ValueError("unsupported runtime mode")
    required = REQUIRED | (PVC_REQUIRED if mode == "pvc" else set())
    if not required <= config.keys() or config.keys() - required - {"uid", "gid", "runtime_mode", "selector_labels"}:
        raise ValueError("missing or unknown configuration keys")
    c = dict(config)
    if mode == "pvc":
        if c["image"] != PVC_IMAGE:
            raise ValueError("PVC runtime requires supported provider image")
        match(c["runtime_pvc"], r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
        for key in ("source_manifest_sha256", "venv_freeze_sha256"):
            match(c[key], r"[0-9a-f]{64}")
        release, venv = (PurePosixPath(runtime_path(c[k])) for k in ("release_dir", "venv_dir"))
        if release == venv or release in venv.parents or venv in release.parents:
            raise ValueError("release and venv must be disjoint")
    for key in ("namespace", "name", "model_pvc", "token_secret"):
        match(c[key], r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
    match(c["token_key"], r"[A-Za-z0-9_.-]{1,253}")
    match(c["image"], r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}")
    match(c["source_revision"], r"[0-9a-f]{40}")
    for key in ("detector_sha256", "embedder_sha256"):
        match(c[key], r"[0-9a-f]{64}")
    match(c["gpu_resource"], r"nvidia\.com/(?:gpu|mig-[0-9]+g\.[0-9]+gb(?:\.[a-z]+)?)")
    for key in ("uid", "gid"):
        c.setdefault(key, 10001)
        if type(c[key]) is not int or not 1 <= c[key] <= 2147483647:
            raise ValueError("uid/gid must be nonroot integers")
    if not isinstance(c["storage_origins"], list) or not c["storage_origins"]:
        raise ValueError("storage origins required")
    data = {
        "MEDIA_GPU_API_URL": origin(c["api_url"]),
        "MEDIA_GPU_STORAGE_ORIGINS": ",".join(origin(o) for o in c["storage_origins"]),
        "MEDIA_GPU_TOKEN_FILE": "/run/worker-token/token",
        "MEDIA_GPU_DETECTOR_PATH": model_file(c["detector_file"]),
        "MEDIA_GPU_EMBEDDER_PATH": model_file(c["embedder_file"]),
        "MEDIA_GPU_DETECTOR_SHA256": c["detector_sha256"],
        "MEDIA_GPU_EMBEDDER_SHA256": c["embedder_sha256"],
    }
    labels = {"app.kubernetes.io/name": c["name"]}
    selector = selector_labels(c.get("selector_labels", labels), labels)
    labels.update(selector)
    metadata = {"name": c["name"], "namespace": c["namespace"]}
    pod = {
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 370,
        "securityContext": {"runAsNonRoot": True, "runAsUser": c["uid"],
                            "runAsGroup": c["gid"], "fsGroup": c["gid"],
                            "fsGroupChangePolicy": "OnRootMismatch",
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "worker", "image": c["image"], "imagePullPolicy": "IfNotPresent",
            "envFrom": [{"configMapRef": {"name": c["name"]}}],
            "securityContext": {"allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "8", "memory": "24Gi", c["gpu_resource"]: 1,
                                       "ephemeral-storage": "4Gi"},
                          "limits": {"cpu": "16", "memory": "50Gi", c["gpu_resource"]: 1,
                                     "ephemeral-storage": "5Gi"}},
            "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True},
                             {"name": "token", "mountPath": "/run/worker-token", "readOnly": True},
                             {"name": "scratch", "mountPath": "/scratch"},
                             {"name": "shm", "mountPath": "/dev/shm"}],
        }],
        "volumes": [
            {"name": "models", "persistentVolumeClaim": {"claimName": c["model_pvc"], "readOnly": True}},
            {"name": "token", "secret": {"secretName": c["token_secret"], "defaultMode": 288,
                                           "items": [{"key": c["token_key"], "path": "token"}]}},
            {"name": "scratch", "emptyDir": {"sizeLimit": "4Gi"}},
            {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}},
        ],
    }
    extra_items = []
    if mode == "pvc":
        data.update({
            "MEDIA_GPU_RELEASE_DIR": c["release_dir"], "MEDIA_GPU_VENV_DIR": c["venv_dir"],
            "MEDIA_GPU_SOURCE_REVISION": c["source_revision"],
            "MEDIA_GPU_SOURCE_MANIFEST_SHA256": c["source_manifest_sha256"],
            "MEDIA_GPU_VENV_FREEZE_SHA256": c["venv_freeze_sha256"],
            "MEDIA_GPU_PROGRESS_FILE": "/scratch/progress", "TMPDIR": "/scratch",
            "HOME": "/scratch", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        })
        verifier = Path(__file__).with_name("gpu_release.py").read_text()
        verifier_hash = hashlib.sha256(verifier.encode()).hexdigest()
        # Content addressing rolls verifier changes without mutable startup code.
        extra_items = [{"apiVersion": "v1", "kind": "ConfigMap",
                        "metadata": {"name": "gpu-bootstrap-" + verifier_hash[:20], "namespace": c["namespace"]},
                        "immutable": True, "data": {"gpu_release.py": verifier}}]
        data["MEDIA_GPU_BOOTSTRAP_SHA256"] = verifier_hash
        container = pod["containers"][0]
        container["command"] = ["python3", "-I", "-S", "/bootstrap/gpu_release.py", "launch"]
        container["workingDir"] = "/scratch"
        container["volumeMounts"].extend([
            {"name": "runtime", "mountPath": c[key], "subPath": c[key][len("/data/"):], "readOnly": True}
            for key in ("release_dir", "venv_dir")
        ] + [{"name": "bootstrap", "mountPath": "/bootstrap", "readOnly": True}])
        pod["volumes"].extend([
            {"name": "runtime", "persistentVolumeClaim": {"claimName": c["runtime_pvc"], "readOnly": True}},
            {"name": "bootstrap", "configMap": {"name": extra_items[0]["metadata"]["name"], "defaultMode": 292}},
        ])
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": data},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                  "selector": {"matchLabels": selector},
                  "template": {"metadata": {"labels": labels,
                                            "annotations": {
                                                "indiedex.org/source-revision": c["source_revision"],
                                                "indiedex.org/config-sha256": hashlib.sha256(
                                                    json.dumps(data, sort_keys=True).encode()).hexdigest()}},
                               "spec": pod}}},
    ] + extra_items}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    try:
        result = render(json.loads(args.config.read_text()))
    except (OSError, ValueError, TypeError):
        print("gpu deployment: invalid configuration; no manifest rendered", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
