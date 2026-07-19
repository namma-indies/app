"""Video -> phash-diverse frame extraction.

Decodes a short video clip via imageio (backed by imageio-ffmpeg's bundled
static ffmpeg binary -- no system ffmpeg / apt dependency needed, works in
the slim container too), subsamples to roughly `target_fps`, runs each
sampled frame through the existing `process_photo` pipeline, and greedily
keeps a visually diverse subset by perceptual-hash hamming distance.

The raw video bytes are never persisted -- only the resulting frame
`ProcessedPhoto`s are returned; callers store those as ordinary sighting
photos and discard the video.
"""

import os
import tempfile

import imageio.v2 as imageio
import imagehash
from PIL import Image
import io

from app.photos import process_photo, ProcessedPhoto


def extract_diverse_frames(
    raw_video: bytes,
    *,
    target_fps: float = 2.0,  # sample ~2 frames/sec
    max_raw: int = 20,  # never decode more than this many sampled frames
    keep: int = 12,  # final cap of diverse frames
    phash_hamming_min: int = 8,  # keep a frame only if >= this hamming distance from all kept frames
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
