"""whisper.cpp kept resident on the GPU: sound in, transcript out.

``Transcriber(binary, model, vad_model).start()`` launches ``whisper-server``
on loopback in its own process group and waits for it to answer /health, so
large-v3 is loaded onto the slice once, at worker startup, like the ONNX
models and the local Qwen. ``transcribe(raw)`` then decodes the stored bytes
of a voice note or a clip to 16 kHz mono PCM with ffmpeg (a bounded child, as
before) and posts the WAV to it. No DB, object store or outside network; the
caller owns leases and retries.

A server that dies or hangs is not trusted again: a hung request kills it,
and the next transcription starts a fresh one, which costs one model load.

Flags, each for a measured reason on real field recordings:
- ``-l auto``: observers speak Kannada, Hindi and Tamil as well as English.
  Forcing English garbles those; the detected language is returned instead.
  But only from the languages people here speak (``ALLOWED``): auto-detection
  also heard English in a video as some other language entirely, and then
  wrote the note out in it. ``choose_language`` reads the probabilities
  whisper-server already returns and, when the detection is outside that
  set, transcribes once more with the language named: the most likely one in
  the set, or English when none of them is likely (``FALLBACK_MIN``).
  A job may also carry a language hint (#171), from what the person said they
  speak their notes in: forced, it is the language, in one pass; otherwise it
  is transcribed in unless whisper is very sure (``HINT_OVERRIDE_MIN``) of
  another language in the set. Indian English heard as Hindi is the case it
  catches. Jobs without one behave exactly as before.
- ``--vad``: without it large-v3 invents repeated lines over silent windows.
- ``-vp 200``: pad each detected speech span by 200 ms. whisper-server's
  default pad clipped the first word whenever speech began at the very start
  of a recording ("She is asleep" came back as "is asleep" on the H200), and
  that first word is often the pronoun a sex suggestion rests on.
- ``-mc 0``: even with VAD, carried-over context produced repetition loops;
  dropping it removed them and cost nothing in coverage.
- ``-sns``: suppress non-speech tokens ("(music)", "[BLANK_AUDIO]").
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

import httpx

# Bracketed or parenthesised on its own: a sound description, not speech.
_NON_SPEECH = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}$")
# whisper-server names languages in full ("kannada"); transcripts store the
# code. Generated from whisper.cpp's own table (src/whisper.cpp, g_lang).
_CODES = {"english": "en", "chinese": "zh", "german": "de", "spanish": "es", "russian": "ru", "korean": "ko", "french": "fr", "japanese": "ja", "portuguese": "pt", "turkish": "tr", "polish": "pl", "catalan": "ca", "dutch": "nl", "arabic": "ar", "swedish": "sv", "italian": "it", "indonesian": "id", "hindi": "hi", "finnish": "fi", "vietnamese": "vi", "hebrew": "he", "ukrainian": "uk", "greek": "el", "malay": "ms", "czech": "cs", "romanian": "ro", "danish": "da", "hungarian": "hu", "tamil": "ta", "norwegian": "no", "thai": "th", "urdu": "ur", "croatian": "hr", "bulgarian": "bg", "lithuanian": "lt", "latin": "la", "maori": "mi", "malayalam": "ml", "welsh": "cy", "slovak": "sk", "telugu": "te", "persian": "fa", "latvian": "lv", "bengali": "bn", "serbian": "sr", "azerbaijani": "az", "slovenian": "sl", "kannada": "kn", "estonian": "et", "macedonian": "mk", "breton": "br", "basque": "eu", "icelandic": "is", "armenian": "hy", "nepali": "ne", "mongolian": "mn", "bosnian": "bs", "kazakh": "kk", "albanian": "sq", "swahili": "sw", "galician": "gl", "marathi": "mr", "punjabi": "pa", "sinhala": "si", "khmer": "km", "shona": "sn", "yoruba": "yo", "somali": "so", "afrikaans": "af", "occitan": "oc", "georgian": "ka", "belarusian": "be", "tajik": "tg", "sindhi": "sd", "gujarati": "gu", "amharic": "am", "yiddish": "yi", "lao": "lo", "uzbek": "uz", "faroese": "fo", "haitian creole": "ht", "pashto": "ps", "turkmen": "tk", "nynorsk": "nn", "maltese": "mt", "sanskrit": "sa", "luxembourgish": "lb", "myanmar": "my", "tibetan": "bo", "tagalog": "tl", "malagasy": "mg", "assamese": "as", "tatar": "tt", "hawaiian": "haw", "lingala": "ln", "hausa": "ha", "bashkir": "ba", "javanese": "jw", "sundanese": "su", "cantonese": "yue"}
# English and the Indian languages whisper knows (it has no Odia). Kept in
# step with app/suggestions.EXPECTED_LANGUAGES by hand: this module must not
# import the API.
ALLOWED = ("en", "hi", "kn", "ta", "te", "ml", "mr", "bn", "gu", "pa", "ur")
# Below this, the likeliest allowed language is a guess too; English is the
# safer one (it is what most notes are in, and what a misheard one was).
FALLBACK_MIN = 0.2
# A hinted language gives way only to another allowed one whisper is this
# sure of: Indian English heard as Hindi scores well below it.
HINT_OVERRIDE_MIN = 0.8
logger = logging.getLogger(__name__)
# Never hand the worker's API token to a child process.
_SECRET_ENV = ("MEDIA_GPU_TOKEN",)
# As in clip.py: an uploaded file must not make FFmpeg open anything but
# itself (a playlist or concat file could otherwise fetch URLs from the
# worker's network), nor be read by a demuxer outside what phones produce.
# "mov" covers MP4/M4A; the rest are WebM/Opus, Ogg, ADTS AAC, MP3 and WAV.
_FFMPEG_INPUT = ("-protocol_whitelist", "file",
                 "-format_whitelist", "mov,matroska,webm,ogg,aac,mp3,wav")


@dataclass(frozen=True)
class Transcript:
    outcome: str  # "transcribed" | "no_speech" | "no_audio"
    language: str | None = None
    text: str = ""
    segments: list[dict] = field(default_factory=list)


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(command: list[str], timeout: float, env: dict[str, str], cwd: Path) -> tuple[int, str]:
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, env=env, cwd=cwd, close_fds=True,
                               start_new_session=True)
    try:
        _, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Its own process group, so a hung CUDA child dies with everything it
        # spawned rather than outliving the job.
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise TimeoutError("transcription child exceeded its time limit") from None
    # Only the tail: enough to classify, never a whole log in memory.
    return process.returncode, stderr[-4096:].decode("utf-8", "replace")


class TranscriberError(RuntimeError):
    """The whisper server failed to start, exited, or answered off-contract."""


class Transcriber:
    def __init__(self, binary: Path, model: Path, vad_model: Path, *, ffmpeg: str | None = None,
                 threads: int = 4, max_seconds: int = 600, decode_timeout: float = 60,
                 request_timeout: float = 240, startup_timeout: float = 180, port: int = 8092,
                 pass_fds: tuple[int, ...] = ()):
        self.command = [
            str(binary), "-m", str(model), "-l", "auto",
            "--vad", "-vm", str(vad_model), "-vp", "200", "-mc", "0", "-sns",
            "-t", str(threads), "--host", "127.0.0.1", "--port", str(port),
        ]
        self.url = f"http://127.0.0.1:{port}"
        self.ffmpeg = ffmpeg
        self.max_seconds = max_seconds
        self.decode_timeout, self.request_timeout = decode_timeout, request_timeout
        self.startup_timeout = startup_timeout
        self.pass_fds = pass_fds
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        # It needs the GPU: it keeps the CUDA/NVIDIA variables that select the
        # MIG slice, minus anything secret.
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, start_new_session=True, close_fds=True,
            pass_fds=self.pass_fds)
        deadline = time.monotonic() + self.startup_timeout
        with httpx.Client(trust_env=False, timeout=5) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise TranscriberError("whisper server exited during startup")
                try:
                    if client.get(self.url + "/health").status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.5)
        self.stop()
        raise TranscriberError("whisper server did not become ready")

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()

    def transcribe(self, raw: bytes, hint: str | None = None, forced: bool = False) -> Transcript:
        """`hint` is the language the person speaks their notes in, `forced` if
        they chose it for this one note (see the module docstring)."""
        if hint not in ALLOWED:
            hint, forced = None, False
        if not isinstance(raw, bytes) or not raw:
            raise ValueError("empty audio")
        with tempfile.TemporaryDirectory(prefix="gpu-audio-") as directory:
            root = Path(directory)
            source, wav = root / "input", root / "audio.wav"
            source.write_bytes(raw)
            # The decoder needs nothing from the worker: no CUDA, no credentials.
            decode_env = {"PATH": os.defpath, "HOME": directory, "TMPDIR": directory,
                          "CUDA_VISIBLE_DEVICES": ""}
            code, stderr = _run([
                self.ffmpeg or _ffmpeg(), "-nostdin", "-v", "error", "-threads", "1",
                *_FFMPEG_INPUT, "-i", str(source),
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                "-t", str(self.max_seconds), "-y", str(wav),
            ], self.decode_timeout, decode_env, root)
            if code != 0:
                if "matches no streams" in stderr:
                    return Transcript("no_audio")
                raise ValueError("audio decoding failed")
            # A 44-byte WAV is a header and no samples.
            if not wav.exists() or wav.stat().st_size <= 44:
                return Transcript("no_speech")
            if not self.alive():
                self.start()
            if forced:
                return parse(self._infer(wav, hint))
            raw = self._infer(wav)
            heard = parse(raw)
            if heard.outcome != "transcribed":
                return heard
            chosen = choose_language(raw) if hint is None else with_hint(raw, hint)
            if chosen is None or chosen == heard.language:
                return heard
            # Codes and probabilities only, never what was said.
            logger.warning("whisper language %s replaced by %s (%s)", heard.language, chosen,
                           _odds(raw, heard.language, chosen))
            again = parse(self._infer(wav, chosen))
            if hint is not None:
                # The person's language, or one whisper was sure of: what it
                # hears in that stands, even nothing. Falling back to the first
                # reading would bring back the language the hint just overruled.
                return again
            # Named, it may find no words where auto-detection found some; the
            # first reading is then all there is.
            return again if again.outcome == "transcribed" else heard

    def _infer(self, wav: Path, language: str | None = None) -> bytes:
        data = {"response_format": "verbose_json", "temperature": "0.0"}
        if language is not None:
            data["language"] = language
        try:
            with httpx.Client(trust_env=False, timeout=self.request_timeout) as client, wav.open("rb") as audio:
                response = client.post(self.url + "/inference",
                                       files={"file": ("audio.wav", audio, "audio/wav")}, data=data)
        except httpx.TimeoutException:
            # A hung request means a server not to trust with the next one.
            self.stop()
            raise TimeoutError("whisper server exceeded its time limit") from None
        except httpx.TransportError:
            self.stop()
            raise TranscriberError("whisper server unreachable") from None
        if response.status_code != 200:
            raise TranscriberError("whisper server refused the audio")
        return response.content


def _probabilities(raw: bytes) -> dict[str, float] | None:
    """whisper-server's ``language_probabilities`` (code to probability,
    negligible ones left out), or None when absent or not that shape."""
    try:
        found = json.loads(raw.decode("utf-8", "replace")).get("language_probabilities")
    except (ValueError, AttributeError):
        return None
    if not isinstance(found, dict) or not found:
        return None
    odds = {}
    for code, p in found.items():
        if not isinstance(code, str) or isinstance(p, bool) or not isinstance(p, (int, float)):
            return None
        if not math.isfinite(p) or not 0 <= p <= 1:
            return None
        odds[code] = float(p)
    return odds


def choose_language(raw: bytes) -> str | None:
    """The language to transcribe in: the likeliest of ``ALLOWED``, or English
    when even that one is below ``FALLBACK_MIN``. None when the reply carries
    no probabilities (an older server): then the detection stands, as before.
    """
    odds = _probabilities(raw)
    if odds is None:
        return None
    best = max(ALLOWED, key=lambda code: odds.get(code, 0.0))
    return best if odds.get(best, 0.0) >= FALLBACK_MIN else "en"


def with_hint(raw: bytes, hint: str) -> str:
    """The language to transcribe in when the person speaks `hint`: theirs,
    unless whisper is at least ``HINT_OVERRIDE_MIN`` sure of another allowed
    language. With no probabilities to go on (an older server), theirs."""
    odds = _probabilities(raw)
    if odds is None:
        return hint
    best = max(ALLOWED, key=lambda code: odds.get(code, 0.0))
    return best if best != hint and odds.get(best, 0.0) >= HINT_OVERRIDE_MIN else hint


def _odds(raw: bytes, *codes: str) -> str:
    odds = _probabilities(raw) or {}
    return ", ".join(f"{code}={odds.get(code, 0.0):.2f}" for code in codes)


def parse(raw: bytes) -> Transcript:
    """whisper-server's ``verbose_json`` reply to a Transcript. Strict about shape."""
    document = json.loads(raw.decode("utf-8", "replace"))
    name = document["language"]
    segments = []
    for item in document["segments"]:
        text = str(item["text"]).strip()
        start, end = round(float(item["start"]) * 1000), round(float(item["end"]) * 1000)
        if not text or _NON_SPEECH.match(text):
            continue
        if not 0 <= start <= end:
            raise ValueError("segment offsets out of order")
        segments.append({"start_ms": start, "end_ms": end, "text": text})
    if not segments:
        # Whisper still names a language for silence; it means nothing here.
        return Transcript("no_speech")
    language = _CODES.get(name) if isinstance(name, str) else None
    if language is None or not _LANGUAGE.match(language):
        raise ValueError("unexpected language")
    return Transcript("transcribed", language, " ".join(s["text"] for s in segments), segments)
