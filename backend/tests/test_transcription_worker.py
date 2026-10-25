"""Worker side of voice-note transcription: no CUDA, S3, database or whisper."""
from dataclasses import replace
import json
from pathlib import Path
import stat
from uuid import uuid4

import httpx
import pytest

from gpu_worker import transcribe
from gpu_worker.client import (Config, ProtocolError, TRANSCRIBE_PIPELINE_VERSION,
                               WHISPER_MODEL, Worker)
from tests.test_gpu_client import LEASE, SIGNED, TOKEN, FakeAdapter, expiry


def whisper_json(segments, language="kn"):
    return json.dumps({"result": {"language": language}, "transcription": [
        {"offsets": {"from": a, "to": b}, "text": t} for a, b, t in segments]}).encode()


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def test_parse_keeps_speech_and_drops_sound_descriptions():
    heard = transcribe.parse(whisper_json([
        (0, 1200, " She is asleep."), (1200, 1500, " [BLANK_AUDIO]"), (1500, 3000, " (dog barking) "),
        (3000, 5200, " Same dog as last week."),
    ]))
    assert heard.outcome == "transcribed" and heard.language == "kn"
    assert heard.text == "She is asleep. Same dog as last week."
    assert heard.segments == [
        {"start_ms": 0, "end_ms": 1200, "text": "She is asleep."},
        {"start_ms": 3000, "end_ms": 5200, "text": "Same dog as last week."},
    ]


def test_parse_reports_silence_without_a_language():
    heard = transcribe.parse(whisper_json([(0, 30000, " [BLANK_AUDIO]")], language="en"))
    assert (heard.outcome, heard.language, heard.text) == ("no_speech", None, "")


@pytest.mark.parametrize("raw", [
    whisper_json([(5, 1, "backwards")]),
    whisper_json([(0, 1, "hi")], language="English"),
    b"{}",
])
def test_parse_rejects_unexpected_output(raw):
    with pytest.raises((ValueError, KeyError)):
        transcribe.parse(raw)


# ---------------------------------------------------------------------------
# Transcriber, with stand-ins for ffmpeg and whisper-cli
# ---------------------------------------------------------------------------

def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def tools(tmp_path):
    # Writes its last argument (the output WAV): a header plus some samples.
    ffmpeg = script(tmp_path / "ffmpeg", 'for a; do out="$a"; done; head -c 4044 /dev/zero > "$out"\n')
    output = whisper_json([(0, 900, " Friendly, wags his tail.")], language="en").decode()
    # Writes <-of value>.json, and records its environment.
    whisper = script(tmp_path / "whisper", f"""
while [ $# -gt 0 ]; do [ "$1" = "-of" ] && out="$2"; shift; done
env > "{tmp_path}/whisper.env"
cat > "$out.json" <<'JSON'
{output}
JSON
""")
    return tmp_path, ffmpeg, whisper


def test_transcriber_decodes_then_transcribes_without_the_worker_token(tools, monkeypatch):
    root, ffmpeg, whisper = tools
    monkeypatch.setenv("MEDIA_GPU_TOKEN", "never-in-a-child")
    heard = transcribe.Transcriber(whisper, root / "m", root / "v", ffmpeg=str(ffmpeg)).transcribe(b"note")
    assert (heard.outcome, heard.language, heard.text) == ("transcribed", "en", "Friendly, wags his tail.")
    assert "never-in-a-child" not in (root / "whisper.env").read_text()


def test_a_clip_without_a_sound_track_is_no_audio(tools):
    root, _, whisper = tools
    ffmpeg = script(root / "ffmpeg-mute", "echo \"Stream map '0:a:0' matches no streams.\" >&2; exit 1\n")
    heard = transcribe.Transcriber(whisper, root / "m", root / "v", ffmpeg=str(ffmpeg)).transcribe(b"clip")
    assert heard.outcome == "no_audio"


def test_undecodable_audio_is_a_permanent_failure(tools):
    root, _, whisper = tools
    ffmpeg = script(root / "ffmpeg-bad", "echo 'Invalid data found' >&2; exit 1\n")
    with pytest.raises(ValueError):
        transcribe.Transcriber(whisper, root / "m", root / "v", ffmpeg=str(ffmpeg)).transcribe(b"junk")


def test_a_hung_whisper_is_killed(tools):
    root, ffmpeg, _ = tools
    hung = script(root / "whisper-hung", "sleep 30\n")
    t = transcribe.Transcriber(hung, root / "m", root / "v", ffmpeg=str(ffmpeg), whisper_timeout=.3)
    with pytest.raises(TimeoutError):
        t.transcribe(b"note")


