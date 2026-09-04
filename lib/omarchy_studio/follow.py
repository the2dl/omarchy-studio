"""Turning a window track into a crop that follows the window.

A window capture records the whole monitor and crops back to the window. That crop
is a single rectangle taken the instant the user picked the window, and a window is
free to move afterwards -- which today means the take is of the desktop the window
left behind, with nothing to be done about it after the fact.

This module is the "after the fact". `events/window.jsonl` says where the window
actually was; this turns that into a crop whose POSITION varies with time, so the
framing becomes an editing decision rather than a bet placed before recording.

Three things are deliberate:

(a) The crop's SIZE is constant. A video stream has one frame size, so a window
    that is resized mid-take cannot resize the output. The size is the largest the
    window ever reached, and a smaller moment shows some desktop around it -- which
    is honest, and is what a human framing this by hand would do.

(b) Motion is settled, not tracked. A drag emits a sample every 100ms, and panning
    the frame through every one of them reproduces the user's wobble. A run of
    changes collapses into ONE move to where it ended, eased with a smoothstep.
    This is why the output reads as camera work rather than as telemetry.

(c) Following widens what the overlays have to stay clear of. On KMS nothing can
    hide the HUD or the teleprompter from the stream, so they are PARKED outside the
    chosen frame instead -- and a pan can reach a spot that was outside the frame at
    record time. The default framing is unaffected; a user who turns following on for
    a take whose window travelled toward a parked overlay can see it enter the frame.
    Recording the parked rectangles and steering around them is the fix; until then
    this is a known edge rather than an unknown one.

(d) The plan is emitted as an ffmpeg expression over `t`, not as a per-frame
    keyframe list. `crop` evaluates x/y per frame anyway, the graph already goes to
    ffmpeg as a file so length is free, and one expression cannot drift out of sync
    with the frame grid the way a second time-indexed sidecar could.
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import WindowTrack
from .project import Capture

# How long a settle-to-new-position takes. Long enough to read as a move rather than
# a cut, short enough that the window is not still sliding when the user starts
# talking about what is on it.
DEFAULT_EASE_S = 0.55

# Samples closer together than this belong to the same gesture. A drag is one move
# to where it ended, not thirty moves to where it passed through.
SETTLE_S = 0.35

# Below this, a "move" is a window manager nudging by a pixel or a border redraw.
# Panning the whole frame for it would be visible and pointless.
MIN_MOVE_PX = 8


class FollowError(RuntimeError):
    pass


@dataclass(frozen=True)
class Move:
    """A settle from `frm` to `to`, starting at `t_s` and taking `dur_s`."""

    t_s: float
    dur_s: float
    frm: tuple[float, float]
    to: tuple[float, float]


@dataclass
class FollowPlan:
    """A crop of fixed size whose origin follows the window.

    `moves` is empty when the window never went anywhere, in which case this is an
    ordinary static crop and the renderer emits exactly what it emitted before.
    """

    w: int
    h: int
    origin: tuple[int, int]
    moves: list[Move]

    @property
    def static(self) -> bool:
        return not self.moves

    def rect(self) -> tuple[int, int, int, int]:
        """The static rectangle: where the window started."""
        return (self.origin[0], self.origin[1], self.w, self.h)

    def _expr(self, axis: int) -> str:
        """A smoothstep-interpolated piecewise expression over `t`, built from the
        last move backwards so each move's tail is the next one's head.

        Commas are left bare: the caller wraps this in single quotes inside the
        filtergraph, which is how every other expression in this codebase survives
        contact with ffmpeg's argument splitting.
        """
        expr = f"{self.moves[-1].to[axis]:.1f}"
        for m in reversed(self.moves):
            a, b = m.frm[axis], m.to[axis]
            if abs(b - a) < 1e-9:
                continue
            # Inside this branch t is in [t_s, t_s+dur), so progress is already in
            # [0,1) and needs no clamping -- the guards do it.
            p = f"((t-{m.t_s:.3f})/{m.dur_s:.3f})"
            eased = f"({a:.1f}+{b - a:.1f}*{p}*{p}*(3-2*{p}))"
            expr = (
                f"if(lt(t,{m.t_s:.3f}),{a:.1f},"
                f"if(lt(t,{m.t_s + m.dur_s:.3f}),{eased},{expr}))"
            )
        # Even offsets, for the same chroma-siting reason a static crop is evened.
        # An odd x on yuv420 is not a rounding detail; ffmpeg refuses it.
        return f"2*floor(({expr})/2)"

    def x_expr(self) -> str:
        return self._expr(0)

    def y_expr(self) -> str:
        return self._expr(1)


def stream_origin(capture: Capture) -> tuple[int, int]:
    """Where the STREAM's top-left sits in physical desktop pixels.

    Recovered rather than stored: the frame's physical origin and the frame's offset
    inside the stream are both already in capture.json, and their difference is the
    stream's origin by construction. Storing it again would be a second copy of a
    derived fact, free to disagree with the first.
    """
    phys = capture.physical_geometry or {}
    crop = capture.source_crop or {}
    try:
        fx, fy = int(phys["x"]), int(phys["y"])
    except (KeyError, TypeError, ValueError) as e:
        raise FollowError("capture has no physical_geometry origin") from e
    return fx - int(crop.get("x", 0) or 0), fy - int(crop.get("y", 0) or 0)


def plan(
    track: WindowTrack,
    capture: Capture,
    *,
    ease_s: float = DEFAULT_EASE_S,
    settle_s: float = SETTLE_S,
    min_move_px: float = MIN_MOVE_PX,
) -> FollowPlan | None:
    """Build the follow plan, or None when there is nothing to follow.

    None means "keep whatever crop capture.json already has" -- an empty track, or a
    capture that is not a crop of a larger stream, or a stream whose anchor is
    unknown so the track cannot be placed against frame 0.
    """
    screen = capture.screen
    if screen is None or not track.samples:
        return None
    if not capture.source_crop:
        # No surrounding pixels to pan into: the stream IS the frame.
        return None
    if screen.anchor_us is None:
        # Same rule as clicks: without frame 0 the track's CLOCK_MONOTONIC stamps
        # place the moves at plausible-looking wrong times, which is worse than not
        # moving at all.
        return None

    scale = float(capture.monitor_scale or 1.0)
    ox, oy = stream_origin(capture)
    latency_us = round(capture.calibration_c_ms * 1000.0)

    # (t_s, rect) in stream pixels.
    pts: list[tuple[float, tuple[float, float, float, float]]] = []
    for s in track.samples:
        t_rel = (s.t_us - screen.anchor_us + latency_us) / 1e6
        pts.append((
            max(0.0, t_rel),
            (s.x * scale - ox, s.y * scale - oy, s.w * scale, s.h * scale),
        ))

    # (a) one size for the whole take: the largest the window ever was, capped by
    # the stream, and even for yuv420 chroma siting.
    cw = min(int(max(p[1][2] for p in pts)), screen.width)
    ch = min(int(max(p[1][3] for p in pts)), screen.height)
    cw -= cw & 1
    ch -= ch & 1
    if cw <= 0 or ch <= 0:
        return None

    def origin_for(r: tuple[float, float, float, float]) -> tuple[float, float]:
        """Centre the fixed-size crop on the window, then keep it inside the stream.
        Clamping is what stops a window dragged half off-screen from cropping past
        the frame, which ffmpeg refuses outright rather than clipping."""
        cx = r[0] + r[2] / 2.0
        cy = r[1] + r[3] / 2.0
        x = min(max(cx - cw / 2.0, 0.0), float(screen.width - cw))
        y = min(max(cy - ch / 2.0, 0.0), float(screen.height - ch))
        return x, y

    origins = [(t, origin_for(r)) for t, r in pts]

    # (b) collapse gestures into settles.
    moves: list[Move] = []
    cur = origins[0][1]
    i = 1
    while i < len(origins):
        t0, pos = origins[i]
        if _dist(pos, cur) < min_move_px:
            i += 1
            continue
        j = i
        while j + 1 < len(origins) and origins[j + 1][0] - origins[j][0] <= settle_s:
            j += 1
        dest = origins[j][1]
        if _dist(dest, cur) >= min_move_px:
            span = origins[j][0] - t0
            moves.append(Move(t_s=t0, dur_s=max(ease_s, span), frm=cur, to=dest))
            cur = dest
        i = j + 1

    return FollowPlan(w=cw, h=ch, origin=_even(origins[0][1]), moves=moves)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _even(p: tuple[float, float]) -> tuple[int, int]:
    x, y = int(p[0]), int(p[1])
    return x - (x & 1), y - (y & 1)


def track_path(bundle) -> "object":
    """Where a bundle's window track lives. One spelling, so the recorder, the
    planner and the editor cannot each pick a different filename."""
    return bundle.events_dir / "window.jsonl"


def for_bundle(bundle) -> FollowPlan | None:
    """The plan this bundle's edit asks for, or None.

    The result is memoised on the bundle: `canvas` reads it on every frame the
    editor draws, and the track is written once by the recorder and never again.
    The cache key is the toggle, because that is the only input that changes after
    the recording stops.
    """
    from .events import read_window_track

    want = bool(getattr(bundle.edit, "follow_window", False))
    cached = getattr(bundle, "_follow_cache", None)
    if cached is not None and cached[0] == want:
        return cached[1]
    result = plan(read_window_track(track_path(bundle)), bundle.capture) if want else None
    try:
        bundle._follow_cache = (want, result)
    except AttributeError:
        pass
    return result


def has_track(bundle) -> bool:
    """Whether following is even offerable. The editor asks this to decide whether
    to draw the control at all -- an inert toggle on a recording with nothing to
    follow is a worse answer than no toggle.

    Two conditions, not one. The window has to have MOVED, and the recording has to
    have kept the pixels around it: a take that captured only the selected rectangle
    has nowhere to pan to, so `plan` would answer None and the toggle would sit there
    doing nothing.
    """
    from .events import read_window_track

    if not bundle.capture.source_crop:
        return False
    return read_window_track(track_path(bundle)).moved
