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


def best_dog_box(image_bytes: bytes) -> tuple[tuple[int, int, int, int], float] | None:
    """Box of the highest-confidence dog, as (x1, y1, x2, y2) in ORIGINAL image
    pixels, plus that confidence. None when nothing clears the threshold.

    The presence gate above deliberately skips box decoding because it only
    needs a score. Re-ID needs the crop: embedding a whole frame with a small
    dog in the corner mostly embeds the street. We take the single
    highest-confidence anchor rather than running NMS -- with one subject per
    capture the winner is the same either way, and NMS would be machinery
    earning nothing.

    Deviation worth knowing: the offline benchmark picked the *largest* box
    among detections; this picks the *most confident*. They coincide on
    single-dog frames, which is the capture flow, but they can disagree when
    two dogs are in shot.
    """
    img = load_upright(image_bytes)
    sess = _get_session()
    out = sess.run(None, {sess.get_inputs()[0].name: _letterbox(img)})[0]
    o = out[0]
    if o.shape[0] == _NUM_ATTRS:
        dog_scores, boxes = o[4 + _DOG_CLASS], o[:4]
    elif o.shape[1] == _NUM_ATTRS:
        dog_scores, boxes = o[:, 4 + _DOG_CLASS], o[:, :4].T
    else:
        raise ValueError(f"unexpected YOLO output shape {o.shape}")

    i = int(np.argmax(dog_scores))
    conf = float(dog_scores[i])
    if conf < DOG_CONF_THRESHOLD:
        return None

    # Undo the letterbox: model coords -> original pixels.
    w, h = img.size
    scale = min(_INPUT / w, _INPUT / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    pad_x, pad_y = (_INPUT - nw) // 2, (_INPUT - nh) // 2
    cx, cy, bw, bh = (float(v) for v in boxes[:, i])
    x1 = (cx - bw / 2 - pad_x) / scale
    y1 = (cy - bh / 2 - pad_y) / scale
    x2 = (cx + bw / 2 - pad_x) / scale
    y2 = (cy + bh / 2 - pad_y) / scale

    # 10% margin on each side, matching the validated offline recipe: a tight
    # box clips ears and tail, which are exactly the identity cues.
    mx, my = 0.10 * (x2 - x1), 0.10 * (y2 - y1)
    x1, y1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
    x2, y2 = min(w, int(x2 + mx)), min(h, int(y2 + my))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2), conf


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
