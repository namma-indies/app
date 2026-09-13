"""Offline real-weight CUDA proof on deterministic synthetic pixels only.

Run from backend with --models and --reports pointing at private directories.
Profiles demonstrate kernel placement, not just registered execution providers.
Timings include profiling overhead and are not throughput measurements.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gc
import hashlib
from importlib import metadata
import io
import json
from pathlib import Path
import platform
import time
from unittest.mock import patch

import numpy as np
import onnxruntime as ort
from PIL import Image

from app import detect_reid, embed
from gpu_worker.inference import GpuInference, Identity, create_cuda_session, preprocessing_identity

PINS = {
    "yolo26x.onnx": "b35fa5afbf74509d3bdfa5e8371a5c38bad5f7f640832d5424e39d291ce364ba",
    "miewid_msv3.onnx": "689e9494502cc06b8dde46280db209f238c24211b4e40fa51019779299cd2844",
}


def run(session, batch):
    start = time.perf_counter()
    outputs = session.run(None, {session.get_inputs()[0].name: batch})
    elapsed = (time.perf_counter() - start) * 1000
    assert len(outputs) == 1 and np.isfinite(outputs[0]).all()
    return outputs[0], elapsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--disable-tf32", action="store_true",
                        help="Compare full FP32 after a default-provider parity failure")
    args = parser.parse_args()
    assert args.reports.is_dir()
    packages = {d.metadata["Name"].lower(): d.version for d in metadata.distributions()}
    assert "onnxruntime-gpu" in packages
    assert "onnxruntime" not in packages and "torch" not in packages
    result = {
        "python": platform.python_version(),
        "packages": dict(sorted(packages.items())),
        "available_providers": ort.get_available_providers(),
        "input": "deterministic synthetic RGB pattern; no user media",
        "timing_caveat": "one initial and two warmed runs, profiling enabled; not throughput",
        "models": {},
    }
    for name, expected in PINS.items():
        with (args.models / name).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        assert actual == expected, f"model hash mismatch: {name}"
        print(f"HASH_VERIFIED {name} {actual}", flush=True)
    y, x = np.indices((480, 720))
    pixels = np.stack(((x + 2 * y) % 256, (3 * x + y) % 256,
                       (x // 13 * 47 + y // 17 * 29) % 256), axis=-1).astype(np.uint8)
    image = Image.fromarray(pixels)
    batches = {
        "yolo26x.onnx": detect_reid._letterbox(image)[0],
        "miewid_msv3.onnx": embed.preprocess(image.crop((80, 60, 620, 420))),
    }
    sessions = {}
    options_class = ort.SessionOptions
    session_class = ort.InferenceSession
    result["diagnostic_disable_tf32_override"] = args.disable_tf32

    def configured_session(*positional, **kwargs):
        if args.disable_tf32:
            kwargs["providers"] = [("CUDAExecutionProvider", {"use_tf32": "0"})]
        return session_class(*positional, **kwargs)
    for name, batch in batches.items():
        print(f"MODEL_START {name}", flush=True)

        def profiled_options():
            options = options_class()
            options.enable_profiling = True
            options.profile_file_prefix = str(args.reports / name)
            return options

        start = time.perf_counter()
        with (patch.object(ort, "SessionOptions", side_effect=profiled_options),
              patch.object(ort, "InferenceSession", side_effect=configured_session)):
            gpu = create_cuda_session(args.models / name, threads=1)
        initialization_ms = (time.perf_counter() - start) * 1000
        cuda_options = gpu.get_provider_options()["CUDAExecutionProvider"]
        assert cuda_options.get("use_tf32") == "0", "default adapter must use full FP32"
        initial, initial_ms = run(gpu, batch)
        warmed = [run(gpu, batch) for _ in range(2)]
        np.testing.assert_allclose(initial, warmed[-1][0], rtol=1e-5, atol=1e-5)
        cpu_options = options_class()
        cpu_options.intra_op_num_threads = 1
        cpu_options.inter_op_num_threads = 1
        cpu_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        cpu_options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        cpu_options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        cpu = ort.InferenceSession(str(args.models / name), sess_options=cpu_options,
                                   providers=["CPUExecutionProvider"])
        cpu.disable_fallback()
        reference, cpu_ms = run(cpu, batch)
        assert initial.shape == reference.shape
        stats = {
            "sha256": PINS[name], "providers": gpu.get_providers(),
            "cuda_use_tf32": cuda_options["use_tf32"],
            "shape": list(initial.shape), "session_initialization_ms": initialization_ms,
            "gpu_initial_ms": initial_ms, "gpu_warm_ms": [v[1] for v in warmed],
            "cpu_initial_ms": cpu_ms,
            "raw_max_abs_difference": float(np.max(np.abs(initial - reference))),
        }
        if name.startswith("miewid"):
            assert initial.shape == (1, 2152)
            a, b = initial[0], reference[0]
            a_norm, b_norm = float(np.linalg.norm(a)), float(np.linalg.norm(b))
            assert a_norm > 1e-8 and b_norm > 1e-8
            a, b = a / a_norm, b / b_norm
            difference = float(np.max(np.abs(a - b)))
            cosine = float(np.dot(a, b))
            stats.update(gpu_raw_norm=a_norm, cpu_raw_norm=b_norm,
                         gpu_l2_norm=float(np.linalg.norm(a)), cpu_l2_norm=float(np.linalg.norm(b)),
                         normalized_max_abs_difference=difference, cosine_similarity=cosine,
                         parity_tolerance={"normalized_max_abs": 0.001, "cosine_min": 0.9999})
            assert difference <= 0.001 and cosine >= 0.9999
            assert np.isclose(np.linalg.norm(a), 1, atol=1e-5)
        else:
            assert initial.ndim == 3 and initial.shape[0] == 1 and initial.shape[2] == 6
            np.testing.assert_allclose(initial, reference, rtol=0.001, atol=0.001)
            stats.update(parity_tolerance={"rtol": 0.001, "atol": 0.001},
                         nonzero_confidences=int(np.count_nonzero(initial[0, :, 4])))
        del cpu
        gc.collect()
        sessions[name] = gpu
        result["models"][name] = stats
        print("MODEL_PARITY " + json.dumps({name: stats}), flush=True)

    adapter = GpuInference(sessions["yolo26x.onnx"], sessions["miewid_msv3.onnx"],
                           Identity(PINS["yolo26x.onnx"], PINS["miewid_msv3.onnx"], preprocessing_identity()))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    processed = adapter.process_photo(buffer.getvalue())
    analysis = adapter.analyse_photo(processed.original)
    result["adapter"] = {"dog_confidence": analysis.dog_confidence,
                         "cat_confidence": analysis.cat_confidence,
                         "bbox": analysis.bbox, "has_vector": analysis.vector is not None,
                         "preprocessing_identity": analysis.identity.preprocessing,
                         "miewid_direct_crop_executed": True}
    for name, session in sessions.items():
        profile_path = Path(session.end_profiling())
        profile = json.loads(profile_path.read_text())
        nodes = [event for event in profile if event.get("cat") == "Node"
                 and event.get("args", {}).get("provider")]
        counts = Counter(event["args"]["provider"] for event in nodes)
        cuda_ops = Counter(event["args"].get("op_name", "unknown") for event in nodes
                           if event["args"]["provider"] == "CUDAExecutionProvider")
        assert counts["CUDAExecutionProvider"] > 0, f"no CUDA kernels for {name}"
        assert any("Conv" in op or "Gemm" in op or "MatMul" in op for op in cuda_ops)
        result["models"][name].update(profile_file=str(profile_path),
                                      profiled_kernel_events=dict(counts),
                                      cuda_ops=dict(cuda_ops))
    result["status"] = "PASS"
    report = args.reports / "smoke.json"
    report.write_text(json.dumps(result, indent=2) + "\n")
    print("SMOKE_RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
