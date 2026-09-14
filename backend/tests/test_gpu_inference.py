"""Pure synthetic GPU adapter contracts: no models, GPU, DB, or network."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app import analyse, detect, detect_reid, embed, photos, video
from gpu_worker import clip
from gpu_worker import inference as gpu


class FakeSession:
    def __init__(self, output, providers=None):
        self.output = output
        self.providers = providers if providers is not None else [gpu.CUDA, "CPUExecutionProvider"]
        self.calls = []
        self.fallback_disabled = False

    def get_providers(self):
        return self.providers

    def disable_fallback(self):
        self.fallback_disabled = True

    def get_inputs(self):
        return [SimpleNamespace(name="pixels")]

    def run(self, names, feeds):
        assert self.fallback_disabled
        self.calls.append(feeds["pixels"].copy())
        return [self.output]


def image_bytes(width=160, height=80, fmt="PNG"):
    pixels = np.arange(width * height * 3, dtype=np.uint8).reshape(height, width, 3)
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, fmt)
    return stream.getvalue()


def identity():
    return gpu.Identity(hashlib.sha256(b"detector").hexdigest(),
                        hashlib.sha256(b"embedder").hexdigest(), gpu.preprocessing_identity())


def worker(detections=None, embedding=None, **kwargs):
    if detections is None:
        detections = np.array([[[80, 200, 560, 400, .6, 16]]], dtype=np.float32)
    if embedding is None:
        embedding = np.arange(1, 2153, dtype=np.float32)[None]
    return gpu.GpuInference(FakeSession(detections), FakeSession(embedding), identity(), **kwargs)


@pytest.mark.parametrize("dimensions", [(160, 80), (80, 160), (100, 100)])
def test_exact_preprocessing_and_reference_analysis(monkeypatch, dimensions):
    raw = photos.process_photo(image_bytes(*dimensions)).original
    detections = np.array([[[80, 200, 560, 400, .6, 16],
                            [200, 230, 240, 260, .9, 15],
                            [0, 0, 40, 40, .05, 16]]], dtype=np.float32)
    adapter = worker(detections=detections)
    result = adapter.analyse_photo(raw)
    reference_session = FakeSession(detections)
    reference_session.disable_fallback()
    monkeypatch.setattr(analyse, "_get_session", lambda: reference_session)
    reference = analyse.analyse(raw)
    assert result.dog_confidence == reference.dog_confidence
    assert result.cat_confidence == reference.cat_confidence
    assert result.bbox == reference.box
    np.testing.assert_array_equal(adapter._detector.calls[0], reference_session.calls[0])
    np.testing.assert_array_equal(adapter._embedder.calls[0], embed.preprocess(reference.image.crop(reference.box)))
    expected = adapter._embedder.output[0]
    expected = expected / (np.linalg.norm(expected) + 1e-8)
    np.testing.assert_array_equal(result.vector, expected)
    assert result.vector.dtype == np.float32 and result.vector.shape == (2152,)
    assert result.identity == adapter.identity


def test_photo_processing_exact_and_stored_photo_never_reencoded(monkeypatch):
    raw = image_bytes()
    adapter = worker()
    assert adapter.process_photo(raw) == photos.process_photo(raw)
    stored = photos.process_photo(raw).original
    monkeypatch.setattr(photos, "process_photo", lambda _: pytest.fail("stored photo re-encoded"))
    adapter.analyse_photo(stored)


def test_no_app_global_sessions_initialized():
    prior = (detect_reid._session, embed._session)
    worker().analyse_photo(image_bytes())
    assert (detect_reid._session, embed._session) == prior


@pytest.mark.parametrize("detections, dog, cat", [
    (np.empty((1, 0, 6)), 0, 0),
    (np.array([[[0, 0, 100, 100, .04, 16], [0, 0, 100, 100, .03, 15]]]), .04, .03),
    (np.array([[[800, 800, 900, 900, .5, 16]]]), .5, 0),
])
def test_no_animal_skips_embedder(detections, dog, cat):
    adapter = worker(detections=detections)
    result = adapter.analyse_photo(image_bytes())
    assert (result.dog_confidence, result.cat_confidence) == (dog, cat)
    assert result.bbox is None and result.vector is None
    assert not adapter._embedder.calls


@pytest.mark.parametrize("bad", [
    np.empty((1, 0)), np.ones((2152,)), np.ones((2, 2152)),
    np.zeros((1, 2152)), np.full((1, 2152), np.nan),
    np.full((1, 2152), np.inf), np.full((1, 2152), 1e-30),
])
def test_reject_invalid_vectors_and_release_slot(bad):
    adapter = worker(embedding=bad)
    with pytest.raises(ValueError):
        adapter.analyse_photo(image_bytes())
    assert not adapter._lock.locked()


@pytest.mark.parametrize("bad", [np.ones((1, 6)), np.ones((1, 301, 6)),
                                 np.full((1, 1, 6), np.nan),
                                 np.array([[[0, 0, 1, 1, 2, 16]]]),
                                 np.array([[[0, 0, 1, 1, .5, 16.5]]])])
def test_reject_invalid_detector_output(bad):
    with pytest.raises(ValueError):
        worker(detections=bad).analyse_photo(image_bytes())


@pytest.mark.parametrize("method", ["analyse_photo", "process_photo", "extract_clip"])
def test_one_item_at_a_time(method):
    adapter = worker()
    adapter._lock.acquire()
    try:
        with pytest.raises(gpu.WorkerBusy):
            getattr(adapter, method)(image_bytes())
    finally:
        adapter._lock.release()


@pytest.mark.parametrize("raw", [b"", b"not a photo", None])
def test_invalid_photo(raw):
    with pytest.raises((ValueError, OSError)):
        worker().analyse_photo(raw)


def test_input_limits():
    adapter = worker(limits=gpu.Limits(max_photo_bytes=2, max_clip_bytes=2))
    with pytest.raises(ValueError):
        adapter.analyse_photo(image_bytes())
    with pytest.raises(ValueError):
        adapter.extract_clip(b"123")
    with pytest.raises(ValueError, match="pixel"):
        worker(limits=gpu.Limits(max_pixels=10)).process_photo(image_bytes())


@pytest.mark.parametrize("values", [{"keep": 13}, {"max_raw": 21}, {"keep": 3, "max_raw": 2},
                                     {"max_pixels": 0}, {"clip_timeout_seconds": float("inf")},
                                     {"max_raw": 1.5}, {"max_pixels": True}])
def test_limits_reject_invalid_values(values):
    with pytest.raises(ValueError):
        gpu.Limits(**values)


def test_cuda_required_for_both_injected_sessions():
    for cpu_index in (0, 1):
        sessions = [FakeSession(None), FakeSession(None)]
        sessions[cpu_index].providers = ["CPUExecutionProvider"]
        with pytest.raises(gpu.CudaUnavailable):
            gpu.GpuInference(*sessions, identity())


def test_runtime_provider_loss_is_not_silent():
    adapter = worker()
    old_run = adapter._detector.run

    def run(*args):
        result = old_run(*args)
        adapter._detector.providers = ["CPUExecutionProvider"]
        return result

    adapter._detector.run = run
    with pytest.raises(gpu.CudaUnavailable):
        adapter.analyse_photo(image_bytes())
    assert not adapter._embedder.calls


def test_run_failure_propagates_without_cpu_retry():
    adapter = worker()

    def fail(*args):
        raise RuntimeError("CUDA failure")

    adapter._detector.run = fail
    with pytest.raises(RuntimeError, match="CUDA failure"):
        adapter.analyse_photo(image_bytes())
    assert not adapter._lock.locked()


def test_constructor_cpu_fallback_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu.ort, "get_available_providers", lambda: [gpu.CUDA])
    monkeypatch.setattr(gpu.ort, "InferenceSession", lambda *a, **k: FakeSession(None, ["CPUExecutionProvider"]))
    with pytest.raises(gpu.CudaUnavailable):
        gpu.create_cuda_session(tmp_path / "fake.onnx")


def test_cuda_absent_rejected_before_session_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu.ort, "get_available_providers", lambda: ["CPUExecutionProvider"])
    monkeypatch.setattr(gpu.ort, "InferenceSession", lambda *a, **k: pytest.fail("constructed CPU session"))
    with pytest.raises(gpu.CudaUnavailable):
        gpu.create_cuda_session(tmp_path / "fake.onnx")


def test_session_options_clamped_and_hashes_real(monkeypatch, tmp_path):
    options_seen = []
    monkeypatch.setattr(gpu.ort, "get_available_providers", lambda: [gpu.CUDA])

    def session(path, sess_options, providers):
        assert providers == [(gpu.CUDA, {"use_tf32": "0"})]
        options_seen.append(sess_options)
        return FakeSession(None)

    monkeypatch.setattr(gpu.ort, "InferenceSession", session)
    detector, embedder = tmp_path / "detector.onnx", tmp_path / "embedder.onnx"
    detector.write_bytes(b"detector")
    embedder.write_bytes(b"embedder")
    adapter = gpu.GpuInference.from_paths(detector, embedder, threads=999)
    assert adapter.identity == identity()
    for options in options_seen:
        assert options.intra_op_num_threads == 2
        assert options.inter_op_num_threads == 1
        assert options.execution_mode == gpu.ort.ExecutionMode.ORT_SEQUENTIAL
    assert gpu.preprocessing_identity() == gpu.preprocessing_identity()


def test_clip_child_reuses_existing_extraction_with_decode_and_thread_caps(monkeypatch, tmp_path):
    (tmp_path / "input.clip").write_bytes(b"synthetic")
    frames = [np.full((32, 32, 3), i, dtype=np.uint8) for i in range(10)]
    readers = []

    class Reader:
        closed = False
        yielded = 0

        def get_meta_data(self):
            return {"fps": 1, "size": (32, 32)}

        def __iter__(self):
            for frame in frames:
                self.yielded += 1
                yield frame

        def close(self):
            self.closed = True

    def get_reader(*args, **kwargs):
        assert kwargs["input_params"] == list(clip.FFMPEG_INPUT_PARAMS)
        assert kwargs["output_params"] == ["-threads", "1", "-frames:v", "3"]
        reader = Reader()
        readers.append(reader)
        return reader

    monkeypatch.setattr(video.imageio, "get_reader", get_reader)
    clip._extract_child(str(tmp_path), asdict(gpu.Limits(max_decoded_frames=3, keep=2)))
    metadata = json.loads((tmp_path / "frames.json").read_text())
    assert len(metadata) == 2
    assert readers[0].closed
    assert readers[0].yielded == 3
    for i in range(2):
        buf = io.BytesIO()
        Image.fromarray(frames[i]).save(buf, "JPEG")
        expected = photos.process_photo(buf.getvalue())
        assert (tmp_path / f"{i}.webp").read_bytes() == expected.original
        assert (tmp_path / f"{i}.thumb.webp").read_bytes() == expected.thumbnail


def test_embedding_resize_limit_rejects_extreme_crop():
    detections = np.array([[[0, 319, 640, 320, .8, 16]]], dtype=np.float32)
    adapter = worker(detections=detections, limits=gpu.Limits(max_pixels=200_000))
    with pytest.raises(ValueError, match="resize exceeds"):
        adapter.analyse_photo(image_bytes())
    assert not adapter._embedder.calls


def test_clip_results_roundtrip_and_size_limit(monkeypatch):
    from pathlib import Path

    frame = photos.process_photo(image_bytes())

    class Process:
        pid = 123456

        def wait(self, timeout=None):
            return 0

    def popen(args, **kwargs):
        root = Path(args[3])
        (root / "0.webp").write_bytes(frame.original)
        (root / "0.thumb.webp").write_bytes(frame.thumbnail)
        (root / "frames.json").write_text(json.dumps([{key: getattr(frame, key) for key in (
            "width", "height", "phash", "content_type",
        )}]))
        return Process()

    monkeypatch.setattr(clip.subprocess, "Popen", popen)
    monkeypatch.setattr(clip.os, "killpg", lambda *args: None)
    assert worker().extract_clip(b"synthetic") == [frame]
    with pytest.raises(ValueError, match="result byte limit"):
        worker(limits=gpu.Limits(max_result_bytes=1)).extract_clip(b"synthetic")


def test_clip_empty_rejected_before_spawning(monkeypatch):
    monkeypatch.setattr(clip.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned on empty clip"))
    with pytest.raises(ValueError):
        worker().extract_clip(b"")


def test_clip_timeout_kills_process_group_and_bounds_environment(monkeypatch):
    waits, killed = [], []

    class Process:
        pid = 123456

        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            return -9

    def popen(args, **kwargs):
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["OMP_NUM_THREADS"] == "1"
        assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == ""
        assert kwargs["env"]["TMPDIR"] == args[3]
        return Process()

    monkeypatch.setattr(clip.subprocess, "Popen", popen)
    monkeypatch.setattr(clip.os, "killpg", lambda *args: killed.append(args))
    adapter = worker(limits=gpu.Limits(clip_timeout_seconds=.1))
    with pytest.raises(ValueError, match="time limit"):
        adapter.extract_clip(b"synthetic")
    assert waits == [.1, None]
    assert killed == [(123456, clip.signal.SIGKILL)]
    assert not adapter._lock.locked()


def test_decoder_environment_excludes_credentials_and_loader_hooks(monkeypatch, tmp_path):
    secrets = {
        "MEDIA_GPU_TOKEN": "synthetic-secret", "MEDIA_GPU_TOKEN_FILE": "/run/worker-token/token",
        "AWS_SECRET_ACCESS_KEY": "synthetic-aws", "DATABASE_URL": "synthetic-db",
        "HTTP_PROXY": "http://synthetic-proxy", "LD_PRELOAD": "/synthetic/preload.so",
        "PYTHONHOME": "/synthetic/python", "IMAGEIO_FFMPEG_EXE": "/synthetic/ffmpeg",
        "UNRECOGNIZED_CREDENTIAL": "synthetic-other",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    env = clip._child_environment(str(tmp_path))
    assert not set(secrets) & env.keys()
    result = subprocess.run([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert not set(secrets) & observed.keys()
    assert observed["HOME"] == str(tmp_path)
    assert observed["CUDA_VISIBLE_DEVICES"] == ""


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_child_limits_only_lower_existing_hard_caps(monkeypatch, platform):
    import resource

    applied = {}
    monkeypatch.setattr(clip.sys, "platform", platform)
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (resource.RLIM_INFINITY, 512))
    monkeypatch.setattr(resource, "setrlimit", lambda kind, bounds: applied.setdefault(kind, bounds))
    clip._limit_child(asdict(gpu.Limits()))
    assert applied[resource.RLIMIT_CORE] == (0, 0)
    assert applied[resource.RLIMIT_CPU] == (90, 90)
    assert applied[resource.RLIMIT_NOFILE] == (64, 64)
    assert applied[resource.RLIMIT_FSIZE] == (512, 512)
    assert (resource.RLIMIT_AS in applied) == (platform == "linux")


@pytest.fixture(params=[("mp4", "libx264"), ("mov", "libx264"), ("webm", "libvpx-vp9")])
def real_clip(request, tmp_path):
    import imageio_ffmpeg

    extension, codec = request.param
    path = tmp_path / f"real.{extension}"
    result = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i",
        "testsrc=size=64x48:rate=4:duration=3", "-c:v", codec, "-pix_fmt", "yuv420p",
        "-threads", "1", str(path),
    ], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode()
    return path.read_bytes()


def test_real_clip_preserves_existing_frame_selection(real_clip):
    reference = video.extract_diverse_frames(real_clip)
    result = clip.extract_bounded(real_clip, gpu.Limits())
    assert len(result) == len(reference) == 3
    assert result == reference


def test_real_clip_pixel_and_decode_limits(real_clip, monkeypatch, tmp_path):
    monkeypatch.setattr(clip.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(ValueError, match="decoding failed"):
        clip.extract_bounded(real_clip, gpu.Limits(max_pixels=64))
    result = clip.extract_bounded(real_clip, gpu.Limits(max_decoded_frames=2))
    assert len(result) == 1
    assert not list(tmp_path.glob("dgx-frames-*"))


@pytest.mark.parametrize("protocol", ["http", "https", "tcp", "udp", "concat", "subfile", "crypto", "data", "pipe"])
def test_ffmpeg_denies_non_file_input_protocols(protocol):
    import imageio_ffmpeg

    # FFmpeg rejects these before opening anything; never requires a remote host
    # or listening test server. Explicit demuxer keeps the assertion independent
    # of probing a nonexistent resource.
    result = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", *clip.FFMPEG_INPUT_PARAMS,
        "-f", "mov", "-i", f"{protocol}:synthetic-denied", "-f", "null", "-",
    ], capture_output=True, text=True, timeout=5, env=clip._child_environment("/tmp"))
    assert result.returncode != 0
    assert "not on whitelist" in result.stderr or "Protocol not found" in result.stderr


@pytest.mark.parametrize("playlist", [
    b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttps://example.invalid/secret.ts\n#EXT-X-ENDLIST\n",
    b"ffconcat version 1.0\nfile '/run/worker-token/token'\n",
    b"ffconcat version 1.0\nfile 'https://example.invalid/secret.mp4'\n",
])
def test_disguised_playlists_are_rejected_and_cleaned(playlist, monkeypatch, tmp_path):
    monkeypatch.setattr(clip.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(ValueError, match="decoding failed"):
        clip.extract_bounded(playlist, gpu.Limits(clip_timeout_seconds=10))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("demuxer,contents", [
    ("concat", "ffconcat version 1.0\nfile '/run/worker-token/token'\n"),
    ("hls", "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttps://example.invalid/secret.ts\n"),
])
def test_playlist_demuxers_denied_before_references_are_opened(demuxer, contents, tmp_path):
    import imageio_ffmpeg

    path = tmp_path / "disguised.mp4"
    path.write_text(contents)
    result = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", *clip.FFMPEG_INPUT_PARAMS,
        "-f", demuxer, "-i", str(path), "-f", "null", "-",
    ], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "not on whitelist" in result.stderr


def test_real_timeout_kills_decoder_group_and_cleans_scratch(monkeypatch, tmp_path):
    popen = subprocess.Popen
    spawned = []
    roots = []

    def slow_decoder(args, **kwargs):
        roots.append(Path(args[3]))
        child_code = "import time; time.sleep(60)"
        code = ("import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(60)")
        process = popen([sys.executable, "-c", code], **kwargs)
        spawned.append(process)
        assert kwargs["close_fds"] and kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["cwd"] == args[3]
        return process

    monkeypatch.setattr(clip.subprocess, "Popen", slow_decoder)
    start = time.monotonic()
    with pytest.raises(ValueError, match="time limit"):
        clip.extract_bounded(b"synthetic", gpu.Limits(clip_timeout_seconds=.5))
    assert time.monotonic() - start < 5
    assert spawned[0].returncode == -clip.signal.SIGKILL
    assert not roots[0].exists()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(spawned[0].pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.01)
    else:
        pytest.fail("decoder process group survived cleanup")
