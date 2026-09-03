"""Speech to text for a recording bundle, entirely on this machine.

The output is `transcript.json` beside `capture.json` -- a third file in the bundle,
derived like proxy/ but kept rather than disposable, because regenerating it costs
minutes of CPU where a proxy costs seconds. It is not part of `edit.json`: the editor
renders captions FROM it, and a user who deletes edit.json to reset their edit must not
also lose a ten-minute transcription.

TIMES ARE SOURCE-TIMELINE SECONDS, the same clock `capture.json`'s streams use, where
t=0 is screen frame 0. Two consequences that are easy to get wrong:

* `calibration_c_ms` is NOT applied. It is compositor-to-capture latency -- the gap
  between the compositor stamping an input event and the pixels reaching the encoder --
  and it exists because input events are recorded by a different process than the video.
  Audio arrives inside the same container as the video it is muxed with, so it needs no
  such correction, and adding one would slide every caption 36-45ms late.
* when the audio comes off the CAMERA stream rather than the screen, the camera's own
  anchor offset is added. The two streams start at different times by launch order plus
  per-pipeline warm-up (KMS 128-137ms, V4L2 210-228ms), so a mic on the webcam is
  offset by a fifth of a second against the video -- enough for a caption to appear on
  the wrong sentence.

Seconds, not frame indices, unlike cuts and layers. A transcript is not snapped to
anything: whisper's boundaries fall wherever the speech does, and rounding them to the
capture's frame grid at write time would throw away precision that the caption renderer
can use, for no gain.

TWO ENGINES, both optional at runtime. `faster_whisper` is preferred and is a pip
install into the venv; a whisper.cpp binary on PATH is the pacman route and needs no
Python at all. Neither is a hard dependency, so nothing in this module may import an
engine at module scope -- `omarchy-recordings` imports the whole package to draw a list
of recordings, and a missing optional dependency must not take the library window with
it. Detection is therefore `find_spec`/`which` only, never a trial import: loading
faster_whisper pulls in ctranslate2's native library and costs ~1s, which is far too
much to spend deciding whether to grey out a button.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .project import Bundle

TRANSCRIPT_VERSION = 1

FASTER_WHISPER = "faster-whisper"
WHISPER_CPP = "whisper-cpp"
# Preference order, and the order `available_engine` walks. faster_whisper first
# because it takes a model NAME and downloads it once, where whisper.cpp takes a path to
# a ggml file the user has to have fetched themselves.
ENGINE_ORDER = (FASTER_WHISPER, WHISPER_CPP)

# whisper.cpp renamed its CLI from `main` to `whisper-cli` in late 2024 and Arch ships it
# as `whisper-cpp`; all three are the same program and a machine may have any one of
# them. `main` is checked last because it is a name anything could claim on PATH.
WHISPER_CPP_BINARIES = ("whisper-cli", "whisper-cpp", "main")

# whisper.cpp cannot download a model, so it has to be found on disk. These are where
# the Arch package and the upstream `models/download-ggml-model.sh` put them.
WHISPER_CPP_MODEL_DIRS = (
    "/usr/share/whisper.cpp/models",
    "/usr/share/whisper.cpp",
    "~/.local/share/whisper.cpp/models",
    "~/.cache/whisper.cpp",
)

SAMPLE_RATE = 16000

INSTALL_HELP = (
    "install one of them:\n"
    "  .venv/bin/pip install faster-whisper     (preferred; downloads its own model)\n"
    "  sudo pacman -S whisper-cpp               (then fetch a ggml-*.bin model)"
)

# whisper.cpp emits this for stretches it decided were silence. It is a marker, not
# speech, and a caption layer that renders it puts "[BLANK_AUDIO]" on screen.
NON_SPEECH = {"[blank_audio]", "(silence)", "[silence]", "[ music ]", "[music]"}


class TranscribeError(RuntimeError):
    pass


# --- the transcript ---------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One utterance. `start`/`end` are seconds on the source timeline."""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(float(d["start"]), float(d["end"]), str(d["text"]))


