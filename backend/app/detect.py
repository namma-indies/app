"""Dog-presence gate for captures.

Runs a YOLOv8n detector (COCO) over an uploaded photo and reports the maximum
'dog'-class confidence anywhere in the frame. We only need presence, so we skip
box decoding / NMS entirely and read the raw class-score channel -- cheap and
robust. The threshold is tuned for high recall (catch real dogs) while still
rejecting obvious non-dog frames (a bed, a wall).

Model: Ultralytics YOLOv8n, AGPL-3.0. See app/ml/NOTICE.
"""

import io
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps

_MODEL_PATH = Path(__file__).resolve().parent / "ml" / "yolov8n.onnx"
_INPUT = 640
_DOG_CLASS = 16  # COCO class index for "dog"
_NUM_ATTRS = 84  # 4 box coords + 80 class scores

# High-recall presence threshold. Lower = fewer real dogs rejected (more
# non-dogs slip through); this is the single knob to tune from field feedback.
DOG_CONF_THRESHOLD = 0.25

_session: ort.InferenceSession | None = None
_lock = threading.Lock()


def _get_session() -> ort.InferenceSession:
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                _session = ort.InferenceSession(
                    str(_MODEL_PATH), providers=["CPUExecutionProvider"]
                )
    return _session


def load_upright(image_bytes: bytes) -> Image.Image:
    """Decode to RGB with EXIF orientation applied. Phone cameras store
    portrait shots rotated with an orientation tag; feeding those to the
    detector sideways measurably drops dog confidence, so we must match what
    `photos.process_photo` does before it hashes and stores the same pixels."""
    return ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")


def _letterbox(img: Image.Image) -> np.ndarray:
    """Resize keeping aspect ratio, pad to _INPUT square (grey), return a
    normalized NCHW float32 batch of 1."""
    w, h = img.size
    scale = min(_INPUT / w, _INPUT / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    resized = img.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("RGB", (_INPUT, _INPUT), (114, 114, 114))
    canvas.paste(resized, ((_INPUT - nw) // 2, (_INPUT - nh) // 2))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    return arr.transpose(2, 0, 1)[None]  # (1, 3, H, W)


def dog_confidence(image_bytes: bytes) -> float:
    """Max 'dog' confidence in the image, in [0, 1]. May raise on an
    unreadable image or model error -- callers should fail open (treat a
    detection failure as 'inconclusive' and let the save proceed)."""
    img = load_upright(image_bytes)
    sess = _get_session()
    out = sess.run(None, {sess.get_inputs()[0].name: _letterbox(img)})[0]
    o = out[0]  # drop batch dim
    # Export layout may be (84, 8400) or (8400, 84); pick the dog-score vector.
    if o.shape[0] == _NUM_ATTRS:
        dog_scores = o[4 + _DOG_CLASS]
    elif o.shape[1] == _NUM_ATTRS:
        dog_scores = o[:, 4 + _DOG_CLASS]
    else:
        raise ValueError(f"unexpected YOLO output shape {o.shape}")
    return float(np.max(dog_scores))
