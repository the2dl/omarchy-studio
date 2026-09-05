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


def test_a_5k_display_is_captured_natively():
    """5120x2880 is this machine's panel, and it used to be halved to 2560x1440.

    Halving cost three quarters of the pixels to stay inside h264's 4096 ceiling. The
    ceiling is real -- h264_vaapi FAILS at 5120x2880 on this hardware -- but it is a
    property of the codec, not of the capture, so the codec moves instead of the size.
    """
    assert capture.capture_size({"width": 5120, "height": 2880}) is None
    assert capture.capture_codec({"width": 5120, "height": 2880}) == "hevc"


def test_a_display_h264_can_take_is_left_on_auto():
    assert capture.capture_codec({"width": 3840, "height": 2160}) == "auto"
    assert capture.capture_codec({"width": 4096, "height": 4096}) == "auto"


def test_capture_size_result_is_always_within_the_cap_and_even():
    for w, h in ((16384, 4320), (10000, 1000), (8193, 8193)):
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

    finalized, fault = capture.finalize(bundle.root)
    assert fault == ""
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

    finalized, fault = capture.finalize(bundle.root, camera="media/cam.mp4")
    assert fault == ""
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


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is required to build a probe target")
def test_an_unreadable_camera_costs_the_camera_and_nothing_else(tmp_path):
    """The screen recording must survive a camera that did not close cleanly.

    A truncated cam.mp4 -- no moov atom, which is what a hard-killed ffmpeg leaves --
    used to raise straight out of ffprobe and abandon capture.json entirely, so a good
    screen recording came back as an unopenable bundle. Reproduced from
    ~/Videos/screenrecording-2026-09-02_19-10-25.
    """
    bundle = capture.begin(
        tmp_path / "rec",
        logical_geometry=capture.parse_geometry("1920x1080+0+0"),
        monitor_name="DP-1",
    )
    _synthetic_video(bundle.media("screen.mp4"), 0.5, "640x360", 60)
    # Header bytes and no trailer: exactly the shape ffmpeg leaves behind on a hard exit.
    bundle.media("cam.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4096)
    bundle.media("cam.tsv").write_text("")

    finalized, fault = capture.finalize(bundle.root, camera="media/cam.mp4")
    assert fault, "a truncated camera must be reported, not silently dropped"
    assert finalized.capture.camera is None
    assert finalized.capture.screen.width == 640      # the screen still finalized
    project.Bundle(bundle.root)                        # and the bundle still opens


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
    assert out["CAPTURE_SIZE"] == "0x0"  # native; the codec is what moves
    assert out["CAPTURE_CODEC"] == "hevc"


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


# --- the self-view's placement ------------------------------------------------
#
# The setup bar drags the bubble on a monitor-sized sheet and reports an absolute
# logical rect; begin() is the only place that knows the capture rectangle to divide
# it by. Before this the placement was discarded entirely and the editor opened every
# recording at WebcamSettings' 0.72/0.70 defaults.


def test_a_full_display_capture_normalizes_the_placement_against_the_display():
    placement = capture.camera_placement(
        {"x": 1843, "y": 1008, "width": 563, "height": 563},
        {"x": 0, "y": 0, "width": 2560, "height": 1440},
    )
    assert placement["x"] == pytest.approx(0.72, abs=0.001)
    assert placement["w"] == pytest.approx(0.22, abs=0.001)
    # 1008/1440 is 0.70 -- WebcamSettings' own default -- and a box 563px tall from
    # there hangs 131px off the bottom of a 1440 screen. Everything stays inside the
    # frame, which is also what the sheet shows: SelfView clamps to the same rule, so
    # the placement the user sees is the placement that lands.
    assert placement["y"] == pytest.approx(1.0 - placement["h"], abs=0.001)
    assert placement["y"] + placement["h"] <= 1.0


def test_a_region_capture_normalizes_against_the_region_not_the_display():
    """The bubble is placed on the sheet but composited inside the RECORDING.

    A rect 100px inside a region that starts at 400,300 is 100px inside the video --
    normalizing against the monitor instead would put it somewhere else entirely.
    """
    placement = capture.camera_placement(
        {"x": 500, "y": 400, "width": 200, "height": 200},
        {"x": 400, "y": 300, "width": 800, "height": 600},
    )
    assert placement["x"] == pytest.approx(0.125)   # (500-400)/800
    assert placement["y"] == pytest.approx(0.1667, abs=0.001)
    assert placement["w"] == pytest.approx(0.25)
    assert placement["h"] == pytest.approx(0.3333, abs=0.001)


def test_a_placement_outside_the_capture_is_clamped_rather_than_refused():
    placement = capture.camera_placement(
        {"x": 5000, "y": 5000, "width": 200, "height": 200},
        {"x": 0, "y": 0, "width": 800, "height": 600},
    )
    assert 0.0 <= placement["x"] <= 1.0 - placement["w"]
    assert 0.0 <= placement["y"] <= 1.0 - placement["h"]


def test_the_bar_and_the_edit_agree_on_every_shape_name():
    # They did not: the bar said "corner" for the edit's "rounded" and "squircle" for
    # its "squircle", and the editor panel offered a third naming again -- so a shape
    # chosen before recording was not the shape the editor showed afterwards. One
    # vocabulary now, and this is the seam where a second one would reappear.
    rect = {"x": 0, "y": 0, "width": 100, "height": 100}
    logical = {"x": 0, "y": 0, "width": 800, "height": 600}
    for shape in ("circle", "rounded", "rect"):
        assert capture.camera_placement(rect, logical, shape)["shape"] == shape
    # A bar that somehow sends something else seeds a circle rather than a broken edit.
    assert capture.camera_placement(rect, logical, "nonsense")["shape"] == "circle"


def test_begin_seeds_the_edit_with_the_placement(tmp_path):
    bundle = capture.begin(
        tmp_path / "rec",
        logical_geometry=capture.parse_geometry("2560x1440+0+0"),
        monitor_name="DP-1",
        monitor_scale=2.0,
        camera_rect={"x": 1843, "y": 1008, "width": 563, "height": 563},
        camera_shape="rounded",
    )
    # Written to disk, not just held in memory: finalize reloads the bundle later and
    # would otherwise overwrite the placement with the defaults.
    reloaded = project.Bundle(bundle.root)
    assert reloaded.edit.webcam.x == pytest.approx(0.72, abs=0.001)
    assert reloaded.edit.webcam.shape == "rounded"


def test_begin_without_a_placement_leaves_the_defaults_alone(tmp_path):
    bundle = capture.begin(
        tmp_path / "rec",
        logical_geometry=capture.parse_geometry("2560x1440+0+0"),
    )
    assert not bundle.edit_path.exists()   # nothing written where nothing was chosen
    assert bundle.edit.webcam.x == project.WebcamSettings().x


def test_every_bar_shape_maps_to_a_shape_the_export_can_draw():
    """The bar and the model use different words for one of these, and a mapping that
    silently fell through to "circle" would ship a squircle that records as a circle."""
    from omarchy_studio import layers, setup_sources

    rect = {"x": 0, "y": 0, "width": 100, "height": 100}
    logical = {"x": 0, "y": 0, "width": 800, "height": 600}
    for mode in setup_sources.CAMERA_MODES:
        if mode == "off":
            continue
        shape = capture.camera_placement(rect, logical, mode)["shape"]
        assert shape in ("circle", "squircle", "rounded", "rect")
        # and the export must actually have a branch for it
        chain = layers.compile_layer(
            project.Layer(id="w", type="webcam", w=0.2, h=0.2,
                          props={"shape": shape}),
            __import__("omarchy_studio.geometry", fromlist=["Canvas"]).Canvas(1920, 1080),
            __import__("omarchy_studio.timebase", fromlist=["CutMap"]).CutMap([], 100),
            __import__("omarchy_studio.timebase", fromlist=["Timebase"]).Timebase(60, 1),
            _camera_registry(),
        ).filter_chain
        assert "alphamerge" in chain or shape == "rect"


def _camera_registry():
    from omarchy_studio.layers import InputRegistry

    reg = InputRegistry()
    reg.bind("camera", "[cam]")
    return reg


# --- a take must survive being stopped by something else ----------------------


def test_the_dispatch_recovers_an_orphaned_take():
    """The bar's recording indicator is stock Omarchy and its stop is
    `pkill -SIGINT -f "^gpu-screen-recorder"`. Anything that ends the recorder without
    going through this script leaves media on disk, no streams in capture.json, no
    editor and an uncleared marker -- and --stop-recording used to exit 1 rather than
    finish it, which made the take unrecoverable except by running finalize by hand.
    A real recording was lost that way before this branch existed.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] /
           "bin" / "omarchy-capture-screenrecording").read_text()
    dispatch = src[src.index("if screenrecording_active; then"):]
    # The recorder being gone is not the same question as the take being finished.
    assert 'elif [[ -f $BUNDLE_FILE ]]; then' in dispatch
    assert dispatch.index('elif [[ -f $BUNDLE_FILE ]]') < dispatch.index('exit 1'), \
        "recovery has to be tried before the give-up branch"


def test_the_watcher_finalizes_when_nobody_else_did():
    """Second layer: the detached watcher already stopped the camera and the event
    daemon when gsr died. It finalizes too now, so a take is finished even if nothing
    ever runs --stop-recording."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] /
           "bin" / "omarchy-capture-screenrecording").read_text()
    watcher = src[src.index("while kill -0 '\"$GSR_PID\"'"):]
    watcher = watcher[:watcher.index("' >/dev/null 2>&1 < /dev/null &")]
    assert "--stop-recording" in watcher
    # The marker is the interlock: the normal stop removes it as it finalizes, so its
    # presence afterwards is what says nobody did.
    assert "BUNDLE_FILE" in watcher


