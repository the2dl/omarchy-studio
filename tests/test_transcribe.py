"""Transcription, with the engine seam stubbed.

No test here may download a model or run a real engine: `small` is ~460MB over the
network and a minute of CPU per minute of audio, which is neither offline nor a unit
test. `transcribe_bundle` takes a `runner`, and that is the ONLY thing these substitute
-- the ffmpeg extraction, the offset onto the source timeline, the normalization and the
JSON write are all exercised for real against a synthetic bundle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import needs_ffmpeg

from omarchy_studio import transcribe
from omarchy_studio.project import Bundle
from omarchy_studio.transcribe import Segment, Transcript, TranscribeError

SECONDS = 2.0

# What an engine hands back: its own timeline, its own spacing, its own markers.
FAKE_ROWS = [
    (0.0, 0.8, " hello there"),
    (0.9, 1.6, " second segment "),
]


def fake_runner(rows=FAKE_ROWS, language="en", calls=None):
    def run(wav, model, lang):
        assert Path(wav).exists(), "the runner must be handed a wav that exists"
        if calls is not None:
            calls.append((Path(wav).name, model, lang))
        return (lang or language), list(rows)

    return run


@pytest.fixture
def bundle(tmp_path) -> Bundle:
    root = tmp_path / "rec"
    synthetic.make_bundle(root, seconds=SECONDS, width=320, height=240, camera=False)
    return Bundle(root)


@pytest.fixture
def silent_bundle(tmp_path) -> Bundle:
    """A bundle whose capture declares no audio on any stream."""
    root = tmp_path / "silent"
    synthetic.make_bundle(root, seconds=SECONDS, width=320, height=240,
                          camera=False, media=False)
    cap = json.loads((root / "capture.json").read_text())
    cap["screen"]["has_audio"] = False
    (root / "capture.json").write_text(json.dumps(cap))
    return Bundle(root)


# --- engine detection -------------------------------------------------------


def test_faster_whisper_is_preferred_when_both_are_present(monkeypatch):
    monkeypatch.setattr(transcribe, "have_faster_whisper", lambda: True)
    monkeypatch.setattr(transcribe, "whisper_cpp_binary", lambda: "/usr/bin/whisper-cli")
    assert transcribe.available_engines() == [transcribe.FASTER_WHISPER,
                                              transcribe.WHISPER_CPP]
    assert transcribe.available_engine() == transcribe.FASTER_WHISPER


def test_whisper_cpp_is_used_when_the_python_package_is_absent(monkeypatch):
    monkeypatch.setattr(transcribe, "have_faster_whisper", lambda: False)
    monkeypatch.setattr(transcribe, "whisper_cpp_binary", lambda: "/usr/bin/whisper-cpp")
    assert transcribe.available_engine() == transcribe.WHISPER_CPP


def test_detection_reports_nothing_rather_than_raising(monkeypatch):
    """The UI greys out a button from this; it must not have to catch to ask."""
    monkeypatch.setattr(transcribe, "have_faster_whisper", lambda: False)
    monkeypatch.setattr(transcribe, "whisper_cpp_binary", lambda: None)
    assert transcribe.available_engines() == []
    assert transcribe.available_engine() is None


def test_with_no_engine_the_error_names_both_install_routes(monkeypatch):
    monkeypatch.setattr(transcribe, "have_faster_whisper", lambda: False)
    monkeypatch.setattr(transcribe, "whisper_cpp_binary", lambda: None)
    with pytest.raises(TranscribeError) as e:
        transcribe.require_engine()
    msg = str(e.value)
    assert "pip install faster-whisper" in msg
    assert "pacman -S whisper-cpp" in msg


def test_forcing_an_absent_engine_says_so_rather_than_falling_back(monkeypatch):
    """Silently using the other engine would change the model and the timings under a
    user who asked for a specific one."""
    monkeypatch.setattr(transcribe, "have_faster_whisper", lambda: True)
    monkeypatch.setattr(transcribe, "whisper_cpp_binary", lambda: None)
    assert transcribe.available_engine(transcribe.WHISPER_CPP) is None
    with pytest.raises(TranscribeError, match="not installed"):
        transcribe.require_engine(transcribe.WHISPER_CPP)


def test_detection_never_imports_an_engine(monkeypatch):
    """`have_faster_whisper` must stay a path-finder lookup: importing the package
    pulls ctranslate2's native library in, which is ~1s and hundreds of MB of RSS to
    answer a question the UI asks on every list refresh."""
    import builtins

    real_import = builtins.__import__

    def guard(name, *a, **kw):
        assert not name.startswith("faster_whisper"), "detection imported the engine"
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", guard)
    transcribe.available_engines()


def test_whisper_cpp_is_found_under_any_of_its_three_names(monkeypatch):
    for name in transcribe.WHISPER_CPP_BINARIES:
        monkeypatch.setattr(
            transcribe.shutil, "which", lambda n, _want=name: f"/usr/bin/{n}" if n == _want else None
        )
        assert transcribe.whisper_cpp_binary() == f"/usr/bin/{name}"


# --- audio extraction -------------------------------------------------------


def test_the_extract_asks_ffmpeg_for_16k_mono_pcm():
    """Both engines resample to 16 kHz mono internally; handing them anything else makes
    them do it a second time."""
    cmd = transcribe.extract_command(Path("/in/screen.mp4"), Path("/tmp/a.wav"))
    assert cmd[0] == "ffmpeg"
    assert "-vn" in cmd
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-c:a") + 1] == "pcm_s16le"
    assert cmd[cmd.index("-map") + 1] == "0:a:0"
    assert cmd[cmd.index("-i") + 1] == "/in/screen.mp4"
    assert cmd[-1] == "/tmp/a.wav"
    # -vn has to precede the input selection or ffmpeg decodes the video to discard it.
    assert cmd.index("-vn") < cmd.index("-map")


def test_the_screen_stream_is_the_audio_source_when_it_has_audio(bundle):
    stream, path, offset = transcribe.audio_source(bundle)
    assert stream == "screen"
    assert path == bundle.media("screen.mp4")
    assert offset == 0.0


def test_a_bundle_with_no_audio_raises_rather_than_transcribing_nothing(silent_bundle):
    """An empty transcript is indistinguishable from a failed one downstream, and the
    editor would offer a caption layer with no captions in it."""
    with pytest.raises(TranscribeError, match="no audio track"):
        transcribe.audio_source(silent_bundle)
    with pytest.raises(TranscribeError, match="no audio track"):
        transcribe.transcribe_bundle(silent_bundle, runner=fake_runner())


def test_camera_audio_is_shifted_onto_the_screens_timeline(tmp_path):
    """The two streams start at different times by launch order plus pipeline warm-up,
    so a mic on the webcam is a fifth of a second out against the video."""
    root = tmp_path / "cam"
    synthetic.make_bundle(root, seconds=SECONDS, camera=True, media=False,
                          cam_anchor_delta_us=200_000)
    cap = json.loads((root / "capture.json").read_text())
    cap["screen"]["has_audio"] = False
    cap["camera"]["has_audio"] = True
    (root / "capture.json").write_text(json.dumps(cap))
    (root / "media").mkdir(exist_ok=True)
    (root / "media" / "camera.mp4").write_bytes(b"not really an mp4")

    stream, path, offset = transcribe.audio_source(Bundle(root))
    assert stream == "camera"
    assert path.name == "camera.mp4"
    # 200ms at 30fps is 6 frames, which is 0.2s back on the screen's clock.
    assert offset == pytest.approx(0.2, abs=0.02)


# --- normalization ----------------------------------------------------------


def test_normalization_strips_the_leading_space_whisper_keeps():
    segs = transcribe.normalize_segments([(0.0, 1.0, "  hello   there ")])
    assert segs[0].text == "hello there"


def test_non_speech_markers_are_dropped():
    """[BLANK_AUDIO] is whisper.cpp's annotation for silence, not something anyone
    said, and a caption layer would render it on screen."""
    segs = transcribe.normalize_segments(
        [(0.0, 1.0, "[BLANK_AUDIO]"), (1.0, 2.0, "real speech"), (2.0, 3.0, "   ")]
    )
    assert [s.text for s in segs] == ["real speech"]


def test_a_backwards_segment_is_clamped_rather_than_kept():
    segs = transcribe.normalize_segments([(5.0, 4.0, "hallucinated tail")])
    assert segs[0].start == 5.0 and segs[0].end == 5.0


def test_segments_come_back_sorted_and_rounded_to_milliseconds():
    segs = transcribe.normalize_segments(
        [(1.23456789, 2.0, "second"), (0.00049, 1.1119, "first")]
    )
    assert [s.text for s in segs] == ["first", "second"]
    assert segs[0].start == 0.0 and segs[0].end == 1.112
    assert segs[1].start == 1.235


def test_the_offset_moves_every_segment_and_never_below_zero():
    segs = transcribe.normalize_segments([(0.05, 1.0, "a"), (2.0, 3.0, "b")], offset=-0.2)
    assert segs[0].start == 0.0  # clamped; a negative caption time has no meaning
    assert segs[1].start == 1.8


# --- the schema -------------------------------------------------------------


def test_a_transcript_round_trips_through_json():
    t = Transcript(engine="faster-whisper", model="small", language="en",
                   source_stream="camera", offset_s=0.2,
                   segments=[Segment(0.0, 0.8, "hello"), Segment(0.9, 1.6, "there")])
    back = Transcript.from_dict(json.loads(json.dumps(t.to_dict())))
    assert back.to_dict() == t.to_dict()
    assert back.text == "hello there"


def test_a_future_schema_version_refuses_to_load_rather_than_guessing():
    with pytest.raises(TranscribeError, match="version"):
        Transcript.from_dict({"version": transcribe.TRANSCRIPT_VERSION + 1})


def test_an_unparseable_transcript_reads_as_absent(bundle):
    """It is derived state; the cure is to transcribe again, not to make the recording
    un-openable."""
    transcribe.transcript_path(bundle).write_text("{ this is not json")
    assert transcribe.load(bundle) is None


# --- the job ----------------------------------------------------------------


@needs_ffmpeg
def test_transcribing_writes_the_schema_and_returns_it(bundle):
    calls = []
    t = transcribe.transcribe_bundle(bundle, model="tiny", runner=fake_runner(calls=calls))

    assert calls and calls[0][1] == "tiny"
    assert t.engine in transcribe.ENGINE_ORDER
    assert t.model == "tiny"
    assert t.language == "en"
    assert t.source_stream == "screen"
    assert [s.text for s in t.segments] == ["hello there", "second segment"]

    on_disk = json.loads(transcribe.transcript_path(bundle).read_text())
    assert on_disk["version"] == transcribe.TRANSCRIPT_VERSION
    assert on_disk["segments"][0] == {"start": 0.0, "end": 0.8, "text": "hello there"}
    assert transcribe.load(bundle).to_dict() == t.to_dict()


@needs_ffmpeg
def test_the_temporary_wav_is_deleted_afterwards(bundle):
    seen = {}

    def run(wav, model, lang):
        seen["wav"] = Path(wav)
        assert seen["wav"].exists()
        return "en", list(FAKE_ROWS)

    transcribe.transcribe_bundle(bundle, runner=run)
    assert not seen["wav"].exists()
    assert not seen["wav"].parent.exists()


@needs_ffmpeg
def test_an_existing_transcript_is_left_alone_without_force(bundle):
    """The job costs minutes and the editor may already have captions built from the
    old one, so a second click must not spend them again or replace it."""
    first = transcribe.transcribe_bundle(bundle, model="tiny", runner=fake_runner())

    calls = []
    again = transcribe.transcribe_bundle(
        bundle, model="tiny", runner=fake_runner(calls=calls)
    )
    assert calls == [], "the engine ran despite an existing transcript"
    assert again.to_dict() == first.to_dict()


@needs_ffmpeg
def test_force_replaces_the_transcript(bundle):
    transcribe.transcribe_bundle(bundle, model="tiny", runner=fake_runner())
    replaced = transcribe.transcribe_bundle(
        bundle, model="tiny", force=True,
        runner=fake_runner(rows=[(0.0, 1.0, "different words")], language="fr"),
    )
    assert [s.text for s in replaced.segments] == ["different words"]
    assert replaced.language == "fr"
    assert transcribe.load(bundle).language == "fr"


def test_no_engine_fails_before_any_audio_is_extracted(bundle, monkeypatch):
    """Ordering matters: extracting audio from a long recording is a minute of work to
    throw away when the answer was knowable up front."""
    monkeypatch.setattr(transcribe, "have_faster_whisper", lambda: False)
    monkeypatch.setattr(transcribe, "whisper_cpp_binary", lambda: None)
    monkeypatch.setattr(
        transcribe, "extract_audio",
        lambda *a, **kw: pytest.fail("audio was extracted with no engine available"),
    )
    with pytest.raises(TranscribeError):
        transcribe.transcribe_bundle(bundle, runner=fake_runner())


# --- whisper.cpp output parsing ---------------------------------------------


def test_whisper_cpp_json_offsets_are_milliseconds(tmp_path, monkeypatch):
    """`offsets` is ms and `timestamps` is the same values as display strings; reading
    the wrong one gives a transcript 1000x too long."""
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"RIFF")
    out = tmp_path / "audio.json"
    out.write_text(json.dumps({
        "result": {"language": "en"},
        "transcription": [
            {"offsets": {"from": 0, "to": 1240},
             "timestamps": {"from": "00:00:00,000", "to": "00:00:01,240"},
             "text": " hello"},
            {"offsets": {"from": 1240, "to": 2500}, "text": " world"},
        ],
    }))

    monkeypatch.setattr(transcribe, "whisper_cpp_binary", lambda: "/usr/bin/whisper-cli")
    monkeypatch.setattr(transcribe, "_whisper_cpp_model", lambda m: "/models/ggml-tiny.bin")

    class Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(transcribe.subprocess, "run", lambda *a, **kw: Done())
    language, rows = transcribe.run_whisper_cpp(wav, "tiny", None)

    assert language == "en"
    assert rows == [(0.0, 1.24, " hello"), (1.24, 2.5, " world")]
    # The sidecar is cleaned up; the temp dir is shared with the wav.
    assert not out.exists()


def test_whisper_cpp_says_how_to_get_a_model_when_it_cannot_find_one(monkeypatch):
    monkeypatch.setattr(transcribe, "WHISPER_CPP_MODEL_DIRS", (str(Path("/nonexistent")),))
    with pytest.raises(TranscribeError) as e:
        transcribe._whisper_cpp_model("small")
    assert "download-ggml-model.sh" in str(e.value)
