"""The camera's auto-exposure ramp: detecting it, and holding it out of the export.

The bug: the sensor's iris is still opening when ffmpeg starts writing frames, so the
first ~0.4 s of every camera file is a fade up from black -- mean luma 0.9, 0.9, 15, 40,
56, 66, 78, 91, 100 on media/cam.mp4 of the 2026-09-03_07-55-15 capture, settling at
~106 from frame 9. Recorded, and therefore exported: every video opened with the camera
bubble fading in.

The fix is a swap, not a shift. `_align_camera` trims the warm-up and then pads by
`offset + warmup` instead of by `offset`, so the head holds the first SETTLED frame and
every camera frame lands on exactly the screen frame it landed on before. That is the
property the render half of this file is here to prove, because getting it wrong desyncs
the lips silently -- the file still plays.

The marker-clip machinery is imported from test_camera_cuts rather than copied: it
encodes the frame number into the pixels as a bar position, which is the only way seen
to read alignment back out of a lossy render to better than +/-1 frame, and one frame is
precisely the error these tests exist to catch.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import FFMPEG, needs_ffmpeg
from test_camera_cuts import (
    CAM_FRAMES,
    SCREEN_FRAMES,
    WEBCAM,
    H,
    W,
    _decode_frame,
    _encode_bar_clip,
    _gray_frames,
)

from omarchy_studio import capture as capture_mod, render
from omarchy_studio.project import Bundle, FrameRange

FPS = synthetic.FPS  # 30


# --- synthetic ramps --------------------------------------------------------


def _encode_luma(dst: Path, expr: str, n_frames: int) -> Path:
    """A 64x64 grey clip whose luma is `expr`, a geq expression in N.

    Built with geq rather than shipped as a fixture: the detector's whole job is to read
    a brightness curve, so the curve has to be something the test states outright.
    Lossless (-qp 0), because a curve the encoder rounded is a curve the test no longer
    knows -- and signalstats then reports the exact value asked for.
    """
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
         "-f", "lavfi", "-i", f"color=c=black:s=64x64:r={FPS}:d={n_frames / FPS:.6f}",
         "-vf", f"format=gray,geq=lum='{expr}',format=yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-g", "15", "-bf", "0",
         "-frames:v", str(n_frames), str(dst)],
        check=True, capture_output=True,
    )
    return dst


@needs_ffmpeg
def test_a_ramp_from_black_is_measured_to_the_frame(tmp_path):
    """A ten-frame linear fade up to 180, then steady -- the shape of the real file.

    Frame 9 is at 162, which is 90% of settled and still visibly dim in the bubble;
    frame 10 is the first at the settled level. So the answer is 10: the count of frames
    to drop, not the index of the last bad one.
    """
    clip = _encode_luma(tmp_path / "ramp.mp4", "180*min(N,10)/10", 90)
    assert capture_mod.measure_warmup_frames(clip, FPS) == 10


@needs_ffmpeg
def test_a_dark_room_is_not_a_warm_up(tmp_path):
    """Luma wandering 9-13 for the whole take, i.e. a room that is simply dark.

    This is the case an absolute threshold gets catastrophically wrong: every frame here
    is darker than the DARKEST frame of a warm-up that ends at 106, and trimming it
    would eat the recording. The head never started dark RELATIVE to where it ended, so
    there is nothing to trim.
    """
    clip = _encode_luma(tmp_path / "dark.mp4", "9+2*mod(N,3)", 90)
    assert capture_mod.measure_warmup_frames(clip, FPS) == 0


@needs_ffmpeg
def test_a_camera_that_starts_settled_measures_zero(tmp_path):
    clip = _encode_luma(tmp_path / "clean.mp4", "120", 90)
    assert capture_mod.measure_warmup_frames(clip, FPS) == 0


@needs_ffmpeg
def test_a_four_second_fade_is_clamped_rather_than_believed(tmp_path):
    """Nobody's iris takes four seconds; that is a lamp being switched on, i.e. content.

    Clamped at 1.5 s (45 frames at 30) rather than trusted, because trusting it would
    hold one frozen frame over four seconds of the take.
    """
    clip = _encode_luma(tmp_path / "slow.mp4", "200*min(N,120)/120", 150)
    assert capture_mod.measure_warmup_frames(clip, FPS) == 45


@needs_ffmpeg
def test_an_unreadable_file_measures_zero_instead_of_raising(tmp_path):
    """Zero is the answer that leaves the render as it was. A camera that survived
    finalize must never be dropped over a luma measurement that did not."""
    assert capture_mod.measure_warmup_frames(tmp_path / "nope.mp4", FPS) == 0


def test_a_nonsense_frame_rate_measures_zero_without_shelling_out():
    assert capture_mod.measure_warmup_frames(Path("/nonexistent"), 0.0) == 0


# --- the field --------------------------------------------------------------


def _bundle(tmp_path, *, offset_us: int = 120_000, warmup: int = 0,
            cam_fps: int = FPS) -> Bundle:
    """A media-less bundle: everything here is manifest arithmetic and graph text."""
    synthetic.make_bundle(tmp_path, media=False, cam_anchor_delta_us=offset_us)
    b = Bundle(tmp_path)
    b.capture.camera.warmup_frames = warmup
    b.capture.camera.fps_num = cam_fps
    return b


def test_the_warm_up_is_converted_from_camera_frames_to_screen_frames(tmp_path):
    """Nine camera frames at 30 fps is 18 frames of a 60 fps screen. Trimming 9 on the
    project grid would leave half the fade in shot."""
    b = _bundle(tmp_path, warmup=9, cam_fps=30)
    b.capture.screen.fps_num = 60
    assert b.camera_warmup_frames() == 18


def _strip_warmup(root: Path) -> None:
    """Turn a manifest back into one written before the field existed."""
    d = json.loads((root / "capture.json").read_text())
    assert "warmup_frames" in d["camera"], "new manifests should carry it"
    for stream in ("screen", "camera"):
        d[stream].pop("warmup_frames", None)
    (root / "capture.json").write_text(json.dumps(d, indent=2) + "\n")


def test_a_capture_json_without_the_field_loads_as_zero(tmp_path):
    """Every bundle recorded before this existed. It must open, so the field defaults
    rather than being required -- the WebcamSettings.LEGACY_SHAPES precedent."""
    synthetic.make_bundle(tmp_path, media=False)
    _strip_warmup(tmp_path)
    b = Bundle(tmp_path)
    assert b.capture.camera.warmup_frames == 0
    assert b.camera_warmup_frames() == 0


# --- the filtergraph --------------------------------------------------------


def _graph(tmp_path_factory, **kw) -> str:
    """One graph off a real (tiny) recording; build_graph needs a frame count."""
    root = tmp_path_factory.mktemp("g")
    warmup = kw.pop("warmup", 0)
    synthetic.make_bundle(root, seconds=1.0, width=160, height=120, **kw)
    b = Bundle(root)
    b.capture.camera.warmup_frames = warmup
    return render.build_graph(b).graph


@needs_ffmpeg
def test_zero_warm_up_leaves_the_graph_exactly_as_it_was(tmp_path):
    """The normal answer is zero, and it must cost nothing.

    Compared against the same recording with the field deleted from capture.json --
    literally the bundle as an older build wrote it -- so this is the byte-for-byte
    "nothing changed for a camera that starts clean" check, not a self-comparison.
    """
    synthetic.make_bundle(tmp_path, seconds=1.0, width=160, height=120)
    measured_zero = render.build_graph(Bundle(tmp_path)).graph
    _strip_warmup(tmp_path)
    old_bundle = render.build_graph(Bundle(tmp_path)).graph
    assert measured_zero == old_bundle
    assert "tpad=start=4:start_mode=clone" in measured_zero
    assert "trim=start_frame=" not in measured_zero


@needs_ffmpeg
def test_a_warm_up_is_trimmed_and_paid_back_into_the_pad(tmp_path_factory):
    """offset 4 + warm-up 6 -> drop 6, pad 10. The sum is what preserves the sync."""
    g = _graph(tmp_path_factory, warmup=6)
    assert (
        "[cam_tb]trim=start_frame=6,setpts=PTS-STARTPTS,"
        "tpad=start=10:start_mode=clone[cam_aligned]"
    ) in g


@needs_ffmpeg
def test_a_camera_on_the_same_anchor_still_holds_a_settled_head(tmp_path_factory):
    """Offset 0 is not "no alignment" once there is a warm-up: camera frame W belongs at
    screen frame W, so the head is padded by exactly what was trimmed."""
    g = _graph(tmp_path_factory, cam_anchor_delta_us=0, warmup=5)
    assert (
        "[cam_tb]trim=start_frame=5,setpts=PTS-STARTPTS,"
        "tpad=start=5:start_mode=clone[cam_aligned]"
    ) in g


@needs_ffmpeg
def test_a_camera_early_enough_to_swallow_its_warm_up_is_unchanged(tmp_path_factory):
    """Offset -8 with a 3-frame warm-up: the first 8 camera frames belong before screen
    frame 0 and are dropped anyway, warm-up and all. No second trim, no pad."""
    g = _graph(tmp_path_factory, cam_anchor_delta_us=-266_667, warmup=3)
    assert "[cam_tb]trim=start_frame=8,setpts=PTS-STARTPTS[cam_aligned]" in g
    assert "tpad=" not in g


# --- sync, read back out of the pixels --------------------------------------


@pytest.fixture(scope="module")
def marker_media(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("warmup-marker-media")
    _encode_bar_clip(d / "screen.mp4", SCREEN_FRAMES, 5, audio=True)
    _encode_bar_clip(d / "camera.mp4", CAM_FRAMES, 4, audio=False)
    return d


# (name, offset, warm-up in camera frames, cuts). The warm-ups bracket the 9 and 10
# frames measured on the two real captures; the cut cases are there because the cut runs
# after the alignment and a trim inserted before it is exactly the kind of change that
# shifts what the cut lands on.
CASES = [
    ("late_camera", 4, 6, []),
    ("same_anchor", 0, 5, []),
    ("early_camera_warmup_swallowed", -8, 3, []),
    ("early_camera_warmup_survives", -2, 7, []),
    ("late_camera_with_cuts", 4, 9, [FrameRange(10, 16), FrameRange(30, 40)]),
]


@needs_ffmpeg
@pytest.mark.parametrize("name,offset,warmup,cuts", CASES, ids=[c[0] for c in CASES])
def test_the_warm_up_trim_moves_nothing_but_the_head(
    name, offset, warmup, cuts, marker_media, tmp_path
):
    """THE test. Camera frame k landed on screen frame k+offset before the trim, and it
    still does after it -- the trimmed frames are paid straight back into the pad. The
    only frames whose content changes are the head ones, which used to show the black
    frame 0 and now show the first settled frame.

    Read out of the rendered pixels rather than off the graph string, because the graph
    being plausible is not the claim; the claim is about where the frames land.
    """
    root = tmp_path / "rec"
    synthetic.make_bundle(root, media=False, width=W, height=H,
                          cam_anchor_delta_us=round(offset * 1_000_000 / FPS))
    for f in ("screen.mp4", "camera.mp4"):
        (root / "media" / f).write_bytes((marker_media / f).read_bytes())
    bundle = Bundle(root)
    assert bundle.camera_offset_frames() == offset
    bundle.capture.camera.warmup_frames = warmup
    bundle.edit.cuts = cuts
    bundle.edit.webcam = WEBCAM

    cutmap = render.effective_cutmap(bundle)
    out = tmp_path / "out.mp4"
    render.render(bundle, out)
    buf = _gray_frames(out)
    assert len(buf) == cutmap.output_frames * W * H

    drift = []
    for o in range(cutmap.output_frames):
        src = cutmap.to_source(o)
        screen_n, cam_n = _decode_frame(buf, o)
        assert screen_n == src, f"{name}: output {o} shows screen frame {screen_n}"
        # Unchanged from the no-warm-up expectation (max(src - offset, 0)) everywhere
        # the camera has real frames to show; the clone under the head is frame `warmup`
        # now instead of frame 0.
        drift.append(cam_n - max(src - offset, warmup))
    assert drift == [0] * cutmap.output_frames, f"{name}: camera drift: {drift}"


@needs_ffmpeg
def test_the_head_holds_the_settled_frame_rather_than_the_first_one(
    marker_media, tmp_path
):
    """The bug itself, stated in pixels: with a 6-frame warm-up and the camera 4 frames
    late, none of the ten head frames may show camera frame 0 -- the black one."""
    root = tmp_path / "rec"
    synthetic.make_bundle(root, media=False, width=W, height=H,
                          cam_anchor_delta_us=round(4 * 1_000_000 / FPS))
    for f in ("screen.mp4", "camera.mp4"):
        (root / "media" / f).write_bytes((marker_media / f).read_bytes())
    bundle = Bundle(root)
    bundle.capture.camera.warmup_frames = 6
    bundle.edit.webcam = WEBCAM
    out = tmp_path / "out.mp4"
    render.render(bundle, out)

    buf = _gray_frames(out)
    head = [_decode_frame(buf, o)[1] for o in range(10)]
    assert head == [6] * 10, f"head showed camera frames {head}"


# --- finalize ---------------------------------------------------------------


@needs_ffmpeg
def test_finalize_records_the_measurement_in_capture_json(tmp_path):
    """Measured once, where every other derived fact about the media is derived. A
    render that measured it itself would re-decode the camera on every export."""
    synthetic.make_bundle(tmp_path, media=False)
    _encode_luma(tmp_path / "media" / "screen.mp4", "120", 30)
    _encode_luma(tmp_path / "media" / "cam.mp4", "180*min(N,10)/10", 90)
    bundle, fault = capture_mod.finalize(
        tmp_path, screen="media/screen.mp4", camera="media/cam.mp4",
        camera_timestamps=None,
    )
    assert fault == ""
    assert bundle.capture.camera.warmup_frames == 10
    assert '"warmup_frames": 10' in (tmp_path / "capture.json").read_text()
