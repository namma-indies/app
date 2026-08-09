#!/usr/bin/env python
"""One-time export of YOLO26x to ONNX for `app/detect_reid.py`.

Run on a machine with ultralytics installed; the service needs only
onnxruntime. Output: backend/app/ml/yolo26x.onnx (gitignored, 223 MB).

    pip install ultralytics
    python scripts/export_yolo26x_onnx.py

NMS is baked into the export (`nms=True`), so the runtime output is already
[x1, y1, x2, y2, conf, cls] and the service never reimplements NMS -- the one
part of YOLO post-processing most likely to be subtly wrong.
"""
import argparse
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "backend" / "app" / "ml" / "yolo26x.onnx"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default="yolo26x.pt")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    from ultralytics import YOLO

    exported = YOLO(args.weights).export(
        format="onnx", imgsz=640, opset=17, nms=True, simplify=False
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(exported), args.out)

    import numpy as np
    import onnxruntime as ort

    sess = ort.InferenceSession(str(args.out), providers=["CPUExecutionProvider"])
    shape = sess.run(
        None, {sess.get_inputs()[0].name: np.zeros((1, 3, 640, 640), np.float32)}
    )[0].shape
    if len(shape) != 3 or shape[2] != 6:
        print(f"FAIL: expected (1, N, 6) NMS output, got {shape}")
        return 1
    print(f"wrote {args.out}  ({args.out.stat().st_size / 1e6:.1f} MB)  output {shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
