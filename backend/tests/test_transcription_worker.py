"""Worker side of voice-note transcription: no CUDA, S3, database or whisper."""
from dataclasses import replace
import json
from pathlib import Path
import stat
import sys
import textwrap
from uuid import uuid4

import httpx
import pytest

from gpu_worker import transcribe
from gpu_worker.client import (Config, ProtocolError, TRANSCRIBE_PIPELINE_VERSION,
                               WHISPER_MODEL, Worker)
from tests.test_gpu_client import LEASE, SIGNED, TOKEN, FakeAdapter, expiry


def whisper_json(segments, language="kannada", odds=None):
    """A whisper-server ``verbose_json`` reply; times in seconds, as it sends them.
    `odds` is its ``language_probabilities`` (code to probability)."""
    reply = {"language": language, "text": "", "segments": [
        {"start": a / 1000, "end": b / 1000, "text": t} for a, b, t in segments]}
    if odds is not None:
        reply["language_probabilities"] = odds
    return json.dumps(reply).encode()


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


@pytest.mark.parametrize("name,code", [("english", "en"), ("hindi", "hi"), ("tamil", "ta"), ("hawaiian", "haw")])
def test_parse_stores_the_language_code(name, code):
    assert transcribe.parse(whisper_json([(0, 900, " ok")], language=name)).language == code


def test_parse_reports_silence_without_a_language():
    heard = transcribe.parse(whisper_json([(0, 30000, " [BLANK_AUDIO]")], language="english"))
    assert (heard.outcome, heard.language, heard.text) == ("no_speech", None, "")


@pytest.mark.parametrize("raw", [
    whisper_json([(5, 1, "backwards")]),
    whisper_json([(0, 1, "hi")], language="klingon"),
    b"{}",
])
def test_parse_rejects_unexpected_output(raw):
    with pytest.raises((ValueError, KeyError)):
        transcribe.parse(raw)


# ---------------------------------------------------------------------------
# Transcriber, with stand-ins for ffmpeg and whisper-server
# ---------------------------------------------------------------------------

def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