def test_finalize_folds_the_hud_rect_into_the_chrome_it_ignores(tmp_path):
    """The Stop button was landing in every take -- not as pixels, but as a click.

    no_screen_share keeps the pill out of the frame, so auto-zoom was the only thing
    that saw it: every recording ended with a zoom and a ripple lunging at a button the
    viewer cannot see. `hyprctl monitors` does not cover it, because those rects are the
    compositor's RESERVED edges and the HUD floats above its own strip. Measured on a
    real take: reserved was y=1406..1440 and the Stop click landed at y=1386.
    """
    import json
    import synthetic
    from omarchy_studio import capture as C

    root = tmp_path / "take"
    synthetic.make_bundle(root, seconds=1.0, width=320, height=240, camera=False)
    (root / "events").mkdir(exist_ok=True)
    (root / "events" / "chrome.json").write_text(
        json.dumps([{"x": 100, "y": 200, "width": 120, "height": 30}])
    )
    C.finalize(root)
    rects = json.loads((root / "capture.json").read_text())["chrome_rects"]
    assert {"x": 100, "y": 200, "width": 120, "height": 30} in rects


def test_a_broken_hud_rect_never_costs_the_take(tmp_path):
    """One ignored rectangle is worth losing; a finalize that raises is not."""
    import json
    import synthetic
    from omarchy_studio import capture as C

    for payload in ("not json at all", "{}", '[{"x": "x"}]', '[{"width": 0}]'):
        root = tmp_path / f"take{abs(hash(payload))}"
        synthetic.make_bundle(root, seconds=1.0, width=320, height=240, camera=False)
        (root / "events").mkdir(exist_ok=True)
        (root / "events" / "chrome.json").write_text(payload)
        C.finalize(root)  # must not raise
        json.loads((root / "capture.json").read_text())


