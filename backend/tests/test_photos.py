import io

import pytest

import piexif
from PIL import Image

from app.photos import ProcessedPhoto, process_photo


def _jpeg_with_gps(size=(1200, 900), color=(120, 90, 60)) -> bytes:
    img = Image.new("RGB", size, color)
    # Draw a gradient/pattern so the image isn't flat -- flat fills can
    # collide under phash regardless of color, which would make the
    # discrimination test meaningless.
    pixels = img.load()
    w, h = size
    cr, cg, cb = color
    for y in range(h):
        for x in range(0, w, max(1, w // 64)):
            shade = (
                (x * 255 // w + cr) % 256,
                (y * 255 // h + cg) % 256,
                ((x + y) * 255 // (w + h) + cb) % 256,
            )
            for dx in range(min(max(1, w // 64), w - x)):
                pixels[x + dx, y] = shade
    # inject EXIF incl. a GPS tag
    exif = {
        "0th": {piexif.ImageIFD.Make: b"TestCam"},
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: [(12, 1), (58, 1), (0, 1)],
        },
        "Exif": {},
        "1st": {},
        "thumbnail": None,
    }
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=piexif.dump(exif))
    return buf.getvalue()


def test_original_strips_all_exif_but_keeps_dimensions():
    raw = _jpeg_with_gps(size=(1200, 900))
    out = process_photo(raw)
    assert isinstance(out, ProcessedPhoto)
    reopened = Image.open(io.BytesIO(out.original))
    assert dict(reopened.getexif()) == {}  # no EXIF, no GPS
    assert (out.width, out.height) == (1200, 900)  # original not downscaled
    assert reopened.size == (1200, 900)



@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_no_location_metadata_survives_in_anything_stored(fmt):
    """Originals and thumbnails are both served to other observers, so neither
    may carry the uploader's GPS -- as EXIF or as an XMP packet, whatever the
    upload's format. Crops and clip frames are re-encoded through this same
    function, so this covers them too."""
    gps_exif = piexif.dump({"GPS": {
        piexif.GPSIFD.GPSLatitudeRef: b"N",
        piexif.GPSIFD.GPSLatitude: [(12, 1), (58, 1), (0, 1)],
    }})
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
           b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
           b'xmlns:exif="http://ns.adobe.com/exif/1.0/" exif:GPSLatitude="12,58.0N"/>'
           b'</rdf:RDF></x:xmpmeta>')
    buf = io.BytesIO()
    extra = {"xmp": xmp} if fmt != "PNG" else {}
    Image.new("RGB", (640, 480), (90, 130, 110)).save(buf, fmt, exif=gps_exif, **extra)
    assert Image.open(io.BytesIO(buf.getvalue())).info.get("exif"), "fixture must carry EXIF"

    out = process_photo(buf.getvalue())

    for stored in (out.original, out.thumbnail):
        reopened = Image.open(io.BytesIO(stored))
        assert dict(reopened.getexif()) == {}
        assert not reopened.info.get("exif") and not reopened.info.get("xmp")
        assert b"xmpmeta" not in stored and b"GPS" not in stored

def test_thumbnail_is_smaller():
    out = process_photo(_jpeg_with_gps(size=(1200, 900)))
    t = Image.open(io.BytesIO(out.thumbnail))
    assert max(t.size) <= 512
    assert len(out.thumbnail) < len(out.original)


def test_phash_stable_and_discriminating():
    a1 = process_photo(_jpeg_with_gps(color=(120, 90, 60))).phash
    a2 = process_photo(_jpeg_with_gps(color=(120, 90, 60))).phash
    b = process_photo(_jpeg_with_gps(color=(10, 200, 10))).phash
    assert a1 == a2
    assert a1 != b


# --- thumbnail key derivation ------------------------------------------------
# Regression: dex.py derived the thumbnail key with
# `s3_key.replace(".jpg", "_thumb.jpg")`, which is a silent no-op on the .webp
# keys the pipeline has written since the WebP switch -- every gallery cell and
# map popup served the full-resolution original. Extension-agnostic by design:
# photos captured before that switch still have .jpg keys on prod, and their
# thumbnails are .jpg too.


def test_thumb_key_webp():
    from app.photos import thumb_key

    assert (
        thumb_key("sightings/abc/def.webp") == "sightings/abc/def_thumb.webp"
    )


def test_thumb_key_legacy_jpg():
    from app.photos import thumb_key

    assert thumb_key("sightings/abc/def.jpg") == "sightings/abc/def_thumb.jpg"


def test_thumb_key_never_returns_the_original():
    """The bug's signature: a derivation that quietly returns its input."""
    from app.photos import thumb_key

    for key in ("sightings/a/b.webp", "sightings/a/b.jpg", "sightings/a/b.png"):
        assert thumb_key(key) != key


def test_thumb_key_only_touches_the_final_extension():
    """A dot in a directory name must not be mistaken for the extension."""
    from app.photos import thumb_key

    assert (
        thumb_key("sightings/v1.2/photo.webp") == "sightings/v1.2/photo_thumb.webp"
    )


def test_thumb_key_extensionless_key():
    from app.photos import thumb_key

    assert thumb_key("sightings/a/b") == "sightings/a/b_thumb"
