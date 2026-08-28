"""Read capture date and place out of a photo's EXIF, before it is stripped.

For a live capture the device supplies both -- its GPS fix is more accurate
than EXIF and always present. This module exists for the other case: a photo
chosen from the camera roll, taken somewhere else, some time ago.

Why that matters beyond labelling: `captured_at` and `geog` are the two fields
`resolve_sighting`'s "PostGIS 1km -> HNSW" candidate search runs on. Recording
an import as here-and-now inserts a phantom sighting into the spatial prior of
wherever the phone happens to be, degrading matching for every real sighting in
that area. The date and place have to come from the file, or from the person.

Server-side by choice: `photos.process_photo` already opens these bytes with
PIL one function above where it discards the metadata, so nothing new is read
from disk, and one implementation covers web, iOS and Android identically
rather than trusting each platform's picker to report metadata the same way.

**No timezone is resolved here.** EXIF's `DateTimeOriginal` is naive local time;
`OffsetTimeOriginal` carries the zone but most cameras omit it. Guessing UTC
would shift an Indian evening capture across a day boundary. So this returns
the naive local string plus the offset *if the file stated one*, and the client
-- which knows the device's timezone -- resolves the rest.

Every parse here runs on unvalidated uploaded bytes, so every field is
independently best-effort: a malformed one yields None rather than an error.
"""

import io
import re
from dataclasses import dataclass

from PIL import Image

# EXIF tag numbers. Named rather than magic so the lookups below read.
_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_DATETIME_ORIGINAL = 36867
_OFFSET_TIME_ORIGINAL = 36881
_GPS_LAT_REF, _GPS_LAT = 1, 2
_GPS_LNG_REF, _GPS_LNG = 3, 4

# "YYYY:MM:DD HH:MM:SS" -- EXIF's colon-separated date is not ISO 8601.
_EXIF_DATETIME = re.compile(
    r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
)
_UTC_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


@dataclass(frozen=True)
class CaptureMetadata:
    """What the file claims about when and where it was taken.

    `captured_at_local` is a naive ISO-8601 string with no zone designator, e.g.
    "2026-08-05T18:42:11" -- deliberately not a datetime, so it cannot be
    mistaken for an absolute instant on the way through JSON.
    """

    captured_at_local: str | None
    utc_offset_minutes: int | None
    lat: float | None
    lng: float | None

    @property
    def has_date(self) -> bool:
        return self.captured_at_local is not None

    @property
    def has_location(self) -> bool:
        return self.lat is not None and self.lng is not None


EMPTY = CaptureMetadata(
    captured_at_local=None, utc_offset_minutes=None, lat=None, lng=None
)


def read_capture_metadata(raw: bytes) -> CaptureMetadata:
    """Best-effort capture date and coordinates from `raw`'s EXIF.

    Accepts a truncated file. EXIF's APP1 segment is capped at 64KB by the spec
    and sits at the head, so the first 128KB of a JPEG always contains it --
    which lets the preflight upload a slice instead of a whole 4MB photo.
    """
    try:
        img = Image.open(io.BytesIO(raw))
        exif = img.getexif()
    except Exception:
        # Not an image, truncated past usefulness, or a format PIL will not
        # open. Indistinguishable from "no metadata" as far as callers care.
        return EMPTY

    if not exif:
        return EMPTY

    try:
        exif_ifd = exif.get_ifd(_EXIF_IFD)
    except Exception:
        exif_ifd = {}
    try:
        gps_ifd = exif.get_ifd(_GPS_IFD)
    except Exception:
        gps_ifd = {}

    lat, lng = _read_coordinates(gps_ifd)
    return CaptureMetadata(
        captured_at_local=_read_datetime(exif_ifd),
        utc_offset_minutes=_read_offset(exif_ifd),
        lat=lat,
        lng=lng,
    )


def _read_datetime(exif_ifd: dict) -> str | None:
    raw = exif_ifd.get(_DATETIME_ORIGINAL)
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "ignore")
    if not isinstance(raw, str):
        return None
    m = _EXIF_DATETIME.match(raw.strip())
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.groups())
    # A zeroed date ("0000:00:00 00:00:00") is how some devices say "unknown".
    if not (1 <= month <= 12 and 1 <= day <= 31 and year >= 1900):
        return None
    if not (hour <= 23 and minute <= 59 and second <= 59):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"


def _read_offset(exif_ifd: dict) -> int | None:
    raw = exif_ifd.get(_OFFSET_TIME_ORIGINAL)
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "ignore")
    if not isinstance(raw, str):
        return None
    m = _UTC_OFFSET.match(raw.strip())
    if not m:
        return None
    sign, hours, minutes = m.groups()
    total = int(hours) * 60 + int(minutes)
    return -total if sign == "-" else total


def _read_coordinates(gps_ifd: dict) -> tuple[float | None, float | None]:
    lat = _degrees(gps_ifd.get(_GPS_LAT), gps_ifd.get(_GPS_LAT_REF), "S")
    lng = _degrees(gps_ifd.get(_GPS_LNG), gps_ifd.get(_GPS_LNG_REF), "W")
    if lat is None or lng is None:
        # Half a coordinate is no coordinate.
        return None, None
    if abs(lat) > 90 or abs(lng) > 180:
        return None, None
    if lat == 0.0 and lng == 0.0:
        # Devices write 0/0 when they never got a fix. Null Island, in the Gulf
        # of Guinea, is not a street-dog sighting.
        return None, None
    return lat, lng


def _degrees(dms, ref, negative_ref: str) -> float | None:
    """EXIF stores coordinates as three rationals plus a hemisphere letter."""
    if dms is None or len(dms) != 3:
        return None
    try:
        degrees, minutes, seconds = (float(part) for part in dms)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    value = degrees + minutes / 60 + seconds / 3600
    if isinstance(ref, bytes):
        ref = ref.decode("ascii", "ignore")
    if isinstance(ref, str) and ref.strip().upper().startswith(negative_ref):
        value = -value
    return value
