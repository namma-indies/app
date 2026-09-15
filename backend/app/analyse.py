"""One detection pass per photo, used by everything that needs it.

WHAT THIS REPLACES
------------------
Every uploaded photo ran yolo26x **twice** and decoded the JPEG **three
times**, from two background tasks launched by the same request:

    _score_and_save_dog_confidence -> animal_confidence(raw)
                                        -> load_upright + sess.run
    _embed_and_save                -> embed_photo(raw)
                                        -> load_upright
                                        -> best_animal_box(raw)
                                             -> load_upright + sess.run

Both forward passes were over identical bytes and produced overlapping
answers: one wanted the per-class confidences, the other wanted the best box.
A single `sess.run` gives both.

WHY IT MATTERS MORE THAN IT SOUNDS
----------------------------------
Measured on a 2-core box (the production shape), one yolo26x pass is ~864 ms.
So the duplication cost ~864 ms per photo, and clips multiply it by the frame
count: a 12-frame clip was spending about **ten seconds** of CPU re-detecting
animals it had already found, on the same two cores that serve requests.

Video makes this the dominant cost rather than an annoyance, which is why it
is worth fixing before any of the harder questions about where inference runs.
"""

import logging
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tracking import CaptureAnalysis, FrameAnalysis

import numpy as np
from PIL import Image

from app.detect import load_upright
from app.detect_reid import (
    BOX_MARGIN,
    COCO_CAT,
    COCO_DOG,
    REID_CONF_THRESHOLD,
    _get_session,
    _letterbox,
    AnimalDetection,
    animal_detections,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Analysis:
    """Everything one detection pass can tell us about a frame."""

    dog_confidence: float
    cat_confidence: float
    # Largest confident dog/cat box in ORIGINAL pixels, with the crop margin
    # already applied, or None when nothing cleared the threshold.
    box: tuple[int, int, int, int] | None
    # The decoded, upright image. Carried so callers can crop without decoding
    # the JPEG a second time.
    image: Image.Image
    instances: tuple[AnimalDetection, ...] = ()

    @property
    def has_animal(self) -> bool:
        return self.box is not None


def analyse(image_bytes: bytes) -> Analysis:
    """Decode once, forward once, return both the confidences and the box.

    Deliberately does not embed. The detector and the embedder have different
    failure modes -- a missing box means "no animal here", a failed embed means
    "we could not measure this animal" -- and collapsing them would lose that
    distinction, which `_embed_and_save` relies on to decide what to log.
    """
    img = load_upright(image_bytes)
    batch, scale, pad_x, pad_y = _letterbox(img)
    sess = _get_session()
    dets = sess.run(None, {sess.get_inputs()[0].name: batch})[0][0]

    w, h = img.size
    dog = cat = 0.0
    best = None
    best_area = 0.0

    for x1, y1, x2, y2, conf, cls in dets:
        c = float(conf)
        k = int(cls)
        # Confidences are read over EVERY detection regardless of threshold,
        # matching the old animal_confidence: it reported the highest score
        # anywhere in the frame, and a label is allowed to be low.
        if k == COCO_DOG:
            dog = max(dog, c)
        elif k == COCO_CAT:
            cat = max(cat, c)

        if c < REID_CONF_THRESHOLD or k not in (COCO_DOG, COCO_CAT):
            continue
        # Largest, not most confident, matching the offline crop recipe: with
        # two animals in frame the bigger one is the subject, while the most
        # confident may be a clearer but incidental animal behind it.
        ox1 = (float(x1) - pad_x) / scale
        oy1 = (float(y1) - pad_y) / scale
        ox2 = (float(x2) - pad_x) / scale
        oy2 = (float(y2) - pad_y) / scale
        area = max(0.0, ox2 - ox1) * max(0.0, oy2 - oy1)
        if area > best_area:
            best_area, best = area, (ox1, oy1, ox2, oy2)

    box = None
    if best is not None:
        ox1, oy1, ox2, oy2 = best
        mx, my = BOX_MARGIN * (ox2 - ox1), BOX_MARGIN * (oy2 - oy1)
        x1i, y1i = max(0, int(ox1 - mx)), max(0, int(oy1 - my))
        x2i, y2i = min(w, int(ox2 + mx)), min(h, int(oy2 + my))
        # A margin can push a sliver box inside-out at the frame edge. The old
        # best_animal_box returned None for that rather than an empty crop.
        if x2i > x1i and y2i > y1i:
            box = (x1i, y1i, x2i, y2i)

    return Analysis(dog_confidence=dog, cat_confidence=cat, box=box, image=img,
                    instances=animal_detections(dets, img.size, scale, pad_x, pad_y))


def embed_analysis(a: Analysis) -> np.ndarray | None:
    """The MiewID vector for an already-analysed frame, or None if no animal.

    Separate from `analyse` so the caller can record dog_confidence even when
    the embedding step fails -- which is the existing contract: a detector
    failure costs a label, an embedder failure costs matchability, and neither
    costs the sighting.
    """
    if a.box is None:
        return None
    from app.embed import embed_crop

    return embed_crop(a.image.crop(a.box))


def analyse_instances(image_bytes: bytes, *, source_id: str = "photo:0",
                      frame_index: int = 0, timestamp_seconds: float | None = None) -> "FrameAnalysis":
    """CPU photo fallback with the same per-instance contract as the GPU path."""
    from app.embed import embed_crop
    from app.tracking import make_frame

    if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= 32 * 1024 * 1024:
        raise ValueError("photo exceeds byte budget")
    with Image.open(io.BytesIO(image_bytes)) as image:
        if image.width * image.height > 24_000_000 or getattr(image, "n_frames", 1) != 1:
            raise ValueError("photo exceeds pixel budget or is animated")
    result = analyse(image_bytes)
    evidence = []
    for detection in result.instances:
        try:
            crop = result.image.crop(detection.crop_box)
            scale = 440 / min(crop.size)
            if max(440, round(crop.width * scale)) * max(440, round(crop.height * scale)) > 24_000_000:
                raise ValueError("embedding resize exceeds pixel budget")
            vector = embed_crop(crop)
            if vector.shape != (2152,) or not np.isfinite(vector).all() or not np.isclose(
                    np.linalg.norm(vector), 1, atol=1e-5):
                raise ValueError("invalid instance embedding")
            evidence.append((detection, vector, None))
        except Exception:
            logger.warning("instance embedding failed", exc_info=True)
            evidence.append((detection, None, "embedding_failed"))
    return make_frame(source_id, frame_index, timestamp_seconds, *result.image.size,
                      result.dog_confidence, result.cat_confidence, evidence)


def analyse_photos(stored_photos: list[bytes]) -> "CaptureAnalysis":
    """A photo burst is one capture; repeated views require association review."""
    from app.tracking import SamplingCoverage, associate_frames

    if not 1 <= len(stored_photos) <= 12:
        raise ValueError("capture must contain 1..12 photos")
    frames = []
    count = 0
    for index, raw in enumerate(stored_photos):
        frame = analyse_instances(raw, source_id=f"photo:{index}", frame_index=index)
        count += len(frame.instances)
        if count > 96:
            raise ValueError("capture exceeds instance budget")
        frames.append(frame)
    frames = tuple(frames)
    coverage = SamplingCoverage("photos", len(frames), len(frames), None, None, None,
                                12, 12, True)
    return associate_frames(frames, coverage)
