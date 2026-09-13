"""Bound existing frame selection inside a disposable, CPU-only subprocess.

The decoder iterator guard is local to the child: app.video's global reader
and thread/environment settings in the API process are never changed.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict

from app.photos import ProcessedPhoto


def extract_bounded(raw, limits) -> list[ProcessedPhoto]:
    with tempfile.TemporaryDirectory(prefix="dgx-frames-") as directory:
        root = Path(directory)
        (root / "input.clip").write_bytes(raw)
        env = os.environ.copy()
        env.update({name: "1" for name in (
            "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
        )})
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["TMPDIR"] = directory
        env["TEMP"] = directory
        env["TMP"] = directory
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        # The same session group contains ffmpeg; killing only Python can leave
        # a timed-out decoder running after its media lease has expired.
        process = subprocess.Popen(
            [sys.executable, "-m", "gpu_worker.clip", directory, json.dumps(asdict(limits))],
            env=env, start_new_session=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            code = process.wait(timeout=limits.clip_timeout_seconds)
            if code:
                raise ValueError("clip decoding failed or exceeded media limits")
        except subprocess.TimeoutExpired as exc:
            raise ValueError("clip decoding exceeded time limit") from exc
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        metadata = json.loads((root / "frames.json").read_text())
        if not 1 <= len(metadata) <= limits.keep:
            raise ValueError("invalid frame count")
        frames = []
        total = 0
        for index, item in enumerate(metadata):
            original_path, thumb_path = root / f"{index}.webp", root / f"{index}.thumb.webp"
            total += original_path.stat().st_size + thumb_path.stat().st_size
            if total > limits.max_result_bytes:
                raise ValueError("processed clip exceeds result byte limit")
            frames.append(ProcessedPhoto(original_path.read_bytes(), thumb_path.read_bytes(), **item))
        return frames


def _extract_child(directory: str, limits: dict):
    from app import video

    original_reader = video.imageio.get_reader

    class BoundedReader:
        def __init__(self, reader):
            self.reader = reader

        def get_meta_data(self):
            metadata = self.reader.get_meta_data()
            width, height = metadata.get("size", (0, 0))
            if width * height > limits["max_pixels"]:
                raise ValueError("video exceeds pixel limit")
            return metadata

        def __iter__(self):
            for frame in itertools.islice(self.reader, limits["max_decoded_frames"]):
                if frame.shape[0] * frame.shape[1] > limits["max_pixels"]:
                    raise ValueError("video frame exceeds pixel limit")
                yield frame

        def close(self):
            self.reader.close()

    def bounded_reader(*args, **kwargs):
        return BoundedReader(original_reader(
            *args, **kwargs, input_params=["-threads", "1"], output_params=["-threads", "1"],
        ))

    video.imageio.get_reader = bounded_reader
    root = Path(directory)
    frames = video.extract_diverse_frames(
        (root / "input.clip").read_bytes(), max_raw=limits["max_raw"], keep=limits["keep"],
    )
    metadata = []
    total = 0
    for index, frame in enumerate(frames):
        total += len(frame.original) + len(frame.thumbnail)
        if total > limits["max_result_bytes"]:
            raise ValueError("processed clip exceeds result byte limit")
        (root / f"{index}.webp").write_bytes(frame.original)
        (root / f"{index}.thumb.webp").write_bytes(frame.thumbnail)
        metadata.append({key: getattr(frame, key) for key in (
            "width", "height", "phash", "content_type",
        )})
    (root / "frames.json").write_text(json.dumps(metadata))


if __name__ == "__main__":
    _extract_child(sys.argv[1], json.loads(sys.argv[2]))
