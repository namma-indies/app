"""Bound existing frame selection inside a disposable, CPU-only subprocess.

The decoder iterator guard is local to the child: app.video's global reader
and thread/environment settings in the API process are never changed.

Credential-free environment and FFmpeg allowlists reduce exposure, but are NOT
an OS sandbox: the child still shares the worker UID, mounts and network namespace.
A native-code exploit can still read the token mount or open sockets. Provider
filesystem/network isolation is a separate deployment requirement.
"""

from __future__ import annotations

import itertools
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.photos import ProcessedPhoto
    from app.tracking import SamplingCoverage


# Self-contained clip containers only: no HLS, DASH, concat or image playlists.
# `file` is needed for seekable MP4 input; stdout's pipe is an OUTPUT protocol.
# MOV external data references remain disabled by FFmpeg's default; do not enable
# enable_drefs/use_absolute_path (MOV-only options fail on ordinary WebM input).
FFMPEG_INPUT_PARAMS = (
    "-threads", "1", "-protocol_whitelist", "file",
    "-format_whitelist", "mov,matroska,webm,avi,mpeg,mpegts,ogg,flv",
)


def _child_environment(directory: str) -> dict[str, str]:
    # Keep only loader paths required by provider Python/native CPU libraries.
    # In particular do not pass tokens, token paths, proxies, PYTHONHOME, preload
    # hooks or IMAGEIO_FFMPEG_EXE overrides into the untrusted-media process.
    env = {name: os.environ[name] for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")
           if name in os.environ}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.defpath
    env.update({name: "1" for name in (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
        "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE",
    )})
    env.update({name: directory for name in ("TMPDIR", "TEMP", "TMP", "HOME")})
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return env


def _limit_child(limits: dict) -> None:
    import resource

    # Set limits in the fresh interpreter, never preexec_fn in a threaded worker.
    # The address-space bound is Linux-only (macOS does not implement RLIMIT_AS
    # reliably). It covers FFmpeg allocations made before metadata can be checked.
    caps = {
        resource.RLIMIT_CORE: 0,
        resource.RLIMIT_CPU: max(1, math.ceil(limits["clip_timeout_seconds"])),
        resource.RLIMIT_FSIZE: max(limits["max_clip_bytes"], limits["max_result_bytes"], 1024 * 1024),
        resource.RLIMIT_NOFILE: 64,
    }
    if sys.platform == "linux":
        caps[resource.RLIMIT_AS] = 4 * 1024 ** 3
    for kind, cap in caps.items():
        _, hard = resource.getrlimit(kind)
        cap = cap if hard == resource.RLIM_INFINITY else min(cap, hard)
        resource.setrlimit(kind, (cap, cap))