def test_the_stop_click_stops_producing_a_zoom(tmp_path):
    """End to end, in the coordinates the real take actually used."""
    import json
    import synthetic
    from omarchy_studio import capture as C, events as E, zoom as Z
    from omarchy_studio.project import Bundle
    from omarchy_studio.timebase import CutMap

    root = tmp_path / "hud"
    synthetic.make_bundle(root, seconds=4.0, width=2560, height=1440, camera=False,
                          clicks=())
    (root / "events").mkdir(exist_ok=True)
    (root / "events" / "chrome.json").write_text(
        json.dumps([{"x": 1180, "y": 1330, "width": 460, "height": 80}])
    )
    C.finalize(root)
    # Clicks are CLOCK_MONOTONIC and mean nothing without the anchor for frame 0. The
    # synthetic bundle has no gsr .ts sidecar to derive one from, so pin it here.
    cap = json.loads((root / "capture.json").read_text())
    cap["screen"]["anchor_us"] = 0
    (root / "capture.json").write_text(json.dumps(cap))
    anchor = 0
    (root / "events" / "input.jsonl").write_text(
        '{"t_us":%d,"type":"click","button":"left","x":912,"y":975}\n'
        '{"t_us":%d,"type":"click","button":"left","x":1411,"y":1386}\n'
        % (anchor + 1_000_000, anchor + 3_000_000)
    )
    b = Bundle(root)
    b.edit.zoom.enabled = True
    clicks = E.map_clicks(E.read_clicks(b.events_dir / "input.jsonl"),
                          b.capture, b.timebase)
    segs = Z.zoom_segments(clicks, b.edit.zoom, b.timebase, CutMap([], 600))
    assert len(segs) == 1, f"expected only the real click to zoom, got {len(segs)}"
    # ~1s in at 30fps; the Stop click at ~3s must not have produced anything
    assert segs[0].anchor < 60


