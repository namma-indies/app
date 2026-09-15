"""Photo-only CPU fallback through the same fenced v2 publication contract."""
import io
import math

from starlette.concurrency import run_in_threadpool

from app.capture_contracts import MULTI_PIPELINE_VERSION, MultiCompletion, MultiURLRequest
from app.capture_jobs import capture_sources, complete_capture_job, multi_lease, upload_urls
from app.media_jobs import MODEL_NAME, payload_of
from app.photos import process_photo, thumb_key


def evidence_photo(raw,box):
    from app.detect import load_upright
    image = load_upright(raw)
    data = io.BytesIO()
    image.crop(box).save(data,"PNG")
    return process_photo(data.getvalue())


async def cpu_process_capture(pool,storage,job,token):
    if job["kind"] != "photo":
        raise ValueError("CPU capture fallback is photo-only")
    from app.analyse import analyse_photos
    async with pool.acquire() as conn:
        sources = await capture_sources(conn,job)
    raw = []
    total = 0
    for source in sources:
        data = await storage.get(source["s3_key"])
        total += len(data)
        if total > 256 * 1024 * 1024:
            raise ValueError("capture input exceeds byte budget")
        raw.append(data)
    analysis = await run_in_threadpool(analyse_photos,raw)
    track_ids = {iid:t.track_id for t in analysis.tracks for iid in t.instance_ids}
    frames,instances = [],[]
    for ordinal,frame in enumerate(analysis.frames):
        source = sources[ordinal]
        frames.append(dict(photo_id=source["photo_id"],width=source["width"],height=source["height"],
            phash=source["phash"],dog_confidence=frame.dog_confidence,cat_confidence=frame.cat_confidence))
        for instance in frame.instances:
            det = instance.detection
            photo = await run_in_threadpool(evidence_photo,raw[ordinal],det.crop_box)
            index = len(instances)
            spec = dict(kind="evidence",index=index,original_bytes=len(photo.original),thumbnail_bytes=len(photo.thumbnail))
            async with pool.acquire() as conn:
                await upload_urls(conn,storage,job["id"],MultiURLRequest(lease_token=token,uploads=[spec]),cpu=True)
                async with conn.transaction():
                    current = await multi_lease(conn,job["id"],token,cpu=True)
                    key = payload_of(current)["slots"][f"evidence:{index}"]["key"]
            await storage.put(key,photo.original,photo.content_type)
            await storage.put(thumb_key(key),photo.thumbnail,photo.content_type)
            x1,y1,x2,y2 = det.raw_box
            bbox = (max(0,math.floor(x1)),max(0,math.floor(y1)),min(frame.width,math.floor(x2)),min(frame.height,math.floor(y2)))
            instances.append(dict(index=index,source_photo_id=source["photo_id"],track_id=track_ids[instance.instance_id],
                species=det.species,confidence=det.confidence,bbox=bbox,crop_bbox=det.crop_box,
                width=photo.width,height=photo.height,phash=photo.phash,
                vector=instance.vector.tolist() if instance.vector is not None else None))
    body = MultiCompletion(lease_token=token,pipeline_version=MULTI_PIPELINE_VERSION,model=MODEL_NAME,
        frames=frames,instances=instances,needs_review=analysis.requires_review)
    async with pool.acquire() as conn:
        await complete_capture_job(conn,storage,job["id"],body,cpu=True)
