"""Pure, DB-free image processing for uploaded photos.

Given raw uploaded photo bytes, produce:
  (a) a metadata-stripped, fidelity-preserved original (privacy-safe but
      NOT degraded -- the ML-grade asset for future re-ID work),
  (b) a small thumbnail for the gallery,
  (c) dimensions plus a perceptual hash.

Format: WebP q90, no downscale. This replaces the conservative JPEG q95
default that was pending GitHub issue #1, now that the vision-model question
has been measured: across 47,671 frames and 21 encodings, WebP q90 costs
0.2 accuracy points against a lossless reference (0.9464 vs 0.9485) while
being 66% smaller than JPEG q95 on a real 12MP phone capture (674 kB vs
2001 kB). WebP also degrades far more gracefully than JPEG at low quality,
which matters if this number is ever pushed lower.
"""

import io
from dataclasses import dataclass

import imagehash
from PIL import Image, ImageOps

THUMBNAIL_MAX = 512  # longest edge of the thumbnail, px


@dataclass
class ProcessedPhoto:
    original: bytes  # metadata-stripped, full-resolution WebP (no downscale)
    thumbnail: bytes  # small WebP for the gallery
    width: int  # of the original
    height: int
    phash: str  # perceptual hash hex (imagehash.phash)
    content_type: str  # "image/webp"


def process_photo(raw: bytes) -> ProcessedPhoto:
    img = Image.open(io.BytesIO(raw))

    # Apply EXIF orientation first so pixels are upright, then we discard
    # all metadata (including GPS) by simply never re-attaching it below.
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    width, height = img.size

    original_buf = io.BytesIO()
    img.save(original_buf, "WEBP", quality=90, method=4)
    original_bytes = original_buf.getvalue()

    thumb = img.copy()
    thumb.thumbnail((THUMBNAIL_MAX, THUMBNAIL_MAX))
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, "WEBP", quality=80, method=4)
    thumbnail_bytes = thumb_buf.getvalue()

    phash = str(imagehash.phash(img))

    return ProcessedPhoto(
        original=original_bytes,
        thumbnail=thumbnail_bytes,
        width=width,
        height=height,
        phash=phash,
        content_type="image/webp",
    )
