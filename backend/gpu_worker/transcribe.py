"""whisper.cpp in bounded child processes: sound in, transcript out.

``Transcriber(binary, model, vad_model).transcribe(raw)`` takes the stored
bytes of a voice note or a clip, decodes the first audio stream to 16 kHz mono
PCM with ffmpeg, and runs ``whisper-cli`` on it. No DB, object store or
network; the caller owns leases and retries.

Flags, each for a measured reason on real field recordings:
- ``-l auto``: observers speak Kannada, Hindi and Tamil as well as English.
  Forcing English garbles those; the detected language is returned instead.
- ``--vad``: without it large-v3 invents repeated lines over silent windows.
- ``-mc 0``: even with VAD, carried-over context produced repetition loops;
  dropping it removed them and cost nothing in coverage.
- ``-sns``: suppress non-speech tokens ("(music)", "[BLANK_AUDIO]").
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

# Bracketed or parenthesised on its own: a sound description, not speech.
_NON_SPEECH = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}$")
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


class Transcriber:
    def __init__(self, binary: Path, model: Path, vad_model: Path, *, ffmpeg: str | None = None,
                 threads: int = 4, max_seconds: int = 600, decode_timeout: float = 60,
                 whisper_timeout: float = 240):
        self.binary, self.model, self.vad_model = str(binary), str(model), str(vad_model)
        self.ffmpeg = ffmpeg
        self.threads, self.max_seconds = threads, max_seconds
        self.decode_timeout, self.whisper_timeout = decode_timeout, whisper_timeout

    def transcribe(self, raw: bytes) -> Transcript:
        if not isinstance(raw, bytes) or not raw:
            raise ValueError("empty audio")
        with tempfile.TemporaryDirectory(prefix="gpu-audio-") as directory:
            root = Path(directory)
            source, wav, out = root / "input", root / "audio.wav", root / "out"
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
            # whisper-cli does need the GPU: it keeps the CUDA/NVIDIA variables
            # that select the MIG slice, minus anything secret.
            whisper_env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
            whisper_env.update(HOME=directory, TMPDIR=directory)
            code, _ = _run([
                self.binary, "-m", self.model, "-f", str(wav), "-l", "auto",
                "--vad", "-vm", self.vad_model, "-mc", "0", "-sns",
                "-t", str(self.threads), "-np", "-oj", "-of", str(out),
            ], self.whisper_timeout, whisper_env, root)
            if code != 0:
                raise RuntimeError("whisper failed")
            return parse(out.with_suffix(".json").read_bytes())


def parse(raw: bytes) -> Transcript:
    """whisper-cli's ``-oj`` output to a Transcript. Strict about shape."""
    document = json.loads(raw.decode("utf-8", "replace"))
    language = document["result"]["language"]
    segments = []
    for item in document["transcription"]:
        text = item["text"].strip()
        start, end = int(item["offsets"]["from"]), int(item["offsets"]["to"])
        if not text or _NON_SPEECH.match(text):
            continue
        if not 0 <= start <= end:
            raise ValueError("segment offsets out of order")
        segments.append({"start_ms": start, "end_ms": end, "text": text})
    if not segments:
        # Whisper still names a language for silence; it means nothing here.
        return Transcript("no_speech")
    if not isinstance(language, str) or not _LANGUAGE.match(language):
        raise ValueError("unexpected language code")
    return Transcript("transcribed", language, " ".join(s["text"] for s in segments), segments)
