"""The recording bundle: an immutable capture and a mutable edit beside it.

A recording is a directory, not a file:

    screenrecording-2026-09-02_14-32-17/
      capture.json          immutable manifest, written once at finalize
      media/                immutable -- screen.mp4, cam.mp4, cam.tsv, screen.mp4.ts
      events/               immutable -- cursor.bin, input.jsonl
      edit.json             MUTABLE -- the only file the editor writes
      assets/               MUTABLE -- user-added images
      proxy/                derived, disposable, regenerated on demand

The split is the whole point. Re-rendering with different settings must never be able
to damage the original capture, and `reset to defaults` has to be "delete edit.json".

Two fields exist because they are unrecoverable after the fact and cheap now:

* `calibration_c_ms` -- compositor-to-capture latency, measured at 36-45ms and machine
  dependent. Defaults to 0, which is defensible against a 200-300ms zoom ease-in, but
  retrofitting a global time shift once projects exist is a migration.
* geometry in BOTH logical and physical pixels, plus the monitor scale. Cursor events
  arrive in logical coordinates and the video is physical; on a scale-2 display
  `-w 1600x900+200+200` yields a 3200x1800 video, and conflating the two silently
  doubles or halves every zoom focal point.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .geometry import Canvas, Placement, Zoom
from .timebase import CutMap, FrameRange, Timebase

CAPTURE_VERSION = 1
EDIT_VERSION = 1


class ProjectError(RuntimeError):
    pass


# --- immutable side ---------------------------------------------------------


@dataclass
class Stream:
    """One captured media file and the anchor that ties it to the shared clock."""

    path: str
    width: int
    height: int
    fps_num: int
    fps_den: int = 1
    # CLOCK_MONOTONIC microseconds of this stream's frame 0. For the screen this comes
    # from gsr's -write-first-frame-ts sidecar; for the camera, from the per-frame
    # CLOCK_REALTIME track ffmpeg writes, which is strictly better -- it needs no
    # separate calibration because it stamps every frame rather than only the first.
    anchor_us: int | None = None
    has_audio: bool = False

    @property
    def timebase(self) -> Timebase:
        return Timebase(self.fps_num, self.fps_den)


@dataclass
class Capture:
    """Everything about how the recording was made. Never rewritten after finalize."""

    version: int = CAPTURE_VERSION
    created: str = ""
    screen: Stream | None = None
    camera: Stream | None = None
    # Logical (compositor) geometry of what was captured, and the physical pixels it
    # became. These differ by monitor_scale and conflating them is a verified trap.
    logical_geometry: dict = field(default_factory=dict)
    physical_geometry: dict = field(default_factory=dict)
    monitor_scale: float = 1.0
    monitor_name: str = ""
    # Compositor-to-capture latency in ms. Applied when mapping events to frames.
    calibration_c_ms: float = 0.0
    # True when the camera was burned into the screen pixels (the --burn-in path).
    # Such a recording cannot have its webcam moved, and the editor must say so
    # rather than silently offering a control that does nothing.
    camera_burned_in: bool = False

    @property
    def canvas(self) -> Canvas:
        if self.screen is None:
            raise ProjectError("capture has no screen stream")
        return Canvas(self.screen.width, self.screen.height)

    @property
    def timebase(self) -> Timebase:
        if self.screen is None:
            raise ProjectError("capture has no screen stream")
        return self.screen.timebase

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Capture":
        v = int(d.get("version", 0))
        if v > CAPTURE_VERSION:
            raise ProjectError(
                f"capture.json is version {v}; this build understands {CAPTURE_VERSION}. "
                "Upgrade omarchy-studio rather than editing the file."
            )
        streams = {}
        for key in ("screen", "camera"):
            s = d.get(key)
            streams[key] = Stream(**s) if s else None
        rest = {k: val for k, val in d.items() if k not in ("screen", "camera")}
        return cls(**rest, **streams)


# --- mutable side -----------------------------------------------------------


@dataclass
class Layer:
    """One time-ranged element composited over the base video.

    `t` is a source-timeline frame range. Source time, not output time: rendering the
    same project cut-first with remapped ranges and cut-last with verbatim ranges gave
    bit-identical frames, so the remap is provably lossless -- and source time means
    adding or removing a cut never slides an annotation off what it annotates.

    Unknown `type` values are preserved on load and skipped at render, so a project
    written by a newer build degrades to "some overlays missing" rather than failing.
    """

    id: str
    type: str  # image | text | shape | blur | pixelate | webcam | zoom
    t: FrameRange | None = None  # None means "the whole recording"
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    anchor: str = "top-left"
    opacity: float = 1.0
    fade_frames: int = 0
    z: int = 0
    props: dict = field(default_factory=dict)
    enabled: bool = True

    @property
    def placement(self) -> Placement:
        return Placement(self.x, self.y, self.w, self.h, self.anchor)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["t"] = self.t.to_dict() if self.t else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Layer":
        d = dict(d)
        t = d.pop("t", None)
        known = {f for f in cls.__dataclass_fields__ if f != "t"}
        # Fields this build does not know about are stashed in props so a round-trip
        # through an older editor does not silently delete them.
        extra = {k: v for k, v in d.items() if k not in known}
        core = {k: v for k, v in d.items() if k in known}
        layer = cls(**core, t=FrameRange.from_dict(t) if t else None)
        if extra:
            layer.props.setdefault("_unknown", {}).update(extra)
        return layer


@dataclass
class ZoomSettings:
    """Auto-zoom derived from the click track. Toggleable as a whole."""

    enabled: bool = False
    amount: float = 1.8
    hold_frames: int = 72  # ~1.2s at 60fps
    ease_frames: int = 18  # ~0.3s
    # Clicks closer together than this merge into one zoom rather than pumping.
    merge_gap_frames: int = 90


@dataclass
class WebcamSettings:
    """The camera overlay. Only meaningful when the camera was recorded separately."""

    enabled: bool = True
    x: float = 0.72
    y: float = 0.70
    w: float = 0.22
    h: float = 0.22
    shape: str = "circle"  # circle | rounded | rect
    corner_radius: float = 0.12
    mirror: bool = True

    def placement(self, canvas: Canvas) -> Placement:
        """The camera box, square in PIXELS for a circle.

        `w` and `h` are normalized against different axes, so equal values give an
        ellipse on any non-square canvas -- 0.22/0.22 on 1280x720 is 282x158, which is
        what shipped and looked wrong. The design carries ONE size control (spec 1g),
        so `w` is the size and a circular camera derives its height from it.

        `rounded` and `rect` keep both values, because a deliberately wide camera box is
        a legitimate thing to want and only a circle is a lie when it is not round.
        """
        h = self.h
        if self.shape == "circle":
            h = self.w * canvas.width / canvas.height
        return Placement(self.x, self.y, self.w, h, "top-left")


@dataclass
class BackdropSettings:
    enabled: bool = False
    color: str = "#1b1d24"
    gradient: str | None = None
    padding: float = 0.04
    corner_radius: float = 0.015
    shadow: bool = True


@dataclass
class Edit:
    """Everything the editor writes. Deleting this file resets to defaults."""

    version: int = EDIT_VERSION
    cuts: list[FrameRange] = field(default_factory=list)
    layers: list[Layer] = field(default_factory=list)
    zoom: ZoomSettings = field(default_factory=ZoomSettings)
    webcam: WebcamSettings = field(default_factory=WebcamSettings)
    backdrop: BackdropSettings = field(default_factory=BackdropSettings)
    normalize_audio: bool = True
    trim_head_frames: int = 0

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "cuts": [c.to_dict() for c in self.cuts],
            "layers": [l.to_dict() for l in self.layers],
            "zoom": asdict(self.zoom),
            "webcam": asdict(self.webcam),
            "backdrop": asdict(self.backdrop),
            "normalize_audio": self.normalize_audio,
            "trim_head_frames": self.trim_head_frames,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Edit":
        v = int(d.get("version", 0))
        if v > EDIT_VERSION:
            raise ProjectError(
                f"edit.json is version {v}; this build understands {EDIT_VERSION}."
            )
        return cls(
            version=EDIT_VERSION,
            cuts=[FrameRange.from_dict(c) for c in d.get("cuts", [])],
            layers=[Layer.from_dict(l) for l in d.get("layers", [])],
            zoom=ZoomSettings(**d.get("zoom", {})),
            webcam=WebcamSettings(**d.get("webcam", {})),
            backdrop=BackdropSettings(**d.get("backdrop", {})),
            normalize_audio=bool(d.get("normalize_audio", True)),
            trim_head_frames=int(d.get("trim_head_frames", 0)),
        )


# --- the bundle -------------------------------------------------------------


class Bundle:
    """A recording directory. Reads are cheap; only `save_edit` writes."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.capture = self._load_capture()
        self.edit = self._load_edit()

    # -- paths
    @property
    def capture_path(self) -> Path:
        return self.root / "capture.json"

    @property
    def edit_path(self) -> Path:
        return self.root / "edit.json"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def events_dir(self) -> Path:
        return self.root / "events"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def proxy_dir(self) -> Path:
        return self.root / "proxy"

    def media(self, name: str) -> Path:
        return self.media_dir / name

    # -- load / save
    def _load_capture(self) -> Capture:
        if not self.capture_path.exists():
            raise ProjectError(f"{self.root} is not a recording bundle (no capture.json)")
        return Capture.from_dict(json.loads(self.capture_path.read_text()))

    def _load_edit(self) -> Edit:
        if not self.edit_path.exists():
            return Edit()
        return Edit.from_dict(json.loads(self.edit_path.read_text()))

    def save_edit(self) -> None:
        """Atomic replace -- a crash mid-save must not leave an unparseable edit.json
        that makes the recording look corrupt."""
        self.validate()
        tmp = self.edit_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.edit.to_dict(), indent=2) + "\n")
        tmp.replace(self.edit_path)

    def reset_edit(self) -> None:
        self.edit_path.unlink(missing_ok=True)
        self.edit = Edit()

    # -- derived
    @property
    def timebase(self) -> Timebase:
        return self.capture.timebase

    @property
    def canvas(self) -> Canvas:
        return self.capture.canvas

    def source_frames(self) -> int:
        s = self.capture.screen
        if s is None:
            raise ProjectError("no screen stream")
        from .probe import frame_count  # local import: probe shells out to ffprobe

        return frame_count(self.media(Path(s.path).name))

    def cutmap(self) -> CutMap:
        return CutMap(self.edit.cuts, self.source_frames())

    def camera_offset_frames(self) -> int:
        """Camera frame 0 relative to screen frame 0, in screen frames.

        Read per recording, and it can be either sign: the offset seen between two
        files is launch order plus per-pipeline warm-up (KMS 128-137ms, V4L2 210-228ms),
        not a fixed property. Hardcoding a constant here was a verified mistake.
        """
        s, c = self.capture.screen, self.capture.camera
        if s is None or c is None or s.anchor_us is None or c.anchor_us is None:
            return 0
        delta_s = (c.anchor_us - s.anchor_us) / 1e6
        return round(delta_s * s.fps_num / s.fps_den)

    # -- integrity
    def validate(self) -> None:
        """Assert the invariants that silently corrupt renders when violated."""
        total = None
        try:
            total = self.source_frames()
        except Exception:
            pass  # media may be absent in tests; range checks below are then skipped

        for c in self.edit.cuts:
            if total is not None and c.end > total:
                raise ProjectError(f"cut {c} runs past the recording ({total} frames)")
        # Cuts are frame indices by type, so they are snapped by construction. This
        # asserts nothing has hand-edited the file into fractional territory.
        for c in self.edit.cuts:
            if not isinstance(c.start, int) or not isinstance(c.end, int):
                raise ProjectError(f"cut {c} has non-integer bounds; times are frames")
        for l in self.edit.layers:
            if l.t is not None and total is not None and l.t.end > total:
                raise ProjectError(f"layer {l.id} range {l.t} runs past the recording")
            if not 0.0 <= l.opacity <= 1.0:
                raise ProjectError(f"layer {l.id} opacity {l.opacity} outside 0..1")

    def asset(self, name: str) -> Path:
        p = self.assets_dir / name
        if not p.exists():
            raise ProjectError(f"missing asset {name}")
        return p

    def add_asset(self, src: Path) -> str:
        """Copy a user image into the bundle so it stays portable."""
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        dest = self.assets_dir / Path(src).name
        n = 1
        while dest.exists() and dest.read_bytes() != Path(src).read_bytes():
            dest = self.assets_dir / f"{Path(src).stem}-{n}{Path(src).suffix}"
            n += 1
        if not dest.exists():
            shutil.copy2(src, dest)
        return dest.name


def create(root: Path, capture: Capture) -> Bundle:
    """Lay out a new bundle. Called once, at record start."""
    root = Path(root)
    for d in (root, root / "media", root / "events", root / "assets"):
        d.mkdir(parents=True, exist_ok=True)
    (root / "capture.json").write_text(json.dumps(capture.to_dict(), indent=2) + "\n")
    return Bundle(root)
