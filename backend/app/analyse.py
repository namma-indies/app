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
from dataclasses import dataclass

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

    return Analysis(dog_confidence=dog, cat_confidence=cat, box=box, image=img)


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
