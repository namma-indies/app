"""EXIF capture-metadata extraction, for camera-roll imports.

A photo chosen from the camera roll was taken somewhere else, some time ago.
`captured_at` and `geog` are exactly the two fields `resolve_sighting`'s
"PostGIS 1km -> HNSW" candidate search depends on, so recording an import as
here-and-now does not merely mislabel it -- it poisons the spatial prior for
every match against that area. The date and place have to come from the file.

Parsing lives on the server because the pipeline already opens these bytes with
PIL one function above where it discards the metadata, and because one
implementation then covers web, iOS and Android identically.
"""

import io

import piexif
import pytest
from PIL import Image

from app.exif import CaptureMetadata, read_capture_metadata


def _dms(value: float) -> tuple:
    """Decimal degrees -> the (deg, min, sec) rational triple EXIF stores."""
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = round(((v - d) * 60 - m) * 60 * 10000)
    return ((d, 1), (m, 1), (s, 10000))


def _jpeg_with_exif(exif_dict: dict, size=(64, 48)) -> bytes:
    img = Image.new("RGB", size, (120, 140, 110))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, exif=piexif.dump(exif_dict))
    return buf.getvalue()


def _exif(*, date=None, offset=None, lat=None, lng=None) -> dict:
    d: dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    if date is not None:
        d["Exif"][piexif.ExifIFD.DateTimeOriginal] = date.encode()
    if offset is not None:
        d["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = offset.encode()
    if lat is not None:
        d["GPS"][piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
        d["GPS"][piexif.GPSIFD.GPSLatitude] = _dms(lat)
    if lng is not None:
        d["GPS"][piexif.GPSIFD.GPSLongitudeRef] = b"E" if lng >= 0 else b"W"
        d["GPS"][piexif.GPSIFD.GPSLongitude] = _dms(lng)
    return d


# --- the happy path ---------------------------------------------------------


def test_reads_date_and_location():
    raw = _jpeg_with_exif(_exif(date="2026:08:05 18:42:11", lat=12.9352, lng=77.6245))
    md = read_capture_metadata(raw)
    assert md.captured_at_local == "2026-08-05T18:42:11"
    assert md.lat == pytest.approx(12.9352, abs=1e-5)
    assert md.lng == pytest.approx(77.6245, abs=1e-5)


def test_reads_utc_offset_when_present():
    raw = _jpeg_with_exif(_exif(date="2026:08:05 18:42:11", offset="+05:30"))
    assert read_capture_metadata(raw).utc_offset_minutes == 330


def test_negative_utc_offset():
    raw = _jpeg_with_exif(_exif(date="2026:08:05 18:42:11", offset="-04:00"))
    assert read_capture_metadata(raw).utc_offset_minutes == -240


def test_offset_absent_is_none_not_zero():
    """None means 'the device did not say', which the client resolves with its
    own timezone. Zero would silently claim UTC and shift an Indian capture by
    five and a half hours -- across a day boundary for an evening sighting."""
    raw = _jpeg_with_exif(_exif(date="2026:08:05 18:42:11"))
    assert read_capture_metadata(raw).utc_offset_minutes is None


# --- southern / western hemispheres ----------------------------------------


def test_south_and_west_are_negated():
    raw = _jpeg_with_exif(_exif(lat=-33.8688, lng=-70.6693))
    md = read_capture_metadata(raw)
    assert md.lat == pytest.approx(-33.8688, abs=1e-5)
    assert md.lng == pytest.approx(-70.6693, abs=1e-5)


# --- the common absent cases ----------------------------------------------


def test_no_exif_at_all():
    img = Image.new("RGB", (32, 32))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    md = read_capture_metadata(buf.getvalue())
    assert md == CaptureMetadata(captured_at_local=None, utc_offset_minutes=None, lat=None, lng=None)


def test_date_without_location():
    """A WhatsApp forward or a screenshot: some fields survive, GPS rarely does."""
    md = read_capture_metadata(_jpeg_with_exif(_exif(date="2026:08:05 18:42:11")))
    assert md.captured_at_local == "2026-08-05T18:42:11"
    assert md.lat is None and md.lng is None


def test_location_without_date():
    md = read_capture_metadata(_jpeg_with_exif(_exif(lat=12.9, lng=77.6)))
    assert md.captured_at_local is None
    assert md.lat is not None


def test_zero_zero_coordinates_treated_as_absent():
    """Devices write 0/0 when they had no fix. Null Island is not a sighting."""
    md = read_capture_metadata(_jpeg_with_exif(_exif(lat=0.0, lng=0.0)))
    assert md.lat is None and md.lng is None


def test_half_a_coordinate_is_no_coordinate():
    md = read_capture_metadata(_jpeg_with_exif(_exif(lat=12.9)))
    assert md.lat is None and md.lng is None


# --- robustness: this parses attacker-supplied bytes ------------------------


def test_garbage_bytes_do_not_raise():
    assert read_capture_metadata(b"this is not an image") == CaptureMetadata(
        captured_at_local=None, utc_offset_minutes=None, lat=None, lng=None
    )


def test_empty_bytes_do_not_raise():
    md = read_capture_metadata(b"")
    assert md.captured_at_local is None


def test_malformed_date_is_ignored_not_fatal():
    md = read_capture_metadata(_jpeg_with_exif(_exif(date="not a date")))
    assert md.captured_at_local is None


def test_out_of_range_coordinates_rejected():
    """A latitude of 200 degrees is not a place. PostGIS would take it."""
    raw = _jpeg_with_exif(_exif(lat=91.0, lng=200.0))
    md = read_capture_metadata(raw)
    assert md.lat is None and md.lng is None


# --- the head-only optimisation -------------------------------------------


def test_reads_from_a_truncated_head():
    """The client uploads only the first slice of the file for the preflight --
    EXIF's APP1 segment is capped at 64KB by the spec and sits at the front, so
    a 128KB head always contains it. This keeps the preflight from re-uploading
    a 4MB photo just to learn its date."""
    img = Image.new("RGB", (1600, 1200))
    px = img.load()
    for y in range(0, 1200, 5):
        for x in range(0, 1600, 5):
            px[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
    buf = io.BytesIO()
    img.save(
        buf, "JPEG", quality=92, exif=piexif.dump(_exif(date="2026:08:05 18:42:11", lat=12.9, lng=77.6))
    )
    raw = buf.getvalue()
    assert len(raw) > 200_000, "test image must be large enough for truncation to matter"

    md = read_capture_metadata(raw[:131_072])
    assert md.captured_at_local == "2026-08-05T18:42:11"
    assert md.lat == pytest.approx(12.9, abs=1e-4)
