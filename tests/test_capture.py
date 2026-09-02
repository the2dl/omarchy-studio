"""The manifest the capture scripts write, and the traps it exists to close.

The two that bite silently:

* logical vs physical pixels. Events are compositor coordinates and the video is
  physical; on a scale-2 display a 1600x900 pick is a 3200x1800 video, and conflating
  them halves or doubles every zoom focal point without erroring.
* the camera anchor. The camera's timestamps are CLOCK_REALTIME and the screen's are
  CLOCK_MONOTONIC, so an anchor that skips the conversion is off by the machine's
  boot time -- days -- rather than by the tens of milliseconds it is meant to express.
"""

import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from omarchy_studio import capture, project

FFMPEG = shutil.which("ffmpeg")


# --- geometry ----------------------------------------------------------------


def test_parse_geometry_accepts_negative_origins():
    # Hyprland puts monitors at negative coordinates in plenty of layouts.
    assert capture.parse_geometry("1600x900+-1920+-200") == {
        "x": -1920, "y": -200, "width": 1600, "height": 900,
    }


@pytest.mark.parametrize("bad", ["1600x900", "1600x900+10", "0x900+0+0", "junk"])
def test_parse_geometry_rejects_junk(bad):
    with pytest.raises(capture.CaptureError):
        capture.parse_geometry(bad)


def test_geometry_round_trips_through_format():
    geo = capture.parse_geometry("1600x900+200+200")
    assert capture.format_geometry(geo) == "1600x900+200+200"


def test_physical_geometry_scales_origin_and_size():
    logical = capture.parse_geometry("1600x900+200+200")
    assert capture.to_physical(logical, 2.0) == {
        "x": 400, "y": 400, "width": 3200, "height": 1800,
    }


def test_physical_geometry_is_identity_at_scale_one():
    logical = capture.parse_geometry("1920x1080+0+0")
    assert capture.to_physical(logical, 1.0) == logical


def test_capture_size_is_native_when_already_under_the_cap():
    assert capture.capture_size({"width": 3200, "height": 1800}) is None


def test_capture_size_halves_a_5k_display():
    # 5120x2880 is this machine's panel; h264 VAAPI hard-fails above 4096.
    assert capture.capture_size({"width": 5120, "height": 2880}) == (2560, 1440)


def test_capture_size_result_is_always_within_the_cap_and_even():
    for w, h in ((5120, 2880), (7680, 4320), (10000, 1000), (4097, 4097)):
        size = capture.capture_size({"width": w, "height": h})
        assert size is not None
        assert size[0] <= capture.MAX_CAPTURE_DIM and size[1] <= capture.MAX_CAPTURE_DIM
        assert size[0] % 2 == 0 and size[1] % 2 == 0


# --- anchors -----------------------------------------------------------------


def test_read_gsr_ts_skips_the_header(tmp_path):
    ts = tmp_path / "screen.mp4.ts"
    ts.write_text("monotonic_microsec realtime_microsec\n123456789 1788368895233008\n")
    assert capture.read_gsr_ts(ts) == (123456789, 1788368895233008)


def test_read_gsr_ts_rejects_a_file_with_no_row(tmp_path):
    ts = tmp_path / "screen.mp4.ts"
    ts.write_text("monotonic_microsec realtime_microsec\n")
    with pytest.raises(capture.CaptureError):
        capture.read_gsr_ts(ts)


def test_camera_realtime_comes_from_the_timestamp_sidecar(tmp_path):
    # mkvtimestamp_v2: a comment header then absolute milliseconds per frame.
    tsv = tmp_path / "cam.tsv"
    tsv.write_text("# timecode format v2\n1788368895229\n1788368895241\n")
    assert capture.read_camera_realtime_us(tsv, tmp_path / "cam.mp4") == 1788368895229000


def test_camera_anchor_uses_gsrs_clock_pair(tmp_path):
    # gsr sampled both clocks at once, so the offset is measured during the recording
    # rather than reconstructed afterwards.
    reference = (1_000_000, 1788368890_000_000)
    cam_realtime = 1788368890_250_000
    assert capture.realtime_to_monotonic_us(cam_realtime, reference) == 1_250_000


def test_camera_anchor_falls_back_to_the_current_clock_offset():
    # Without a reference the answer must still be near "now" on the monotonic clock,
    # not a realtime value dozens of years out.
    import time

    now_real = time.clock_gettime_ns(time.CLOCK_REALTIME) // 1000
    now_mono = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000
    got = capture.realtime_to_monotonic_us(now_real, None)
    assert abs(got - now_mono) < 1_000_000


# --- the manifest ------------------------------------------------------------


def test_begin_lays_out_the_bundle_and_records_both_geometries(tmp_path):
    bundle = capture.begin(
        tmp_path / "rec",
        logical_geometry=capture.parse_geometry("1600x900+200+200"),
        monitor_name="DP-1",
        monitor_scale=2.0,
    )
    for sub in ("media", "events", "assets"):
        assert (bundle.root / sub).is_dir()
    assert bundle.capture.monitor_scale == 2.0
    assert bundle.capture.monitor_name == "DP-1"
    assert bundle.capture.logical_geometry["width"] == 1600
    assert bundle.capture.physical_geometry["width"] == 3200
    assert bundle.capture.camera_burned_in is False
    assert bundle.capture.screen is None  # filled in by finalize


