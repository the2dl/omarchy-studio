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
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import probe, project
from .project import Bundle, Capture, Stream

_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")

# h264 VAAPI hard-fails above 4096x4096 on this hardware, and gsr's `-k auto` may pick
# hevc on a large display, so the cap cannot be conditioned on the codec -- an export
# that assumed h264 would be the thing that broke. Halving (rather than fitting to a
# nominal 4K box) keeps the video an exact integer division of the physical grid, so the
# logical -> video mapping stays one clean ratio.
MAX_CAPTURE_DIM = 4096


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
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=start_time",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
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


# --- manifest ---------------------------------------------------------------


def begin(
    root: Path,
    *,
    logical_geometry: dict,
    monitor_name: str = "",
    monitor_scale: float = 1.0,
    camera_burned_in: bool = False,
    calibration_c_ms: float = 0.0,
) -> Bundle:
    """Lay out the bundle and record everything that is only knowable now."""
    capture = Capture(
        created=datetime.now().astimezone().isoformat(timespec="seconds"),
        logical_geometry=dict(logical_geometry),
        physical_geometry=to_physical(logical_geometry, monitor_scale),
        monitor_scale=float(monitor_scale),
        monitor_name=monitor_name,
        calibration_c_ms=float(calibration_c_ms),
        camera_burned_in=bool(camera_burned_in),
    )
    return project.create(Path(root), capture)


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
) -> Bundle:
    """Probe the recorded files and complete capture.json.

    Missing media is an error, not a warning: a bundle whose manifest describes streams
    that are not there fails much later and much more confusingly.
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

    if camera:
        cam_path = root / camera
        if not cam_path.exists():
            raise CaptureError(f"no camera recording at {cam_path}")
        tsv = root / camera_timestamps if camera_timestamps else None
        anchor = realtime_to_monotonic_us(read_camera_realtime_us(tsv, cam_path), ts_pair)
        capture.camera = _stream(root, camera, anchor)

    (root / "capture.json").write_text(json.dumps(capture.to_dict(), indent=2) + "\n")
    return Bundle(root)


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

    f = sub.add_parser("finalize", help="probe the media and complete capture.json")
    f.add_argument("--root", required=True)
    f.add_argument("--screen", default="media/screen.mp4")
    f.add_argument("--camera", default=None)
    f.add_argument("--camera-timestamps", default="media/cam.tsv")

    a = parser.parse_args(argv)

    if a.cmd == "begin":
        logical = parse_geometry(a.logical)
        bundle = begin(
            Path(a.root),
            logical_geometry=logical,
            monitor_name=a.monitor,
            monitor_scale=a.scale,
            camera_burned_in=a.burn_in,
            calibration_c_ms=a.calibration_ms,
        )
        physical = bundle.capture.physical_geometry
        size = capture_size(physical)
        # Shell-quoted because the caller evals this and the recordings directory is a
        # user path: XDG_VIDEOS_DIR is routinely "~/My Videos".
        emit = {
            "BUNDLE": str(bundle.root),
            "LOGICAL": format_geometry(logical),
            "PHYSICAL": format_geometry(physical),
            # gsr reads 0x0 as "native"; keeping the sentinel here means the script never
            # has to branch on whether a cap applied.
            "CAPTURE_SIZE": f"{size[0]}x{size[1]}" if size else "0x0",
        }
        for key, value in emit.items():
            print(f"{key}={shlex.quote(value)}")
        return 0

    bundle = finalize(
        Path(a.root),
        screen=a.screen,
        camera=a.camera,
        camera_timestamps=a.camera_timestamps,
    )
    s = bundle.capture.screen
    print(f"SCREEN={s.width}x{s.height}@{s.fps_num}/{s.fps_den} anchor={s.anchor_us}")
    if bundle.capture.camera:
        c = bundle.capture.camera
        print(f"CAMERA={c.width}x{c.height}@{c.fps_num}/{c.fps_den} anchor={c.anchor_us}")
        print(f"CAMERA_OFFSET_FRAMES={bundle.camera_offset_frames()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
