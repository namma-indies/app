#!/usr/bin/env python
"""Export the on-device models: instance segmentation for the live preview.

Run on a machine with ultralytics and tensorflow; neither the service nor the
app build needs them. Same posture as the other two export scripts here.

    pip install ultralytics tensorflow onnx onnxruntime
    python scripts/export_mobile_models.py --out frontend/android/app/src/main/assets

WHY A SECOND DETECTOR AT ALL
----------------------------
The server runs yolo26x: 223 MB, ~314 ms per photo on CPU. That is fine for a
background task and impossible in a camera preview, where the entire frame
budget at 30 fps is 33 ms including capture, colour conversion, inference,
mask assembly and drawing.

yolo26n-seg is the same family two sizes down, and it segments rather than just
boxing: 3.13 M parameters against yolo26x's ~62 M. The point of segmentation
here is not prettier overlays. It is that `app/detect_reid.best_animal_box`
returns exactly ONE box -- the largest -- so when two dogs share a frame the
second is silently dropped and can never be matched. Masks are what let the
app show a person which animal it is about to log, and eventually let it log
both.

WHAT THE RUNTIME HAS TO DEAL WITH
---------------------------------
Two output tensors, verified below rather than assumed:

    (1, 300, 38)      300 detections, NMS already applied at export
                      [x1, y1, x2, y2, conf, cls, m0..m31]
    (1, 32, H/4, W/4) 32 mask prototypes

A detection's mask is its 32 coefficients matmul'd with the prototypes, then
sigmoid, then cropped to the box. NMS baked in matters more on a phone than on
the server: it is the part of YOLO post-processing most likely to be subtly
wrong, and reimplementing it in Kotlin inside a 33 ms budget is how that goes
wrong quietly.

QUANTISATION: FP16, AND NOT STATIC INT8
---------------------------------------
FP16 by default, and static INT8 is deliberately not the default even though it
is a third of the size.

Ultralytics' own exporter carries the reason, in a comment next to the flag:
`enable_batchmatmul_unfold=not use_int8,  # fix lower no. of detected objects on
GPU delegate`. Static INT8 keeps a BatchMatMul the GPU delegate accepts and then
computes wrong, so the failure is *fewer detections*, silently -- exactly the
failure this feature exists to fix, reintroduced one layer down. Measured
elsewhere at roughly -6.5 mAP for YOLOv8n, and naive A8W8 has produced zero
detections.

There is also no speed argument for going below FP16: the GPU delegate computes
in FP16 internally whatever the file says, and INT8 has measured *slower* than
FP32 on it. At 3 M parameters the difference is a few megabytes of APK either
way, which is not worth a silent accuracy cliff.

RESOLUTION IS THE KNOB THAT DECIDES THE FRAME RATE
--------------------------------------------------
Exported at several sizes on purpose. Cost scales with pixels, so 320 is about
four times cheaper than 640, and a phone that misses 30 fps at 640 may hold it
at 320. Which one ships is a measurement on real devices, not a preference --
see docs/. Do not delete the sizes you are not currently using; the next device
that shows up is the reason they exist.
"""

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "frontend" / "android" / "app" / "src" / "main" / "assets"

WEIGHTS = "yolo26n-seg.pt"
# 640 is the training resolution and the accuracy reference. 320 is the
# realistic live-preview candidate. 256 is the fallback for a phone that cannot
# hold 30 fps at 320 -- worth having exported before you are holding the phone
# that needs it.
SIZES = (640, 320, 256)

# 4 box + 1 confidence + 1 class + 32 mask coefficients.
DET_STRIDE = 38
N_PROTOS = 32


