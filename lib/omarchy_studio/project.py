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

from . import backgrounds
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
    # Frames of sensor warm-up at the head of this stream, in THIS stream's own frames.
    # Zero for the screen, which has no iris; measured for the camera at finalize by
    # capture.measure_warmup_frames. Nine on media/cam.mp4 of the 2026-09-03 capture,
    # whose mean luma runs 0.9, 0.9, 15, 40, 56, 66, 78, 91, 100 and only settles at
    # ~106 from frame 9 -- 0.3 s of the camera bubble fading up from black at the top of
    # every export. Stored rather than re-derived, because deriving it costs a decode of
    # the camera's head (0.28 s measured) and a render must not pay that every time.
    #
    # Absent from every capture.json written before this existed, which is why it has a
    # default: an old bundle loads with 0 and renders byte-identically to how it did.
    warmup_frames: int = 0

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
    # Where the FRAME sits inside the captured STREAM, in stream pixels, or empty when
    # they are the same thing.
    #
    # A region capture cannot have a live self-view on the KMS backend: KMS reads the
    # DRM scanout below the compositor, so no_screen_share is invisible to it and the
    # bubble would be welded into every frame. The portal CAN hide it -- that is what
    # the plugin is for -- but the portal only ever hands over a whole monitor. So a
    # region that wants a self-view captures the MONITOR through the portal and carries
    # the region here; the renderer crops before anything else and every stage
    # downstream sees exactly the frame the user chose.
    #
    # It also means the framing stops being a decision made before recording: the whole
    # monitor is on disk, so the crop can be moved afterwards.
    source_crop: dict = field(default_factory=dict)
    # The stream IS the window's own surface tree, captured through Hyprland's
    # toplevel-export protocol rather than as a rectangle of the screen. Nothing that
    # was drawn over the window is in these frames, and the frame followed the window
    # without needing a track -- so `follow` has nothing to offer such a bundle.
    window_isolated: bool = False
    # Absolute logical rectangles that are DESKTOP CHROME rather than content -- the
    # bar, principally. Recorded at begin time because the compositor knows it then and
    # nothing downstream can work it out later. A click landing in one of these is a
    # click on the recorder's own controls, not on the thing being demonstrated.
    chrome_rects: list = field(default_factory=list)

    @property
    def canvas(self) -> Canvas:
        """The FRAME, which is the crop when there is one -- not the stream.

        Everything downstream composes on the canvas, so making the crop the canvas is
        what keeps a portal-and-crop region capture identical to a direct one from here
        on: the same coordinates, the same webcam placement, the same zoom.
        """
        if self.screen is None:
            raise ProjectError("capture has no screen stream")
        crop = self.crop_rect()
        if crop is not None:
            return Canvas(crop[2], crop[3])
        return Canvas(self.screen.width, self.screen.height)

    def crop_rect(self) -> tuple[int, int, int, int] | None:
        """(x, y, w, h) in STREAM pixels, or None. Clamped to the stream, because a
        crop running past the frame makes ffmpeg refuse the whole graph."""
        c = self.source_crop
        if not c or self.screen is None:
            return None
        try:
            x, y = max(0, int(c["x"])), max(0, int(c["y"]))
            w, h = int(c["width"]), int(c["height"])
        except (KeyError, TypeError, ValueError):
            return None
        w = min(w, self.screen.width - x)
        h = min(h, self.screen.height - y)
        if w <= 0 or h <= 0:
            return None
        # Even, for yuv420 chroma siting.
        return x - (x & 1), y - (y & 1), w - (w & 1), h - (h & 1)

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
    # "" | "head" | "tail". When set, `t` is frames within THAT PAD rather than source
    # frames -- a pad has no source frames, so it needs its own coordinate space. See
    # CutMap.remap_pad for why this is not folded into source time.
    pad: str = ""
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
    # Bottom-right with an even margin, at a size that reads as a talking head rather
    # than a second subject. 0.22 was a quarter of the frame wide -- big enough that
    # people asked how to shrink it before they asked anything else about it.
    x: float = 0.83
    y: float = 0.72
    w: float = 0.14
    h: float = 0.14
    shape: str = "circle"  # circle | rounded | rect
    corner_radius: float = 0.12
    mirror: bool = True

    # One vocabulary, three shapes, everywhere: the setup bar, this model, the editor
    # panel and the live self-view all say circle / rounded / rect. They did not always
    # -- the bar said "squircle" and "corner", the model said "squircle" and "rounded",
    # and the editor panel offered three labels against four values, so choosing
    # "Rounded" in the editor actually wrote "squircle" and choosing "Rect" wrote
    # "rounded". `rounded` IS the superellipse now; the old shallow rounded rectangle is
    # gone, and both of its former names land on it.
    LEGACY_SHAPES = {"squircle": "rounded", "corner": "rounded"}

    def __post_init__(self) -> None:
        self.shape = self.LEGACY_SHAPES.get(self.shape, self.shape)
        if self.shape not in ("circle", "rounded", "rect"):
            self.shape = "circle"

    def placement(self, canvas: Canvas) -> Placement:
        """The camera box, square in PIXELS for a circle.

        `w` and `h` are normalized against different axes, so equal values give an
        ellipse on any non-square canvas -- 0.22/0.22 on 1280x720 is 282x158, which is
        what shipped and looked wrong. The design carries ONE size control (spec 1g),
        so `w` is the size and a circular camera derives its height from it.

        `rect` keeps both values, because a deliberately wide camera box is a legitimate
        thing to want and only a round shape is a lie when it is not round.
        """
        h = self.h
        if self.shape in ("circle", "rounded"):
            h = self.w * canvas.width / canvas.height
        return Placement(self.x, self.y, self.w, h, "top-left")


