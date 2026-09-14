"""Decoding a photo the way the rest of the pipeline expects it.

This file used to hold a YOLOv8n dog-presence scorer. It is gone: detection
moved to `detect_reid` (YOLO26x) and `analyse.analyse` is the one pass every
caller uses. Its `DOG_CONF_THRESHOLD = 0.25` went with it, deliberately --
that number was calibrated against a model that scored a clearly visible dog
at 0.021, and leaving it in the tree is how it becomes the new detector's
threshold by inheritance. The current one is `settings.animal_confidence_min`,
chosen against rescored numbers (#67).

What is left is the one thing every path shares.
"""

import io

from PIL import Image, ImageOps


def load_upright(image_bytes: bytes) -> Image.Image:
    """Decode to RGB with EXIF orientation applied.

    Phone cameras store portrait shots rotated with an orientation tag;
    feeding those to the detector sideways measurably drops confidence, so
    this must match what `photos.process_photo` does before it hashes and
    stores the same pixels.
    """
    return ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