def _verify_onnx(path: Path, size: int) -> bool:
    """Run the export once and check the shapes the Android code will index
    into. A silently reshaped export produces masks that land on the wrong
    pixels, which looks like a bad model rather than a bad export."""
    import numpy as np
    import onnxruntime as ort

    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    outs = sess.run(None, {sess.get_inputs()[0].name: np.zeros((1, 3, size, size), np.float32)})
    shapes = [tuple(o.shape) for o in outs]
    det = next((s for s in shapes if len(s) == 3 and s[2] == DET_STRIDE), None)
    proto = next((s for s in shapes if len(s) == 4 and s[1] == N_PROTOS), None)
    if det is None or proto is None:
        print(f"FAIL: expected a (1,N,{DET_STRIDE}) head and a (1,{N_PROTOS},h,w) "
              f"prototype tensor, got {shapes}", file=sys.stderr)
        return False
    if proto[2] != size // 4 or proto[3] != size // 4:
        print(f"FAIL: prototypes are {proto[2]}x{proto[3]}, expected "
              f"{size // 4}x{size // 4} (stride 4)", file=sys.stderr)
        return False
    print(f"    verified: detections {det}, prototypes {proto}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weights", default=WEIGHTS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    ap.add_argument("--formats", nargs="+", default=["tflite", "onnx"],
                    help="tflite for Android; onnx to verify shapes on this machine")
    # `quantize` is the parameter this version of ultralytics actually takes for
    # tflite (16 = FP16, 8 = static INT8, "w8a16", 32/None = FP32). `half=` and
    # `int8=` are derived from it internally and are not the public knob.
    ap.add_argument("--quantize", default="16", choices=["16", "32", "8"],
                    help="16 (default) = FP16. 32 = FP32, four times the size for "
                         "nothing: the GPU delegate computes in FP16 internally "
                         "either way. 8 = static INT8, which is a TRAP on the GPU "
                         "delegate -- see the note in the module docstring.")
    args = ap.parse_args()

    from ultralytics import YOLO

    args.out.mkdir(parents=True, exist_ok=True)
    failures = 0

    for size in args.sizes:
        print(f"\n=== {args.weights} @ {size}px ===")
        for fmt in args.formats:
            model = YOLO(args.weights)
            try:
                # nms=True: the phone must not reimplement NMS inside a
                # 33 ms budget. Quantisation is FP16 by default, see the
                # module docstring on why static INT8 is not.
                kwargs = dict(format=fmt, imgsz=size, nms=True)
                if fmt == "tflite":
                    kwargs["quantize"] = int(args.quantize)
                elif fmt == "onnx":
                    kwargs["opset"] = 17
                    kwargs["simplify"] = False
                produced = Path(model.export(**kwargs))
            except Exception as e:
                print(f"  {fmt}: FAILED -- {e}", file=sys.stderr)
                failures += 1
                continue

            suffix = {"16": "fp16", "32": "fp32", "8": "int8"}[args.quantize]
            tag = f"{size}_{suffix}" if fmt == "tflite" else str(size)
            dest = args.out / f"yolo26n_seg_{tag}{produced.suffix}"
            if produced.is_dir():
                # tflite export lands in a _saved_model directory; take the
                # .tflite out of it and leave the rest behind.
                inner = sorted(produced.glob("*.tflite"))
                if not inner:
                    print(f"  {fmt}: FAILED -- no .tflite inside {produced}", file=sys.stderr)
                    failures += 1
                    continue
                shutil.copy(inner[0], dest)
            else:
                shutil.move(str(produced), dest)

            mb = dest.stat().st_size / 1e6
            print(f"  {fmt}: {dest.name}  ({mb:.1f} MB)")
            if fmt == "onnx" and not _verify_onnx(dest, size):
                failures += 1

    if failures:
        print(f"\n{failures} export(s) failed", file=sys.stderr)
        return 1
    print(f"\nwrote to {args.out}")
    print("These ship inside the APK. At ~6 MB that is not a size problem -- but "
          "note that putting AGPL-3.0 weights into a distributed APK is the "
          "redistribution act itself, which gitignoring them does not avoid. "
          "See backend/app/ml/NOTICE.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