def test_a_lost_self_view_is_explained_rather_than_silent():
    """The realistic hyprpm failure is silence, not breakage.

    A Hyprland update leaves the screenshare plugin unbuilt until `hyprpm update`
    runs, so it stops loading and the live self-view stops appearing. Nothing is
    broken -- the camera is still recorded and the take is fine -- but an unexplained
    "it used to show my face" goes uninvestigated for weeks. Exactly how hyprexpo was
    lost on this machine.
    """
    src = (Path(__file__).resolve().parents[1]
           / "bin" / "omarchy-capture-screenrecording").read_text()
    i = src.index("Recording without a live self-view")
    guard = src[src.index('if [[ $self_view_mode == "off"'):i]
    # only when a camera was asked for, and only for the missing-exclusion reason
    assert "$WEBCAM ==" in guard
    assert "$BURN_IN !=" in guard
    assert "! self_view_exclusion_available" in guard
    # and it must name the way out
    assert "hyprpm update" in src[i:i + 400]


def test_no_plugin_script_ever_updates_every_repo():
    """`hyprpm update` rebuilds EVERY registered repository, and a foreign plugin whose
    upstream no longer builds gets unloaded from the live session as collateral. That
    is how a working hyprexpo build was destroyed on this machine, so our installer
    re-registers only its own repo."""
    d = Path(__file__).resolve().parents[1] / "contrib" / "hyprland-studio-screenshare"
    for name in ("install.sh", "ensure-loaded.sh"):
        for line in (d / name).read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "hyprpm update" not in stripped, f"{name} runs a global update: {line}"


def test_the_autostart_hook_is_a_no_op_when_the_plugin_is_loaded():
    """It is meant for `exec_on_start`, so the common path -- every login after the
    first -- must be one hyprctl call and an exit, not a build."""
    src = (Path(__file__).resolve().parents[1] / "contrib"
           / "hyprland-studio-screenshare" / "ensure-loaded.sh").read_text()
    body = src[src.index("loaded && exit 0"):]
    # the cheap exit comes before anything that builds or reloads
    assert body.index("loaded && exit 0") < body.index("install.sh")
    assert "hyprctl -j plugin list" in src
