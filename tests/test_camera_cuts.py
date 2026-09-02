"""Camera/screen alignment measured through the full render, not inferred from strings.

The align-then-cut combination is the risky part of the graph: `_align_camera` runs
first (tpad/trim onto the screen's grid) and `cuts.cut_chain` then excises the same
frame indices from both streams. Getting the order or the sign wrong desyncs the webcam
by exactly the total cut duration -- and the failure is silent, because the file still
plays. So these tests burn ground truth into the pixels and read it back out of the
rendered file.

The encoding is POSITION, not brightness: each source frame is black except for one
white vertical bar whose x places the frame number (screen frame N at x=5N, camera
frame N at x=4N). A bar centroid survives x264 at crf 20 and bicubic scaling with
sub-pixel accuracy, where a brightness ramp read back through two yuv420p range
conversions was only good to about +/-1 frame -- one frame being precisely the error
these tests exist to catch.

Offsets are +/-4 and +5 frames, inside the range the capture pipelines actually
produce (KMS warm-up 128-137 ms, V4L2 210-228 ms, either order), and the cuts include
one that begins before the camera's first real frame, where the cut removes cloned
padding rather than camera material.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import FFMPEG, needs_ffmpeg

from omarchy_studio import render
from omarchy_studio.project import Bundle, FrameRange, WebcamSettings

W, H = 320, 180
FPS = synthetic.FPS
SCREEN_FRAMES = 60  # 2 s
# Longer than the screen by more than any offset under test: a camera that started
# early (negative offset) contributes frames PAST screen frame 59 after the trim.
CAM_FRAMES = 78

# The webcam box: the bottom-right quadrant, rect + unmirrored so the camera's pixels
# land in the output as a pure 0.5x scale of the source and the bar stays decodable.
CAM_RECT_X, CAM_RECT_Y = 160, 90
WEBCAM = WebcamSettings(enabled=True, x=0.5, y=0.5, w=0.5, h=0.5,
                        shape="rect", mirror=False)


# --- synthetic media with the frame number in the pixels --------------------


def _encode_bar_clip(dst: Path, n_frames: int, bar_step: int, audio: bool) -> None:
    """Frame N is black with a `bar_step`-wide white bar at x = N * bar_step.

    Encoded with -qp 0 (x264 lossless) so the source really is ground truth; the lossy
    encode under test is the render's own.
    """
    dur = n_frames / FPS
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}:d={dur:.6f}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur:.6f}",
                "-c:a", "aac"]
    cmd += [
        "-vf",
        f"format=gray,"
        f"geq=lum='if(between(X,N*{bar_step},N*{bar_step}+{bar_step - 1}),255,0)',"
        f"format=yuv420p",
        "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-g", "15", "-bf", "0",
        "-frames:v", str(n_frames), str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def marker_media(tmp_path_factory) -> Path:
    """The two marker clips, encoded once: the lavfi+x264 work is the slow part."""
    d = tmp_path_factory.mktemp("marker-media")
    _encode_bar_clip(d / "screen.mp4", SCREEN_FRAMES, 5, audio=True)
    _encode_bar_clip(d / "camera.mp4", CAM_FRAMES, 4, audio=False)
    return d


def _make_bundle(root: Path, media_dir: Path, offset_frames: int,
                 cuts: list[FrameRange]) -> Bundle:
    # round() both ways: camera_offset_frames rounds delta * fps, so a delta built by
    # rounding frames/fps survives the round trip exactly for |offset| < fps/2.
    delta_us = round(offset_frames * 1_000_000 / FPS)
    synthetic.make_bundle(root, media=False, cam_anchor_delta_us=delta_us,
                          width=W, height=H)
    for name in ("screen.mp4", "camera.mp4"):
        (root / "media" / name).write_bytes((media_dir / name).read_bytes())
    bundle = Bundle(root)
    assert bundle.camera_offset_frames() == offset_frames  # the maths under test needs it
    bundle.edit.cuts = cuts
    bundle.edit.webcam = WEBCAM
    return bundle


# --- reading the markers back out of the render -----------------------------


def _gray_frames(path: Path) -> bytes:
    """The rendered video as raw 8-bit luma, all frames concatenated."""
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
         "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True, capture_output=True,
    )
    return r.stdout


def _bar_centroid(row: bytes, x0: int, x1: int) -> float:
    """Sub-pixel centre of the bright bar in row[x0:x1], in absolute x.

    A centroid over the above-threshold columns, because argmax alone is off by a pixel
    whenever the codec rings -- and one pixel is a fifth of a frame on the screen scale
    and half a frame on the camera scale.
    """
    seg = row[x0:x1]
    lo, hi = min(seg), max(seg)
    assert hi - lo > 60, "no bar found in the sampled row"
    thr = lo + (hi - lo) // 2
    num = den = 0
    for i, v in enumerate(seg):
        if v > thr:
            num += (x0 + i) * (v - thr)
            den += v - thr
    return num / den


def _decode_frame(buf: bytes, frame: int) -> tuple[int, int]:
    """(screen_source_frame, camera_source_frame) read out of one rendered frame."""
    base = frame * W * H
    # Screen: a clear row above the webcam box. Bar N spans [5N, 5N+5), centre 5N+2.
    row = buf[base + 40 * W: base + 41 * W]
    screen_n = round((_bar_centroid(row, 0, W) - 2.0) / 5.0)
    # Camera: mid-webcam-box row. Source bar centre 4N+2 lands at 160 + (4N+2)/2 after
    # the 0.5x scale, i.e. 161 + 2N.
    row = buf[base + 135 * W: base + 136 * W]
    cam_n = round((_bar_centroid(row, CAM_RECT_X, W) - (CAM_RECT_X + 1.0)) / 2.0)
    return screen_n, cam_n


# --- the matrix -------------------------------------------------------------

# (name, camera offset in frames, cuts). The two-cut case is there because a
# cut-ordering bug accumulates drift per cut, not per render; the last case starts its
# cut before the camera's first real frame, so the cut removes cloned padding.
CASES = [
    ("zero_offset_no_cuts", 0, []),
    ("camera_late_no_cuts", 4, []),
    ("camera_early_no_cuts", -4, []),
    ("camera_late_one_cut", 4, [FrameRange(20, 30)]),
    ("camera_early_one_cut", -4, [FrameRange(20, 30)]),
    ("camera_late_two_cuts", 4, [FrameRange(10, 16), FrameRange(30, 40)]),
    ("cut_before_camera_start", 5, [FrameRange(2, 10)]),
]


@needs_ffmpeg
@pytest.mark.parametrize("name,offset,cuts", CASES, ids=[c[0] for c in CASES])
def test_camera_stays_aligned_through_cuts(name, offset, cuts, marker_media, tmp_path):
    bundle = _make_bundle(tmp_path / "rec", marker_media, offset, cuts)
    cutmap = render.effective_cutmap(bundle)
    out = tmp_path / "out.mp4"
    render.render(bundle, out)

    buf = _gray_frames(out)
    assert len(buf) == cutmap.output_frames * W * H, (
        f"rendered {len(buf) // (W * H)} frames, cut map says {cutmap.output_frames}"
    )

    drift: list[int] = []
    for o in range(cutmap.output_frames):
        src = cutmap.to_source(o)
        screen_n, cam_n = _decode_frame(buf, o)
        # The screen check first: it validates the harness and the cut expansion, so a
        # camera failure below is attributable to alignment rather than to decoding.
        assert screen_n == src, (
            f"{name}: output frame {o} shows screen frame {screen_n}, expected {src}"
        )
        # tpad's clones mean everything before the camera woke up shows its frame 0.
        expected_cam = max(src - offset, 0)
        drift.append(cam_n - expected_cam)

    assert drift == [0] * cutmap.output_frames, (
        f"{name}: camera drift per output frame (frames): {drift}"
    )