# ---------------------------------------------------------------------------
# Worker queue order and wire contract
# ---------------------------------------------------------------------------

class FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, raw):
        self.calls.append(raw)
        return transcribe.Transcript("transcribed", "hi", "यह कुत्ता दोस्ताना है",
                                     [{"start_ms": 0, "end_ms": 1800, "text": "यह कुत्ता दोस्ताना है"}])


class Server:
    def __init__(self, media_job=None, kind="note"):
        self.media_job = media_job
        self.job = dict(id=str(uuid4()), kind=kind, pipeline_version=TRANSCRIBE_PIPELINE_VERSION,
                        model=WHISPER_MODEL, lease_token=LEASE, lease_expires_at=expiry(),
                        source={"url": SIGNED})
        self.calls = []
        self.completions = []

    async def api(self, request):
        assert request.headers["authorization"] == "Bearer " + TOKEN
        queue, route = request.url.path.split("/")[2], request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((queue, route))
        if route == "claim":
            if queue == "media-jobs":
                return httpx.Response(200, json={"job": self.media_job})
            claimed, self.job = self.job, None
            return httpx.Response(200, json={"job": claimed})
        assert queue == "transcription-jobs"
        if route == "complete":
            from app.transcription_jobs import TranscriptCompletion
            TranscriptCompletion.model_validate(body)
            self.completions.append(body)
            return httpx.Response(200, json={"status": "done"})
        if route == "heartbeat":
            return httpx.Response(200, json={"lease_expires_at": expiry()})
        raise AssertionError(route)

    async def storage(self, request):
        return httpx.Response(200, content=b"voice-note-bytes")

    def worker(self, transcriber):
        cfg = replace(Config("https://api.example", TOKEN, ("https://objects.example",),
                             heartbeat_interval=.015, retry_delay=.001, idle_delay=.01),
                      transcribe=transcriber is not None)
        return Worker(cfg, FakeAdapter(), transcriber=transcriber,
                      api_transport=httpx.MockTransport(self.api),
                      storage_transport=httpx.MockTransport(self.storage))


async def test_transcribes_only_when_no_image_work_is_waiting():
    server, transcriber = Server(), FakeTranscriber()
    async with server.worker(transcriber) as worker:
        assert await worker.run_once()
    assert server.calls[:2] == [("media-jobs", "claim"), ("transcription-jobs", "claim")]
    assert transcriber.calls == [b"voice-note-bytes"]
    done = server.completions[0]
    assert (done["outcome"], done["language"], done["model"]) == ("transcribed", "hi", WHISPER_MODEL)


async def test_a_worker_without_whisper_never_asks_for_transcription():
    server = Server()
    async with server.worker(None) as worker:
        assert not await worker.run_once()
    assert ("transcription-jobs", "claim") not in server.calls


async def test_rejects_a_job_for_another_pipeline():
    server, transcriber = Server(), FakeTranscriber()
    server.job["pipeline_version"] = "whisper-something-else"
    failures = []
    original = server.api

    async def api(request):
        if request.url.path.endswith("/fail"):
            failures.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "failed"})
        return await original(request)
    server.api = api
    async with server.worker(transcriber) as worker:
        assert await worker.run_once()
    assert not transcriber.calls
    assert failures[0]["error"] == ProtocolError.code and failures[0]["retryable"] is False


def test_config_and_transcriber_must_agree():
    cfg = Config("https://api.example", TOKEN, ("https://objects.example",), transcribe=True)
    with pytest.raises(ProtocolError):
        Worker(cfg, FakeAdapter())


def test_transcribe_flag_is_explicit(monkeypatch):
    monkeypatch.setenv("MEDIA_GPU_TOKEN", TOKEN)
    monkeypatch.setenv("MEDIA_GPU_API_URL", "https://api.example")
    monkeypatch.setenv("MEDIA_GPU_STORAGE_ORIGINS", "https://objects.example")
    assert Config.from_env().transcribe is False
    monkeypatch.setenv("MEDIA_GPU_TRANSCRIBE", "yes")
    with pytest.raises(ProtocolError):
        Config.from_env()


def test_a_playlist_upload_cannot_make_ffmpeg_fetch_anything(tmp_path):
    """Real FFmpeg: an HLS playlist naming a URL is refused, not followed."""
    playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://169.254.169.254/latest\n#EXT-X-ENDLIST\n"
    never = script(tmp_path / "whisper-never", "exit 99\n")
    with pytest.raises(ValueError):
        transcribe.Transcriber(never, tmp_path / "m", tmp_path / "v").transcribe(playlist)
