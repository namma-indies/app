"""What the detector saw besides animals, for screening an upload (#181).

The YOLO pass every upload already gets is a COCO detector: it reports people
and furniture as readily as dogs, and until now everything but dog and cat
was thrown away. Three numbers per frame are kept for `app.screening`:

* `person`: the highest person score anywhere in the frame.
* `person_area`: the share of the frame covered by the largest confident
  person box. This, not `person`, is what screening holds on: a feeder's
  hand or a passer-by behind the dog is ordinary in a street-dog photo, and
  someone filling the frame is not. YOLO has no face class; a person large in
  the frame is the stand-in for a face.
* `indoor`: the highest score of furniture that only belongs indoors. Chairs
  and tables are left out: street stalls have them.

Kept apart from `app.detect_reid` on purpose: the GPU worker hashes that
module's source into its pipeline identity, and a screening rule is not a
change to how animals are found or embedded.
"""

from dataclasses import dataclass

COCO_PERSON = 0
# couch, bed, toilet, tv, microwave, oven, refrigerator.
INDOOR_CLASSES = frozenset({57, 59, 61, 62, 68, 69, 72})
# A person box counts toward `person_area` from this score. The same bar as a
# confident detection elsewhere; below it the box is too often a shadow.
PERSON_BOX_MIN = 0.5


@dataclass(frozen=True)
class SceneSignals:
    person: float = 0.0
    person_area: float = 0.0
    indoor: float = 0.0


def scene_signals(dets, size, scale=1.0, pad_x=0, pad_y=0) -> SceneSignals:
    """Read one frame's NMS rows ([x1, y1, x2, y2, conf, cls], letterboxed).

    `size` is the upright image's (width, height); the scale and padding map
    a letterboxed box back onto it, as for the animal boxes.
    """
    w, h = size
    frame = float(w) * float(h)
    person = area = indoor = 0.0
    for x1, y1, x2, y2, conf, cls in dets:
        c, k = float(conf), int(cls)
        if k == COCO_PERSON:
            person = max(person, c)
            if c >= PERSON_BOX_MIN and frame > 0:
                bw = min(w, (float(x2) - pad_x) / scale) - max(0.0, (float(x1) - pad_x) / scale)
                bh = min(h, (float(y2) - pad_y) / scale) - max(0.0, (float(y1) - pad_y) / scale)
                area = max(area, max(0.0, bw) * max(0.0, bh) / frame)
        elif k in INDOOR_CLASSES:
            indoor = max(indoor, c)
    return SceneSignals(min(person, 1.0), min(area, 1.0), min(indoor, 1.0))


SCENE_FIELDS = ("person", "person_area", "indoor")


def as_wire(scene: SceneSignals | None) -> dict:
    """The optional completion fields for a frame; none when not measured."""
    if scene is None:
        return {}
    return {"person": scene.person, "person_area": scene.person_area, "indoor": scene.indoor}


def from_wire(frame) -> SceneSignals | None:
    """A completion frame's signals, or None from a worker that sent none."""
    values = [getattr(frame, k, None) for k in SCENE_FIELDS]
    if all(v is None for v in values):
        return None
    return SceneSignals(*(0.0 if v is None else float(v) for v in values))


def digest_frames(frames: list[dict]) -> None:
    """Leave unmeasured signals out of a completion digest, in place, so a
    completion replayed across the deploy that added them still matches."""
    for frame in frames:
        for k in SCENE_FIELDS:
            if frame.get(k, 0) is None:
                del frame[k]