@dataclass
class Transcript:
    """What `transcribe_bundle` writes. Read-only downstream."""

    version: int = TRANSCRIPT_VERSION
    engine: str = ""
    model: str = ""
    # As the engine detected it, or as the caller forced it. Empty when the engine
    # declined to guess, which is a real outcome on a recording with no speech.
    language: str = ""
    # Which capture stream the audio was taken from, and the offset that was applied to
    # put it on the source timeline. Recorded because a caption track that is uniformly
    # late is otherwise indistinguishable from a bad model, and this says which.
    source_stream: str = "screen"
    offset_s: float = 0.0
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "engine": self.engine,
            "model": self.model,
            "language": self.language,
            "source_stream": self.source_stream,
            "offset_s": self.offset_s,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        v = int(d.get("version", 0))
        if v > TRANSCRIPT_VERSION:
            raise TranscribeError(
                f"transcript.json is version {v}; this build understands "
                f"{TRANSCRIPT_VERSION}. Upgrade omarchy-studio rather than editing it."
            )
        return cls(
            version=TRANSCRIPT_VERSION,
            engine=str(d.get("engine", "")),
            model=str(d.get("model", "")),
            language=str(d.get("language", "")),
            source_stream=str(d.get("source_stream", "screen")),
            offset_s=float(d.get("offset_s", 0.0)),
            segments=[Segment.from_dict(s) for s in d.get("segments", [])],
        )


def transcript_path(bundle: Bundle) -> Path:
    return bundle.root / "transcript.json"


def load(bundle: Bundle) -> Transcript | None:
    """The bundle's transcript, or None if it has never been transcribed.

    A corrupt file reads as None rather than raising: transcript.json is derived state,
    and the cure for an unparseable one is to run the transcription again, not to make
    the recording un-openable.
    """
    p = transcript_path(bundle)
    if not p.exists():
        return None
    try:
        return Transcript.from_dict(json.loads(p.read_text()))
    except (OSError, ValueError):
        return None


def save(bundle: Bundle, transcript: Transcript) -> Path:
    """Atomic replace, for the same reason `Bundle.save_edit` is: a transcription
    interrupted at the write is a killed process, and a half-written file that parses
    as a short transcript would silently truncate every caption after it."""
    p = transcript_path(bundle)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(transcript.to_dict(), indent=2) + "\n")
    tmp.replace(p)
    return p


# --- engine detection -------------------------------------------------------


def have_faster_whisper() -> bool:
    """True when the package is importable, WITHOUT importing it.

    `find_spec` only walks the path finders; importing faster_whisper loads ctranslate2's
    native extension, which is ~1s and several hundred MB of RSS. That is affordable
    once per transcription and not affordable to decide the enabled state of a button.
    """
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        # A broken half-installed distribution raises out of the finder rather than
        # returning None, and "no engine" is the honest answer to that.
        return False


def whisper_cpp_binary() -> str | None:
    for name in WHISPER_CPP_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    return None


def available_engines() -> list[str]:
    """Every engine this machine could run, in preference order."""
    found = []
    if have_faster_whisper():
        found.append(FASTER_WHISPER)
    if whisper_cpp_binary():
        found.append(WHISPER_CPP)
    return found


def available_engine(preferred: str = "auto") -> str | None:
    """Which engine `transcribe_bundle` would use, or None if it would fail.

    This is the query the UI asks before drawing the button: transcription is a
    minutes-long job started by a click, and discovering there is no engine after the
    click means a progress dialog that immediately becomes an error dialog.

    Deliberately returns None rather than raising -- a caller that WANTS the actionable
    message calls `require_engine`.
    """
    engines = available_engines()
    if preferred in ("auto", "", None):
        return engines[0] if engines else None
    if preferred not in ENGINE_ORDER:
        return None
    return preferred if preferred in engines else None


def require_engine(preferred: str = "auto") -> str:
    engine = available_engine(preferred)
    if engine:
        return engine
    if preferred not in ("auto", "", None) and preferred not in ENGINE_ORDER:
        raise TranscribeError(
            f"unknown engine {preferred!r}; expected auto, "
            f"{FASTER_WHISPER} or {WHISPER_CPP}"
        )
    if preferred in ("auto", "", None):
        raise TranscribeError("no local speech-to-text engine found; " + INSTALL_HELP)
    raise TranscribeError(
        f"engine {preferred!r} was requested but is not installed; " + INSTALL_HELP
    )


# --- audio ------------------------------------------------------------------


def audio_source(bundle: Bundle) -> tuple[str, Path, float]:
    """(stream name, media path, source-timeline offset in seconds) for the audio.

    `Stream.has_audio` is the authority rather than a fresh ffprobe: it was written at
    finalize by probing the file, media/ is immutable afterwards, and asking again costs
    a subprocess per call in a UI that wants to know on every list refresh.

    The screen wins when both have audio -- gsr records the chosen audio device into it,
    so a camera track present alongside is the webcam's own mic picking up the same room
    a second time.
    """
    fps = None
    for name in ("screen", "camera"):
        stream = getattr(bundle.capture, name, None)
        if stream is None:
            continue
        if name == "screen":
            fps = stream.fps_num / stream.fps_den
        if not stream.has_audio:
            continue
        path = bundle.media(Path(stream.path).name)
        if not path.exists():
            raise TranscribeError(f"{path} is missing; the bundle's media is incomplete")
        offset = 0.0
        if name == "camera" and fps:
            offset = bundle.camera_offset_frames() / fps
        return name, path, round(offset, 3)

    raise TranscribeError(
        f"{bundle.root.name} has no audio track, so there is nothing to transcribe. "
        "Record with an audio source selected (omarchy-capture-setup) to get captions."
    )


