import io

import numpy as np
import piexif
from PIL import Image

from app.detect import _letterbox, load_upright


def _patterned_image(size=(1200, 900)) -> Image.Image:
    """A non-symmetric image -- a flat or symmetric fill would survive rotation
    unchanged and make the orientation assertions vacuous."""
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 255 // w, y * 255 // h, 40)
    return img


def _as_jpeg(img: Image.Image, orientation: int | None = None) -> bytes:
    buf = io.BytesIO()
    kwargs = {}
    if orientation is not None:
        exif = {"0th": {piexif.ImageIFD.Orientation: orientation}, "Exif": {},
                "GPS": {}, "1st": {}, "thumbnail": None}
        kwargs["exif"] = piexif.dump(exif)
    img.save(buf, "JPEG", quality=95, **kwargs)
    return buf.getvalue()


def test_load_upright_applies_exif_orientation():
    """A portrait phone photo arrives rotated with orientation=6. The detector
    must see the same upright pixels the rest of the pipeline stores, or a real
    dog gets scored sideways and the gate rejects it."""
    upright = _patterned_image()
    # How a camera writes it: pixels rotated 90° CCW, tag says "rotate back".
    rotated_bytes = _as_jpeg(upright.rotate(90, expand=True), orientation=6)

    loaded = load_upright(rotated_bytes)

    assert loaded.size == upright.size
    # JPEG is lossy, so compare approximately rather than byte-for-byte.
    diff = np.abs(
        np.asarray(loaded, dtype=np.int16)
        - np.asarray(upright.convert("RGB"), dtype=np.int16)
    )
    assert diff.mean() < 3.0


def test_letterbox_identical_for_tagged_and_untagged_same_scene():
    """End of the preprocessing chain: what reaches the model must not depend
    on how the orientation was encoded."""
    upright = _patterned_image()
    plain = _letterbox(load_upright(_as_jpeg(upright)))
    tagged = _letterbox(load_upright(_as_jpeg(upright.rotate(90, expand=True),
                                              orientation=6)))

    assert plain.shape == tagged.shape
    assert np.abs(plain - tagged).mean() < 0.02


def test_load_upright_handles_missing_exif():
    """Most non-phone uploads carry no orientation tag at all."""
    upright = _patterned_image()
    assert load_upright(_as_jpeg(upright)).size == upright.size
