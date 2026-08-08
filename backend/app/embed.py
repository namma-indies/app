"""MiewID-msv3 embeddings for re-identification.

Produces a 2152-d L2-normalised vector for the dog in a capture, so two
sightings of the same animal can be compared by cosine similarity. Runs under
ONNX Runtime on CPU, mirroring `app/detect.py` -- no torch in the service.

Pipeline, matching the offline benchmark exactly (any deviation invalidates the
measured thresholds): detect box -> crop with 10% margin -> resize shorter side
to 440 (bicubic) -> centre-crop 440 -> ImageNet normalise -> forward ->
L2-normalise.

Model: conservationxlabs/miewid-msv3, exported to ONNX (opset 17, dynamic
batch). Verified against the torch original at export time: max abs difference
9.2e-06, cosine 1.000000. See app/ml/NOTICE.md for the licensing caveat.
"""

import io
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

from app.detect import load_upright
from app.detect_reid import best_animal_box

_MODEL_PATH = Path(__file__).resolve().parent / "ml" / "miewid_msv3.onnx"

MODEL_NAME = "miewid-msv3"  # written to embeddings.model
EMBED_DIM = 2152
_INPUT = 440

# ImageNet statistics, as used when the model was trained and benchmarked.
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(3, 1, 1)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(3, 1, 1)

_session: ort.InferenceSession | None = None
_lock = threading.Lock()


class ModelUnavailable(RuntimeError):
    """The ONNX weights are not present. Not committed to the repo (206 MB,
    and upstream declares no licence) -- run scripts/export_miewid_onnx.py."""


def _get_session() -> ort.InferenceSession:
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                if not _MODEL_PATH.exists():
                    raise ModelUnavailable(
                        f"{_MODEL_PATH} missing -- run scripts/export_miewid_onnx.py"
                    )
                _session = ort.InferenceSession(
                    str(_MODEL_PATH), providers=["CPUExecutionProvider"]
                )
    return _session


def preprocess(crop: Image.Image) -> np.ndarray:
    """Crop -> normalised NCHW batch of 1.

    Deliberately reproduces torchvision's Resize(440) + CenterCrop(440): resize
    the SHORTER side to 440 preserving aspect, then take the centre square.
    Resizing both sides to 440 instead would distort aspect ratio and silently
    shift every embedding away from the benchmarked distribution.
    """
    w, h = crop.size
    scale = _INPUT / min(w, h)
    nw, nh = max(_INPUT, round(w * scale)), max(_INPUT, round(h * scale))
    resized = crop.resize((nw, nh), Image.BICUBIC)

    left, top = (nw - _INPUT) // 2, (nh - _INPUT) // 2
    square = resized.crop((left, top, left + _INPUT, top + _INPUT))

    arr = np.asarray(square, dtype=np.float32).transpose(2, 0, 1) / 255.0
    return ((arr - _MEAN) / _STD)[None]


def embed_crop(crop: Image.Image) -> np.ndarray:
    """Embed an already-cropped dog. Returns (2152,) float32, L2-normalised."""
    sess = _get_session()
    out = sess.run(None, {sess.get_inputs()[0].name: preprocess(crop)})[0]
    vec = out[0].astype(np.float32)
    return vec / (np.linalg.norm(vec) + 1e-8)


def embed_photo(
    image_bytes: bytes,
) -> tuple[np.ndarray, tuple[int, int, int, int]] | None:
    """Detect the dog, crop, and embed. Returns (vector, box) or None when no
    dog clears the detection threshold.

    Callers should treat None and exceptions the same way the capture path
    treats a detector failure: record that no embedding exists and move on. A
    missing vector delays matching; a failed upload loses a sighting.
    """
    img = load_upright(image_bytes)
    found = best_animal_box(image_bytes)
    if found is None:
        return None
    box, _conf, _cls = found
    return embed_crop(img.crop(box)), box
