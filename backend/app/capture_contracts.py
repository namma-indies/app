"""V2 capture contracts. Source photos belong to a capture; evidence belongs to one animal.

POST /capture: legacy multipart fields except animal details, 1..12 photos or
one video (not both), 201 CaptureReceipt.
GET /captures: {items: CaptureStatus[]} (owner only, newest first).
GET /capture/{id}: CaptureStatus (owner only, works when intake is disabled).
POST /capture/{id}/review: CaptureReview -> CaptureStatus. Every box is either in
exactly one group or excluded as not an animal. publish=false saves a private
draft; publish=true publishes atomically, and excluding every box discards the
upload (no_animal). After publication grouping is immutable, but the same groups
can update details.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.media_jobs import EMBED_DIM, MODEL_NAME, Lease, WireModel

MULTI_PIPELINE_VERSION = "stored-webp-q90-yolo26x-miewid-msv3-multi-v2"
MAX_INSTANCES = 96
MAX_SOURCE_FRAMES = 48
CaptureState = Literal["queued", "processing", "needs_review", "ready", "no_animal", "failed", "legacy"]
Species = Literal["dog", "cat"]


class AnimalDetails(WireModel):
    known_name: str | None = Field(default=None, max_length=80)

    @field_validator("known_name", mode="before")
    @classmethod
    def normalize_known_name(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("known name must be a single line without control characters")
            return value or None
        return value

    sex: Literal["male", "female", "unsure"] | None = None
    ear_notch: Literal["none", "left", "right", "unsure"] | None = None
    condition: Literal["healthy", "injured", "unsure"] | None = None


class AnimalGroup(AnimalDetails):
    instance_ids: list[UUID] = Field(min_length=1, max_length=MAX_INSTANCES)


class CaptureReview(WireModel):
    revision: int = Field(ge=0)
    publish: bool = True
    groups: list[AnimalGroup] = Field(max_length=MAX_INSTANCES)
    # Boxes the observer says show no animal: a doubled box, or a detector
    # mistake. They publish nothing.
    excluded_instance_ids: list[UUID] = Field(default_factory=list, max_length=MAX_INSTANCES)

    @model_validator(mode="after")
    def unique_instances(self):
        ids = [i for g in self.groups for i in g.instance_ids] + self.excluded_instance_ids
        if not ids or len(ids) != len(set(ids)) or len(ids) > MAX_INSTANCES:
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
    source_width: int = Field(gt=0)
    source_height: int = Field(gt=0)
    details: AnimalDetails


class DetailSuggestion(WireModel):
    """A detail a model read from the voice note, about the animal in one box.
    Shown in the review sheet; saving the sheet is the observer's decision."""
    id: UUID
    instance_id: UUID
    field: Literal["sex", "ear_notch", "condition", "known_name"]
    value: str
    quote: str
    # The note added later it was read from (#156); None for the upload's own
    # note or clip. The quote is marked in that transcript only.
    note_id: UUID | None = None


class AnsweredSuggestion(DetailSuggestion):
    """A suggestion that is no longer open, so the transcript can still mark where it came from.

    `status` is the recorded answer (accepted / corrected / dismissed), or
    "filled" for one never answered whose field now holds the suggested value.
    `chosen` is what the field was set to, if anything.
    """
    status: Literal["accepted", "corrected", "dismissed", "filled"]
    chosen: str | None = None


class Transcript(WireModel):
    """What was heard in the voice note or the clip's sound."""
    source: Literal["note", "clip"]
    language: str
    text: str
    # Translation of a non-English one, shown under it; never evidence.
    english: str | None = None
    # A note added after the upload (#156): which, when, and its audio.
    note_id: UUID | None = None
    added_at: datetime | None = None
    audio_url: str | None = None


class LaterNote(WireModel):
    """A voice note added after the upload, and how far along it is (app/voice_notes.py)."""
    id: UUID
    # The animal it was recorded about; None for one added to the whole capture.
    sighting_id: UUID | None = None
    added_at: datetime
    audio_url: str
    state: Literal["transcribing", "reading", "done", "no_speech", "failed"]


class CaptureStatus(CaptureReceipt):
    revision: int
    captured_at: datetime
    note: str | None
    instances: list[InstanceEvidence]
    groups: list[AnimalGroup]
    excluded_instance_ids: list[UUID] = Field(default_factory=list)
    suggestions: list[DetailSuggestion] = Field(default_factory=list)
    # Added beside `suggestions` (open only), never in place of it: older builds read that.
    answered_suggestions: list[AnsweredSuggestion] = Field(default_factory=list)
    voice_transcripts: list[Transcript] = Field(default_factory=list)
    later_notes: list[LaterNote] = Field(default_factory=list)


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
