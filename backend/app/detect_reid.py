"""Higher-accuracy animal detection for the re-identification path.

`detect.py` shipped YOLOv8n as a *presence gate*. This module replaces it for
both jobs -- locating the animal to crop, and scoring the capture. Neither runs
on the user's critical path (both are background tasks after the 201), so the
weaker model bought nothing.

Measured on 29 varied real-world photos (distant subjects, black-and-white,
groups, occlusion -- much harder than a deliberate close-up capture):

    model          dogs found   cats found   ms/photo (CPU)
    yolov8n            9/17        10/12            17
    yolov8s-seg       12/17         9/12            20
    yolov8x           13/17        10/12         1,560
    yolo26x           15/17        10/12         1,843

(Timings above are via torch; through ONNX Runtime yolo26x measures ~314 ms.)

Six sightings in seventeen that yolov8n silently drops, yolo26x finds. A missed
box means no embedding, so the sighting can never be matched, and nothing
surfaces that as an error. The old gate was also simply wrong on some captures:
it scored a clearly visible dog at 0.021 where this model gives 0.800, which
made `sightings.dog_confidence` unreliable as a review signal.

Note this does not crop *tighter*, only *more often*: where a dog is being held
the correct box legitimately includes the handler.

Model: Ultralytics YOLO26x, exported with NMS baked in (output is already
[x1, y1, x2, y2, conf, cls]). AGPL-3.0 -- see app/ml/NOTICE.md.
"""

import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

from app.detect import load_upright

_MODEL_PATH = Path(__file__).resolve().parent / "ml" / "yolo26x.onnx"
_INPUT = 640

COCO_CAT = 15
COCO_DOG = 16
# Both, because a cat sighting that silently produces no embedding is
# indistinguishable from one that failed. `animal_confidence` keeps them
# separate so the dog_confidence label stays a dog score.
_ANIMAL_CLASSES = frozenset({COCO_CAT, COCO_DOG})

# Lower than the presence gate's 0.25: a false box here costs one junk vector
# that a human review would reject, while a missed box costs the whole sighting
# silently. The offline benchmark used this value.
REID_CONF_THRESHOLD = 0.10

BOX_MARGIN = 0.10  # matches the validated offline crop recipe

_session: ort.InferenceSession | None = None
_lock = threading.Lock()


class DetectorUnavailable(RuntimeError):
    """Weights absent -- 223 MB, not committed. See scripts/export_yolo26x_onnx.py."""


def _get_session() -> ort.InferenceSession:
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                if not _MODEL_PATH.exists():
                    raise DetectorUnavailable(
                        f"{_MODEL_PATH} missing -- run scripts/export_yolo26x_onnx.py"
                    )
                _session = ort.InferenceSession(
                    str(_MODEL_PATH), providers=["CPUExecutionProvider"]
                )
    return _session


def _letterbox(img: Image.Image) -> tuple[np.ndarray, float, int, int]:
    """Aspect-preserving resize onto a grey _INPUT square. Returns the batch
    plus the scale and padding needed to map boxes back to original pixels."""
    w, h = img.size
    scale = min(_INPUT / w, _INPUT / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    canvas = Image.new("RGB", (_INPUT, _INPUT), (114, 114, 114))
    pad_x, pad_y = (_INPUT - nw) // 2, (_INPUT - nh) // 2
    canvas.paste(img.resize((nw, nh), Image.BILINEAR), (pad_x, pad_y))
    arr = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    return arr, scale, pad_x, pad_y


def best_animal_box(
    image_bytes: bytes,
) -> tuple[tuple[int, int, int, int], float, int] | None:
    """Largest confident dog/cat box as ((x1,y1,x2,y2), confidence, coco_class)
    in ORIGINAL image pixels, or None if nothing clears the threshold.

    Largest rather than most-confident, matching the offline recipe: with two
    animals in frame the bigger one is the subject being photographed, while
    the most confident may be a clearer but incidental animal in the
    background.
    """
    img = load_upright(image_bytes)
    batch, scale, pad_x, pad_y = _letterbox(img)
    sess = _get_session()
    out = sess.run(None, {sess.get_inputs()[0].name: batch})[0]

    dets = out[0]  # (300, 6) -- NMS already applied at export time
    w, h = img.size
    best = None
    best_area = 0.0
    for x1, y1, x2, y2, conf, cls in dets:
        if conf < REID_CONF_THRESHOLD or int(cls) not in _ANIMAL_CLASSES:
            continue
        # undo letterbox
        ox1 = (float(x1) - pad_x) / scale
        oy1 = (float(y1) - pad_y) / scale
        ox2 = (float(x2) - pad_x) / scale
        oy2 = (float(y2) - pad_y) / scale
        area = max(0.0, ox2 - ox1) * max(0.0, oy2 - oy1)
        if area > best_area:
            best_area, best = area, (ox1, oy1, ox2, oy2, float(conf), int(cls))

    if best is None:
        return None

    ox1, oy1, ox2, oy2, conf, cls = best
    mx, my = BOX_MARGIN * (ox2 - ox1), BOX_MARGIN * (oy2 - oy1)
    x1i, y1i = max(0, int(ox1 - mx)), max(0, int(oy1 - my))
    x2i, y2i = min(w, int(ox2 + mx)), min(h, int(oy2 + my))
    if x2i <= x1i or y2i <= y1i:
        return None
    return (x1i, y1i, x2i, y2i), conf, cls


def animal_confidence(image_bytes: bytes) -> tuple[float, float]:
    """(dog_confidence, cat_confidence) -- the highest score for each class
    anywhere in the frame, in [0, 1].

    Replaces the yolov8n presence gate for labelling captures. Both callers now
    share one model and one session: the gate was never on the user's critical
    path (it has always been a background task), so its only justification for
    a weaker model was a latency budget that does not exist.

    The old gate was measurably wrong on real photos -- it scored a clearly
    visible dog at 0.021 where this model gives 0.800.
    """
    img = load_upright(image_bytes)
    batch, _scale, _px, _py = _letterbox(img)
    sess = _get_session()
    dets = sess.run(None, {sess.get_inputs()[0].name: batch})[0][0]
    dog = cat = 0.0
    for *_box, conf, cls in dets:
        c = float(conf)
        if int(cls) == COCO_DOG:
            dog = max(dog, c)
        elif int(cls) == COCO_CAT:
            cat = max(cat, c)
    return dog, cat
