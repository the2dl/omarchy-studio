"""Turns what the capture scripts measured into capture.json.

The bash side owns the processes; this module owns the manifest. It runs twice per
recording:

* `begin` lays out the bundle before anything is recorded, because the geometry it
  records -- the logical rectangle the user picked and the monitor scale in force at
  that moment -- is unrecoverable afterwards. A window moved, a monitor rescaled, or a
  second display unplugged between record and finalize would all silently rewrite it.
* `finalize` fills in the two Streams once the files exist, by probing them. Dimensions
  and fps are read from the containers rather than from the flags we passed, because
  `-s` is a request and `-k auto` may pick a codec that rounds it.

capture.json is complete only after `finalize`; nothing rewrites it after that.

Anchors are CLOCK_MONOTONIC microseconds, per Stream.anchor_us. gsr's sidecar gives a
monotonic/realtime pair sampled at its own first frame, and the camera's timestamps are
realtime (ffmpeg's v4l2 `-ts abs`), so that pair is also the clock-offset measurement
that converts the camera onto the same monotonic scale -- measured during the recording
instead of after it, which is what keeps an NTP step from moving the two streams apart.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import probe, project
from .project import Bundle, Capture, Stream

_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")

# Capture is the one stage whose fidelity cannot be recovered later, so it runs at the
# panel's native grid and the codec is chosen to allow that -- rather than the reverse,
# which is what this used to do.
#
# It used to cap at h264's 4096 and HALVE past it, so a 5120x2880 panel recorded at
# 2560x1440: a quarter of the pixels, and the reason desktop text came out soft. The cap
# could not be conditioned on the codec because `-k auto` might pick either one, and an
# export that assumed h264 would break. That reasoning had the dependency backwards --
# nothing downstream assumes h264 (render, proxy and thumbnails all ENCODE x264 and
# decode whatever they are given), so the fix is to stop leaving `-k` to chance and name
# the codec that matches the size being asked for.
#
# Probed on this machine's Radeon: h264_vaapi encodes 4096x2304 and FAILS at 5120x2880;
# hevc_vaapi encodes 5120x2880. Native also keeps the mapping cleaner than a nominal 4K
# box would -- logical 2560x1440 to a 5120x2880 video is exactly 2x, where fitting to
# 4096 would have been a fractional 1.6x.
H264_MAX_DIM = 4096
MAX_CAPTURE_DIM = 8192


class CaptureError(RuntimeError):
    pass


# --- geometry ---------------------------------------------------------------


def parse_geometry(spec: str) -> dict:
    """Parse slurp/gsr's `WxH+X+Y`. X and Y are signed: Hyprland puts monitors at
    negative coordinates in plenty of multi-display layouts."""
    m = _GEOMETRY_RE.match(spec.strip())
    if not m:
        raise CaptureError(f"unparseable geometry {spec!r}; expected WxH+X+Y")
    w, h, x, y = (int(g) for g in m.groups())
    if w <= 0 or h <= 0:
        raise CaptureError(f"degenerate geometry {spec!r}")
    return {"x": x, "y": y, "width": w, "height": h}


def format_geometry(geo: dict) -> str:
    return f"{geo['width']}x{geo['height']}+{geo['x']}+{geo['y']}"


def to_physical(logical: dict, scale: float) -> dict:
    """Logical (compositor) pixels -> physical pixels.

    Events arrive logical and the video is physical; on this scale-2 display
    `-w 1600x900+200+200` yields a 3200x1800 video. Both rectangles are stored so no
    consumer has to guess which space a number is in.
    """
    if scale <= 0:
        raise CaptureError(f"invalid monitor scale {scale}")
    return {
        "x": int(round(logical["x"] * scale)),
        "y": int(round(logical["y"] * scale)),
        "width": int(round(logical["width"] * scale)),
        "height": int(round(logical["height"] * scale)),
    }


def capture_codec(physical: dict) -> str:
    """The `-k` argument for gsr: the codec that can actually encode this grid.

    Named rather than left to `-k auto`, which decides from the hardware and the
    container and would silently pick h264 on a panel h264 cannot encode.
    """
    w, h = int(physical["width"]), int(physical["height"])
    return "hevc" if w > H264_MAX_DIM or h > H264_MAX_DIM else "auto"


def capture_size(physical: dict, max_dim: int = MAX_CAPTURE_DIM) -> tuple[int, int] | None:
    """The `-s` argument for gsr, or None when the native size is already safe.

    Even dimensions because yuv420 chroma subsampling requires them.
    """
    w, h = int(physical["width"]), int(physical["height"])
    if w <= 0 or h <= 0:
        raise CaptureError(f"degenerate physical geometry {physical}")
    if w <= max_dim and h <= max_dim:
        return None
    while w > max_dim or h > max_dim:
        w, h = w // 2, h // 2
    return max(w - (w & 1), 2), max(h - (h & 1), 2)


# --- anchors ----------------------------------------------------------------


def read_gsr_ts(path: Path) -> tuple[int, int]:
    """(monotonic_us, realtime_us) of the screen's first frame, from gsr's `.ts`.

    The file is a header line naming the columns followed by one row of values. The
    file itself stays in media/ as provenance; this lifts the value into capture.json so
    the two can never drift apart.
    """
    rows = [
        line.split()
        for line in Path(path).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "monotonic"))
    ]
    for row in rows:
        if len(row) >= 2 and row[0].isdigit() and row[1].isdigit():
            return int(row[0]), int(row[1])
    raise CaptureError(f"{path} has no monotonic/realtime row")


def read_camera_realtime_us(tsv: Path | None, video: Path) -> int:
    """CLOCK_REALTIME microseconds of the camera's first frame.

    The mkvtimestamp_v2 sidecar is preferred over the container's start_time: it is a
    stream copy of the input packet timestamps, while the mp4 value comes back through
    the encoder and the container timebase (observed 4ms apart on the same capture).
    """
    if tsv is not None and Path(tsv).exists():
        for line in Path(tsv).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return int(float(line) * 1000)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=start_time",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        # A camera file ffmpeg never got to close has no moov atom, and ffprobe exits
        # non-zero on it. Raised as a CaptureError so finalize can treat it as "no
        # usable camera" rather than dying on an unhandled CalledProcessError.
        raise CaptureError(f"{video} is unreadable: {getattr(e, 'stderr', '') or e}") from e
    try:
        return int(float(out) * 1e6)
    except ValueError as e:
        raise CaptureError(f"{video} has no absolute start_time (was -ts abs passed?)") from e


def realtime_to_monotonic_us(realtime_us: int, reference: tuple[int, int] | None) -> int:
    """Move a realtime stamp onto the monotonic scale the anchors share.

    `reference` is gsr's (monotonic_us, realtime_us) pair, sampled mid-recording. Only
    when there is none do we fall back to reading both clocks now, which is a worse
    measurement for exactly the reason the pair exists.
    """
    if reference is not None:
        mono_ref, real_ref = reference
        return realtime_us + (mono_ref - real_ref)
    now_real = time.clock_gettime_ns(time.CLOCK_REALTIME) // 1000
    now_mono = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000
    return realtime_us + (now_mono - now_real)


# --- camera warm-up ---------------------------------------------------------
#
# The sensor is still opening its iris when ffmpeg starts writing frames. On
# media/cam.mp4 of the 2026-09-03_07-55-15 capture the mean luma runs
#
#     0.9  0.9  15  40  56  66  78  91  100  101  103  ...  ~106 from here on
#
# and the 2026-09-02_21-45-14 capture does the same thing one frame slower. That ramp is
# recorded, so every export used to open with the camera bubble fading up out of black.
# The render holds a settled frame across the head instead (render._align_camera), and
# this is where the "how many frames are warm-up" question is answered -- once, at
# finalize, beside every other derived fact about the media. Answering it at render time
# would re-decode the camera's head on every export and every proxy build.

# Everything below is in the CAMERA's own frames, and every threshold is RELATIVE to
# what the camera settled at. An absolute luma cutoff would declare a legitimately dark
# room -- dim from frame 0 to the last one -- to be one long warm-up and trim it away.
#
# Probe 2.5 s and take the settled reference from everything past the 1.5 s clamp, so
# the reference can never be drawn out of a candidate warm-up. That leaves a 1 s (30
# frame) median, long enough to shrug off a blink, and costs 0.28 s to decode.
_WARMUP_PROBE_SECONDS = 2.5
# A warm-up longer than this is not a warm-up, it is the content -- somebody switching a
# lamp on. The two measured captures ramp in 0.30 s and 0.33 s and V4L2's own pipeline
# warm-up is 210-228 ms, so 1.5 s is about five times the worst case seen. Past it we
# clamp and move on rather than deciding the whole take is a fade-in.
_WARMUP_MAX_SECONDS = 1.5
# The ramp is a RATE, not a level, and it ends where the rise stops -- the knee.
#
# A level test was tried first (the ramp is over once the frame reaches 0.95 of the
# settled median) and it is wrong on any camera whose auto-exposure hunts UPWARD. The
# settled reference is taken from past the clamp, but on such a camera that window is
# still climbing, so the reference lands above live footage and -- because the scan
# takes the LAST frame under the floor, to survive a non-monotonic ramp -- it swallows
# the entire drift. Measured on the 2026-09-03 18:36 capture: the ramp is over by frame
# 9 (1.2 -> 108.3), but the drift reaches 125 by frame 75, putting the 0.95 floor at
# 118.8 and returning 39 instead of 8. Those thirty phantom frames are held by the
# editor and the export alike, which is what made the bubble stutter.
#
# The knee is relative to the ramp's own peak rate, not absolute: an iris opening moves
# tens of luma per frame, a lamp being switched on over four seconds moves under two,
# and an absolute threshold cannot call both. A fraction of the fastest frame separates
# them and keeps the four-second fade on the clamp path where it belongs.
_WARMUP_KNEE_FRACTION = 0.15
# ...with a floor, so a stream that only ever creeps cannot produce a knee so small that
# its own noise satisfies it.
_WARMUP_KNEE_MIN_DELTA = 0.5
# Frames that must all sit under the knee before the ramp is called over. One is not
# enough: a real ramp stalls for a frame (frames 2 and 3 of the 09-02 capture both sit
# at 13.7 before it moves again) and a single stalled frame must not end it early.
_WARMUP_KNEE_RUN = 3
# ...and nothing is a warm-up unless the head actually started DARK relative to where it
# ended. This is the gate that protects the dark room: the bug's signature is 0.9
# against 106, a ratio of 0.009, while a merely dim room begins within a few percent of
# where it ends and never trips this.
_WARMUP_DARK_FRACTION = 0.5
# A stream that is black throughout (lens cap, disconnected sensor) has no settled level
# to measure against and nothing to gain from trimming.
_WARMUP_MIN_SETTLED_LUMA = 2.0

_YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


def head_luma(path: Path, frames: int) -> list[float]:
    """Mean luma of the first `frames` frames, one float each.

    `-frames:v` on the output rather than a `select` in the graph: select still decodes
    the whole file, while the frame limit stops the decoder (0.28 s against a full pass
    over a 32 MB camera file). `signalstats` is the cheapest per-frame average ffmpeg
    already has, and `metadata=print` is how it gets out to a pipe.
    """
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-frames:v", str(int(frames)),
         "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
         "-fps_mode", "passthrough", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [float(m) for m in _YAVG_RE.findall(out)]


def measure_warmup_frames(path: Path, fps: float) -> int:
    """Frames of auto-exposure ramp at the head of a camera file, in ITS OWN frames.

    Zero is the normal answer and the safe one: a camera that starts settled must leave
    the render exactly as it was. So every way this can fail -- no ffmpeg, an
    unparseable stream, a clip too short to hold a reference -- returns 0 rather than
    raising. A warm-up we failed to measure costs the first tenth of a second of one
    camera bubble; a finalize that died measuring it costs the whole recording.
    """
    if fps <= 0:
        return 0
    try:
        ys = head_luma(path, max(4, round(_WARMUP_PROBE_SECONDS * fps)))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0
    if len(ys) < 4:
        return 0

    # len(ys) - 1 so a clip shorter than the clamp can never be trimmed away entirely:
    # `tpad` clones the first surviving frame, and there has to be one.
    clamp = min(round(_WARMUP_MAX_SECONDS * fps), len(ys) - 1)
    tail = ys[clamp:]
    if len(tail) < 3:
        # A clip shorter than the probe window has no room past the clamp, so the
        # reference comes from its last third instead. Any warm-up still inside that
        # third drags the reference DOWN, which shortens the trim -- erring towards
        # leaving a frame of ramp in, never towards eating live footage.
        tail = ys[-max(3, len(ys) // 3):]
    settled = statistics.median(tail)
    if settled < _WARMUP_MIN_SETTLED_LUMA:
        return 0
    if ys[0] >= _WARMUP_DARK_FRACTION * settled:
        return 0

    deltas = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
    knee = max(_WARMUP_KNEE_MIN_DELTA,
               _WARMUP_KNEE_FRACTION * max(abs(d) for d in deltas[:clamp]))

    # Wait for the rise to actually START before looking for where it stops. The first
    # frames of a v4l2 stream are commonly identical black -- a delta of zero that would
    # otherwise read as "already settled" and return 0 on the very files this exists for.
    rise = next((i for i, d in enumerate(deltas[:clamp]) if d >= knee), None)
    if rise is None:
        return 0

    # The knee index IS the count of frames to drop: deltas[i] is the step from frame i
    # to frame i+1, so the first i whose step (and the next few) is under the knee makes
    # frame i the first settled one, and frames 0..i-1 the warm-up.
    for i in range(rise, min(clamp, len(deltas) - _WARMUP_KNEE_RUN + 1)):
        if all(abs(d) < knee for d in deltas[i:i + _WARMUP_KNEE_RUN]):
            return i
    # Still rising when the clamp ran out: not an iris, so hold the clamp rather than
    # believing a warm-up that long.
    return clamp


# --- manifest ---------------------------------------------------------------


# The setup bar and WebcamSettings share one vocabulary now, so this is the identity --
# and that is the point of still writing it down. It used to translate, badly: the bar
# said "squircle" and "corner" for what the model called "squircle" and "rounded", the
# editor panel offered a third set of names, and a shape chosen before recording was not
# the shape the editor showed afterwards. WebcamSettings.LEGACY_SHAPES carries the old
# names forward for bundles already on disk.
SETUP_SHAPES = {"circle": "circle", "rounded": "rounded", "rect": "rect"}


def camera_placement(rect: dict, logical: dict, shape: str = "circle") -> dict:
    """The self-view's absolute logical rect as WebcamSettings fields.

    The setup bar drags the self-view on a MONITOR-sized sheet, but the camera overlay
    is normalized against the CAPTURE rectangle, and those are only the same thing when
    the whole display is being recorded. Doing the division here, where the capture rect
    is known, is what lets the bar stay ignorant of the target it is placing against.

    Clamped into the capture rect rather than rejected: a camera dragged half outside a
    region is a placement the user can still see and fix in the editor, and refusing the
    recording over it would be absurd.
    """
    w = min(max(rect["width"] / logical["width"], 0.02), 1.0)
    h = min(max(rect["height"] / logical["height"], 0.02), 1.0)
    x = min(max((rect["x"] - logical["x"]) / logical["width"], 0.0), 1.0 - w)
    y = min(max((rect["y"] - logical["y"]) / logical["height"], 0.0), 1.0 - h)
    # "corner" is the bar's name for the rounded-rectangle preset; WebcamSettings has
    # only ever known it as "rounded", and the export reads that name. Everything else
    # is spelled the same on both sides.
    return {"x": round(x, 5), "y": round(y, 5), "w": round(w, 5), "h": round(h, 5),
            "shape": SETUP_SHAPES.get(shape, "circle")}


def begin(
    root: Path,
    *,
    logical_geometry: dict,
    monitor_name: str = "",
    monitor_scale: float = 1.0,
    camera_burned_in: bool = False,
    calibration_c_ms: float = 0.0,
    camera_rect: dict | None = None,
    camera_shape: str = "circle",
    source_logical: dict | None = None,
    window_isolated: bool = False,
) -> Bundle:
    """Lay out the bundle and record everything that is only knowable now.

    `camera_rect` is the self-view's absolute logical rectangle from the setup bar. It
    lands in edit.json rather than capture.json on purpose: where the camera sits is an
    editing decision the user goes on to change, and capture.json is the immutable
    record of what the hardware did.

    `source_logical` is the rectangle actually STREAMED, when that is larger than the
    frame. A region that wants a live self-view has to record the whole monitor through
    the portal -- the only backend that can hide the bubble -- so the stream is the
    monitor and `logical_geometry` stays the region the user chose. Everything that maps
    events to pixels keeps normalising against the region, and the renderer crops. When
    they are the same rectangle there is no crop and nothing changes.
    """
    frame_physical = to_physical(logical_geometry, monitor_scale)
    source_crop: dict = {}
    if source_logical and dict(source_logical) != dict(logical_geometry):
        # Offsets are absolute desktop coordinates, so the crop is the frame's origin
        # measured from the STREAM's origin, not from the desktop's.
        src_physical = to_physical(source_logical, monitor_scale)
        source_crop = {
            "x": int(frame_physical["x"]) - int(src_physical["x"]),
            "y": int(frame_physical["y"]) - int(src_physical["y"]),
            "width": int(frame_physical["width"]),
            "height": int(frame_physical["height"]),
        }

    capture = Capture(
        created=datetime.now().astimezone().isoformat(timespec="seconds"),
        logical_geometry=dict(logical_geometry),
        physical_geometry=frame_physical,
        monitor_scale=float(monitor_scale),
        monitor_name=monitor_name,
        calibration_c_ms=float(calibration_c_ms),
        camera_burned_in=bool(camera_burned_in),
        source_crop=source_crop,
        window_isolated=bool(window_isolated),
    )
    bundle = project.create(Path(root), capture)
    if camera_rect:
        placement = camera_placement(camera_rect, logical_geometry, camera_shape)
        for field, value in placement.items():
            setattr(bundle.edit.webcam, field, value)
        bundle.save_edit()
    return bundle


def _stream(root: Path, rel: str, anchor_us: int | None) -> Stream:
    path = Path(root) / rel
    width, height = probe.dimensions(path)
    num, den = probe.fps(path)
    return Stream(
        path=rel,
        width=width,
        height=height,
        fps_num=num,
        fps_den=den,
        anchor_us=anchor_us,
        has_audio=probe.has_audio(path),
    )


def finalize(
    root: Path,
    *,
    screen: str = "media/screen.mp4",
    camera: str | None = None,
    camera_timestamps: str | None = "media/cam.tsv",
) -> tuple[Bundle, str]:
    """Probe the recorded files and complete capture.json.

    Returns the bundle and a camera fault string, empty when there was none.

    A missing or unreadable SCREEN is still fatal -- a manifest describing a stream that
    is not there fails much later and much more confusingly. A broken CAMERA is not:
    it costs the camera track and nothing else, and refusing to write capture.json over
    it throws away a perfectly good screen recording along with every cursor sample and
    every zoom the editor could still have applied. That is exactly what happened when
    a double SIGINT left cam.mp4 without its moov atom (see bin/omarchy-capture-camera's
    LOCK_FILE): one broken camera, and the whole recording came back unopenable.
    """
    root = Path(root)
    capture = Capture.from_dict(json.loads((root / "capture.json").read_text()))

    screen_path = root / screen
    if not screen_path.exists():
        raise CaptureError(f"no screen recording at {screen_path}")

    ts_pair: tuple[int, int] | None = None
    ts_file = screen_path.with_name(screen_path.name + ".ts")
    if ts_file.exists():
        ts_pair = read_gsr_ts(ts_file)
    capture.screen = _stream(root, screen, ts_pair[0] if ts_pair else None)

    fault = ""
    if camera:
        cam_path = root / camera
        try:
            if not cam_path.exists():
                raise CaptureError(f"no camera recording at {cam_path}")
            tsv = root / camera_timestamps if camera_timestamps else None
            anchor = realtime_to_monotonic_us(
                read_camera_realtime_us(tsv, cam_path), ts_pair)
            cam = _stream(root, camera, anchor)
            # After the probe, so the measurement runs on the frame rate the container
            # actually reports rather than the one we asked v4l2 for. Non-fatal by
            # construction (see measure_warmup_frames): a camera that survived this far
            # must not be dropped over a luma measurement.
            cam.warmup_frames = measure_warmup_frames(
                cam_path, cam.fps_num / cam.fps_den)
            capture.camera = cam
        except (CaptureError, probe.ProbeError, OSError, ValueError) as e:
            # Left null rather than half-filled: every consumer already branches on
            # `camera is None` for a recording made without one, so a camera that did
            # not survive lands on a path that is already exercised.
            capture.camera = None
            fault = str(e)

    (root / "capture.json").write_text(json.dumps(capture.to_dict(), indent=2) + "\n")
    return Bundle(root), fault


# --- CLI --------------------------------------------------------------------
#
# The capture scripts are bash; this is how they reach the manifest. `begin` prints
# shell-evaluable assignments so the caller does not re-derive geometry that was already
# derived here -- two implementations of the logical/physical conversion is precisely
# the drift the manifest exists to prevent.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omarchy_studio.capture")
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("begin", help="create the bundle and print shell assignments")
    b.add_argument("--root", required=True)
    b.add_argument("--logical", required=True, help="WxH+X+Y in compositor pixels")
    b.add_argument("--monitor", default="")
    b.add_argument("--scale", type=float, default=1.0)
    b.add_argument("--burn-in", action="store_true")
    b.add_argument("--calibration-ms", type=float, default=0.0)
    b.add_argument("--camera-rect", default="",
                   help="WxH+X+Y in logical pixels: where the setup bar left the "
                        "self-view. Seeds edit.json's webcam placement.")
    b.add_argument("--source-logical", default="", metavar="WxH+X+Y",
                   help="the rectangle actually STREAMED, when larger than --logical. "
                        "A region that needs a live self-view records the whole monitor "
                        "through the portal -- the only backend that can hide the "
                        "bubble -- and the renderer crops back to --logical.")
    b.add_argument("--window-isolated", action="store_true",
                   help="the stream is one window's own surface tree, captured through "
                        "hyprland_toplevel_export_v1 rather than as a rectangle of the "
                        "screen. Nothing drawn over the window is in it, and the frame "
                        "followed the window without a track.")
    b.add_argument("--camera-shape", default="circle",
                   choices=["circle", "rounded", "rect"])

    su = sub.add_parser("setup", help="parse the setup bar's JSON into shell assignments")
    su.add_argument("--json", required=True)

    f = sub.add_parser("finalize", help="probe the media and complete capture.json")
    f.add_argument("--root", required=True)
    f.add_argument("--screen", default="media/screen.mp4")
    f.add_argument("--camera", default=None)
    f.add_argument("--camera-timestamps", default="media/cam.tsv")
    f.add_argument(
        "--trim-head-seconds",
        type=float,
        default=0.0,
        help="drop this many seconds from the head (the setup countdown)",
    )
    f.add_argument(
        "--trim-until-monotonic-us",
        type=int,
        default=0,
        help="drop every frame captured before this CLOCK_MONOTONIC microsecond "
             "(the instant the setup surfaces were gone). Preferred over "
             "--trim-head-seconds: it is measured against the same first-frame anchor "
             "the video is timed by, so it stays correct however long gsr took to "
             "produce that frame.",
    )

    a = parser.parse_args(argv)

    if a.cmd == "setup":
        # One authoritative parse. The shell could pick these out with jq, but the target
        # forms and their validation live in setup_sources, and a second implementation
        # in bash is exactly how the two drift.
        from .setup_sources import parse_target

        cfg = json.loads(a.json)
        target = str(cfg["target"])
        parts = parse_target(target)  # validates; raises on a malformed form
        if parts["kind"] == "camera":
            # The recorder has no camera-only path -- that is a different pipeline with
            # no screen stream at all. Fail loudly here rather than letting gsr be handed
            # a target it will reject with something unreadable.
            print(
                "camera-only recording is not supported by this recorder yet",
                file=sys.stderr,
            )
            return 2
        emit = {
            # Passed through verbatim: the recorder's own select_capture_target emits
            # these same two forms, so the setup bar is a drop-in for it.
            "TARGET": target,
            "TARGET_KIND": parts["kind"],
            "SETUP_MIC": "true" if cfg.get("mic") else "false",
            # Empty means "whatever the default source is at record time", which is
            # what the recorder did unconditionally before the picker existed.
            "SETUP_MIC_DEVICE": str(cfg.get("mic_device") or ""),
            "SETUP_DESKTOP_AUDIO": "true" if cfg.get("desktop_audio") else "false",
            "SETUP_CAMERA": str(cfg.get("camera") or "off"),
            "SETUP_CAMERA_DEVICE": str(cfg.get("camera_device") or ""),
            # WxH+X+Y, the same spelling as every other rectangle that crosses this
            # boundary, so the shell never has to know a second geometry format.
            # Empty when the bar sent none, which means "keep the defaults".
            "SETUP_CAMERA_RECT": (
                format_geometry(cfg["camera_rect"]) if cfg.get("camera_rect") else ""
            ),
            # The window this target was cut from, when it was cut from one. The
            # recorder logs where that window goes, so a window that moves mid-take
            # is a framing decision the editor can still make rather than a ruined
            # recording. Empty for a monitor or a hand-drawn area.
            "SETUP_WINDOW": str(cfg.get("window") or ""),
            # Opt-in: record the display and carry the selection as a crop, so the
            # framing stays editable afterwards. Off means capture only what was
            # selected, which is what picking one window usually MEANS.
            "SETUP_FULL_MONITOR": "true" if cfg.get("full_monitor") else "false",
            # Capture the window's own pixels rather than the rectangle it sits in.
            # A different backend entirely -- see the recorder's capture_backend.
            "SETUP_WINDOW_ISOLATED": "true" if cfg.get("window_isolated") else "false",
            "SETUP_COUNTDOWN_S": str(int(cfg.get("countdown") or 0)),
        }
        for k, v in emit.items():
            print(f"{k}={shlex.quote(v)}")
        return 0

    if a.cmd == "begin":
        logical = parse_geometry(a.logical)
        bundle = begin(
            Path(a.root),
            logical_geometry=logical,
            monitor_name=a.monitor,
            monitor_scale=a.scale,
            camera_burned_in=a.burn_in,
            calibration_c_ms=a.calibration_ms,
            camera_rect=parse_geometry(a.camera_rect) if a.camera_rect else None,
            camera_shape=a.camera_shape,
            source_logical=parse_geometry(a.source_logical) if a.source_logical else None,
            window_isolated=a.window_isolated,
        )
        # The grid gsr actually ENCODES, which is the stream, not the frame. When the
        # capture records more than it shows -- a region that needs a self-view, a
        # window whose framing has to stay editable -- the frame can sit well under
        # h264's 4096 ceiling while the monitor being encoded sits well over it, and
        # sizing the encoder to the frame would name a codec that cannot encode what
        # is being handed to it.
        physical = bundle.capture.physical_geometry
        encoded = (
            to_physical(parse_geometry(a.source_logical), a.scale)
            if a.source_logical
            else physical
        )
        size = capture_size(encoded)
        # Shell-quoted because the caller evals this and the recordings directory is a
        # user path: XDG_VIDEOS_DIR is routinely "~/My Videos".
        emit = {
            "BUNDLE": str(bundle.root),
            "LOGICAL": format_geometry(logical),
            "PHYSICAL": format_geometry(physical),
            # gsr reads 0x0 as "native"; keeping the sentinel here means the script never
            # has to branch on whether a cap applied.
            "CAPTURE_SIZE": f"{size[0]}x{size[1]}" if size else "0x0",
            # Paired with the size: a grid past h264's ceiling is only capturable at all
            # because the codec was named to match it.
            "CAPTURE_CODEC": capture_codec(encoded),
            # Non-empty when the stream is larger than the frame, which is the recorder's
            # signal that it must take the PORTAL: only that backend can hide the
            # self-view, and it is the reason we are streaming more than was asked for.
            "SOURCE_CROP": (
                "{width}x{height}+{x}+{y}".format(**bundle.capture.source_crop)
                if bundle.capture.source_crop else ""
            ),
        }
        for key, value in emit.items():
            print(f"{key}={shlex.quote(value)}")
        return 0

    bundle, camera_fault = finalize(
        Path(a.root),
        screen=a.screen,
        camera=a.camera,
        camera_timestamps=a.camera_timestamps,
    )
    s = bundle.capture.screen

    # The countdown's frames are recorded on purpose -- capture starts the moment Record
    # is pressed so gsr's startup hides inside the countdown -- and simply not kept.
    # render.py already treats trim_head_frames as a leading cut, so events, layers and
    # zoom all remap through the same CutMap and nothing downstream has to know these
    # frames were a countdown rather than an edit. Converted here because this is the
    # first point that knows the real frame rate; assuming 60 would mistrim every 30fps
    # capture by half.
    #
    # Against the ANCHOR, not against zero. gsr's first frame does not arrive when gsr
    # is launched: on a cold start it has been seen to land after the whole countdown
    # had already elapsed, and a flat "drop the first 3 seconds" would then have cut
    # three seconds of the actual take. Subtracting the anchor makes the trim exactly
    # "the frames captured while the setup surfaces were still up", which is zero when
    # capture only got going afterwards.
    trim_frames = 0
    if a.trim_until_monotonic_us > 0 and s.anchor_us is not None:
        ahead_us = a.trim_until_monotonic_us - s.anchor_us
        if ahead_us > 0:
            trim_frames = round(ahead_us / 1e6 * s.fps_num / s.fps_den)
    elif a.trim_head_seconds > 0:
        trim_frames = round(a.trim_head_seconds * s.fps_num / s.fps_den)

    if trim_frames > 0:
        bundle.edit.trim_head_frames = trim_frames
        bundle.save_edit()
        print(f"TRIM_HEAD_FRAMES={bundle.edit.trim_head_frames}")

    print(f"SCREEN={s.width}x{s.height}@{s.fps_num}/{s.fps_den} anchor={s.anchor_us}")
    if bundle.capture.camera:
        c = bundle.capture.camera
        print(f"CAMERA={c.width}x{c.height}@{c.fps_num}/{c.fps_den} anchor={c.anchor_us}")
        print(f"CAMERA_OFFSET_FRAMES={bundle.camera_offset_frames()}")
        # In camera frames, as stored. The render works in screen frames
        # (Bundle.camera_warmup_frames), but this line is provenance for the log, and
        # what was measured is what belongs in it.
        print(f"CAMERA_WARMUP_FRAMES={c.warmup_frames}")
    if camera_fault:
        # stdout, because the caller greps this line to tell the user the camera
        # specifically was lost -- as opposed to the manifest failing, which is now a
        # different and much rarer thing.
        print(f"CAMERA_UNREADABLE={camera_fault}")
        print(f"camera track dropped: {camera_fault}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
