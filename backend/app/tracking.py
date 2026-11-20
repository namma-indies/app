"""Which boxes in one capture are the same animal, and how sure we are.

POLICY (Akash, 2026-11-19, reversing the 2026-09-15 rule of issue #76 / PR #87)
-------------------------------------------------------------------------------
A grouping the model is confident about publishes on its own; the person is
asked -- one tap, with the best grouping already filled in -- only where it is
not. Until this date the rule was the opposite: no model score could ever
publish a merge, and any capture where one species showed two animals in a
frame and reappeared went to the person with every box its own animal. That
made multi-animal clips a chore, so the owner chose to trust confident links.
What did not change: two boxes in the SAME frame are never the same animal,
and nothing here ever joins boxes across captures (that is app/matching.py,
whose auto-merge stays unreachable).

HOW
---
Frames are visited in time order (clips) or upload order (photo bursts). Each
box is scored against every animal (track) built so far:

* appearance -- the best MiewID cosine to that animal's recent boxes, mapped
  linearly from `cosine_floor`..`cosine_ceiling` onto 0..1;
* motion (clips only, gaps up to `motion_horizon_s`) -- the centre shift
  between the two boxes in box-sizes per second, `exp(-speed/motion_speed_scale)`,
  or the boxes' IoU when that is higher;
* combined -- `appearance_weight` x appearance + the rest x motion; appearance
  alone for photos; motion alone (x0.5, so never confident) without vectors;
  `species_mismatch_penalty` off when the two boxes carry different labels.

Boxes and animals are paired one-to-one per frame, greedily by score (highest
first, never two boxes of one frame to one animal); a box whose best score is
below `accept_min` starts a new animal. Every decision gets a confidence:

* a link is confident when its score >= `confident_min` AND it beats the
  runner-up -- another animal for this box, another box for this animal, or
  "a new animal" at `accept_min` -- by `margin_min`;
* starting a new animal is confident when no earlier animal scored above
  `new_animal_max` and none was unmeasurable.

Species is a confidence-weighted vote over the animal's boxes (per-crop dog
and cat scores where the detector reported both); a winning share below
`species_share_min` marks it uncertain. A capture publishes without review
only when every decision is confident and no animal's species is uncertain.

The defaults are deliberately conservative and NOT calibrated on street
footage: they are chosen so that clearly distinct animals with clearly
different appearance publish, and anything resembling a look-alike, a crossing
of similar animals, a missing embedding or a dog/cat toss-up asks the person.
Recalibrate on staging clips (see GroupingPolicy).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from collections.abc import Iterable, Mapping
from typing import Literal
import math

import numpy as np

from app.detect_reid import AnimalDetection
from app.scene import SceneSignals

TRACKING_POLICY = "motion-appearance-confidence-v3"

Reason = Literal["low_score", "close_alternative", "embedding_unavailable",
                 "maybe_seen_before", "species_uncertain"]


@dataclass(frozen=True)
class GroupingPolicy:
    """Thresholds for linking boxes into animals. Mirrored by `settings.grouping_*`.

    The API sends its values with every capture job, so they are tuned in one
    place (app/config.py) without redeploying the worker.
    """
    accept_min: float = 0.35         # below this a box is never linked: it starts a new animal
    confident_min: float = 0.65      # a link this strong (with the margin) needs no review
    margin_min: float = 0.20         # ...and must beat its runner-up by this much
    new_animal_max: float = 0.20     # a new animal is certain only if every earlier one scored below this
    cosine_floor: float = 0.20       # MiewID cosine that counts as "no resemblance"
    cosine_ceiling: float = 0.80     # ...and as "certainly the same look"
    appearance_weight: float = 0.75  # appearance vs motion in the combined score
    motion_speed_scale: float = 2.0  # box-sizes per second at which motion scores 1/e
    motion_horizon_s: float = 3.0    # beyond this gap motion says nothing (left and came back)
    species_mismatch_penalty: float = 0.15
    species_share_min: float = 0.70  # winning share of the species vote below this is "uncertain"

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{f.name} must be a finite number")
        unit = ("accept_min", "confident_min", "margin_min", "new_animal_max", "cosine_floor",
                "cosine_ceiling", "appearance_weight", "species_mismatch_penalty")
        if any(not 0 <= getattr(self, name) <= 1 for name in unit):
            raise ValueError("grouping thresholds must lie in [0, 1]")
        if not self.new_animal_max <= self.accept_min <= self.confident_min:
            raise ValueError("need new_animal_max <= accept_min <= confident_min")
        if not -1 <= self.cosine_floor < self.cosine_ceiling <= 1:
            raise ValueError("need cosine_floor < cosine_ceiling")
        if self.motion_speed_scale <= 0 or self.motion_horizon_s <= 0:
            raise ValueError("motion scales must be positive")
        if not 0.5 <= self.species_share_min <= 1:
            raise ValueError("species_share_min must lie in [0.5, 1]")

    @classmethod
    def from_mapping(cls, data: Mapping | None) -> "GroupingPolicy":
        """From the claim payload; unknown keys (a newer API) are ignored."""
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: float(v) for k, v in data.items() if k in known})

    def as_mapping(self) -> dict:
        return asdict(self)


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
    # People and indoor furniture, for screening (#181, app/scene.py). None
    # when the analyser did not measure them.
    scene: "SceneSignals | None" = None


@dataclass(frozen=True)
class Association:
    """One decision: box `to_instance_id` joins the animal whose latest box is
    `from_instance_id` (a link), or starts animal `track_id` (from None)."""
    from_instance_id: str | None
    to_instance_id: str
    track_id: str
    score: float | None
    appearance_similarity: float | None
    motion_score: float | None
    margin: float | None
    confident: bool
    confidence: float
    reasons: tuple[Reason, ...] = ()

    @property
    def requires_review(self) -> bool:
        return not self.confident


@dataclass(frozen=True)
class Tracklet:
    track_id: str
    species: Literal["dog", "cat"]
    instance_ids: tuple[str, ...]
    evidence_instance_ids: tuple[str, ...]
    requires_review: bool
    confidence: float = 1.0
    species_uncertain: bool = False
    species_share: float = 1.0
    representative_instance_id: str | None = None
    reasons: tuple[Reason, ...] = ()

    @property
    def confident(self) -> bool:
        return not self.requires_review


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
    # Evenly spaced candidates decoded before the diversity filter kept
    # `sampled_frames` of them (app/video.py select_diverse). None: no filter.
    candidate_frames: int | None = None


@dataclass(frozen=True)
class CaptureAnalysis:
    frames: tuple[FrameAnalysis, ...]
    tracks: tuple[Tracklet, ...]
    associations: tuple[Association, ...]
    coverage: SamplingCoverage
    requires_review: bool
    schema_version: int = 3
    tracking_policy: str = TRACKING_POLICY
    # Boxes left out because the capture had more than the instance budget;
    # each animal kept its best views (`trim_to_budget`).
    trimmed_instance_ids: tuple[str, ...] = ()


def make_frame(source_id: str, frame_index: int, timestamp_seconds: float | None,
               width: int, height: int, dog: float, cat: float,
               evidence: list[tuple[AnimalDetection, np.ndarray | None, str | None]],
               scene: "SceneSignals | None" = None) -> FrameAnalysis:
    if not source_id or frame_index < 0 or width <= 0 or height <= 0:
        raise ValueError("invalid frame identity or dimensions")
    if timestamp_seconds is not None and (not math.isfinite(timestamp_seconds) or timestamp_seconds < 0):
        raise ValueError("invalid frame timestamp")
    instances = tuple(AnimalInstance(
        f"{source_id}:{detection.detection_index}", source_id, frame_index,
        timestamp_seconds, detection, vector, error,
    ) for detection, vector, error in evidence)
    return FrameAnalysis(source_id, frame_index, timestamp_seconds, width, height, dog, cat, instances, scene)


def covisibility(frames: Iterable[Iterable[str]]) -> tuple[set[str], set[str]]:
    """(co-visible, ambiguous) species, from the species of each frame's animals.

    The pre-2026-11-19 rule, kept only for re-checking captures held by a
    worker that predates confidence (`captures.recheck_held`), which has only
    the stored species to go on. Co-visible: some frame shows two or more of
    it. Ambiguous: co-visible AND seen in more than one frame.
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


