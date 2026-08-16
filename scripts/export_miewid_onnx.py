#!/usr/bin/env python
"""One-time export of MiewID-msv3 to ONNX for `app/embed.py`.

Run this on a machine with torch; the service itself has no torch and never
needs it. Output goes to backend/app/ml/miewid_msv3.onnx (gitignored, 206 MB).

    pip install torch torchvision transformers timm onnx onnxruntime onnxscript
    python scripts/export_miewid_onnx.py

Two things this script does that a bare torch.onnx.export does not:

1. Applies the transformers 5.x compatibility patch. MiewIdNet predates the
   `all_tied_weights_keys` attribute and blows up on load without it.
2. Verifies the export against the torch original before writing it, and
   refuses to emit a model whose outputs have drifted. A silently wrong export
   would produce embeddings that look fine and match nothing.

Note: the default (dynamo) exporter is required. The legacy TorchScript tracer
fails on this model's GeM pooling layer.
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "backend" / "app" / "ml" / "miewid_msv3.onnx"
HF_REPO = "conservationxlabs/miewid-msv3"
HF_REVISION = "4f1d7f2b521149e5fe34bb85f377248ce9971a7d"
INPUT = 440
DIM = 2152
COSINE_FLOOR = 0.9999  # export is rejected below this vs the torch original


def _patch_transformers() -> None:
    import transformers.modeling_utils as mu

    original = mu.PreTrainedModel._move_missing_keys_from_meta_to_device

    def patched(self, *args, **kwargs):
        if not hasattr(self, "all_tied_weights_keys"):
            object.__setattr__(self, "all_tied_weights_keys", {})
        return original(self, *args, **kwargs)

    mu.PreTrainedModel._move_missing_keys_from_meta_to_device = patched


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    _patch_transformers()

    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch
    from transformers import AutoModel

    model = AutoModel.from_pretrained(
        HF_REPO, revision=HF_REVISION, trust_remote_code=True
    ).eval()

    torch.manual_seed(0)
    sample = torch.randn(2, 3, INPUT, INPUT)
    with torch.no_grad():
        reference = model(sample).numpy()
    if reference.shape[1] != DIM:
        print(f"FAIL: expected {DIM}-d output, got {reference.shape[1]}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp.onnx")
    torch.onnx.export(
        model,
        (sample,),
        str(tmp),
        input_names=["images"],
        output_names=["embedding"],
        dynamic_axes={"images": {0: "batch"}, "embedding": {0: "batch"}},
        opset_version=17,
        dynamo=True,
    )

    # Collapse external weight files into one artifact so there is a single
    # thing to hash, host and verify.
    onnx.save_model(onnx.load(str(tmp)), str(args.out), save_as_external_data=False)
    for stray in tmp.parent.glob(tmp.name + "*"):
        stray.unlink()

    sess = ort.InferenceSession(str(args.out), providers=["CPUExecutionProvider"])
    got = sess.run(None, {"images": sample.numpy()})[0]

    a = reference / np.linalg.norm(reference, axis=1, keepdims=True)
    b = got / np.linalg.norm(got, axis=1, keepdims=True)
    cosines = (a * b).sum(1)
    worst = float(cosines.min())
    max_abs = float(np.abs(reference - got).max())

    print(f"output      : {got.shape}")
    print(f"max abs diff: {max_abs:.3e}")
    print(f"worst cosine: {worst:.8f}")

    if worst < COSINE_FLOOR:
        args.out.unlink(missing_ok=True)
        print(f"FAIL: cosine {worst:.8f} below {COSINE_FLOOR}; export discarded",
              file=sys.stderr)
        return 1

    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    print(f"size        : {args.out.stat().st_size / 1e6:.1f} MB")
    print(f"sha256      : {digest}")
    print(f"wrote       : {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
