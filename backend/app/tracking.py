"""Capture-local evidence, never cross-capture identity or automatic merging.

No street-video association threshold has been calibrated, so no score ever
joins two detections. What joins them is co-visibility: within one capture, a
species that never shows two animals in the same frame is taken to be one
animal, and all its detections form one track. Only a species that does show
two together, and also appears in another frame, is ambiguous -- which of the
two is which in the other frames is a question a person must answer. Those keep
singleton tracklets until a contributor groups instance IDs, and motion and
appearance only rank review suggestions; even a perfect cosine cannot publish
a merge.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
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
    tracking_policy: str = "covisible-review-v2"


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


def covisibility(frames: Iterable[Iterable[str]]) -> tuple[set[str], set[str]]:
    """(co-visible, ambiguous) species, from the species of each frame's animals.

    Co-visible: some frame shows two or more of it. Ambiguous: co-visible AND
    seen in more than one frame. Two dogs in a single frame are two animals by
    construction and the review UI cannot merge them, so there is nothing to
    ask. The question only exists once they reappear elsewhere and could be
    either one. Shared with the re-check of held captures
    (`captures.recheck_held`), which has only the stored species to go on.
    """
    covisible, seen_in = set(), {}
    for species_in_frame in frames:
        counts: dict[str, int] = {}
        for species in species_in_frame:
            counts[species] = counts.get(species, 0) + 1
        for species, count in counts.items():
            seen_in[species] = seen_in.get(species, 0) + 1
            if count > 1:
                covisible.add(species)
    return covisible, {species for species in covisible if seen_in[species] > 1}


def associate_frames(frames: tuple[FrameAnalysis, ...], coverage: SamplingCoverage,
                     *, max_instances: int = 96, max_evidence_per_track: int = 12) -> CaptureAnalysis:
    """Bounded, deterministic tracks and review proposals without averaging any vectors.

    Species is a hard constraint. A species is ambiguous only when some frame
    shows two or more of it AND it appears in more than one frame; any
    ambiguous species sends the whole capture to review. Candidate ranking for
    those uses normalized source-plane displacement and appearance, and a gap
    (including an empty sampled frame), competing animals, or missing
    embeddings gets its own review reason.
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
    covisible, ambiguous = covisibility(
        [i.detection.species for i in frame.instances] for frame in frames)
    associations = []
    previous: dict[str, tuple[int, FrameAnalysis, tuple[AnimalInstance, ...]]] = {}
    for ordinal, frame in enumerate(frames):
        for species in ("dog", "cat"):
            current = tuple(i for i in frame.instances if i.detection.species == species)
            if not current or species not in ambiguous:
                continue
            if species in previous:
                old_ordinal, old_frame, candidates = previous[species]
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
    # Co-visible species stay singletons. For an ambiguous one that is not an
    # assertion that each tracklet is a unique animal, and the publication layer
    # must honor requires_review. Every other species is one track named after
    # its first detection, so the ID stays stable and within the wire pattern.
    members: dict[str, tuple[str, list[str]]] = {}
    first_of: dict[str, str] = {}
    for i in instances:
        species = i.detection.species
        track_id = f"track:{i.instance_id}"
        if species not in covisible:
            track_id = first_of.setdefault(species, track_id)
        members.setdefault(track_id, (species, []))[1].append(i.instance_id)
    tracks = tuple(Tracklet(track_id, species, tuple(ids), tuple(ids[:max_evidence_per_track]),
                            species in ambiguous) for track_id, (species, ids) in members.items())
    return CaptureAnalysis(frames, tracks, tuple(associations), coverage, bool(ambiguous))
