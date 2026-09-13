"""Serial, fail-closed CUDA inference with the API's exact pixel preprocessing.

Construct with ``GpuInference.from_paths(detector, embedder)``. ``analyse_photo``
accepts already-stored bytes (never re-encodes); ``process_photo`` is for raw
uploads; ``extract_clip`` returns processed frames for later serial inference.
There is no DB, object-store, queue, retry, or application-session dependency.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime as ort
from PIL import Image

from app import analyse as reference_analysis
from app import detect, detect_reid, embed, photos, video

CUDA = "CUDAExecutionProvider"


class CudaUnavailable(RuntimeError):
    """CUDA was unavailable, failed initialization, or disappeared at runtime."""


class WorkerBusy(RuntimeError):
    """This adapter already owns an item; the caller must apply backpressure."""


class Session(Protocol):
    def get_providers(self): ...
    def disable_fallback(self): ...
    def get_inputs(self): ...
    def run(self, outputs, feeds): ...


@dataclass(frozen=True)
class Limits:
    max_photo_bytes: int = 32 * 1024 * 1024
    max_pixels: int = 24_000_000
    max_clip_bytes: int = 100 * 1024 * 1024
    clip_timeout_seconds: float = 90.0
    max_raw: int = 20
    keep: int = 12
    max_decoded_frames: int = 3600
    max_result_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            if name != "clip_timeout_seconds" and not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if not self.keep <= self.max_raw <= 20 or self.keep > 12:
            raise ValueError("frame caps must satisfy keep <= max_raw <= 20; keep <= 12")


@dataclass(frozen=True)
class Identity:
    detector_sha256: str
    embedder_sha256: str
    preprocessing: str
    embedding_model: str = embed.MODEL_NAME

    def __post_init__(self):
        for value in (self.detector_sha256, self.embedder_sha256):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("model identity must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class PhotoInference:
    dog_confidence: float
    cat_confidence: float
    bbox: tuple[int, int, int, int] | None
    vector: np.ndarray | None
    identity: Identity


def preprocessing_identity() -> str:
    """Recipe version plus source digest, including box selection and codecs."""
    source = "\n".join(inspect.getsource(m) for m in (
        detect, detect_reid, embed, photos, video, reference_analysis,
    )) + inspect.getsource(GpuInference)
    return "stored-webp90-yolo640-miew440-imagenet-l2-v1:" + hashlib.sha256(
        source.encode()
    ).hexdigest()


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _require_cuda(session: Session) -> None:
    if CUDA not in session.get_providers():
        raise CudaUnavailable("CUDAExecutionProvider is required; CPU-only sessions are forbidden")


def create_cuda_session(path: Path, *, threads: int = 1) -> Session:
    """Allow CPU helper nodes, never a CPU-only session or run-time EP retry.

    ORT may silently recover constructor failures with a CPU session, hence
    provider verification is required even after explicitly requesting CUDA.
    """
    if CUDA not in ort.get_available_providers():
        raise CudaUnavailable("install the DGX CUDA runtime; CUDAExecutionProvider is absent")
    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, min(int(threads), 2))
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    # H200 TF32 failed YOLO CPU parity; use full FP32 for the existing model recipe.
    session = ort.InferenceSession(
        str(path), sess_options=options, providers=[(CUDA, {"use_tf32": "0"})]
    )
    session.disable_fallback()
    _require_cuda(session)
    return session


class GpuInference:
    """Own one pair of sessions, one in-flight item, and bounded media input.

    Injected sessions must already have bounded thread settings and accurate
    model identities. Production callers should use ``from_paths`` instead.
    A process supervisor is still needed for native GPU hangs and memory limits.
    """

    def __init__(self, detector: Session, embedder: Session, identity: Identity,
                 *, limits: Limits | None = None):
        for session in (detector, embedder):
            session.disable_fallback()
            _require_cuda(session)
        self._detector, self._embedder = detector, embedder
        self.identity = identity
        self.limits = limits or Limits()
        self._lock = threading.Lock()

    @classmethod
    def from_paths(cls, detector: str | Path, embedder: str | Path,
                   *, threads: int = 1, limits: Limits | None = None):
        detector, embedder = Path(detector), Path(embedder)
        identity = Identity(_sha256(detector), _sha256(embedder), preprocessing_identity())
        return cls(create_cuda_session(detector, threads=threads),
                   create_cuda_session(embedder, threads=threads), identity, limits=limits)

    def _claim(self):
        if not self._lock.acquire(blocking=False):
            raise WorkerBusy("one media item at a time")

    @staticmethod
    def _validate_bytes(raw: bytes, maximum: int):
        if not isinstance(raw, bytes) or not raw or len(raw) > maximum:
            raise ValueError("media must be nonempty bytes within the configured byte limit")

    def _validate_photo(self, raw: bytes):
        self._validate_bytes(raw, self.limits.max_photo_bytes)
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > self.limits.max_pixels:
                raise ValueError("photo exceeds pixel limit")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("animated images must not be treated as a single photo")

    @staticmethod
    def _run(session: Session, batch: np.ndarray):
        _require_cuda(session)
        output = session.run(None, {session.get_inputs()[0].name: batch})
        _require_cuda(session)
        if not output:
            raise ValueError("model returned no outputs")
        return np.asarray(output[0])

    def process_photo(self, raw: bytes) -> photos.ProcessedPhoto:
        """Use the existing EXIF-strip/WebP q90/thumbnail/phash recipe unchanged."""
        self._claim()
        try:
            self._validate_photo(raw)
            return photos.process_photo(raw)
        finally:
            self._lock.release()

    def analyse_photo(self, stored_photo: bytes) -> PhotoInference:
        """Infer on stored pixels once. A missing animal has bbox/vector None."""
        self._claim()
        try:
            self._validate_photo(stored_photo)
            image = detect.load_upright(stored_photo)
            batch, scale, pad_x, pad_y = detect_reid._letterbox(image)
            output = self._run(self._detector, batch)
            if output.ndim != 3 or output.shape[0] != 1 or output.shape[2] != 6:
                raise ValueError("expected NMS detector output (1, N, 6)")
            if output.shape[1] > 300 or not np.isfinite(output).all():
                raise ValueError("invalid or excessive detector output")
            dog = cat = 0.0
            best, best_area = None, 0.0
            for x1, y1, x2, y2, conf, cls in output[0]:
                confidence, species = float(conf), int(cls)
                if not 0 <= confidence <= 1 or species != cls or not 0 <= species < 80:
                    raise ValueError("invalid detector confidence or class")
                if species == detect_reid.COCO_DOG:
                    dog = max(dog, confidence)
                elif species == detect_reid.COCO_CAT:
                    cat = max(cat, confidence)
                if confidence < detect_reid.REID_CONF_THRESHOLD or species not in (15, 16):
                    continue
                ox1, oy1 = (float(x1) - pad_x) / scale, (float(y1) - pad_y) / scale
                ox2, oy2 = (float(x2) - pad_x) / scale, (float(y2) - pad_y) / scale
                area = max(0.0, ox2 - ox1) * max(0.0, oy2 - oy1)
                if area > best_area:
                    best_area, best = area, (ox1, oy1, ox2, oy2)
            box = vector = None
            if best is not None:
                x1, y1, x2, y2 = best
                mx, my = detect_reid.BOX_MARGIN * (x2 - x1), detect_reid.BOX_MARGIN * (y2 - y1)
                left, top = max(0, int(x1 - mx)), max(0, int(y1 - my))
                right, bottom = min(image.width, int(x2 + mx)), min(image.height, int(y2 + my))
                if right > left and bottom > top:
                    box = (left, top, right, bottom)
                    crop = image.crop(box)
                    resize_scale = embed._INPUT / min(crop.size)
                    resized_pixels = max(embed._INPUT, round(crop.width * resize_scale)) * max(
                        embed._INPUT, round(crop.height * resize_scale)
                    )
                    if resized_pixels > self.limits.max_pixels:
                        raise ValueError("embedding resize exceeds pixel limit")
                    output = self._run(self._embedder, embed.preprocess(crop))
                    if output.shape != (1, embed.EMBED_DIM):
                        raise ValueError("expected embedding shape (1, 2152)")
                    vector = output[0].astype(np.float32)
                    norm = np.linalg.norm(vector)
                    if not np.isfinite(vector).all() or not np.isfinite(norm) or norm <= 1e-8:
                        raise ValueError("embedding must be finite and nonzero")
                    vector = vector / (norm + 1e-8)
                    if not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1, atol=1e-5):
                        raise ValueError("embedding failed normalization")
            return PhotoInference(dog, cat, box, vector, self.identity)
        finally:
            self._lock.release()

    def extract_clip(self, raw: bytes) -> list[photos.ProcessedPhoto]:
        """Decode on this worker's CPU, with no GPU session in the subprocess."""
        from gpu_worker.clip import extract_bounded

        self._claim()
        try:
            self._validate_bytes(raw, self.limits.max_clip_bytes)
            return extract_bounded(raw, self.limits)
        finally:
            self._lock.release()