def box_area(instance: AnimalInstance) -> float:
    x1, y1, x2, y2 = instance.detection.raw_box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def view_quality(instance: AnimalInstance, frame_area: float = 1.0) -> float:
    """How good a view this box is: detector confidence x share of the frame."""
    return instance.detection.confidence * box_area(instance) / max(frame_area, 1.0)


def _iou(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def species_vote(detections: Iterable[AnimalDetection], policy: GroupingPolicy = GroupingPolicy()
                 ) -> tuple[Literal["dog", "cat"], float, bool]:
    """(species, winning share, uncertain) for one animal's boxes.

    Each box adds its per-crop dog and cat scores when the detector reported
    them, else its confidence to its own label. Ties go to the label of the
    single most confident box.
    """
    dog = cat = 0.0
    best = None
    for d in detections:
        if d.dog_score is not None or d.cat_score is not None:
            dog += d.dog_score or 0.0
            cat += d.cat_score or 0.0
        elif d.species == "dog":
            dog += d.confidence
        else:
            cat += d.confidence
        if best is None or d.confidence > best.confidence:
            best = d
    total = dog + cat
    if best is None:
        raise ValueError("an animal needs at least one box")
    if total <= 0:
        return best.species, 0.5, True
    species = "dog" if dog > cat else "cat" if cat > dog else best.species
    share = max(dog, cat) / total
    return species, share, share < policy.species_share_min


class _Track:
    def __init__(self, track_id: str, first: AnimalInstance, frame: FrameAnalysis, start: Association):
        self.track_id = track_id
        self.members: list[tuple[AnimalInstance, FrameAnalysis]] = [(first, frame)]
        self.decisions: list[Association] = [start]

    @property
    def last(self) -> tuple[AnimalInstance, FrameAnalysis]:
        return self.members[-1]


def _appearance(instance: AnimalInstance, track: _Track, policy: GroupingPolicy) -> tuple[float | None, float | None]:
    """(best cosine, mapped 0..1 score) against the animal's last eight measured views."""
    if instance.vector is None:
        return None, None
    vectors = [m.vector for m, _ in track.members if m.vector is not None][-8:]
    if not vectors:
        return None, None
    cosine = max(float(np.clip(np.dot(instance.vector, v), -1, 1)) for v in vectors)
    span = policy.cosine_ceiling - policy.cosine_floor
    return cosine, float(np.clip((cosine - policy.cosine_floor) / span, 0, 1))


def _motion(instance: AnimalInstance, frame: FrameAnalysis, track: _Track, policy: GroupingPolicy) -> float | None:
    previous, old_frame = track.last
    if frame.timestamp_seconds is None or old_frame.timestamp_seconds is None:
        return None
    dt = frame.timestamp_seconds - old_frame.timestamp_seconds
    if dt <= 0 or dt > policy.motion_horizon_s:
        return None
    a, b = previous.detection.raw_box, instance.detection.raw_box
    # Normalise to each frame's own pixels (a rotated clip keeps its meaning).
    sa = (old_frame.width, old_frame.height)
    sb = (frame.width, frame.height)
    na = (a[0] / sa[0], a[1] / sa[1], a[2] / sa[0], a[3] / sa[1])
    nb = (b[0] / sb[0], b[1] / sb[1], b[2] / sb[0], b[3] / sb[1])
    ca = np.array([(na[0] + na[2]) / 2, (na[1] + na[3]) / 2])
    cb = np.array([(nb[0] + nb[2]) / 2, (nb[1] + nb[3]) / 2])
    size = math.sqrt(max(1e-6, ((na[2] - na[0]) * (na[3] - na[1]) + (nb[2] - nb[0]) * (nb[3] - nb[1])) / 2))
    speed = float(np.linalg.norm(cb - ca)) / size / max(dt, 0.25)
    return max(math.exp(-speed / policy.motion_speed_scale), _iou(na, nb))


def _score(instance, frame, track, policy):
    cosine, appearance = _appearance(instance, track, policy)
    motion = _motion(instance, frame, track, policy)
    if appearance is not None and motion is not None:
        score = policy.appearance_weight * appearance + (1 - policy.appearance_weight) * motion
    elif appearance is not None:
        score = appearance
    elif motion is not None:
        score = 0.5 * motion
    else:
        score = None
    if score is not None and track.last[0].detection.species != instance.detection.species:
        score = max(0.0, score - policy.species_mismatch_penalty)
    return score, cosine, motion


def link_frames(frames: tuple[FrameAnalysis, ...], policy: GroupingPolicy) -> tuple[list[_Track], list[Association]]:
    tracks: list[_Track] = []
    decisions: list[Association] = []
    for frame in frames:
        scored = {}  # (box ordinal, track ordinal) -> (score, cosine, motion)
        for d, instance in enumerate(frame.instances):
            for t, track in enumerate(tracks):
                scored[d, t] = _score(instance, frame, track, policy)
        pairs = sorted(((s[0], d, t) for (d, t), s in scored.items()
                        if s[0] is not None and s[0] >= policy.accept_min),
                       key=lambda item: (-item[0], item[1], item[2]))
        taken_boxes, taken_tracks, assigned = set(), set(), {}
        for score, d, t in pairs:
            if d in taken_boxes or t in taken_tracks:
                continue
            taken_boxes.add(d)
            taken_tracks.add(t)
            assigned[d] = t
        existing = len(tracks)  # animals started in this frame are not candidates
        for d, instance in enumerate(frame.instances):
            candidates = [(t, scored[d, t]) for t in range(existing)]
            if d in assigned:
                t = assigned[d]
                track = tracks[t]
                score, cosine, motion = scored[d, t]
                rivals = [s[0] for (other, s) in candidates if other != t and s[0] is not None]
                rivals += [scored[o, t][0] for o in range(len(frame.instances))
                           if o != d and scored[o, t][0] is not None]
                runner_up = max(rivals + [policy.accept_min])
                margin = score - runner_up
                reasons: list[Reason] = []
                if score < policy.confident_min:
                    reasons.append("low_score")
                if margin < policy.margin_min:
                    reasons.append("close_alternative")
                if cosine is None:
                    reasons.append("embedding_unavailable")
                decision = Association(track.last[0].instance_id, instance.instance_id, track.track_id,
                    score, cosine, motion, margin, not reasons,
                    float(np.clip(score - max(0.0, policy.margin_min - margin), 0, 1)), tuple(reasons))
                track.members.append((instance, frame))
                track.decisions.append(decision)
            else:
                known = [s[0] for _, s in candidates if s[0] is not None]
                unknown = any(s[0] is None for _, s in candidates)
                best = max(known, default=0.0)
                reasons = []
                if best > policy.new_animal_max:
                    reasons.append("maybe_seen_before")
                if unknown:
                    reasons.append("embedding_unavailable")
                track_id = f"track:{instance.instance_id}"
                decision = Association(None, instance.instance_id, track_id,
                    best if candidates else None, None, None, None, not reasons,
                    0.5 if unknown else float(np.clip(1 - best, 0, 1)), tuple(reasons))
                tracks.append(_Track(track_id, instance, frame, decision))
            decisions.append(decision)
    return tracks, decisions


def trim_to_budget(tracks: list[_Track], max_instances: int) -> set[str]:
    """Instance ids to drop so the capture fits the budget, never a whole animal.

    Each animal keeps its best views (confidence x share of frame); the cap per
    animal is the largest that fits. Fails only when there are more animals
    than the budget allows boxes.
    """
    total = sum(len(t.members) for t in tracks)
    if total <= max_instances:
        return set()
    if len(tracks) > max_instances:
        raise ValueError("capture has more animals than the instance budget")
    cap = max(k for k in range(1, max_instances + 1)
              if sum(min(len(t.members), k) for t in tracks) <= max_instances)
    drop = set()
    for track in tracks:
        ranked = sorted(track.members, key=lambda m: (-view_quality(m[0], m[1].width * m[1].height),
                                                      m[0].instance_id))
        drop.update(m.instance_id for m, _ in ranked[cap:])
    return drop


def keep_frames_with_animals(analysis: CaptureAnalysis) -> CaptureAnalysis:
    """Only frames that still show an animal become review photos.

    A clip with no animal at all keeps its first analysed frame, so the
    upload still has a picture behind its no_animal outcome. Screening (#181)
    must not lose what the dropped frames saw: their person and indoor scores
    are folded, as maxima, into the first kept frame -- the clip as a whole
    showed them, and the clip itself is stored.
    """
    kept = [frame for frame in analysis.frames if frame.instances] or list(analysis.frames[:1])
    kept_ids = {frame.source_id for frame in kept}
    dropped = [frame.scene for frame in analysis.frames
               if frame.source_id not in kept_ids and frame.scene is not None]
    if dropped and kept:
        first = kept[0]
        scenes = dropped + ([first.scene] if first.scene is not None else [])
        kept[0] = replace(first, scene=SceneSignals(*(max(getattr(s, f.name) for s in scenes)
                                                      for f in fields(SceneSignals))))
    return replace(analysis, frames=tuple(kept))


def associate_frames(frames: tuple[FrameAnalysis, ...], coverage: SamplingCoverage,
                     *, max_instances: int = 96, max_evidence_per_track: int = 12,
                     policy: GroupingPolicy | None = None) -> CaptureAnalysis:
    """Group every box of a capture into animals, each with a confidence.

    Never fails for having too many boxes: past `max_instances` each animal
    keeps its best views and the rest are left out of the result (frames that
    lose every box keep no instances).
    """
    policy = policy or GroupingPolicy()
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
    if len({instance.instance_id for instance in instances}) != len(instances):
        raise ValueError("duplicate instance identity")
    tracks, decisions = link_frames(frames, policy)
    dropped = trim_to_budget(tracks, max_instances)
    if dropped:
        frames = tuple(replace(f, instances=tuple(i for i in f.instances if i.instance_id not in dropped))
                       for f in frames)
    result = []
    for track in tracks:
        members = [(m, f) for m, f in track.members if m.instance_id not in dropped]
        species, share, uncertain = species_vote([m.detection for m, _ in track.members], policy)
        reasons = tuple(dict.fromkeys(r for d in track.decisions for r in d.reasons))
        if uncertain:
            reasons += ("species_uncertain",)
        confident = all(d.confident for d in track.decisions) and not uncertain
        representative = max(members, key=lambda m: (view_quality(m[0], m[1].width * m[1].height),
                                                     m[0].instance_id))[0]
        ids = tuple(m.instance_id for m, _ in members)
        result.append(Tracklet(track.track_id, species, ids, ids[:max_evidence_per_track],
            not confident, min(d.confidence for d in track.decisions), uncertain, share,
            representative.instance_id, reasons))
    return CaptureAnalysis(frames, tuple(result), tuple(decisions), coverage,
                           any(t.requires_review for t in result),
                           trimmed_instance_ids=tuple(sorted(dropped)))
