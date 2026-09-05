"""The input-event track: what the pointer did, on the capture's clock.

Two files, because the two kinds of event have opposite shapes.

`events/cursor.bin` is a dense fixed-rate track -- 120 samples a second, forever. JSON
costs ~45 bytes a sample there, so ten minutes of recording is 3.2MB of mostly-repeated
digits. It is stored instead as a 36-byte header plus zigzag-varint deltas of
(dt, dx, dy). The dt delta is taken against the *nominal* sample period rather than
against zero: paced sampling makes every real interval period +/- a few hundred
microseconds, so the residual fits in one or two bytes where the raw 8333 needs three.

`events/input.jsonl` is sparse and heterogeneous -- clicks, scroll notches, chapter
marks, each with its own fields, a few hundred in a long recording. One JSON object per
line costs nothing at that density and stays greppable and hand-editable, which matters
because it is the file a user will look at when a zoom lands in the wrong place.

Both files store CLOCK_MONOTONIC microseconds, never wall time and never frame indices.
Frames cannot be computed at record time: the anchor comes from the screen stream's
first-frame sidecar, which does not exist until the recording is finalized. Wall time is
not usable because it can step. The conversion happens once, at load, in `map_clicks`.

THE THREE-PART MAPPING. A click's position in the finished video needs all of:

  (a) the monotonic anchor -- `capture.screen.anchor_us`, the CLOCK_MONOTONIC time of
      video frame 0. Without it every frame index is offset by however long the
      recorder happened to take to start.
  (b) the calibration offset -- `capture.calibration_c_ms`, the compositor-to-capture
      latency, measured at 36-45ms on this machine. The compositor stamps the click
      when it processes the button; the pixels showing its effect reach the encoder
      that much later, so the offset is ADDED.
  (c) the capture rectangle -- events carry LOGICAL compositor coordinates, so a click
      is placed by NORMALIZING it against the logical rectangle that was captured. A
      click at logical (1000, 650) inside `-w 1600x900+200+200` is 0.5, 0.5 of the way
      across it, whatever resolution the video turned out to be.

      Deliberately not `monitor_scale`. Multiplying logical coordinates by the scale
      assumes the video is the physical size of the region, and it often is not:
      the video is whatever capture.capture_size and --resolution settled on, which
      is not the physical size whenever either applied -- and every click landed at
      twice its coordinate back when a 5120x2880 display was halved to 2560x1440 --
      cx of 1.85 for a click 92% of the way across the screen. The zoom then framed a
      point off the canvas entirely. The captured rectangle and the video's own size
      already carry everything needed, and neither can disagree with itself.

Any one of the three missing misplaces every zoom, and it misplaces them plausibly --
a zoom that is 40ms late or half a screen off still looks like a zoom, so nothing
downstream can detect the mistake. `map_clicks` applies all three or raises.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .project import Capture
from .timebase import Timebase

CURSOR_MAGIC = b"OSCU"
CURSOR_VERSION = 1
# magic, version, flags, reserved, t0_us, hz_milli, scale_milli, x0, y0, count -- 36 bytes.
# Rates and scales are stored as thousandths rather than floats so that every value a
# compositor actually uses (120Hz, scale 1.25, scale 1.6) round-trips exactly.
CURSOR_HEADER = struct.Struct("<4sBBHqIIiiI")
_FLAG_HAS_FIRST = 0x01  # t0_us/x0/y0 hold a real sample rather than placeholder zeros
_FLAG_FINALIZED = 0x02  # close() ran, so `count` is authoritative

INPUT_SCHEMA = 1
WINDOW_SCHEMA = 1


class EventsError(RuntimeError):
    pass


# --- varints ----------------------------------------------------------------


def _zigzag(v: int) -> int:
    return (v << 1) if v >= 0 else ((-v << 1) - 1)


def _unzigzag(u: int) -> int:
    return (u >> 1) ^ -(u & 1)


def _put_uvarint(buf: bytearray, v: int) -> None:
    if v < 0:
        raise EventsError(f"uvarint cannot encode {v}")
    while v >= 0x80:
        buf.append((v & 0x7F) | 0x80)
        v >>= 7
    buf.append(v)


def _get_uvarint(data: bytes, i: int) -> tuple[int, int]:
    """(value, next index). Raises IndexError past the end, which is how a torn
    trailing record is detected -- a SIGKILLed recorder leaves one about half the time."""
    result = 0
    shift = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise EventsError("varint longer than 10 bytes; cursor.bin is corrupt")


# --- cursor.bin -------------------------------------------------------------


@dataclass(frozen=True)
class CursorSample:
    """One pointer position. `x`/`y` are LOGICAL compositor pixels, absolute across the
    whole desktop -- they are not yet relative to the captured region."""

    t_us: int  # CLOCK_MONOTONIC microseconds
    x: int
    y: int


@dataclass(frozen=True)
class CursorTrack:
    samples: list[CursorSample]
    hz: float
    # Monitor scale as the recorder saw it. The authority for coordinate conversion is
    # capture.monitor_scale; this copy exists so a mismatch between the two -- which
    # means the display was rescaled mid-recording -- is detectable rather than silent.
    scale: float
    t0_us: int
    # False when the writer never got to stamp the sample count: the process was killed.
    # The samples are still good; only the tail is unknown.
    finalized: bool

    def __len__(self) -> int:
        return len(self.samples)


def _encode_hz(hz: float) -> int:
    return int(round(hz * 1000))


def _period_us(hz_milli: int) -> int:
    """Nominal microseconds between samples, the baseline the dt deltas are taken
    against. Zero for an unpaced track, which makes the deltas absolute."""
    return round(1_000_000_000 / hz_milli) if hz_milli > 0 else 0


class CursorWriter:
    """Streaming writer. The daemon appends as it samples rather than buffering the
    whole track, so a killed recorder loses at most `flush_every` samples instead of
    all of them -- and the file it leaves behind still reads back (see `read_cursor`).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        hz: float = 120.0,
        scale: float = 1.0,
        flush_every: int = 120,
    ) -> None:
        self.path = Path(path)
        self._hz_milli = _encode_hz(hz)
        self._scale_milli = int(round(scale * 1000))
        self._period = _period_us(self._hz_milli)
        self._flush_every = max(1, flush_every)
        self._fh = self.path.open("wb")
        self._first: CursorSample | None = None
        self._prev: CursorSample | None = None
        self._count = 0
        self._since_flush = 0
        self._closed = False
        # The header is written three times -- now, at the first sample, and at close --
        # so that the file on disk is self-describing at every instant. A recorder that
        # is SIGKILLed mid-run never reaches close(), and a header of placeholder zeros
        # would fail its own magic check and take the whole recording with it.
        self._write_header()

    def _write_header(self) -> None:
        flags = (_FLAG_HAS_FIRST if self._first else 0) | (
            _FLAG_FINALIZED if self._closed else 0
        )
        here = self._fh.tell()
        self._fh.seek(0)
        self._fh.write(
            CURSOR_HEADER.pack(
                CURSOR_MAGIC,
                CURSOR_VERSION,
                flags,
                0,
                self._first.t_us if self._first else 0,
                self._hz_milli,
                self._scale_milli,
                self._first.x if self._first else 0,
                self._first.y if self._first else 0,
                self._count,
            )
        )
        self._fh.seek(max(here, CURSOR_HEADER.size))

    def append(self, sample: CursorSample) -> None:
        if self._closed:
            raise EventsError("append after close")
        if self._prev is None:
            self._first = sample
            self._prev = sample
            self._count = 1
            self._write_header()
            self._fh.flush()
            return
        buf = bytearray()
        _put_uvarint(buf, _zigzag((sample.t_us - self._prev.t_us) - self._period))
        _put_uvarint(buf, _zigzag(sample.x - self._prev.x))
        _put_uvarint(buf, _zigzag(sample.y - self._prev.y))
        self._fh.write(buf)
        self._prev = sample
        self._count += 1
        self._since_flush += 1
        if self._since_flush >= self._flush_every:
            self._fh.flush()
            self._since_flush = 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._write_header()
        self._fh.close()

    def __enter__(self) -> "CursorWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def write_cursor(
    path: str | Path,
    samples: list[CursorSample],
    *,
    hz: float = 120.0,
    scale: float = 1.0,
) -> int:
    """Write a whole track at once. Returns the file size in bytes."""
    with CursorWriter(path, hz=hz, scale=scale, flush_every=len(samples) or 1) as w:
        for s in samples:
            w.append(s)
    return Path(path).stat().st_size


