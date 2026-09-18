"""Synthetic multi-animal contracts: no weights, CUDA, DB or network needed."""
from types import SimpleNamespace
import io

import numpy as np
import pytest
from PIL import Image

from app import analyse, embed, video
from app.detect_reid import AnimalDetection
from app.tracking import SamplingCoverage, associate_frames, make_frame
from gpu_worker import inference as gpu
from gpu_worker import clip


class Session:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def disable_fallback(self):
        pass

    def get_providers(self):
        return [gpu.CUDA]

    def get_inputs(self):
        return [SimpleNamespace(name="pixels")]

    def run(self, _, feeds):
        self.calls += 1
        if callable(self.output):
            return [self.output(self.calls)]
        return [self.output]


def raw():
    stream = io.BytesIO()
    Image.new("RGB", (640, 640), "white").save(stream, "PNG")
    return stream.getvalue()


def unit(index=0):
    vector = np.zeros(2152, dtype=np.float32)
    vector[index] = 1
    return vector


def worker(dets=None, vectors=None, limits=None):
    if dets is None:
        dets = [[[40, 100, 240, 500, .7, 16], [400, 200, 500, 400, .9, 16]]]
    return gpu.GpuInference(Session(np.array(dets, dtype=np.float32)),
                            Session(vectors if vectors is not None else unit()[None]),
                            gpu.Identity("a" * 64, "b" * 64, "test"), limits=limits)


@pytest.mark.parametrize("species", [16, 15])
def test_two_animals_have_independent_crops_species_and_vectors(species):
    adapter = worker([[[40, 100, 240, 500, .7, 16], [400, 200, 500, 400, .9, species]]],
                     lambda call: unit(call - 1)[None])
    frame = adapter.analyse_instances(raw(), source_id="photo:3", frame_index=3)
    assert len(frame.instances) == 2
    first, second = frame.instances
    assert first.instance_id == "photo:3:0" and second.instance_id == "photo:3:1"
    assert first.detection.raw_box == (40, 100, 240, 500)
    assert first.detection.crop_box == (20, 60, 260, 540)
    assert second.detection.species == ("dog" if species == 16 else "cat")
    assert first.detection.confidence == pytest.approx(.7)
    assert np.dot(first.vector, second.vector) == 0
    assert adapter._detector.calls == 1 and adapter._embedder.calls == 2


def test_legacy_still_embeds_only_largest():
    adapter = worker()
    result = adapter.analyse_photo(raw())
    assert result.bbox == (20, 60, 260, 540)
    assert adapter._embedder.calls == 1
    assert result.cat_confidence == 0


def test_failed_embedding_is_not_dropped_or_reused():
    def vectors(call):
        if call == 1:
            raise RuntimeError("synthetic inference failure")
        return unit(2)[None]

    frame = worker(vectors=vectors).analyse_instances(raw())
    first, second = frame.instances
    assert first.vector is None and first.embedding_error == "embedding_failed"
    assert second.instance_id == "photo:0:1" and second.embedding_error is None
    np.testing.assert_array_equal(second.vector, unit(2))


def test_cpu_and_gpu_multi_outputs_agree(monkeypatch):
    adapter = worker()
    monkeypatch.setattr(analyse, "_get_session", lambda: adapter._detector)
    monkeypatch.setattr(embed, "embed_crop", lambda image: unit())
    cpu = analyse.analyse_instances(raw())
    cuda = adapter.analyse_instances(raw())
    assert [i.detection for i in cpu.instances] == [i.detection for i in cuda.instances]
    for a, b in zip(cpu.instances, cuda.instances):
        np.testing.assert_array_equal(a.vector, b.vector)


def frame(index, species=("dog",), positions=None, failed=False):
    positions = positions or range(len(species))
    evidence = [(AnimalDetection(i, sp, .8, (x*100, 0, x*100+80, 80),
                                 (x*100, 0, x*100+80, 80)),
                 None if failed else unit(i), "embedding_failed" if failed else None)
                for i, (sp, x) in enumerate(zip(species, positions))]
    return make_frame(f"video:{index}", index, float(index), 640, 640, .8, 0, evidence)