FAKE_SERVER = textwrap.dedent('''\
    #!{python}
    import json, os, sys, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    args = sys.argv
    port = int(args[args.index("--port") + 1])
    with open({env!r}, "w") as f:
        f.write("\\n".join(f"{{k}}={{v}}" for k, v in os.environ.items()))
    with open({starts!r}, "a") as f:
        f.write("start\\n")
    REPLY, FORCED = {reply!r}, {forced!r}
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404); self.end_headers()
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            # The language the worker named, if any: the multipart field after its header.
            named = 'auto'
            if b'name="language"' in body:
                named = body.split(b'name="language"', 1)[1].split(b'\\r\\n\\r\\n', 1)[1].split(b'\\r\\n', 1)[0].decode()
            with open({requests!r}, "a") as f:
                f.write(named + "\\n")
            if {hang}:
                time.sleep(30)
            data = (FORCED.get(named, REPLY) if FORCED else REPLY).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


@pytest.fixture
def tools(tmp_path):
    # Writes its last argument (the output WAV): a header plus some samples.
    ffmpeg = script(tmp_path / "ffmpeg", 'for a; do out="$a"; done; head -c 4044 /dev/zero > "$out"\n')

    def server(name="whisper-server", hang=False, reply=None, forced=None):
        """`forced` maps a language the worker names to the reply for it."""
        path = tmp_path / name
        path.write_text(FAKE_SERVER.format(
            python=sys.executable, env=str(tmp_path / "whisper.env"), starts=str(tmp_path / "starts"),
            requests=str(tmp_path / "requests"),
            reply=(reply or whisper_json([(0, 900, " Friendly, wags his tail.")], language="english")).decode(),
            forced={k: v.decode() for k, v in (forced or {}).items()}, hang=hang))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path
    return tmp_path, ffmpeg, server


def started(root):
    return (root / "starts").read_text().count("start")


def test_whisper_stays_loaded_across_notes_and_never_sees_the_worker_token(tools, monkeypatch):
    root, ffmpeg, server = tools
    monkeypatch.setenv("MEDIA_GPU_TOKEN", "never-in-a-child")
    t = transcribe.Transcriber(server(), root / "m", root / "v", ffmpeg=str(ffmpeg), port=18941)
    t.start()
    try:
        for _ in range(3):
            heard = t.transcribe(b"note")
            assert (heard.outcome, heard.language, heard.text) == ("transcribed", "en", "Friendly, wags his tail.")
        assert started(root) == 1  # one load, three notes
    finally:
        t.stop()
    assert "never-in-a-child" not in (root / "whisper.env").read_text()


# ---------------------------------------------------------------------------
# Only the languages people here speak; English when none is likely
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("odds,chosen", [
    ({"kn": 0.81, "en": 0.12}, "kn"),          # an allowed language, kept
    ({"en": 0.95}, "en"),
    ({"cy": 0.62, "en": 0.31}, "en"),          # English heard as Welsh
    ({"ms": 0.55, "hi": 0.35, "en": 0.05}, "hi"),  # the likeliest allowed one
    ({"cy": 0.9, "hi": 0.05}, "en"),           # no allowed language is likely: English
    ({"hi": 0.15, "en": 0.12, "ur": 0.1}, "en"),   # even an allowed detection, too unsure
    ({"hi": 0.2}, "hi"),                       # the threshold itself is enough
])
def test_chooses_from_the_languages_people_here_speak(odds, chosen):
    assert transcribe.choose_language(whisper_json([(0, 900, " ok")], "welsh", odds)) == chosen


@pytest.mark.parametrize("odds", [None, {}, {"en": "high"}, {"en": 1.5}, {"en": True}, [0.9]])
def test_without_usable_probabilities_the_detection_stands(odds):
    assert transcribe.choose_language(whisper_json([(0, 900, " ok")], "welsh", odds)) is None


def test_allowed_set_is_whisper_codes_and_english_first():
    assert transcribe.ALLOWED[0] == "en"
    assert set(transcribe.ALLOWED) <= set(transcribe._CODES.values())


def requests_made(root):
    return (root / "requests").read_text().split()


def test_english_heard_as_welsh_is_transcribed_again_as_english(tools, caplog):
    root, ffmpeg, server = tools
    welsh = whisper_json([(0, 900, " Mae hi'n cysgu.")], "welsh", {"cy": 0.62, "en": 0.31})
    english = whisper_json([(0, 900, " She is asleep.")], "english", {"cy": 0.62, "en": 0.31})
    t = transcribe.Transcriber(server(reply=welsh, forced={"en": english}), root / "m", root / "v",
                               ffmpeg=str(ffmpeg), port=18945)
    t.start()
    try:
        heard = t.transcribe(b"note")
    finally:
        t.stop()
    assert (heard.outcome, heard.language, heard.text) == ("transcribed", "en", "She is asleep.")
    assert requests_made(root) == ["auto", "en"]
    (record,) = [r for r in caplog.records if r.name == "gpu_worker.transcribe"]
    assert record.getMessage() == "whisper language cy replaced by en (cy=0.62, en=0.31)"
    assert "asleep" not in caplog.text and "cysgu" not in caplog.text  # never what was said


def test_a_likely_allowed_language_costs_no_second_pass(tools):
    root, ffmpeg, server = tools
    kannada = whisper_json([(0, 900, " ನಾಯಿ ಮಲಗಿದೆ")], "kannada", {"kn": 0.8, "en": 0.1})
    t = transcribe.Transcriber(server(reply=kannada), root / "m", root / "v", ffmpeg=str(ffmpeg), port=18946)
    t.start()
    try:
        heard = t.transcribe(b"note")
    finally:
        t.stop()
    assert (heard.language, heard.text) == ("kn", "ನಾಯಿ ಮಲಗಿದೆ")
    assert requests_made(root) == ["auto"]


def test_if_the_named_language_hears_nothing_the_first_reading_stands(tools):
    root, ffmpeg, server = tools
    welsh = whisper_json([(0, 900, " Mae hi'n cysgu.")], "welsh", {"cy": 0.9})
    silent = whisper_json([(0, 900, " [BLANK_AUDIO]")], "english", {"cy": 0.9})
    t = transcribe.Transcriber(server(reply=welsh, forced={"en": silent}), root / "m", root / "v",
                               ffmpeg=str(ffmpeg), port=18947)
    t.start()
    try:
        heard = t.transcribe(b"note")
    finally:
        t.stop()
    assert (heard.outcome, heard.language) == ("transcribed", "cy")
    assert requests_made(root) == ["auto", "en"]


def test_silence_is_never_transcribed_twice(tools):
    root, ffmpeg, server = tools
    silent = whisper_json([(0, 30000, " [BLANK_AUDIO]")], "welsh", {"cy": 0.9})
    t = transcribe.Transcriber(server(reply=silent), root / "m", root / "v", ffmpeg=str(ffmpeg), port=18948)
    t.start()
    try:
        assert t.transcribe(b"note").outcome == "no_speech"
    finally:
        t.stop()
    assert requests_made(root) == ["auto"]


# ---------------------------------------------------------------------------
# The language the person speaks their notes in (#171)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("odds,hint,chosen", [
    ({"hi": 0.62, "en": 0.30}, "en", "en"),     # Indian English heard as Hindi: theirs
    ({"kn": 0.91, "en": 0.05}, "en", "kn"),     # very sure of Kannada: Kannada
    ({"kn": 0.79, "en": 0.10}, "en", "en"),     # not quite sure enough
    ({"kn": 0.80}, "en", "kn"),                 # the threshold itself is sure enough
    ({"cy": 0.95}, "ta", "ta"),                 # outside the set: theirs
    ({"ta": 0.40, "en": 0.35}, "ta", "ta"),
])
def test_a_hint_gives_way_only_to_a_very_sure_other_language(odds, hint, chosen):
    assert transcribe.with_hint(whisper_json([(0, 900, " ok")], "hindi", odds), hint) == chosen


def test_with_no_probabilities_the_hint_stands():
    assert transcribe.with_hint(whisper_json([(0, 900, " ok")], "hindi"), "en") == "en"


def _run(tools, port, reply, replies=None, **hinted):
    """`replies` maps a language the worker names to the server's answer for it."""
    root, ffmpeg, server = tools
    t = transcribe.Transcriber(server(reply=reply, forced=replies), root / "m", root / "v", ffmpeg=str(ffmpeg), port=port)
    t.start()
    try:
        return t.transcribe(b"note", **hinted), requests_made(root)
    finally:
        t.stop()


def test_english_heard_as_hindi_is_transcribed_in_english_when_that_is_what_they_speak(tools):
    hindi = whisper_json([(0, 900, " वह सो रही है")], "hindi", {"hi": 0.62, "en": 0.30})
    english = whisper_json([(0, 900, " She is asleep.")], "english", {"hi": 0.62, "en": 0.30})
    heard, made = _run(tools, 18951, hindi, {"en": english}, hint="en")
    assert (heard.language, heard.text, made) == ("en", "She is asleep.", ["auto", "en"])


def test_heard_as_hinted_costs_no_second_pass(tools):
    english = whisper_json([(0, 900, " She is asleep.")], "english", {"en": 0.7, "hi": 0.2})
    heard, made = _run(tools, 18952, english, hint="en")
    assert (heard.language, made) == ("en", ["auto"])


def test_a_note_s_own_language_is_one_pass_in_it(tools):
    kannada = whisper_json([(0, 900, " ನಾಯಿ ಮಲಗಿದೆ")], "kannada", {"kn": 0.3})
    english = whisper_json([(0, 900, " ignored")], "english", {"en": 0.9})
    heard, made = _run(tools, 18953, english, {"kn": kannada}, hint="kn", forced=True)
    assert (heard.language, heard.text, made) == ("kn", "ನಾಯಿ ಮಲಗಿದೆ", ["kn"])


def test_a_hint_it_does_not_know_is_no_hint(tools):
    kannada = whisper_json([(0, 900, " ನಾಯಿ ಮಲಗಿದೆ")], "kannada", {"kn": 0.8})
    heard, made = _run(tools, 18954, kannada, hint="xx", forced=True)
    assert (heard.language, made) == ("kn", ["auto"])


def test_whisper_server_listens_on_loopback_only(tools):
    root, ffmpeg, server = tools
    command = transcribe.Transcriber(server(), root / "m", root / "v").command
    assert command[command.index("--host") + 1] == "127.0.0.1"
    for flag in ("--vad", "-sns"):
        assert flag in command
    assert command[command.index("-mc") + 1] == "0" and command[command.index("-l") + 1] == "auto"
    # Without the pad, VAD clipped "She" off "She is asleep" on the real model.
    assert command[command.index("-vp") + 1] == "200"


def test_a_clip_without_a_sound_track_is_no_audio(tools):
    root, _, server = tools
    ffmpeg = script(root / "ffmpeg-mute", "echo \"Stream map '0:a:0' matches no streams.\" >&2; exit 1\n")
    heard = transcribe.Transcriber(server(), root / "m", root / "v", ffmpeg=str(ffmpeg)).transcribe(b"clip")
    assert heard.outcome == "no_audio"


def test_undecodable_audio_is_a_permanent_failure(tools):
    root, _, server = tools
    ffmpeg = script(root / "ffmpeg-bad", "echo 'Invalid data found' >&2; exit 1\n")
    with pytest.raises(ValueError):
        transcribe.Transcriber(server(), root / "m", root / "v", ffmpeg=str(ffmpeg)).transcribe(b"junk")


def test_a_hung_server_is_killed_and_replaced_on_the_next_note(tools):
    root, ffmpeg, server = tools
    t = transcribe.Transcriber(server("whisper-hung", hang=True), root / "m", root / "v",
                               ffmpeg=str(ffmpeg), request_timeout=.5, port=18942)
    t.start()
    with pytest.raises(TimeoutError):
        t.transcribe(b"note")
    assert not t.alive()
    t.command[0] = str(server())  # a healthy build for the next attempt
    try:
        assert t.transcribe(b"note").outcome == "transcribed"
        assert started(root) == 2
    finally:
        t.stop()


def test_a_server_that_dies_on_startup_is_reported(tools):
    root, ffmpeg, _ = tools
    dead = script(root / "whisper-dead", "exit 3\n")
    with pytest.raises(transcribe.TranscriberError):
        transcribe.Transcriber(dead, root / "m", root / "v", port=18943, startup_timeout=10).start()


# ---------------------------------------------------------------------------
# Worker queue order and wire contract
# ---------------------------------------------------------------------------

class FakeTranscriber:
    def __init__(self):
        self.calls = []
        self.hints = []

    def transcribe(self, raw, hint=None, forced=False):
        self.calls.append(raw)
        self.hints.append((hint, forced))
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


async def test_a_job_s_language_reaches_whisper_and_an_old_job_s_absence_changes_nothing():
    server, transcriber = Server(), FakeTranscriber()
    server.job.update(language_hint="en", language_forced=False)
    async with server.worker(transcriber) as worker:
        assert await worker.run_once()
    assert transcriber.hints == [("en", False)]
    server, transcriber = Server(), FakeTranscriber()
    server.job.update(language_hint="kn", language_forced=True)
    async with server.worker(transcriber) as worker:
        assert await worker.run_once()
    assert transcriber.hints == [("kn", True)]
    server, transcriber = Server(), FakeTranscriber()                        # an API from before #167
    async with server.worker(transcriber) as worker:
        assert await worker.run_once()
    assert transcriber.hints == [(None, False)]
    assert set(server.completions[0]) == {"lease_token", "pipeline_version", "model", "outcome",
                                          "language", "text", "segments"}  # nothing new on the wire


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
