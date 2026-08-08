import io

import pytest
from PIL import Image

from app import detect_reid
from app.detect_reid import (
    BOX_MARGIN,
    COCO_CAT,
    COCO_DOG,
    REID_CONF_THRESHOLD,
    DetectorUnavailable,
    _letterbox,
    best_animal_box,
)


def _photo(size=(1200, 900)) -> bytes:
    """Non-symmetric content so the letterbox geometry assertions can't pass by
    accident on a flat fill."""
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 255 // w, y * 255 // h, 30)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def test_letterbox_preserves_aspect_and_reports_mapping():
    """Boxes come back in letterbox space and must be mapped to original
    pixels. If scale/pad are wrong the crop silently lands off-subject, which
    looks like bad matching rather than bad geometry."""
    img = Image.open(io.BytesIO(_photo((1200, 900)))).convert("RGB")
    batch, scale, pad_x, pad_y = _letterbox(img)
    assert batch.shape == (1, 3, 640, 640)
    assert scale == pytest.approx(640 / 1200)
    # 1200x900 is wider than tall, so padding goes top/bottom, not left/right.
    assert pad_x == 0
    assert pad_y > 0
    assert 0.0 <= float(batch.min()) and float(batch.max()) <= 1.0


def test_letterbox_pads_the_other_axis_for_tall_images():
    img = Image.open(io.BytesIO(_photo((600, 1200)))).convert("RGB")
    _batch, scale, pad_x, pad_y = _letterbox(img)
    assert scale == pytest.approx(640 / 1200)
    assert pad_y == 0
    assert pad_x > 0


def test_classes_and_thresholds_are_the_benchmarked_ones():
    """These constants are the contract with the offline measurements: COCO
    15/16, conf 0.10, 10% margin. Changing one invalidates the comparison."""
    assert COCO_CAT == 15 and COCO_DOG == 16
    assert REID_CONF_THRESHOLD == 0.10
    assert BOX_MARGIN == 0.10


def test_missing_weights_raise_a_named_error(monkeypatch, tmp_path):
    monkeypatch.setattr(detect_reid, "_session", None)
    monkeypatch.setattr(detect_reid, "_MODEL_PATH", tmp_path / "absent.onnx")
    with pytest.raises(DetectorUnavailable, match="export_yolo26x_onnx"):
        detect_reid._get_session()


@pytest.mark.skipif(
    not detect_reid._MODEL_PATH.exists(),
    reason="YOLO26x weights absent (223 MB, gitignored)",
)
def test_no_animal_in_a_synthetic_gradient():
    """A gradient contains no animal. Returning a box here would mean the
    threshold is letting noise through, and every such box becomes a junk
    vector in candidate search."""
    assert best_animal_box(_photo()) is None


@pytest.mark.skipif(
    not detect_reid._MODEL_PATH.exists(), reason="YOLO26x weights absent"
)
def test_box_is_inside_the_image_and_non_degenerate():
    """Run against a real dog photo if the benchmark set is present. The margin
    expansion must clip to the frame rather than producing out-of-bounds
    coordinates that PIL would silently accept and pad."""
    from pathlib import Path

    sample = Path("/home/ashie/pet-reid/data/kabosu/kabosu_1.jpg")
    if not sample.exists():
        pytest.skip("benchmark photo set not present on this machine")

    raw = sample.read_bytes()
    found = best_animal_box(raw)
    assert found is not None, "known dog photo produced no box"
    (x1, y1, x2, y2), conf, cls = found
    w, h = Image.open(io.BytesIO(raw)).size
    assert 0 <= x1 < x2 <= w
    assert 0 <= y1 < y2 <= h
    assert conf >= REID_CONF_THRESHOLD
    assert cls in (COCO_CAT, COCO_DOG)