def coverage(count):
    return SamplingCoverage("video", count, count, float(count), 0, float(count-1),
                            120, 3600, True, "nominal_fps")


def test_crossing_similar_dogs_never_merge():
    frames = (frame(0, ("dog", "dog"), (0, 4)), frame(1, ("dog", "dog"), (3, 1)))
    result = associate_frames(frames, coverage(2))
    assert len(result.tracks) == 4
    assert all(len(t.instance_ids) == len(t.evidence_instance_ids) == 1 for t in result.tracks)
    assert result.requires_review and all(t.requires_review for t in result.tracks)
    assert all(a.requires_review and a.reason == "crossing_or_competing" for a in result.associations)
    assert all(a.from_instance_id.split(":")[1] != a.to_instance_id.split(":")[1]
               for a in result.associations)


def test_occlusion_reentry_and_missing_vectors_are_private():
    frames = (frame(0), frame(1, ()), frame(2, failed=True))
    result = associate_frames(frames, coverage(3))
    assert len(result.tracks) == 2 and result.requires_review
    assert result.associations[0].reason == "gap_or_reentry"
    assert result.associations[0].appearance_similarity is None


def test_dog_cat_never_associate_and_single_photo_can_publish():
    result = associate_frames((frame(0, ("dog", "cat")),), coverage(1))
    assert not result.requires_review and len(result.tracks) == 2
    result = associate_frames((frame(0, ("dog",)), frame(1, ("cat",))), coverage(2))
    assert not result.associations and not result.requires_review


def test_same_animal_burst_needs_review_not_duplicate_public_count():
    result = associate_frames((frame(0), frame(1)), coverage(2))
    assert result.requires_review and result.associations[0].reason == "uncalibrated"
    assert len(result.tracks) == 2


def test_tracking_caps_fail_closed_and_reject_shuffled_frames():
    with pytest.raises(ValueError, match="instance budget"):
        associate_frames((frame(0, ("dog", "dog")),), coverage(1), max_instances=1)
    with pytest.raises(ValueError, match="chronological"):
        associate_frames((frame(1), frame(0)), coverage(2))


class Reader:
    def __init__(self, count=100, fps=10, duration=10):
        self.count, self.fps, self.duration = count, fps, duration
        self.yielded = 0

    def get_meta_data(self):
        return {"fps": self.fps, "duration": self.duration, "size": (16, 16)}

    def __iter__(self):
        for i in range(self.count):
            self.yielded += 1
            yield np.full((16, 16, 3), i % 255, dtype=np.uint8)

    def close(self):
        pass


def test_whole_video_sample_is_chronological_bounded_and_reaches_last_frame():
    samples = []
    reader = Reader()
    result = video.sample_chronological(reader, max_samples=5, max_decoded_frames=100,
                                        max_pixels=1000, emit=samples.append)
    assert [s.frame_index for s in samples] == [0, 25, 50, 74, 99]
    assert [s.timestamp_seconds for s in samples] == [0, 2.5, 5, 7.4, 9.9]
    assert result.decoded_frames == 100 and result.sampled_frames == 5
    assert result.reached_end and not result.exhaustive
    assert result.timestamp_basis == "nominal_fps"


@pytest.mark.parametrize("reader", [Reader(duration=100), Reader(duration=float("inf")), Reader(fps=0)])
def test_unbounded_or_unknown_duration_rejected_before_decode(reader):
    with pytest.raises(ValueError):
        video.sample_chronological(reader, max_samples=5, max_decoded_frames=100,
                                   max_pixels=1000, emit=lambda _: None)
    assert reader.yielded == 0


def test_dishonest_duration_cannot_return_truncated_success():
    reader = Reader(count=102)
    with pytest.raises(ValueError, match="budget"):
        video.sample_chronological(reader, max_samples=5, max_decoded_frames=100,
                                   max_pixels=1000, emit=lambda _: None)
    assert reader.yielded == 101


