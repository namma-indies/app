"""V2 capture contracts. Source photos belong to a capture; evidence belongs to one animal.

POST /capture: legacy multipart fields except animal details, 1..12 photos or
one video (not both), 201 CaptureReceipt.
GET /captures: {items: CaptureStatus[]} (owner only, newest first).
GET /capture/{id}: CaptureStatus (owner only, works when intake is disabled).
POST /capture/{id}/review: CaptureReview -> CaptureStatus. A complete partition is
required. publish=false saves a private draft; publish=true publishes atomically.
After publication grouping is immutable, but the same groups can update details.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.media_jobs import EMBED_DIM, MODEL_NAME, Lease, WireModel

MULTI_PIPELINE_VERSION = "stored-webp-q90-yolo26x-miewid-msv3-multi-v2"
MAX_INSTANCES = 96
MAX_SOURCE_FRAMES = 48
CaptureState = Literal["queued", "processing", "needs_review", "ready", "no_animal", "failed", "legacy"]
Species = Literal["dog", "cat"]


class AnimalDetails(WireModel):
    sex: Literal["male", "female", "unsure"] | None = None
    ear_notch: Literal["none", "left", "right", "unsure"] | None = None
    condition: Literal["healthy", "injured", "unsure"] | None = None


class AnimalGroup(AnimalDetails):
    instance_ids: list[UUID] = Field(min_length=1, max_length=MAX_INSTANCES)


class CaptureReview(WireModel):
    revision: int = Field(ge=0)
    publish: bool = True
    groups: list[AnimalGroup] = Field(min_length=1, max_length=MAX_INSTANCES)

    @model_validator(mode="after")
    def unique_instances(self):
        ids = [i for g in self.groups for i in g.instance_ids]
        if len(ids) != len(set(ids)) or len(ids) > MAX_INSTANCES:
            raise ValueError("instance IDs must form a unique bounded partition")
        return self


class CaptureReceipt(WireModel):
    capture_id: UUID
    processing_state: CaptureState
    sighting_ids: list[UUID]
    duplicate: bool = False


class InstanceEvidence(WireModel):
    instance_id: UUID
    source_photo_id: UUID
    track_id: str
    sighting_id: UUID | None
    species: Species
    confidence: float
    bbox: tuple[int, int, int, int]
    crop_bbox: tuple[int, int, int, int]
    timestamp_ms: int | None
    photo_url: str
    thumb_url: str
    source_thumb_url: str
    details: AnimalDetails


class CaptureStatus(CaptureReceipt):
    revision: int
    captured_at: datetime
    note: str | None
    instances: list[InstanceEvidence]
    groups: list[AnimalGroup]


class MultiUpload(WireModel):
    kind: Literal["frame", "evidence"]
    index: int = Field(ge=0, lt=MAX_INSTANCES)
    original_bytes: int = Field(gt=0, le=20 * 1024 * 1024)
    thumbnail_bytes: int = Field(gt=0, le=1024 * 1024)

    @model_validator(mode="after")
    def frame_bound(self):
        if self.kind == "frame" and self.index >= MAX_SOURCE_FRAMES:
            raise ValueError("frame index exceeds budget")
        return self


class MultiURLRequest(Lease):
    uploads: list[MultiUpload] = Field(default_factory=list, max_length=MAX_INSTANCES + MAX_SOURCE_FRAMES)

    @model_validator(mode="after")
    def unique_slots(self):
        if len({(u.kind, u.index) for u in self.uploads}) != len(self.uploads):
            raise ValueError("duplicate upload slots")
        return self


class SourceFrame(WireModel):
    photo_id: UUID
    index: int | None = Field(default=None, ge=0, lt=MAX_SOURCE_FRAMES)
    timestamp_ms: int | None = Field(default=None, ge=0, le=2_147_483_647)
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    phash: str = Field(pattern=r"^[0-9a-f]{16}$")
    dog_confidence: float = Field(ge=0, le=1)
    cat_confidence: float = Field(ge=0, le=1)


class AnimalResult(WireModel):
    index: int = Field(ge=0, lt=MAX_INSTANCES)
    source_photo_id: UUID
    track_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.:-]+$")
    species: Species
    confidence: float = Field(ge=0, le=1)
    bbox: tuple[int, int, int, int]
    crop_bbox: tuple[int, int, int, int]
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    phash: str = Field(pattern=r"^[0-9a-f]{16}$")
    # A detected animal with failed embedding is still an observation.
    vector: list[float] | None = Field(default=None, min_length=EMBED_DIM, max_length=EMBED_DIM)

    @model_validator(mode="after")
    def valid_vector(self):
        import math
        if self.vector is not None and (not all(math.isfinite(v) for v in self.vector)
                or abs(math.sqrt(sum(v*v for v in self.vector)) - 1) > 0.01):
            raise ValueError("vector must be finite and L2-normalized")
        return self


class MultiCompletion(Lease):
    pipeline_version: Literal[MULTI_PIPELINE_VERSION]
    model: Literal[MODEL_NAME]
    frames: list[SourceFrame] = Field(min_length=1, max_length=MAX_SOURCE_FRAMES)
    instances: list[AnimalResult] = Field(max_length=MAX_INSTANCES)
    needs_review: bool

    @model_validator(mode="after")
    def coherent_evidence(self):
        sources = {f.photo_id: f for f in self.frames}
        if len(sources) != len(self.frames):
            raise ValueError("duplicate source photos")
        if len({i.index for i in self.instances}) != len(self.instances):
            raise ValueError("duplicate instance indices")
        tracks = {}
        for item in self.instances:
            source = sources.get(item.source_photo_id)
            if source is None:
                raise ValueError("unknown source photo")
            for box in (item.bbox, item.crop_bbox):
                x1, y1, x2, y2 = box
                if not (0 <= x1 < x2 <= source.width and 0 <= y1 < y2 <= source.height):
                    raise ValueError("box outside source")
            x1, y1, x2, y2 = item.crop_bbox
            if item.width != x2-x1 or item.height != y2-y1:
                raise ValueError("evidence dimensions must equal crop dimensions")
            bx1, by1, bx2, by2 = item.bbox
            if not (x1 <= bx1 < bx2 <= x2 and y1 <= by1 < by2 <= y2):
                raise ValueError("crop must contain raw box")
            species, seen = tracks.setdefault(item.track_id, (item.species, set()))
            if species != item.species or item.source_photo_id in seen:
                raise ValueError("a track cannot mix species or co-visible animals")
            seen.add(item.source_photo_id)
        return self