def read_cursor_track(path: str | Path, *, strict: bool = False) -> CursorTrack:
    """Decode cursor.bin.

    Decoding runs to end-of-file rather than to the header's sample count, and a torn
    trailing record is dropped. That is deliberate: the count is stamped at close, so a
    recorder that was SIGKILLed leaves it at zero, and refusing to open such a file
    would throw away the entire recording over its last 8ms. `strict=True` turns the
    disagreement into an error for tests and for tooling that needs certainty.
    """
    data = Path(path).read_bytes()
    if len(data) < CURSOR_HEADER.size:
        raise EventsError(f"{path} is shorter than a cursor.bin header")
    magic, version, flags, _res, t0_us, hz_milli, scale_milli, x0, y0, count = (
        CURSOR_HEADER.unpack_from(data, 0)
    )
    if magic != CURSOR_MAGIC:
        raise EventsError(f"{path} is not a cursor.bin (magic {magic!r})")
    if version > CURSOR_VERSION:
        raise EventsError(
            f"{path} is cursor.bin version {version}; this build understands "
            f"{CURSOR_VERSION}. Upgrade omarchy-studio rather than editing the file."
        )

    finalized = bool(flags & _FLAG_FINALIZED)
    hz = hz_milli / 1000.0
    scale = scale_milli / 1000.0
    if not flags & _FLAG_HAS_FIRST:
        # No sample was ever taken, so t0/x0/y0 are placeholders and any body bytes are
        # deltas from nothing. Both cases are an empty track.
        return CursorTrack([], hz, scale, 0, finalized)

    period = _period_us(hz_milli)
    samples: list[CursorSample] = [CursorSample(t0_us, x0, y0)]

    i = CURSOR_HEADER.size
    truncated = False
    while i < len(data):
        try:
            ddt, i = _get_uvarint(data, i)
            dx, i = _get_uvarint(data, i)
            dy, i = _get_uvarint(data, i)
        except IndexError:
            truncated = True
            break
        prev = samples[-1]
        samples.append(
            CursorSample(
                prev.t_us + _unzigzag(ddt) + period,
                prev.x + _unzigzag(dx),
                prev.y + _unzigzag(dy),
            )
        )

    if strict and (truncated or not finalized or count != len(samples)):
        why = (
            "torn trailing record"
            if truncated
            else "never finalized -- the recorder was killed"
            if not finalized
            else f"header claims {count} samples, decoded {len(samples)}"
        )
        raise EventsError(f"{path}: {why}")
    return CursorTrack(samples, hz, scale, t0_us, finalized)