@dataclass
class CursorSettings:
    """The synthetic pointer drawn back in over the recording.

    It has to be drawn back in: the screen is captured with the hardware cursor off
    (`-cursor no`), so without this the exported video has no pointer at all. `enabled`
    therefore defaults to TRUE, and an edit.json written before this existed carries no
    `cursor` key and picks the default up -- which is the intended behaviour, because the
    alternative is that every project made so far renders a mouse-driven demo with no
    mouse in it.

    Sizes are normalized like every other dimension in the project, so a setting chosen
    against the 1080p preview proxy renders identically on the 1440p master.
    """

    enabled: bool = True
    # Height as a fraction of the canvas. 0.022 is 32px at 1440p, which is about what a
    # scale-1 desktop cursor measures there; anything under ~24px reads as a speck once
    # the video is playing in a feed at half size.
    size: float = 0.022
    # 0..1, converted to a Gaussian sigma in SECONDS by cursor.py so the same value
    # smooths the same amount at 30 and at 120fps. 0.5 is 40ms.
    smoothing: float = 0.5
    click_ripple: bool = True
    # ~0.35s at 60fps. Stored in frames like every other duration in the project, so it
    # cannot drift against the grid the ripple's gates are evaluated on.
    ripple_frames: int = 21

    # The size control's range, as a fraction of the canvas height. The floor is a
    # pointer still visible at half playback size; the ceiling is a pointer that hides
    # what it is pointing at.
    MIN_SIZE = 0.008
    MAX_SIZE = 0.08

    def __post_init__(self) -> None:
        # Clamped rather than validated, on the WebcamSettings precedent: this object is
        # constructed from a file as often as from the UI, and a hand-edited size of 5.0
        # should render a big pointer, not refuse to open the recording.
        self.size = min(max(float(self.size), self.MIN_SIZE), self.MAX_SIZE)
        self.smoothing = min(max(float(self.smoothing), 0.0), 1.0)
        self.ripple_frames = max(0, int(self.ripple_frames))

    @classmethod
    def from_dict(cls, d: dict) -> "CursorSettings":
        """Unknown keys are dropped rather than fatal.

        Same forward-compatibility rule Layer follows for unknown types: a project
        written by a newer build loses the setting it asked for, it does not refuse to
        open. `CursorSettings(**d)` would raise TypeError on the first field added after
        this one, and it would do it while loading a file the user cannot edit back.
        """
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class BackdropSettings:
    """The plate the recording is inset on.

    `background` names an entry in `backgrounds.CATALOG`; `backgrounds.CUSTOM` means
    "use `color`, and `gradient` if it is set". The DEFAULT is CUSTOM, and that is a
    compatibility decision rather than a taste one: every edit.json written before the
    library existed carries only `color`/`gradient`, and so does everything the headless
    editor writes today (`--backdrop=color:#101010` sets those two fields and nothing
    else). A catalogue id as the default would silently outrank both.
    """

    enabled: bool = False
    background: str = backgrounds.CUSTOM
    color: str = "#1b1d24"
    gradient: str | None = None
    padding: float = 0.04
    corner_radius: float = 0.015
    shadow: bool = True

    def __post_init__(self) -> None:
        # An id this build does not know degrades to the custom colour rather than
        # raising, which is the same forward-compatibility rule Layer follows for
        # unknown types: a project written by a newer build loses the ground it asked
        # for, it does not refuse to open. The BRIDGE does raise on an unknown id --
        # there it came from a click in this build's own UI, so it is a bug, not a file
        # from the future.
        if self.background != backgrounds.CUSTOM and backgrounds.find(self.background) is None:
            self.background = backgrounds.CUSTOM


# An hour of pad at 60fps. A pad is output-only time and its length went straight into
# `-frames:v`, unbounded: `{"tail_pad_frames": 10000000000}` in an edit.json asked
# ffmpeg for ten billion frames, which encodes until the disk is full. A title card is
# seconds long; an hour is already absurd and still finite.
MAX_PAD_FRAMES = 60 * 60 * 60


def _clamp_pad(value: object) -> int:
    """A pad length that cannot turn an export into a disk-fill."""
    try:
        frames = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(frames, MAX_PAD_FRAMES))