def test_video_child_preserves_hardened_protocol_and_timestamp_manifest(monkeypatch, tmp_path):
    import json
    from dataclasses import asdict
    (tmp_path / "input.clip").write_bytes(b"test")

    def get_reader(*args, **kwargs):
        assert kwargs["input_params"] == list(clip.FFMPEG_INPUT_PARAMS)
        assert kwargs["output_params"] == ["-threads", "1", "-frames:v", "101"]
        return Reader()

    monkeypatch.setattr(video.imageio, "get_reader", get_reader)
    clip._sample_child(str(tmp_path), asdict(gpu.Limits(max_decoded_frames=100, max_tracking_frames=5)))
    manifest = json.loads((tmp_path / "frames.json").read_text())
    assert manifest["schema_version"] == 2
    assert manifest["frames"][-1]["timestamp_seconds"] == 9.9
    assert manifest["coverage"]["sampled_frames"] == 5


def test_gpu_video_associates_before_emitting_evidence(monkeypatch):
    from contextlib import contextmanager
    from app.photos import process_photo
    events = []
    photo = process_photo(raw())

    @contextmanager
    def sampled(*args):
        yield SimpleNamespace(coverage=coverage(2), frames=lambda: iter([(0, 0., photo), (1, 1., photo)]))

    monkeypatch.setattr(clip, "sampled_bounded", sampled)
    adapter = worker()
    original = adapter._analyse_instances

    def analyse_frame(*args):
        events.append("infer")
        return original(*args)

    monkeypatch.setattr(adapter, "_analyse_instances", analyse_frame)
    result = adapter.analyse_clip(b"clip", evidence_sink=lambda source, photo: events.append(source))
    assert events == ["infer", "infer", "video:0", "video:1"]
    assert result.requires_review and len(result.tracks) == 4


def test_photo_wire_bridge_matches_backend_contract(tmp_path):
    from uuid import uuid4
    from app.photos import process_photo
    from app.capture_contracts import MultiCompletion, MULTI_PIPELINE_VERSION
    from gpu_worker.multi import prepare_capture

    photo = process_photo(raw())
    job = dict(kind="photo", max_frames=48, max_instances=96, sources=[dict(
        photo_id=str(uuid4()), width=photo.width, height=photo.height, phash=photo.phash)])
    prepared = prepare_capture(worker(), job, str(tmp_path), [photo.original])
    body = prepared.completion({})
    value = MultiCompletion(lease_token="t" * 43, pipeline_version=MULTI_PIPELINE_VERSION,
                            model="miewid-msv3", **body)
    assert len(value.instances) == 2 and not value.needs_review
    assert len(prepared.uploads) == 2
    assert all(u["spec"]["kind"] == "evidence" for u in prepared.uploads)
    assert value.instances[0].source_photo_id == value.frames[0].photo_id


def test_real_video_sample_subprocess_roundtrip(tmp_path):
    import imageio.v2 as imageio
    path = tmp_path / "clip.mp4"
    with imageio.get_writer(str(path), fps=5, format="ffmpeg", macro_block_size=1) as writer:
        for i in range(20):
            writer.append_data(np.full((32, 32, 3), i * 10, dtype=np.uint8))
    with clip.sampled_bounded(path.read_bytes(), gpu.Limits(max_tracking_frames=5)) as sampled:
        frames = list(sampled.frames())
        assert len(frames) == 5
        assert frames[0][0] == 0 and frames[-1][0] == 19
        assert frames[-1][1] == pytest.approx(3.8)
        assert sampled.coverage.decoded_frames == 20


def test_worker_multi_route_is_opt_in():
    from gpu_worker.client import Config
    common = dict(api_url="https://api.example", token="test", storage_origins=("https://storage.example",))
    assert Config(**common).multi_animal is False
    assert Config(**common, multi_animal=True).multi_animal is True