def read_cursor(path: str | Path) -> list[CursorSample]:
    return read_cursor_track(path).samples


# --- input.jsonl ------------------------------------------------------------


@dataclass(frozen=True)
class Click:
    t_us: int  # CLOCK_MONOTONIC microseconds
    button: str  # left | right | middle
    x: int  # LOGICAL compositor pixels
    y: int


@dataclass(frozen=True)
class Scroll:
    t_us: int
    direction: str  # up | down
    x: int
    y: int


@dataclass(frozen=True)
class Chapter:
    t_us: int
    label: str


class InputWriter:
    """Append-only writer for input.jsonl.

    Lives here beside the readers so the two cannot drift apart: the field names are
    written once. Line-buffered and flushed per event, because the editor is allowed to
    read this file while a recording is still running.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh = self.path.open("a", buffering=1)

    def _emit(self, obj: dict) -> None:
        self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def meta(self, **fields: object) -> None:
        self._emit({"type": "meta", "schema": INPUT_SCHEMA, **fields})

    def click(self, t_us: int, button: str, x: int, y: int) -> None:
        self._emit({"t_us": t_us, "type": "click", "button": button, "x": x, "y": y})

    def scroll(self, t_us: int, direction: str, x: int, y: int) -> None:
        self._emit(
            {"t_us": t_us, "type": "scroll", "direction": direction, "x": x, "y": y}
        )

    def chapter(self, t_us: int, label: str) -> None:
        self._emit({"t_us": t_us, "type": "chapter", "label": label})

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass

    def __enter__(self) -> "InputWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: str | Path) -> list[dict]:
    """Every line as a dict, in file order.

    A malformed line is skipped rather than fatal. The file is appended to by a live
    recorder, so the last line can be a partial write, and one bad line must not cost
    the user the other four hundred.
    """
    out: list[dict] = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_clicks(path: str | Path) -> list[Click]:
    return [
        Click(int(e["t_us"]), str(e.get("button", "left")), int(e["x"]), int(e["y"]))
        for e in read_events(path)
        if e.get("type") == "click" and "t_us" in e and "x" in e and "y" in e
    ]


def read_scrolls(path: str | Path) -> list[Scroll]:
    return [
        Scroll(int(e["t_us"]), str(e.get("direction", "down")), int(e["x"]), int(e["y"]))
        for e in read_events(path)
        if e.get("type") == "scroll" and "t_us" in e and "x" in e and "y" in e
    ]


def read_chapters(path: str | Path) -> list[Chapter]:
    return [
        Chapter(int(e["t_us"]), str(e.get("label", "")))
        for e in read_events(path)
        if e.get("type") == "chapter" and "t_us" in e
    ]


# --- the three-part mapping -------------------------------------------------


@dataclass(frozen=True)
class MappedClick:
    """A click placed on the video: which frame, and where in the frame."""

    frame: int  # source-timeline frame index
    px: float  # VIDEO pixels, relative to the capture region -- not physical desktop
    py: float  # pixels, which are the same thing only when no encode cap applied
    cx: float  # normalized 0..1 across the canvas, which is what Zoom wants
    cy: float
    button: str


def capture_region(capture: Capture) -> tuple[float, float, float, float]:
    """(x, y, w, h) of the captured area in LOGICAL compositor pixels.

    Written liberally because `Capture.logical_geometry` is a free-form dict in the
    bundle format: `w`/`h` and `width`/`height` both appear in the wild, and a
    full-screen capture has no origin at all.
    """
    g = capture.logical_geometry or {}
    x = float(g.get("x", 0))
    y = float(g.get("y", 0))
    w = float(g.get("w", g.get("width", 0)) or 0)
    h = float(g.get("h", g.get("height", 0)) or 0)
    return x, y, w, h


def map_clicks(
    clicks: list[Click], capture: Capture, tb: Timebase
) -> list[MappedClick]:
    """Place clicks on the video timeline and canvas.

    Applies the anchor, the calibration offset and the capture rectangle together,
    because applying two of the three is worse than applying none: the result still
    looks like plausible zoom targets, so nothing downstream flags it.

    Clicks before frame 0 clamp to frame 0. That is not a guess -- the recorder arms
    its binds before the encoder produces its first frame, so an early click is a real
    click on content the video does start with.
    """
    screen = capture.screen
    if screen is None:
        raise EventsError("capture has no screen stream; cannot place clicks")
    if screen.anchor_us is None:
        raise EventsError(
            "capture.screen.anchor_us is missing. Events are CLOCK_MONOTONIC and the "
            "video's frame 0 is unknown without it, so every frame index would be off "
            "by however long the recorder took to start."
        )
    origin_x, origin_y, region_w, region_h = capture_region(capture)
    if region_w <= 0 or region_h <= 0:
        raise EventsError(
            "capture.logical_geometry carries no size, so a click cannot be placed "
            "against the captured region. Every zoom would frame the wrong point."
        )
    latency_us = round(capture.calibration_c_ms * 1000.0)

    # Clicks on desktop chrome are dropped here, at the one place raw clicks become
    # canvas coordinates, so the editor and the export cannot disagree about it.
    #
    # The case this exists for: the bar carries a recording indicator, and clicking it
    # is how a take is stopped. That click is real, it lands in the recording, and
    # auto-zoom would then push in on the stop button as the last thing the viewer
    # sees. Nobody wants a video that ends by zooming into the button that ended it.
    chrome = []
    for r in (capture.chrome_rects or []):
        try:
            chrome.append((int(r["x"]), int(r["y"]), int(r["width"]), int(r["height"])))
        except (KeyError, TypeError, ValueError):
            continue

    def is_chrome(cx: int, cy: int) -> bool:
        return any(x <= cx < x + w and y <= cy < y + h for x, y, w, h in chrome)

    out: list[MappedClick] = []
    for c in clicks:
        if is_chrome(c.x, c.y):
            continue
        t_rel_us = c.t_us - screen.anchor_us + latency_us
        frame = tb.to_frame(t_rel_us / 1e6) if t_rel_us > 0 else 0
        # Both spaces are logical, so this is a pure ratio -- monitor_scale cancels out
        # of it and cannot be applied wrongly. See (c) in the module docstring.
        cx = (c.x - origin_x) / region_w
        cy = (c.y - origin_y) / region_h
        out.append(
            MappedClick(
                frame=frame,
                px=cx * screen.width,
                py=cy * screen.height,
                cx=cx,
                cy=cy,
                button=c.button,
            )
        )
    return out


def clicks_to_frames(clicks: list[Click], capture: Capture, tb: Timebase) -> list[int]:
    """Source-timeline frame index of each click, in input order."""
    return [m.frame for m in map_clicks(clicks, capture, tb)]


# --- window track -----------------------------------------------------------
#
# A window target is recorded as a monitor plus a rectangle to crop back to. The
# rectangle is a snapshot of where the window was when the user picked it, and a
# window is free to move afterwards -- so the crop has to be a track, not a number.
#
# Unlike the cursor, a window rect is a step function: it holds still for seconds
# and then jumps. Sampling it into a fixed-rate binary track would spend thousands
# of samples restating "it did not move", so this track stores CHANGES, and the
# reader holds the last value between them. The sampler still polls at a fixed rate;
# what the rate buys is the resolution of the jump, not a row per tick.


@dataclass(frozen=True)
class WindowSample:
    t_us: int
    x: int
    y: int
    w: int
    h: int


@dataclass
class WindowTrack:
    """Where the followed window was, over the life of the recording.

    `gone_us` is when the window stopped existing (closed, or moved to another
    workspace), or None if it outlived the recording. It is kept apart from the
    samples because "the window is gone" and "the window is where it last was"
    have to stay distinguishable: a follow that keeps panning to a dead window's
    last rect is a bug, and one that snaps to the origin is a worse one.
    """

    address: str = ""
    title: str = ""
    hz: float = 0.0
    samples: list[WindowSample] = field(default_factory=list)
    gone_us: int | None = None

    def rect_at(self, t_us: int) -> tuple[int, int, int, int] | None:
        """The window's rect at `t_us`, holding the last sample forward.

        None only when the track is empty or `t_us` precedes the first sample --
        never for a t_us past the end, where the answer is "wherever it last was".
        A closed window is NOT None either; `gone_us` says that, and the caller
        decides whether to keep framing the space it left.
        """
        if not self.samples or t_us < self.samples[0].t_us:
            return None
        lo, hi = 0, len(self.samples) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.samples[mid].t_us <= t_us:
                lo = mid
            else:
                hi = mid - 1
        s = self.samples[lo]
        return (s.x, s.y, s.w, s.h)

    @property
    def moved(self) -> bool:
        """Whether the window ever changed rect. A track with one sample is the
        common case -- nothing moved -- and the editor should not offer to animate
        a crop that would sit perfectly still."""
        return len(self.samples) > 1


class WindowWriter:
    """Append-only writer for window.jsonl, emitting only on change.

    Line-buffered like InputWriter, for the same reason: the editor may read this
    while the recording is still going.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh = self.path.open("a", buffering=1)
        self._last: tuple[int, int, int, int] | None = None

    def _emit(self, obj: dict) -> None:
        self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def meta(self, *, address: str, title: str, hz: float) -> None:
        self._emit({"type": "meta", "schema": WINDOW_SCHEMA,
                    "address": address, "title": title, "hz": round(hz, 3)})

    def sample(self, t_us: int, x: int, y: int, w: int, h: int) -> bool:
        """Record the rect. Returns whether it was actually written, so a caller
        can count real motion rather than ticks."""
        rect = (x, y, w, h)
        if rect == self._last:
            return False
        self._last = rect
        self._emit({"t_us": int(t_us), "x": x, "y": y, "w": w, "h": h})
        return True

    def gone(self, t_us: int) -> None:
        self._emit({"t_us": int(t_us), "type": "gone"})

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass

    def __enter__(self) -> "WindowWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_window_track(path: str | Path) -> WindowTrack:
    """Parse window.jsonl. A missing file is an empty track, not an error: most
    recordings have no window to follow, and the editor asks unconditionally."""
    track = WindowTrack()
    for ev in read_events(path):
        kind = ev.get("type")
        if kind == "meta":
            track.address = str(ev.get("address") or "")
            track.title = str(ev.get("title") or "")
            try:
                track.hz = float(ev.get("hz") or 0.0)
            except (TypeError, ValueError):
                track.hz = 0.0
            continue
        if kind == "gone":
            try:
                track.gone_us = int(ev["t_us"])
            except (KeyError, TypeError, ValueError):
                pass
            continue
        try:
            track.samples.append(WindowSample(
                int(ev["t_us"]), int(ev["x"]), int(ev["y"]), int(ev["w"]), int(ev["h"])))
        except (KeyError, TypeError, ValueError):
            continue
    track.samples.sort(key=lambda s: s.t_us)
    return track