# The export sizes, named by intent. Heights, because that is how the names read; the
# width follows the canvas aspect. `native` is the capture's own grid.
#
# Capture now runs at the panel's native resolution (capture.capture_size), which for a
# 5K panel means the master is 5120x2880 -- right for capture, wrong as a default for
# EXPORT: h264 above 4096 is refused by a lot of players and upload targets, and the
# encode is punishing. So the size is chosen here instead, and the master is never
# touched. This is also why capturing native pays off even when exporting smaller: the
# auto-zoom CROPS INTO the master, so a 1.8x zoom on a 5120-wide capture still has real
# pixels where a 2560-wide one would be upscaling.
EXPORT_PRESETS = ("1080p", "1440p", "4k", "native")
# 1440p on this panel is exactly the logical desktop size, so text lands on the pixel
# grid a viewer actually sees, it plays everywhere, and it renders in reasonable time.
DEFAULT_EXPORT_PRESET = "1440p"


@dataclass
class Edit:
    """Everything the editor writes. Deleting this file resets to defaults."""

    version: int = EDIT_VERSION
    cuts: list[FrameRange] = field(default_factory=list)
    layers: list[Layer] = field(default_factory=list)
    zoom: ZoomSettings = field(default_factory=ZoomSettings)
    webcam: WebcamSettings = field(default_factory=WebcamSettings)
    backdrop: BackdropSettings = field(default_factory=BackdropSettings)
    cursor: CursorSettings = field(default_factory=CursorSettings)
    normalize_audio: bool = True
    # Remove impulsive noise -- a mechanical keyboard under a voice track is the case
    # it exists for. OFF by default: it is a judgement about the recording, and one
    # that should be made after hearing it rather than applied to everything.
    declick_audio: bool = False
    trim_head_frames: int = 0
    # Output-only time at each end, so a title or end card has somewhere to live. Cuts
    # can only remove time; these are the only way the output gets longer than the
    # capture. Nothing recorded exists inside them -- no camera, no audio.
    head_pad_frames: int = 0
    tail_pad_frames: int = 0
    # Named by intent, resolved against the canvas at render time -- see
    # render.EXPORT_HEIGHTS. Lives here rather than as a flag-only option so the editor
    # and the CLI cannot disagree about what "export this" means, and so the choice
    # survives closing the editor. Capture keeps every pixel; this is where the size is
    # actually decided.
    export_preset: str = DEFAULT_EXPORT_PRESET
    # Pan the crop to keep the recorded window in frame, using events/window.jsonl.
    # Default off: turning it on changes the framing of every frame, and that is a
    # decision the user makes after seeing the take, not one inherited silently.
    # Ignored -- not an error -- when the bundle has no window track to follow.
    follow_window: bool = False

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "cuts": [c.to_dict() for c in self.cuts],
            "layers": [l.to_dict() for l in self.layers],
            "zoom": asdict(self.zoom),
            "webcam": asdict(self.webcam),
            "backdrop": asdict(self.backdrop),
            "cursor": asdict(self.cursor),
            "normalize_audio": self.normalize_audio,
            "declick_audio": self.declick_audio,
            "trim_head_frames": self.trim_head_frames,
            "head_pad_frames": self.head_pad_frames,
            "tail_pad_frames": self.tail_pad_frames,
            "export_preset": self.export_preset,
            "follow_window": self.follow_window,
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
            cursor=CursorSettings.from_dict(d.get("cursor", {})),
            normalize_audio=bool(d.get("normalize_audio", True)),
            declick_audio=bool(d.get("declick_audio", False)),
            trim_head_frames=int(d.get("trim_head_frames", 0)),
            head_pad_frames=_clamp_pad(d.get("head_pad_frames", 0)),
            tail_pad_frames=_clamp_pad(d.get("tail_pad_frames", 0)),
            # An unknown name (an older bundle, a hand-edited file) falls back rather
            # than raising: a bad preset must not make a recording unopenable.
            export_preset=(
                str(d.get("export_preset"))
                if str(d.get("export_preset")) in EXPORT_PRESETS
                else DEFAULT_EXPORT_PRESET
            ),
            follow_window=bool(d.get("follow_window", False)),
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
        """The frame everything composes on.

        Following a window changes the crop's SIZE (a window that grew mid-take needs
        a bigger frame than the one it was picked at), so the canvas has to be asked
        of the same plan the renderer crops with. Two answers here would put every
        overlay at the wrong place at once.
        """
        from .follow import for_bundle  # local: follow imports this module

        p = for_bundle(self)
        if p is not None:
            return Canvas(p.w, p.h)
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

    def camera_warmup_frames(self) -> int:
        """The camera's auto-exposure ramp, converted to SCREEN frames.

        Stored in the camera's own frames because that is what was measured, converted
        here because that is what the render needs: the camera is resampled onto the
        project grid before it is aligned, so a trim expressed in camera frames would
        remove the wrong material. Nine camera frames at 30 fps is 18 frames of a 60 fps
        capture, and trimming 9 there would leave half the fade in shot.
        """
        s, c = self.capture.screen, self.capture.camera
        if s is None or c is None or c.warmup_frames <= 0:
            return 0
        seconds = c.warmup_frames * c.fps_den / c.fps_num
        return round(seconds * s.fps_num / s.fps_den)

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