def test_burn_in_is_recorded_so_the_editor_can_say_the_camera_is_stuck(tmp_path):
    bundle = capture.begin(
        tmp_path / "rec",
        logical_geometry=capture.parse_geometry("1920x1080+0+0"),
        camera_burned_in=True,
    )
    assert bundle.capture.camera_burned_in is True


def _synthetic_video(path: Path, seconds: float, size: str, fps: int, audio: bool = False) -> None:
    args = ["ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-c:a", "aac"]
    args += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True, capture_output=True)


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is required to build a probe target")
def test_finalize_probes_the_media_rather_than_trusting_the_flags(tmp_path):
    bundle = capture.begin(
        tmp_path / "rec",
        logical_geometry=capture.parse_geometry("1600x900+200+200"),
        monitor_name="DP-1",
        monitor_scale=2.0,
    )
    _synthetic_video(bundle.media("screen.mp4"), 0.5, "640x360", 60, audio=True)
    (bundle.media("screen.mp4.ts")).write_text(
        "monotonic_microsec realtime_microsec\n5000000 1788368890000000\n"
    )

    finalized = capture.finalize(bundle.root)
    screen = finalized.capture.screen
    assert (screen.width, screen.height) == (640, 360)
    assert screen.timebase.fps == 60
    assert screen.has_audio is True
    assert screen.anchor_us == 5_000_000
    assert screen.path == "media/screen.mp4"
    # The sidecar stays where gsr wrote it; the manifest only lifts its value.
    assert bundle.media("screen.mp4.ts").exists()


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is required to build a probe target")
def test_finalize_puts_the_camera_on_the_screens_clock(tmp_path):
    bundle = capture.begin(
        tmp_path / "rec",
        logical_geometry=capture.parse_geometry("1600x900+200+200"),
        monitor_scale=2.0,
    )
    _synthetic_video(bundle.media("screen.mp4"), 0.5, "640x360", 60)
    _synthetic_video(bundle.media("cam.mp4"), 0.5, "320x180", 30)
    bundle.media("screen.mp4.ts").write_text(
        "monotonic_microsec realtime_microsec\n5000000 1788368890000000\n"
    )
    # The camera's first frame is 200ms of realtime after the screen's.
    bundle.media("cam.tsv").write_text("# timecode format v2\n1788368890200\n")

    finalized = capture.finalize(bundle.root, camera="media/cam.mp4")
    assert finalized.capture.camera.anchor_us == 5_200_000
    # 200ms at the screen's 60fps.
    assert finalized.camera_offset_frames() == 12


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is required to build a probe target")
def test_a_finalized_bundle_loads_as_a_project(tmp_path):
    bundle = capture.begin(
        tmp_path / "rec",
        logical_geometry=capture.parse_geometry("1920x1080+0+0"),
        monitor_name="DP-1",
    )
    _synthetic_video(bundle.media("screen.mp4"), 0.5, "640x360", 60)
    capture.finalize(bundle.root)

    reloaded = project.Bundle(bundle.root)
    assert reloaded.canvas.width == 640
    assert reloaded.timebase.fps == 60
    assert reloaded.source_frames() == 30
    reloaded.save_edit()  # validates, and proves the edit side is writable


def test_finalize_refuses_a_bundle_whose_media_is_missing(tmp_path):
    bundle = capture.begin(
        tmp_path / "rec",
        logical_geometry=capture.parse_geometry("1920x1080+0+0"),
    )
    with pytest.raises(capture.CaptureError):
        capture.finalize(bundle.root)


# --- the CLI the bash scripts call -------------------------------------------


def _eval_assignments(text: str) -> dict:
    """What `eval` in the calling shell would end up with."""
    out = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        out[key] = " ".join(shlex.split(value))
    return out


def test_begin_cli_prints_assignments_the_shell_can_eval(tmp_path, capsys):
    rc = capture.main([
        "begin", "--root", str(tmp_path / "rec"),
        "--logical", "5120x2880+0+0", "--monitor", "DP-1", "--scale", "1",
    ])
    assert rc == 0
    out = _eval_assignments(capsys.readouterr().out)
    assert out["BUNDLE"] == str(tmp_path / "rec")
    assert out["PHYSICAL"] == "5120x2880+0+0"
    assert out["CAPTURE_SIZE"] == "2560x1440"


def test_begin_cli_quotes_paths_because_xdg_videos_dir_may_have_spaces(tmp_path, capsys):
    root = tmp_path / "My Videos" / "screenrecording-2026-09-02_13-22-06"
    capture.main(["begin", "--root", str(root), "--logical", "800x600+0+0"])
    printed = capsys.readouterr().out
    assert _eval_assignments(printed)["BUNDLE"] == str(root)
    # An unquoted assignment would split at the space and lose half the path.
    assert f"BUNDLE={root}\n" not in printed


def test_begin_cli_emits_the_native_sentinel_when_no_cap_applies(tmp_path, capsys):
    capture.main([
        "begin", "--root", str(tmp_path / "rec"),
        "--logical", "1600x900+200+200", "--scale", "2",
    ])
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert out["CAPTURE_SIZE"] == "0x0"  # gsr reads 0x0 as "best available"
    assert out["PHYSICAL"] == "3200x1800+400+400"


def test_capture_json_is_the_only_thing_begin_writes(tmp_path):
    root = tmp_path / "rec"
    capture.begin(root, logical_geometry=capture.parse_geometry("800x600+0+0"))
    assert sorted(p.name for p in root.iterdir()) == ["assets", "capture.json", "events", "media"]
    assert json.loads((root / "capture.json").read_text())["version"] == project.CAPTURE_VERSION