@pytest.mark.asyncio
async def test_worker_v2_claim_upload_completion_and_replay(tmp_path):
    import json
    from uuid import uuid4
    from datetime import datetime, timedelta, timezone
    import httpx
    from app.photos import process_photo
    from app.capture_contracts import MultiCompletion, MULTI_PIPELINE_VERSION
    from gpu_worker.client import Config, Worker

    photo = process_photo(raw())
    job_id, source_id = str(uuid4()), str(uuid4())
    completions, paths, puts = [], [], []
    job = dict(id=job_id, capture_id=str(uuid4()), kind="photo",
        pipeline_version=MULTI_PIPELINE_VERSION, model="miewid-msv3", lease_token="t" * 43,
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        max_frames=48, max_instances=96, sources=[dict(photo_id=source_id,
            url="https://storage.example/source", width=photo.width, height=photo.height, phash=photo.phash)])

    def api(request):
        paths.append(request.url.path)
        assert request.url.path.startswith("/internal/capture-jobs/")
        body = json.loads(request.content)
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": job})
        if request.url.path.endswith("/urls"):
            spec = body["uploads"][0]
            return httpx.Response(200, json={"uploads": [dict(kind=spec["kind"], index=spec["index"],
                photo_id=str(uuid4()), content_type="image/webp", original_url="https://storage.example/orig",
                thumbnail_url="https://storage.example/thumb")]})
        if request.url.path.endswith("/complete"):
            parsed = MultiCompletion(**body)
            assert len(parsed.instances) == 2 and not parsed.needs_review
            completions.append(request.content)
            if len(completions) == 1:
                raise httpx.ReadError("lost acknowledgement")
            return httpx.Response(200, json={"status": "done"})
        pytest.fail(f"unexpected API path {request.url.path}")

    def storage(request):
        if request.method == "GET":
            return httpx.Response(200, content=photo.original)
        puts.append(request.content)
        return httpx.Response(200)

    config = Config(api_url="https://api.example", token="secret", storage_origins=("https://storage.example",),
                    multi_animal=True, retry_delay=.001)
    async with Worker(config, worker(), api_transport=httpx.MockTransport(api),
                      storage_transport=httpx.MockTransport(storage)) as client:
        assert await client.run_once()
    assert len(puts) == 4 and len(completions) == 2
    assert completions[0] == completions[1]
    assert not any(path.endswith("/fail") for path in paths)


@pytest.mark.parametrize("last_timestamp", [1.0, 180.0])
def test_video_wire_bridge_preserves_source_timestamps_and_isolation(tmp_path, monkeypatch, last_timestamp):
    from contextlib import contextmanager
    from dataclasses import replace
    from uuid import uuid4
    from app.photos import process_photo
    from app.capture_contracts import MultiCompletion, MULTI_PIPELINE_VERSION
    from gpu_worker.multi import prepare_capture

    photo = process_photo(raw())
    @contextmanager
    def sampled(*args):
        sampled_coverage = replace(coverage(2), duration_seconds=last_timestamp + 1,
                                   last_timestamp_seconds=last_timestamp)
        yield SimpleNamespace(coverage=sampled_coverage,
                              frames=lambda: iter([(0, 0., photo), (1, last_timestamp, photo)]))
    monkeypatch.setattr(clip, "sampled_bounded", sampled)
    job = dict(kind="video", max_frames=48, max_instances=96, sources=[dict(photo_id=None)])
    prepared = prepare_capture(worker(), job, str(tmp_path), [b"clip"])
    ids = {("frame", index): str(uuid4()) for index in range(2)}
    result = MultiCompletion(lease_token="t"*43, pipeline_version=MULTI_PIPELINE_VERSION,
                            model="miewid-msv3", **prepared.completion(ids))
    assert [f.timestamp_ms for f in result.frames] == [0, round(last_timestamp * 1000)]
    assert [f.index for f in result.frames] == [0, 1]
    assert result.needs_review and len(result.instances) == 4
    assert len({i.track_id for i in result.instances}) == 4
    assert len(prepared.uploads) == 6