def extract_command(src: Path, dest: Path) -> list[str]:
    """The ffmpeg call that turns captured media into engine-ready audio.

    16 kHz mono signed 16-bit PCM is not a preference, it is what both engines want:
    whisper's encoder is trained on 16 kHz mono and every engine resamples to it
    internally. Doing the conversion once here means the model reads the samples
    verbatim instead of resampling a 48 kHz stereo AAC track a second time.

    `-vn` before `-map`: without it ffmpeg still opens and decodes the video stream to
    decide it is unwanted, which on a 5K master is most of the wall time of the extract.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src),
        "-vn",
        "-map", "0:a:0",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        str(dest),
    ]


def extract_audio(src: Path, dest: Path) -> Path:
    cmd = extract_command(src, dest)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise TranscribeError("ffmpeg not found; install ffmpeg") from e
    if r.returncode != 0:
        raise TranscribeError(f"audio extraction failed:\n{r.stderr.strip()[-2000:]}")
    if not dest.exists() or dest.stat().st_size == 0:
        raise TranscribeError(f"audio extraction produced nothing from {src}")
    return dest


# --- segment normalization --------------------------------------------------


def normalize_segments(
    raw: Iterable[tuple[float, float, str]], offset: float = 0.0
) -> list[Segment]:
    """Engine output -> the schema, with the offset onto the source timeline applied.

    Both engines are loose in the same three ways, and all three reach the caption
    renderer as a visible defect if they are not fixed here:

    * leading and trailing spaces on every segment (whisper's tokenizer keeps the space
      that precedes a word), which a centred caption renders as an off-centre one;
    * non-speech markers like [BLANK_AUDIO], which are annotations and not something
      anybody said;
    * a segment whose end precedes its start, which whisper.cpp emits on a hallucinated
      tail past the end of the audio and which becomes a caption of negative duration.

    Times are rounded to milliseconds. The models quantize to 20ms frames internally, so
    the digits past that are noise, and three decimals keeps the JSON readable.
    """
    out: list[Segment] = []
    for start, end, text in raw:
        text = " ".join(str(text).split())
        if not text or text.lower() in NON_SPEECH:
            continue
        start = max(0.0, float(start) + offset)
        end = max(float(end) + offset, start)
        out.append(Segment(round(start, 3), round(end, 3), text))
    # Sorted because a caption renderer walks them in order and whisper.cpp's -oj output
    # is not guaranteed monotonic across its chunk boundaries.
    out.sort(key=lambda s: (s.start, s.end))
    return out


# --- the engines ------------------------------------------------------------

# One shape for both, and the single seam the tests replace: given a wav, a model name
# and an optional language, return the detected language and raw (start, end, text)
# triples in the engine's own timeline. Everything either side of this -- extraction,
# normalization, the offset, the file write -- is exercised for real.
Runner = Callable[[Path, str, "str | None"], "tuple[str, list[tuple[float, float, str]]]"]


def run_faster_whisper(
    wav: Path, model: str, language: str | None
) -> tuple[str, list[tuple[float, float, str]]]:
    from faster_whisper import WhisperModel  # local: optional dependency, ~1s to import

    # CPU int8 rather than device="auto". CUDA needs the cudnn/cublas wheels, which are
    # not pulled in by `pip install faster-whisper` and are not an Arch dependency
    # either, so an auto that finds a GPU fails at model load with a missing .so -- a
    # worse outcome than a transcription that merely takes longer. int8 over float32 is
    # roughly 4x on this class of CPU for no audible difference in the words.
    m = WhisperModel(model, device="cpu", compute_type="int8")
    segments, info = m.transcribe(
        str(wav),
        language=language,
        # Whisper hallucinates confidently over silence -- repeated phrases, subtitle
        # credits it saw in training. The VAD gate is what keeps a quiet screen
        # recording from acquiring a monologue.
        vad_filter=True,
    )
    # `segments` is a generator: the transcription does not actually run until it is
    # consumed, and `info.language` is populated by then either way.
    rows = [(s.start, s.end, s.text) for s in segments]
    return (language or getattr(info, "language", "") or ""), rows


def _whisper_cpp_model(model: str) -> str:
    """Resolve a model name to a ggml file, since whisper.cpp cannot fetch one."""
    p = Path(model).expanduser()
    if p.suffix == ".bin" or p.exists():
        if not p.exists():
            raise TranscribeError(f"whisper.cpp model {p} does not exist")
        return str(p)
    for d in WHISPER_CPP_MODEL_DIRS:
        candidate = Path(d).expanduser() / f"ggml-{model}.bin"
        if candidate.exists():
            return str(candidate)
    raise TranscribeError(
        f"no whisper.cpp model for {model!r}. It cannot download one; fetch it with\n"
        f"  whisper.cpp/models/download-ggml-model.sh {model}\n"
        f"and put ggml-{model}.bin in one of "
        f"{', '.join(WHISPER_CPP_MODEL_DIRS)}, or pass --model with a full path."
    )


def run_whisper_cpp(
    wav: Path, model: str, language: str | None
) -> tuple[str, list[tuple[float, float, str]]]:
    binary = whisper_cpp_binary()
    if not binary:
        raise TranscribeError("whisper.cpp binary vanished from PATH mid-run")

    # -oj writes <prefix>.json, which is the only machine-readable output it has; the
    # stdout transcript carries timestamps as "00:00:01,240" strings that would have to
    # be parsed back, where the JSON carries integer millisecond offsets.
    prefix = wav.with_suffix("")
    out_json = Path(f"{prefix}.json")
    cmd = [
        binary,
        "-m", _whisper_cpp_model(model),
        "-f", str(wav),
        "-oj", "-of", str(prefix),
        # No progress spam on stderr; the caller reports progress itself.
        "-np",
    ]
    if language:
        cmd += ["-l", language]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise TranscribeError(f"{binary} disappeared") from e
    if r.returncode != 0:
        raise TranscribeError(f"whisper.cpp failed:\n{r.stderr.strip()[-2000:]}")
    if not out_json.exists():
        raise TranscribeError(
            f"whisper.cpp wrote no JSON at {out_json}; the build may predate -oj"
        )
    try:
        data = json.loads(out_json.read_text())
    finally:
        out_json.unlink(missing_ok=True)

    rows = []
    for seg in data.get("transcription", []):
        # `offsets` is milliseconds; `timestamps` is the same values as display strings.
        off = seg.get("offsets") or {}
        rows.append(
            (float(off.get("from", 0)) / 1000.0, float(off.get("to", 0)) / 1000.0,
             seg.get("text", ""))
        )
    detected = str((data.get("result") or {}).get("language", "") or "")
    return (language or detected), rows


_RUNNERS: dict[str, Runner] = {
    FASTER_WHISPER: run_faster_whisper,
    WHISPER_CPP: run_whisper_cpp,
}


# --- the job ----------------------------------------------------------------


def transcribe_bundle(
    bundle: Bundle,
    *,
    model: str = "small",
    language: str | None = None,
    engine: str = "auto",
    force: bool = False,
    runner: Runner | None = None,
    progress: Callable[[str], None] | None = None,
) -> Transcript:
    """Transcribe the bundle's audio and write `transcript.json`. Returns the object.

    Without `force`, an existing transcript is returned untouched and no engine runs.
    That is the common case in a UI that offers "transcribe" on every recording: the job
    costs minutes, and a second click must not silently spend them again -- or, worse,
    replace a transcript the user has already had the editor build captions from.
    """
    say = progress or (lambda _msg: None)

    existing = load(bundle)
    if existing is not None and not force:
        say(f"transcript.json already exists ({len(existing.segments)} segments)")
        return existing

    chosen = require_engine(engine)
    run = runner if runner is not None else _RUNNERS[chosen]

    stream, media, offset = audio_source(bundle)
    say(f"engine {chosen}, model {model}, audio from {stream}")

    # A named temp directory rather than a temp file, because whisper.cpp writes its
    # JSON next to the wav and both have to be cleaned up. Deleted on every path,
    # including the failure ones: a 10-minute recording is ~19MB of PCM and leaving it
    # behind in /tmp on each failed attempt fills a tmpfs quickly.
    with tempfile.TemporaryDirectory(prefix="omarchy-studio-stt-") as tmp:
        wav = Path(tmp) / "audio.wav"
        say(f"extracting {SAMPLE_RATE // 1000} kHz mono audio")
        extract_audio(media, wav)
        say("transcribing (this runs entirely locally and can take minutes)")
        detected, rows = run(wav, model, language)

    transcript = Transcript(
        engine=chosen,
        model=model,
        language=detected or "",
        source_stream=stream,
        offset_s=offset,
        segments=normalize_segments(rows, offset),
    )
    save(bundle, transcript)
    say(f"wrote transcript.json ({len(transcript.segments)} segments)")
    return transcript
