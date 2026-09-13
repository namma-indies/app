"""Video -> phash-diverse frame extraction, at one frame per second.

Decodes a short video clip via imageio (backed by imageio-ffmpeg's bundled
static ffmpeg binary -- no system ffmpeg / apt dependency needed, works in
the slim container too), subsamples to roughly `target_fps`, runs each
sampled frame through the existing `process_photo` pipeline, and greedily
keeps a visually diverse subset by perceptual-hash hamming distance.

`target_fps` is 1.0: a five-second clip yields about five frames, each a
second apart. Denser sampling mostly produced near-duplicates that the phash
filter then discarded anyway, and the frames are now averaged into one vector
per sighting, where samples of the same instant add nothing.

This module returns frames and nothing else. The caller
(`routes/sighting.py`) also stores the clip itself now, so a better detector
or a newer embedding model can be re-run over the original footage -- under
the old discard-after-extraction design every frame not chosen was gone for
good.
"""

import io
import os
import tempfile

import imageio.v2 as imageio
import imagehash
from PIL import Image

from app.photos import process_photo, ProcessedPhoto


def extract_diverse_frames(
    raw_video: bytes,
    *,
    target_fps: float = 1.0,  # one frame per second

    max_raw: int = 20,  # never decode more than this many sampled frames
    keep: int = 12,  # final cap of diverse frames
    # Keep every sampled frame by default. This was 8, which discarded any
    # frame within 8 hamming of one already kept -- and frames a second apart
    # from a steady clip are far closer than that, so a five-second clip of a
    # sitting dog yielded ONE frame (measured). That was right when frames were
    # only ever stored as separate photos and near-duplicates were waste. It is
    # wrong now that they are averaged into one vector per sighting, where
    # repeated samples of the same instant are exactly what cancels the noise.
    # Raise it again for a path that wants distinct views rather than a mean.
    phash_hamming_min: int = 0,
) -> list[ProcessedPhoto]:
    """Decode -> subsample to ~target_fps -> process each -> greedily keep
    visually diverse frames by phash hamming distance -> cap at `keep`.
    Raises ValueError if no decodable frames.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(raw_video)
            tmp_path = tmp.name

        reader = imageio.get_reader(tmp_path, format="ffmpeg")
        try:
            meta = reader.get_meta_data()
            fps = meta.get("fps") or target_fps
            stride = max(1, round(fps / target_fps))

            sampled_processed: list[ProcessedPhoto] = []
            for i, frame in enumerate(reader):
                if i % stride != 0:
                    continue
                if len(sampled_processed) >= max_raw:
                    break
                buf = io.BytesIO()
                Image.fromarray(frame).save(buf, "JPEG")
                sampled_processed.append(process_photo(buf.getvalue()))
        finally:
            reader.close()

        if not sampled_processed:
            raise ValueError("no decodable frames in video")

        kept: list[ProcessedPhoto] = [sampled_processed[0]]
        kept_hashes = [imagehash.hex_to_hash(sampled_processed[0].phash)]
        for candidate in sampled_processed[1:]:
            if len(kept) >= keep:
                break
            cand_hash = imagehash.hex_to_hash(candidate.phash)
            min_dist = min(cand_hash - h for h in kept_hashes)
            if min_dist >= phash_hamming_min:
                kept.append(candidate)
                kept_hashes.append(cand_hash)

        return kept
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
