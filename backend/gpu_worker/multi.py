"""DB-free bridge from typed analysis to the capture-jobs v2 wire contract."""
from dataclasses import dataclass
import io
import math
from pathlib import Path
from uuid import UUID

from app.detect import load_upright
from app.photos import process_photo
from app.tracking import CaptureAnalysis


@dataclass
class PreparedCapture:
    analysis: CaptureAnalysis
    frames: list[tuple[str, dict]]
    instances: list[tuple[str, dict]]
    uploads: list[dict]

    def completion(self, photo_ids):
        frames = []
        source_ids = {}
        for source_id, values in self.frames:
            item = dict(values)
            if item["index"] is not None:
                item["photo_id"] = photo_ids[("frame", item["index"])]
            source_ids[source_id] = item["photo_id"]
            frames.append(item)
        instances = []
        for source_id, values in self.instances:
            item = dict(values)
            item["source_photo_id"] = source_ids[source_id]
            instances.append(item)
        return dict(frames=frames, instances=instances, needs_review=self.analysis.requires_review)


def prepare_capture(adapter, job: dict, directory: str, raw: list[bytes]) -> PreparedCapture:
    """Persist bounded encoded evidence; no sessions, database, or HTTP here."""
    root = Path(directory)
    source_files = {}
    source_metadata = {}
    uploads = []
    total = 0
    max_bytes = 256 * 1024 * 1024

    def save_photo(name, photo, kind=None, index=None):
        nonlocal total
        total += len(photo.original) + len(photo.thumbnail)
        if (total > max_bytes or not 0 < len(photo.original) <= 20 * 1024 * 1024
                or not 0 < len(photo.thumbnail) <= 1024 * 1024):
            raise ValueError("capture evidence exceeds byte budget")
        original, thumbnail = root / f"{name}.webp", root / f"{name}.thumb.webp"
        original.write_bytes(photo.original)
        thumbnail.write_bytes(photo.thumbnail)
        if kind:
            uploads.append({"spec": dict(kind=kind, index=index,
                original_bytes=len(photo.original), thumbnail_bytes=len(photo.thumbnail)),
                "original": str(original), "thumbnail": str(thumbnail)})
        return original

    def source_sink(source_id, photo):
        ordinal = len(source_files)
        if ordinal >= min(48, job["max_frames"]):
            raise ValueError("source-frame budget exceeded")
        source_files[source_id] = save_photo(f"source-{ordinal}", photo, "frame", ordinal)
        source_metadata[source_id] = {"phash": photo.phash, "index": ordinal, "photo_id": None}

    if job["kind"] == "video":
        if len(raw) != 1 or len(job["sources"]) != 1 or job["sources"][0]["photo_id"] is not None:
            raise ValueError("invalid video sources")
        result = adapter.analyse_clip(raw[0], evidence_sink=source_sink)
    elif job["kind"] == "photo":
        if not 1 <= len(raw) == len(job["sources"]) <= 12:
            raise ValueError("invalid photo sources")
        result = adapter.analyse_photos(raw)
        for index, (source, data) in enumerate(zip(job["sources"], raw)):
            source_id = f"photo:{index}"
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("source exceeds photo byte budget")
            total += len(data)
            if total > max_bytes:
                raise ValueError("capture evidence exceeds byte budget")
            path = root / f"source-{index}.webp"
            path.write_bytes(data)
            source_files[source_id] = path
            source_metadata[source_id] = {"phash": source["phash"], "index": None,
                                          "photo_id": str(UUID(source["photo_id"]))}
    else:
        raise ValueError("unsupported capture media")
    if len(result.frames) > min(48, job["max_frames"]):
        raise ValueError("capture frame budget exceeded")
    instances = [i for frame in result.frames for i in frame.instances]
    if len(instances) > min(96, job["max_instances"]):
        raise ValueError("capture instance budget exceeded")
    tracks = {iid: track.track_id for track in result.tracks for iid in track.instance_ids}
    frames, evidence = [], []
    for frame in result.frames:
        source = source_metadata[frame.source_id]
        timestamp = None if frame.timestamp_seconds is None else round(frame.timestamp_seconds * 1000)
        if timestamp is not None and not 0 <= timestamp <= 2_147_483_647:
            raise ValueError("invalid evidence timestamp")
        frames.append((frame.source_id, dict(**source, timestamp_ms=timestamp,
            width=frame.width, height=frame.height, dog_confidence=frame.dog_confidence,
            cat_confidence=frame.cat_confidence)))
        image = load_upright(source_files[frame.source_id].read_bytes())
        if image.size != (frame.width, frame.height):
            raise ValueError("source dimensions changed")
        for instance in frame.instances:
            index = len(evidence)
            detection = instance.detection
            crop = image.crop(detection.crop_box)
            buffer = io.BytesIO()
            crop.save(buffer, "PNG")
            photo = process_photo(buffer.getvalue())
            save_photo(f"animal-{index}", photo, "evidence", index)
            x1, y1, x2, y2 = detection.raw_box
            # Wire/storage boxes are integer, clipped pixel extents. The typed
            # analysis retains the detector's original floating-point raw box.
            bbox = (max(0, math.floor(x1)), max(0, math.floor(y1)),
                    min(frame.width, math.floor(x2)), min(frame.height, math.floor(y2)))
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                raise ValueError("raw animal box has no source pixels")
            evidence.append((frame.source_id, dict(index=index, track_id=tracks[instance.instance_id],
                species=detection.species, confidence=detection.confidence, bbox=bbox,
                crop_bbox=detection.crop_box, width=photo.width, height=photo.height,
                phash=photo.phash, vector=None if instance.vector is None else instance.vector.tolist())))
    return PreparedCapture(result, frames, evidence, uploads)