def extract_bounded(raw, limits) -> list[ProcessedPhoto]:
    from app.photos import ProcessedPhoto

    if not isinstance(raw, bytes) or not 0 < len(raw) <= limits.max_clip_bytes:
        raise ValueError("invalid clip byte count")
    with tempfile.TemporaryDirectory(prefix="dgx-frames-") as directory:
        root = Path(directory)
        (root / "input.clip").write_bytes(raw)
        env = _child_environment(directory)
        # The same session group contains ffmpeg; killing only Python can leave
        # a timed-out decoder running after its media lease has expired.
        process = subprocess.Popen(
            [sys.executable, "-m", "gpu_worker.clip", directory, json.dumps(asdict(limits))],
            env=env, cwd=directory, start_new_session=True, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
            *args, **kwargs, input_params=list(FFMPEG_INPUT_PARAMS),
            output_params=["-threads", "1", "-frames:v", str(limits["max_decoded_frames"])],
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


@dataclass(frozen=True)
class SampledClip:
    root: Path
    metadata: tuple[dict, ...]
    coverage: SamplingCoverage

    def frames(self):
        from app.photos import ProcessedPhoto

        for ordinal, item in enumerate(self.metadata):
            photo = ProcessedPhoto((self.root / f"{ordinal}.webp").read_bytes(),
                (self.root / f"{ordinal}.thumb.webp").read_bytes(), **item["photo"])
            yield item["frame_index"], item["timestamp_seconds"], photo


@contextmanager
def sampled_bounded(raw: bytes, limits):
    """Versioned file protocol, retaining the hardened legacy child boundary."""
    from app.tracking import SamplingCoverage

    if not isinstance(raw, bytes) or not 0 < len(raw) <= limits.max_clip_bytes:
        raise ValueError("invalid clip byte count")
    with tempfile.TemporaryDirectory(prefix="dgx-tracking-") as directory:
        root = Path(directory)
        (root / "input.clip").write_bytes(raw)
        process = subprocess.Popen(
            [sys.executable, "-m", "gpu_worker.clip", directory, json.dumps(asdict(limits)), "v2"],
            env=_child_environment(directory), cwd=directory, start_new_session=True, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            if process.wait(timeout=limits.clip_timeout_seconds):
                raise ValueError("whole-video decoding failed or exceeded media limits")
        except subprocess.TimeoutExpired as exc:
            raise ValueError("clip decoding exceeded time limit") from exc
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        manifest = root / "frames.json"
        if manifest.stat().st_size > 256 * 1024:
            raise ValueError("excessive sample manifest")
        payload = json.loads(manifest.read_text())
        if payload.get("schema_version") != 2:
            raise ValueError("invalid sample protocol")
        metadata = payload["frames"]
        if not 1 <= len(metadata) <= limits.max_tracking_frames:
            raise ValueError("invalid sample count")
        coverage = SamplingCoverage(**payload["coverage"])
        if (coverage.media_kind != "video" or coverage.sampled_frames != len(metadata)
                or coverage.max_sampled_frames != limits.max_tracking_frames
                or coverage.max_decoded_frames != limits.max_decoded_frames
                or not 1 <= coverage.decoded_frames <= limits.max_decoded_frames
                or not coverage.reached_end or coverage.exhaustive
                or coverage.timestamp_basis != "nominal_fps"
                or coverage.duration_seconds is None
                or not math.isfinite(coverage.duration_seconds) or coverage.duration_seconds <= 0):
            raise ValueError("invalid sampling coverage")
        total = 0
        previous_index, previous_timestamp = -1, -1.0
        for ordinal, item in enumerate(metadata):
            index, timestamp = item["frame_index"], item["timestamp_seconds"]
            if (not isinstance(index, int) or isinstance(index, bool)
                    or not previous_index < index < coverage.decoded_frames
                    or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp)
                    or not previous_timestamp < timestamp):
                raise ValueError("non-chronological sample metadata")
            previous_index, previous_timestamp = index, timestamp
            photo = item["photo"]
            if (not 0 < photo["width"] * photo["height"] <= limits.max_pixels
                    or photo["content_type"] != "image/webp"):
                raise ValueError("invalid sampled image metadata")
            for suffix in ("webp", "thumb.webp"):
                path = root / f"{ordinal}.{suffix}"
                size = path.stat().st_size
                if path.is_symlink() or not 0 < size <= limits.max_photo_bytes:
                    raise ValueError("invalid sample file")
                total += size
            if total > limits.max_result_bytes:
                raise ValueError("processed clip exceeds result byte limit")
        if (metadata[0]["timestamp_seconds"] != coverage.first_timestamp_seconds
                or metadata[-1]["timestamp_seconds"] != coverage.last_timestamp_seconds
                or metadata[-1]["frame_index"] != coverage.decoded_frames - 1):
            raise ValueError("sample endpoints disagree with coverage")
        yield SampledClip(root, tuple(metadata), coverage)


def _sample_child(directory: str, limits: dict):
    from app import video

    root = Path(directory)
    metadata = []
    total = 0

    def emit(frame):
        nonlocal total
        photo = frame.photo
        total += len(photo.original) + len(photo.thumbnail)
        if len(metadata) >= limits["max_tracking_frames"] or total > limits["max_result_bytes"]:
            raise ValueError("processed clip exceeds result limits")
        if max(len(photo.original), len(photo.thumbnail)) > limits["max_photo_bytes"]:
            raise ValueError("sample exceeds photo byte limit")
        ordinal = len(metadata)
        (root / f"{ordinal}.webp").write_bytes(photo.original)
        (root / f"{ordinal}.thumb.webp").write_bytes(photo.thumbnail)
        metadata.append({"frame_index": frame.frame_index, "timestamp_seconds": frame.timestamp_seconds,
                         "photo": {key: getattr(photo, key) for key in
                                   ("width", "height", "phash", "content_type")}})

    # One sentinel frame beyond the budget detects dishonest duration metadata;
    # it is never inferred or stored, and FFmpeg cannot decode without bound.
    # imageio validates filename suffix before FFmpeg sniffs the container.
    # Renaming does not force MP4 demuxing; the input format allowlist still does.
    input_path = root / "input.mp4"
    (root / "input.clip").rename(input_path)
    reader = video.imageio.get_reader(str(input_path), format="ffmpeg",
        input_params=list(FFMPEG_INPUT_PARAMS),
        output_params=["-threads", "1", "-frames:v", str(limits["max_decoded_frames"] + 1)])
    try:
        coverage = video.sample_chronological(reader,
            max_samples=limits["max_tracking_frames"], max_decoded_frames=limits["max_decoded_frames"],
            max_pixels=limits["max_pixels"], emit=emit)
    finally:
        reader.close()
    (root / "frames.json").write_text(json.dumps({"schema_version": 2,
        "frames": metadata, "coverage": asdict(coverage)}, allow_nan=False))


if __name__ == "__main__":
    limits = json.loads(sys.argv[2])
    _limit_child(limits)
    if len(sys.argv) > 3 and sys.argv[3] == "v2":
        _sample_child(sys.argv[1], limits)
    else:
        _extract_child(sys.argv[1], limits)
