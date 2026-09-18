"""Capture-local evidence, never cross-capture identity or automatic merging.

No street-video association threshold has been calibrated. Each detection is
therefore a tracklet until a contributor groups instance IDs. Motion/appearance
rank review suggestions only; even a perfect cosine cannot publish a merge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import math

import numpy as np

from app.detect_reid import AnimalDetection


@dataclass(frozen=True)
class AnimalInstance:
    instance_id: str
    source_id: str
    frame_index: int
    timestamp_seconds: float | None
    detection: AnimalDetection
    vector: np.ndarray | None
    embedding_error: Literal["embedding_failed"] | None = None


@dataclass(frozen=True)
class FrameAnalysis:
    source_id: str
    frame_index: int
    timestamp_seconds: float | None
    width: int
    height: int
    dog_confidence: float
    cat_confidence: float
    instances: tuple[AnimalInstance, ...]


@dataclass(frozen=True)
class Association:
    from_instance_id: str
    to_instance_id: str
    motion_distance: float
    appearance_similarity: float | None
    reason: Literal["uncalibrated", "crossing_or_competing", "gap_or_reentry", "embedding_unavailable"]
    requires_review: bool = True


@dataclass(frozen=True)
class Tracklet:
    track_id: str
    species: Literal["dog", "cat"]
    instance_ids: tuple[str, ...]
    evidence_instance_ids: tuple[str, ...]
    requires_review: bool


@dataclass(frozen=True)
class SamplingCoverage:
    media_kind: Literal["photos", "video"]
    sampled_frames: int
    decoded_frames: int
    duration_seconds: float | None
    first_timestamp_seconds: float | None
    last_timestamp_seconds: float | None
    max_sampled_frames: int
    max_decoded_frames: int
    reached_end: bool
    # imageio's FFmpeg pipe has no per-frame PTS. Do not mislabel index/fps as
    # presentation timestamps for variable-frame-rate source media.
    timestamp_basis: Literal["not_applicable", "nominal_fps"] = "not_applicable"
    exhaustive: bool = False


@dataclass(frozen=True)
class CaptureAnalysis:
    frames: tuple[FrameAnalysis, ...]
    tracks: tuple[Tracklet, ...]
    associations: tuple[Association, ...]
    coverage: SamplingCoverage
    requires_review: bool
    schema_version: int = 2
    tracking_policy: str = "uncalibrated-tracklets-v1"


def make_frame(source_id: str, frame_index: int, timestamp_seconds: float | None,
               width: int, height: int, dog: float, cat: float,
               evidence: list[tuple[AnimalDetection, np.ndarray | None, str | None]]) -> FrameAnalysis:
    if not source_id or frame_index < 0 or width <= 0 or height <= 0:
        raise ValueError("invalid frame identity or dimensions")
    if timestamp_seconds is not None and (not math.isfinite(timestamp_seconds) or timestamp_seconds < 0):
        raise ValueError("invalid frame timestamp")
    instances = tuple(AnimalInstance(
        f"{source_id}:{detection.detection_index}", source_id, frame_index,
        timestamp_seconds, detection, vector, error,
    ) for detection, vector, error in evidence)
    return FrameAnalysis(source_id, frame_index, timestamp_seconds, width, height, dog, cat, instances)


def _centre(instance: AnimalInstance, frame: FrameAnalysis) -> np.ndarray:
    x1, y1, x2, y2 = instance.detection.raw_box
    return np.array([(x1 + x2) / (2 * frame.width), (y1 + y2) / (2 * frame.height)])


def associate_frames(frames: tuple[FrameAnalysis, ...], coverage: SamplingCoverage,
                     *, max_instances: int = 96, max_evidence_per_track: int = 12) -> CaptureAnalysis:
    """Bounded, deterministic review proposals without averaging any vectors.

    Candidate ranking uses normalized source-plane displacement and appearance;
    species is a hard constraint. A gap (including an empty sampled frame),
    competing animals, or missing embeddings gets its own review reason. All
    uncalibrated links remain private, including apparent single-animal bursts.
    """
    if max_instances < 1 or max_evidence_per_track < 1:
        raise ValueError("invalid tracking budget")
    if len(frames) != coverage.sampled_frames or len(frames) > coverage.max_sampled_frames:
        raise ValueError("frame count disagrees with coverage")
    if len({frame.source_id for frame in frames}) != len(frames):
        raise ValueError("duplicate source frame")
    if coverage.media_kind == "video":
        times = [frame.timestamp_seconds for frame in frames]
        if any(t is None for t in times) or any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("video evidence must be chronological")
    instances = [instance for frame in frames for instance in frame.instances]
    if len(instances) > max_instances:
        raise ValueError("capture exceeds instance budget; no partial animal result")
    if len({instance.instance_id for instance in instances}) != len(instances):
        raise ValueError("duplicate instance identity")
    associations = []
    previous: dict[str, tuple[int, FrameAnalysis, tuple[AnimalInstance, ...]]] = {}
    review_ids = set()
    for ordinal, frame in enumerate(frames):
        for species in ("dog", "cat"):
            current = tuple(i for i in frame.instances if i.detection.species == species)
            if not current:
                continue
            if species in previous:
                old_ordinal, old_frame, candidates = previous[species]
                # All same-species observations across frames are potentially
                # repeated views, including candidates omitted from the top-3 UI.
                review_ids.update(i.instance_id for i in (*candidates, *current))
                for instance in current:
                    ranked = []
                    for candidate in candidates:
                        motion = float(np.linalg.norm(_centre(instance, frame) - _centre(candidate, old_frame)))
                        similarity = None
                        if instance.vector is not None and candidate.vector is not None:
                            similarity = float(np.clip(np.dot(instance.vector, candidate.vector), -1, 1))
                        reason = "uncalibrated"
                        if old_ordinal != ordinal - 1:
                            reason = "gap_or_reentry"
                        elif len(current) > 1 or len(candidates) > 1:
                            reason = "crossing_or_competing"
                        elif similarity is None:
                            reason = "embedding_unavailable"
                        ranked.append((motion + (1 - similarity if similarity is not None else 1),
                                       candidate.instance_id, Association(candidate.instance_id,
                                           instance.instance_id, motion, similarity, reason)))
                    associations.extend(item[2] for item in sorted(ranked, key=lambda item: item[:2])[:3])
            previous[species] = ordinal, frame, current
    # Singleton evidence is intentional, not an assertion that each tracklet is
    # a unique animal. The publication layer must honor requires_review.
    tracks = tuple(Tracklet(f"track:{i.instance_id}", i.detection.species,
                           (i.instance_id,), (i.instance_id,)[:max_evidence_per_track],
                           i.instance_id in review_ids) for i in instances)
    return CaptureAnalysis(frames, tracks, tuple(associations), coverage, bool(review_ids))
