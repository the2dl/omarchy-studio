"""The synthetic cursor: cursor.bin -> a smoothed path -> two generated sprites.

Screen recordings are captured with the hardware cursor OFF (`-cursor no`), so the
pointer in the finished video is drawn here or it does not exist. That is a deliberate
trade: a composited pointer can be smoothed, resized and re-timed after the fact, and a
burned-in one cannot.

FOUR THINGS DECIDE WHETHER THIS LOOKS RIGHT, AND THREE OF THEM ARE INVISIBLE WHEN WRONG.

(a) THE COORDINATE MAPPING. Cursor samples are LOGICAL compositor pixels, absolute
    across the whole desktop. The video is neither logical pixels nor physical desktop
    pixels: `capture.capture_size` caps the encode at the codec's ceiling, so a 5120x2880
    display records at 5120x2880 with hevc, while a capped or `--resolution` capture
    comes out at something else entirely. A sample is therefore placed by NORMALIZING it against
    the logical rectangle that was captured and multiplying by the video's own size --
    exactly what `events.map_clicks` does for clicks, and deliberately NOT
    `* monitor_scale`, which put every click at twice its coordinate the one time this
    project tried it. The rectangle itself is parsed by `events.capture_region` rather
    than re-read here, so the two consumers cannot disagree about what was captured;
    `test_cursor.py` pins a cursor sample and a click at the same logical coordinate to
    the same canvas pixel.

(b) THE STAGE ORDER. The cursor is composited BEFORE the zoom, onto the un-magnified
    base. A pointer drawn after `perspective` would have to have the zoom's own per-frame
    viewport inverted into all four of its overlay expressions -- four more copies of the
    envelope tree -- and any disagreement between the two would show up as the pointer
    sliding across the thing it is pointing at, which is the one artefact a viewer cannot
    un-see. Compositing first means the zoom magnifies the pointer along with the pixels
    under it, for free and exactly.

(c) THE CUT MAP. Positions are sampled per OUTPUT frame through `CutMap.to_source`, so a
    cut removes the pointer's motion during it rather than replaying it late. Smoothing
    runs in SOURCE time, before the remap: the hand really did move continuously across
    the material a cut removes, and a filter run after the cut would smear the two sides
    of the join into each other.

(d) THE FRAME VARIABLE, WHICH IS NOT THE SAME IN BOTH FILTERS. Measured on the installed
    ffmpeg n9.0.1: `overlay`'s `n` is ONE-BASED -- with `x='n'` the first output frame is
    already at x=1 -- while `crop`'s `n` on the same graph is zero-based, frame 0
    selecting tile 0. So the position expressions carry `(n-1)` and the ripple's tile
    selector carries a bare `n`. Writing one where the other was meant puts the pointer a
    frame ahead of the content it is pointing at, which reads as latency, not as a bug.

TWO SPRITES, BOTH GENERATED. Nothing here opens a cursor theme from disk: a render that
silently loses its pointer because a theme moved is worse than one that never had a
pointer. The arrow is rasterized from a polygon and the ripple is a filmstrip, both
written to a cache directory and reused across renders.

THE POSITION IS A SUM OF SATURATING RAMPS, not a keyframe search: `x(n) = x0 +
sum_i clip((n-a_i)/L_i, 0, 1) * dx_i` is piecewise-linear interpolation written as a
balanced sum, the same shape `zoom.py` uses for its envelopes and for the same reason --
libavutil's evaluator has a recursion budget of 100 that a left-linear chain eats one
term at a time, and a balanced tree of 24000 terms is only 15 deep. Measured at
2560x1440, the same term count on EACH of x and y, against a constant expression:

    terms      ms/frame     graph
    0            1.02        --
    2400         1.15       69 KB
    6000         1.80      175 KB
    12000        2.80      354 KB
    24000        5.39      711 KB

so roughly 0.18 ms/frame per thousand ramps. `_MAX_RAMPS` is set from that table, not
from a guess: 6000 is 0.8 ms/frame, a tenth of what the zoom's `perspective` already
costs at the same resolution, and it holds a ten-minute recording to about a pixel.
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .events import Click, CursorTrack, capture_region, map_clicks
from .exprs import balanced_sum
from .geometry import Canvas
from .project import Capture, CursorSettings
from .timebase import CutMap, Timebase

# `overlay` evaluates its first output frame with n=1, `crop` with n=0. Both measured on
# ffmpeg n9.0.1 in the same graph; see (d) above.
FRAME_OVERLAY = "(n-1)"
FRAME_CROP = "n"

# The arrow, as the classic left-pointing polygon on a 12x19 grid, tip first and wound
# clockwise. Normalized by its own height at rasterization time so `size` is a height in
# canvas pixels and nothing else has to know these numbers.
ARROW_POLYGON: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),      # the hotspot: the tip, and the pixel the user was actually pointing at
    (0.0, 16.0),     # straight left edge
    (3.5, 12.5),     # the notch where the head meets the tail
    (6.0, 18.5),     # tail, bottom left
    (8.5, 17.5),     # tail, bottom right
    (6.0, 11.5),     # tail, top right
    (11.0, 11.5),    # the head's right corner
)
_ARROW_HEIGHT = max(p[1] for p in ARROW_POLYGON)
_ARROW_WIDTH_RATIO = max(p[0] for p in ARROW_POLYGON) / _ARROW_HEIGHT

# Outline width as a fraction of the pointer's height. A pointer is drawn white-on-black
# rather than a single colour because it has to stay legible over BOTH a white document
# and a black terminal, and a solid-white arrow disappears into the first while a
# solid-black one disappears into the second. ~1/16 is what the stock themes use.
OUTLINE_FRACTION = 0.062

# Supersampling for the rasterizer. 4x4 is 16 samples a pixel, which puts the arrow's
# near-vertical left edge within half a level of the analytic coverage; 2x2 leaves it
# visibly ropey at 32px, and 8x8 costs four times as much for no visible difference.
_SUPERSAMPLE = 4

# Smoothing strength 1.0 means this much Gaussian sigma. 80ms is roughly the point past
# which a fast flick starts to visibly cut corners; the 0.5 default is 40ms.
SMOOTH_MAX_SECONDS = 0.08

# Below this the kernel is a single tap and the copy is not worth making.
_MIN_SIGMA_FRAMES = 0.05

# Piecewise-linear simplification. The tolerance starts sub-pixel because `overlay`
# rounds its x/y to whole pixels anyway (its position is an int, so there is no such
# thing as a half-pixel cursor), and rises until the path fits the budget. Measured on a
# 36000-frame (10 minute, 60fps) synthetic path: 9763 ramps at 0.5px, 5069 at 1.2px --
# so a long recording lands near a pixel of tolerance, which the integer overlay
# position was going to introduce anyway.
_SIMPLIFY_TOLERANCE_PX = 0.5
_MAX_RAMPS = 6000
_TOLERANCE_STEP = 1.5

# How far outside the canvas a position is allowed to travel before it is clamped. The
# pointer genuinely leaves a region capture -- it is one desktop and the recording is a
# window on it -- and an excursion to x=9000 would otherwise spend keyframes describing
# motion nobody can see.
_OFFCANVAS_MARGIN_SPRITES = 2

# The ripple, in multiples of the pointer height.
_RIPPLE_START_RADIUS = 0.35
_RIPPLE_END_RADIUS = 1.6
_RIPPLE_THICKNESS = 0.14
_RIPPLE_PEAK_ALPHA = 0.9
_RIPPLE_RIM_ALPHA = 0.75
_RIPPLE_FADE_POWER = 1.25


class CursorError(RuntimeError):
    pass


# --- the plan ----------------------------------------------------------------


@dataclass(frozen=True)
class Ripple:
    """One click ring, on the OUTPUT timeline. `x`/`y` are the click's canvas pixels."""

    frame: int
    x: float
    y: float


@dataclass(frozen=True)
class CursorPlan:
    """Everything the graph needs, with the sprites still unwritten.

    `positions` has one entry per OUTPUT frame: the hotspot in canvas pixels, or None
    for a frame the recorded track does not cover. None is not the same as "at the
    origin" -- the recorder arms its sampler after the encoder starts and stops before
    it, so a real recording has uncovered frames at both ends, and drawing a pointer
    parked at (0,0) through them is a visible lie about where the mouse was.
    """

    positions: list[tuple[float, float] | None]
    ripples: list[Ripple]
    size_px: int
    outline_px: int
    sprite_w: int
    sprite_h: int
    ripple_px: int
    ripple_frames: int
    canvas: Canvas

    @property
    def hotspot(self) -> tuple[int, int]:
        """Where the arrow's tip sits inside its own sprite: one outline width in from
        the top-left, because the outline is drawn OUTSIDE the polygon and the polygon's
        first vertex is the tip."""
        return self.outline_px, self.outline_px

    @property
    def visible_frames(self) -> int:
        return sum(1 for p in self.positions if p is not None)


# --- reading and mapping ------------------------------------------------------


def canvas_mapper(capture: Capture) -> Callable[[float, float], tuple[float, float]]:
    """A closure mapping LOGICAL compositor coordinates to canvas (video) pixels.

    The three-part mapping's part (c), applied to cursor samples instead of clicks. Both
    spaces are logical, so this is a pure ratio and `monitor_scale` cancels out of it
    rather than being applied -- which is the whole point, because the video is only
    `monitor_scale` times the logical region when the encode cap did not bite.

    Returned as a closure because the alternative is re-reading `logical_geometry` --
    a free-form dict -- once per frame, tens of thousands of times per render.
    """
    screen = capture.screen
    if screen is None:
        raise CursorError("capture has no screen stream; cannot place the cursor")
    ox, oy, rw, rh = capture_region(capture)
    if rw <= 0 or rh <= 0:
        raise CursorError(
            "capture.logical_geometry carries no size, so a cursor sample cannot be "
            "placed against the captured region. The pointer would land nowhere in "
            "particular, plausibly, on every frame."
        )
    sx, sy = screen.width / rw, screen.height / rh
    return lambda x, y: ((x - ox) * sx, (y - oy) * sy)


def to_canvas(x: float, y: float, capture: Capture) -> tuple[float, float]:
    """One logical coordinate as canvas pixels. See `canvas_mapper`."""
    return canvas_mapper(capture)(x, y)


def source_positions(
    track: CursorTrack, capture: Capture, tb: Timebase, total_frames: int
) -> list[tuple[float, float] | None]:
    """The track resampled onto the SOURCE frame grid, in canvas pixels.

    Frame `s` is evaluated at the sample time that `events.map_clicks` would have mapped
    BACK to frame `s`: anchor + s/fps - calibration. Inverting the click mapping rather
    than inventing a second one is what keeps a click's ripple centred on the pointer
    that caused it; the two used different signs for the calibration offset in an early
    draft and the ring landed a couple of frames off the pointer on every click.

    Frames outside the recorded track are None. Between samples the position is linearly
    interpolated: the track is 120Hz against a 60fps grid, so this is a half-sample
    interpolation in the ordinary case and a genuine glide only across a gap the
    recorder left.
    """
    screen = capture.screen
    if screen is None or screen.anchor_us is None:
        raise CursorError(
            "capture.screen.anchor_us is missing. Cursor samples are CLOCK_MONOTONIC and "
            "the video's frame 0 is unknown without it, so the whole path would be "
            "offset by however long the recorder took to start."
        )
    samples = track.samples
    out: list[tuple[float, float] | None] = [None] * max(0, total_frames)
    if not samples or total_frames <= 0:
        return out

    to_px = canvas_mapper(capture)
    latency_us = round(capture.calibration_c_ms * 1000.0)
    t0, t1 = samples[0].t_us, samples[-1].t_us
    j = 0
    for s in range(total_frames):
        t = screen.anchor_us + round(s * 1e6 * tb.fps_den / tb.fps_num) - latency_us
        if t < t0 or t > t1:
            continue
        while j + 1 < len(samples) and samples[j + 1].t_us < t:
            j += 1
        a = samples[j]
        b = samples[min(j + 1, len(samples) - 1)]
        span = b.t_us - a.t_us
        f = 0.0 if span <= 0 else (t - a.t_us) / span
        out[s] = to_px(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f)
    return out


# --- smoothing ----------------------------------------------------------------


def gaussian_kernel(sigma: float) -> list[float]:
    """A normalized, symmetric Gaussian, truncated at 3 sigma."""
    radius = max(1, int(math.ceil(3.0 * sigma)))
    k = [math.exp(-(i * i) / (2.0 * sigma * sigma)) for i in range(-radius, radius + 1)]
    total = sum(k)
    return [v / total for v in k]


def smooth(values: list[float], sigma: float) -> list[float]:
    """Zero-phase Gaussian smoothing, with the ends held rather than faded.

    WHY A CENTRED KERNEL AND NOT AN EMA. This runs offline, over a track that is already
    complete, so there is no reason to accept the lag a causal filter costs -- and the
    lag is the thing that ruins a synthetic cursor, because a pointer that arrives after
    the click it caused makes every click look mistimed. Measured on a 600-frame
    synthetic path (a 12 px/frame ramp plus 1.5px sigma of white noise) at the shipped
    default of sigma = 2.4 frames, jitter as the RMS of the second difference:

        raw                                    3.578 px/frame^2
        centred Gaussian, sigma 2.4            0.171 px/frame^2   (20.9x quieter)
        one-pole EMA matched to that jitter    0.171 px/frame^2   (alpha = 0.069)

    Both remove exactly the same amount of jitter; they do not cost the same to use. On
    a 100px step the centred kernel is already past halfway AT the step frame (58.3) and
    within 5% by frame +4. The matched EMA does not reach halfway until frame +9 and is
    still 5% short at frame +42 -- 150ms and 700ms at 60fps. The ripple this module
    draws is 350ms long, so the EMA's pointer would still be arriving as the ring it is
    supposed to have caused finished expanding.

    The ends replicate instead of tapering: a kernel that ran off into zeros would drag
    the first and last few frames toward the origin, which is a pointer sliding in from
    the corner of the frame at the start of every export.
    """
    n = len(values)
    if n == 0 or sigma < _MIN_SIGMA_FRAMES:
        return list(values)
    k = gaussian_kernel(sigma)
    r = len(k) // 2
    out: list[float] = []
    for i in range(n):
        acc = 0.0
        for j, w in enumerate(k):
            idx = i + j - r
            acc += w * values[min(max(idx, 0), n - 1)]
        out.append(acc)
    return out


def smooth_path(
    points: list[tuple[float, float] | None], sigma: float
) -> list[tuple[float, float] | None]:
    """`smooth` over the covered span of a path, leaving the uncovered frames None.

    Only the leading and trailing gaps are treated as uncovered; an interior gap is
    smoothed across, because it is a sampler hiccup and the pointer really was somewhere
    in between. A leading gap is different in kind -- the pointer was not yet being
    recorded at all -- and interpolating into it would invent motion.
    """
    idx = [i for i, p in enumerate(points) if p is not None]
    if not idx:
        return list(points)
    lo, hi = idx[0], idx[-1]
    # `source_positions` fills every frame between its first and last sample, so this
    # hold-forward is defensive rather than load-bearing -- but it has to be here, and it
    # has to hold rather than skip: dropping an interior hole would shorten the arrays
    # and slide every position after it earlier by one frame per hole.
    xs: list[float] = []
    ys: list[float] = []
    last = points[lo]
    assert last is not None
    for p in points[lo : hi + 1]:
        if p is not None:
            last = p
        xs.append(last[0])
        ys.append(last[1])
    xs = smooth(xs, sigma)
    ys = smooth(ys, sigma)
    out: list[tuple[float, float] | None] = list(points)
    for i in range(len(xs)):
        out[lo + i] = (xs[i], ys[i])
    return out


# --- the plan -----------------------------------------------------------------


def sigma_frames(settings: CursorSettings, tb: Timebase) -> float:
    """Smoothing strength as a kernel sigma on the frame grid.

    Expressed in SECONDS first and converted here, so the same project smooths the same
    amount at 30 and at 120fps. A sigma stored in frames would make the 120fps recording
    four times as sluggish as the 30fps one for the same slider position.
    """
    return max(0.0, min(1.0, settings.smoothing)) * SMOOTH_MAX_SECONDS * tb.fps


def build_plan(
    track: CursorTrack | None,
    clicks: list[Click],
    capture: Capture,
    tb: Timebase,
    cutmap: CutMap,
    settings: CursorSettings,
    canvas: Canvas,
) -> CursorPlan | None:
    """The whole cursor overlay, or None when there is nothing to draw.

    None rather than an empty plan for every "there is no cursor here" case -- disabled,
    no cursor.bin, an empty track, a track that covers no surviving frame -- so the
    renderer has exactly one thing to test and cannot half-build an overlay that draws
    a parked pointer for the length of the video.
    """
    if not settings.enabled:
        return None
    if track is None or not track.samples:
        return None
    if cutmap.output_frames <= 0:
        return None

    size_px = max(8, int(round(settings.size * canvas.height)))
    outline_px = max(1, int(round(size_px * OUTLINE_FRACTION)))
    sprite_w = int(math.ceil(size_px * _ARROW_WIDTH_RATIO)) + 2 * outline_px
    sprite_h = size_px + 2 * outline_px

    src = source_positions(track, capture, tb, cutmap.total_frames)
    src = smooth_path(src, sigma_frames(settings, tb))

    # SOURCE -> OUTPUT last, so the smoothing above never ran across a cut join. A cut
    # then shows as a clean jump between two independently-smoothed positions, which is
    # what the content under the pointer does too.
    margin_x = _OFFCANVAS_MARGIN_SPRITES * sprite_w
    margin_y = _OFFCANVAS_MARGIN_SPRITES * sprite_h
    positions: list[tuple[float, float] | None] = []
    for f in range(cutmap.output_frames):
        p = src[cutmap.to_source(f)]
        if p is None:
            positions.append(None)
        else:
            positions.append(
                (
                    min(max(p[0], -margin_x), canvas.width + margin_x),
                    min(max(p[1], -margin_y), canvas.height + margin_y),
                )
            )
    if not any(p is not None for p in positions):
        return None

    ripple_frames = max(0, int(settings.ripple_frames)) if settings.click_ripple else 0
    ripple_px = _ripple_tile_px(size_px)
    ripples = (
        _ripple_events(clicks, capture, tb, cutmap, ripple_frames)
        if ripple_frames > 0
        else []
    )

    return CursorPlan(
        positions=positions,
        ripples=ripples,
        size_px=size_px,
        outline_px=outline_px,
        sprite_w=sprite_w,
        sprite_h=sprite_h,
        ripple_px=ripple_px,
        ripple_frames=ripple_frames if ripples else 0,
        canvas=canvas,
    )


def _ripple_tile_px(size_px: int) -> int:
    """Tile side: the largest ring plus its thickness, rounded up to even.

    Even because the tile is centred on the click by subtracting half its side, and an
    odd side would put the ring half a pixel off the point it is meant to mark.
    """
    diameter = 2.0 * _RIPPLE_END_RADIUS * size_px + _RIPPLE_THICKNESS * size_px + 4.0
    side = int(math.ceil(diameter))
    return side + (side & 1)


def _ripple_events(
    clicks: list[Click],
    capture: Capture,
    tb: Timebase,
    cutmap: CutMap,
    ripple_frames: int,
) -> list[Ripple]:
    """Clicks placed on the output timeline, thinned so no two rings overlap.

    Through `events.map_clicks`, not through a second mapping: the ripple has to land on
    the same pixel and the same frame the auto-zoom is aiming at, and two implementations
    of the three-part mapping would drift while both still looked plausible.

    A click inside a cut is dropped -- the viewer never sees what was clicked. A click
    that lands while the previous ring is still running is dropped too: the tile index is
    a SUM of gated terms, so two live rings would add their stages together and select a
    tile off the end of the filmstrip. A double-click therefore ripples once, which is
    also what it should look like.
    """
    out: list[Ripple] = []
    mapped = sorted(map_clicks(clicks, capture, tb), key=lambda m: m.frame)
    for m in mapped:
        f = cutmap.to_output(m.frame)
        if f is None:
            continue
        if out and f - out[-1].frame < ripple_frames:
            continue
        out.append(Ripple(frame=f, x=m.px, y=m.py))
    return out


# --- positions -> filter expressions -------------------------------------------


def _num(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def _ramp_expr(start: int, span: int, frame: str) -> str:
    """`clip((frame-start)/span, 0, 1)`, with a zero span written as a step.

    Substituting a span of 1 to dodge the division would put the step one frame late --
    clip((n-s)/1) is still 0 AT n=s -- which is the same off-by-one `zoom.py` documents.
    """
    if span <= 0:
        return f"gte({frame},{start})"
    return f"clip(({frame}-{start})/{span},0,1)"


def simplify(values: list[float], tolerance: float) -> list[int]:
    """Indices of the keyframes that reproduce `values` to within `tolerance`.

    Douglas-Peucker on the (frame, value) polyline, with an explicit stack: the recursive
    form is O(n) deep on a monotone path, and a monotone path is exactly what a mouse
    dragged across the screen produces.
    """
    n = len(values)
    if n <= 2:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ya, yb = values[a], values[b]
        slope = (yb - ya) / (b - a)
        worst, at = -1.0, -1
        for i in range(a + 1, b):
            d = abs(values[i] - (ya + slope * (i - a)))
            if d > worst:
                worst, at = d, i
        if worst > tolerance:
            keep[at] = True
            stack.append((a, at))
            stack.append((at, b))
    return [i for i, k in enumerate(keep) if k]


def _axis(plan: CursorPlan, axis: int) -> list[float]:
    """One axis of the sprite's top-left corner, per output frame, with the uncovered
    frames parked off canvas. Parking rather than gating with `enable=`: a gated overlay
    still processes every frame, and a position expression is already being evaluated."""
    park = -(plan.sprite_w + 4) if axis == 0 else -(plan.sprite_h + 4)
    off = plan.hotspot[axis]
    return [park if p is None else p[axis] - off for p in plan.positions]


def _keyframe_expr(values: list[float], frame: str) -> str:
    """A piecewise-linear function of the frame index, as a sum of saturating ramps.

    The tolerance climbs until the path fits `_MAX_RAMPS`. It is a real budget rather
    than a hard failure because the alternative -- refusing to render a recording whose
    mouse moved a lot -- is not a defensible way to lose someone's take; past the budget
    the pointer is described a fraction of a pixel more coarsely and nothing else changes.

    The loop has no ceiling on the tolerance, and it terminates for that reason: as the
    tolerance grows the simplification converges on the two endpoints, so there is always
    a tolerance that fits. Capping it at some plausible-looking value instead would leave
    the budget silently unenforced on exactly the input that needs it -- 40000 frames of
    unsmoothed noise came out at 12967 ramps against a 6000 budget when the cap was 64px.
    """
    if not values:
        return "0"
    tol = _SIMPLIFY_TOLERANCE_PX
    keys = simplify(values, tol)
    while len(keys) - 1 > _MAX_RAMPS:
        tol *= _TOLERANCE_STEP
        keys = simplify(values, tol)
    terms = []
    for a, b in zip(keys, keys[1:]):
        dv = values[b] - values[a]
        if abs(dv) < 1e-6:
            continue
        terms.append(f"{_ramp_expr(a, b - a, frame)}*{_num(dv)}")
    if not terms:
        return _num(values[0])
    return f"({_num(values[0])}+{balanced_sum(terms)})"


def overlay_position_exprs(plan: CursorPlan) -> tuple[str, str]:
    """The arrow's `overlay` x and y expressions, in that order."""
    return (
        _keyframe_expr(_axis(plan, 0), FRAME_OVERLAY),
        _keyframe_expr(_axis(plan, 1), FRAME_OVERLAY),
    )


def ripple_stage_expr(plan: CursorPlan) -> str:
    """Which filmstrip tile to show, 0 for none.

    Evaluated against `crop`'s zero-based `n`, unlike everything else here. The gates are
    disjoint by construction (`_ripple_events` thins overlapping clicks), so the sum has
    at most one live term and needs no denominator -- the same argument that lets the
    zoom envelope be a plain sum. The outer clip is belt and braces: a tile index off the
    end of the strip would crop outside the image, which ffmpeg treats as a hard error
    rather than as a transparent frame.
    """
    r = plan.ripple_frames
    if not plan.ripples or r <= 0:
        return "0"
    terms = [
        f"(gte({FRAME_CROP},{e.frame})*lt({FRAME_CROP},{e.frame + r}))"
        f"*({FRAME_CROP}-{e.frame}+1)"
        for e in plan.ripples
    ]
    return f"clip({balanced_sum(terms)},0,{r})"


def ripple_position_exprs(plan: CursorPlan) -> tuple[str, str]:
    """Where to overlay the ripple tile. Zero when no ring is live, which is harmless
    because tile 0 is fully transparent -- the tile selector is what turns the ripple on
    and off, and it is already an expression."""
    r = plan.ripple_frames
    if not plan.ripples or r <= 0:
        return "0", "0"
    half = plan.ripple_px / 2.0
    out = []
    for axis in (0, 1):
        terms = [
            f"(gte({FRAME_OVERLAY},{e.frame})*lt({FRAME_OVERLAY},{e.frame + r}))"
            f"*{_num((e.x if axis == 0 else e.y) - half)}"
            for e in plan.ripples
        ]
        out.append(balanced_sum(terms))
    return out[0], out[1]


# --- sprites ------------------------------------------------------------------


def _png(width: int, height: int, pixels: bytearray) -> bytes:
    """An 8-bit RGBA PNG. `pixels` is straight (non-premultiplied) RGBA, row-major.

    Hand-rolled because the library has no third-party dependencies and adding one to
    draw a 32-pixel arrow would be the most expensive line in the project. Filter type 0
    on every row: these images are tiny and mostly transparent, so zlib does all the work
    that a predictor would and the encoder stays six lines long.
    """
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw += pixels[y * stride : (y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _point_in_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            if px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _distance_to_edges(px: float, py: float, poly: list[tuple[float, float]]) -> float:
    best = float("inf")
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0.0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
        ex, ey = px - (x1 + t * dx), py - (y1 + t * dy)
        best = min(best, math.hypot(ex, ey))
    return best


def arrow_png(size_px: int, outline_px: int) -> bytes:
    """A white arrow with a black outline, sized to `size_px` tall.

    The outline is the set of points within `outline_px` of the polygon's BOUNDARY,
    measured exactly rather than by dilating the raster: an exact distance gives a round
    outer corner at the arrow's tip and its tail, where a square-kernel dilation -- the
    cheap way to do this -- leaves visible flats at the two places the eye is drawn to.
    Seven edges times 16 samples a pixel is a few hundred thousand operations for a 32px
    pointer, which is why the result is cached on disk rather than rebuilt per render.
    """
    scale = size_px / _ARROW_HEIGHT
    poly = [(x * scale, y * scale) for x, y in ARROW_POLYGON]
    # Through _ARROW_WIDTH_RATIO, the same expression CursorPlan.sprite_w uses: the plan
    # publishes the sprite's size to the graph generator, and a rasterizer that rounded
    # its width differently would make the hotspot offset wrong by a pixel.
    w = int(math.ceil(size_px * _ARROW_WIDTH_RATIO)) + 2 * outline_px
    h = size_px + 2 * outline_px
    px = bytearray(w * h * 4)
    step = 1.0 / _SUPERSAMPLE
    n_samples = _SUPERSAMPLE * _SUPERSAMPLE
    for y in range(h):
        for x in range(w):
            cover = 0
            white = 0
            for sy in range(_SUPERSAMPLE):
                fy = y + (sy + 0.5) * step - outline_px
                for sx in range(_SUPERSAMPLE):
                    fx = x + (sx + 0.5) * step - outline_px
                    if _point_in_polygon(fx, fy, poly):
                        cover += 1
                        white += 1
                    elif _distance_to_edges(fx, fy, poly) <= outline_px:
                        cover += 1
            if not cover:
                continue
            alpha = cover / n_samples
            # Straight alpha: the colour is the average of the covered samples only, so a
            # pixel that is half white arrow and half nothing stays white at 50% rather
            # than fading toward the black outline.
            level = int(round(255.0 * white / cover))
            i = (y * w + x) * 4
            px[i] = px[i + 1] = px[i + 2] = level
            px[i + 3] = int(round(255.0 * alpha))
    return _png(w, h, px)


def ripple_png(size_px: int, tile_px: int, frames: int) -> bytes:
    """A horizontal filmstrip: tile 0 empty, then `frames` stages of an expanding ring.

    Tile 0 exists so that "no ripple" is a tile rather than a gate -- see
    `ripple_stage_expr`. The ring eases OUT (fast at first, slow at the end) and fades on
    a steeper curve than it grows, so the eye reads one quick pulse from the click point
    rather than a bubble drifting outward.

    Coverage is analytic rather than supersampled: a ring is |distance - radius| against
    a thickness, so the exact antialiased coverage of a pixel is one clip() and there is
    nothing to sample.
    """
    strip_w = tile_px * (frames + 1)
    px = bytearray(strip_w * tile_px * 4)
    c = tile_px / 2.0
    thickness = max(1.0, _RIPPLE_THICKNESS * size_px)
    rim = max(1.0, thickness * 0.4)
    for k in range(1, frames + 1):
        # (k-1)/frames, not k/frames: tile 1 is the frame the click happened on and must
        # be the ring at full strength. Dividing by `frames` the other way made the LAST
        # tile fade to exactly zero, so the strip's final frame was blank and the ripple
        # visibly ended one frame before the gate said it did.
        p = (k - 1) / float(frames)
        radius = size_px * (
            _RIPPLE_START_RADIUS
            + (_RIPPLE_END_RADIUS - _RIPPLE_START_RADIUS) * (1.0 - (1.0 - p) ** 2)
        )
        # Fades faster than it grows, but not so fast that the tail of the strip is
        # wasted: at the 1.5 this started on, the last quarter of a 21-frame ripple came
        # out under alpha 8/255 -- frames the gate paid for and the eye never saw.
        fade = _RIPPLE_PEAK_ALPHA * (1.0 - p) ** _RIPPLE_FADE_POWER
        if fade <= 0.002:
            continue
        x0 = k * tile_px
        for y in range(tile_px):
            dy = y + 0.5 - c
            for x in range(tile_px):
                dx = x + 0.5 - c
                d = abs(math.hypot(dx, dy) - radius)
                core = min(max(thickness / 2.0 - d + 0.5, 0.0), 1.0)
                halo = min(max(thickness / 2.0 + rim - d + 0.5, 0.0), 1.0) - core
                # Same white-core/dark-rim construction as the arrow, and for the same
                # reason: a plain white ring is invisible on a white document.
                a_core = core * fade
                a_halo = halo * _RIPPLE_RIM_ALPHA * fade
                a = a_core + a_halo
                if a <= 0.002:
                    continue
                i = ((y * strip_w) + x0 + x) * 4
                # Weighted by each part's ALPHA, not by its coverage. Weighting by
                # coverage alone counts the rim as if it were opaque and pulls the core
                # to a flat grey -- which rendered a ring that read as neither white nor
                # dark and disappeared against both backgrounds it was meant to survive.
                level = int(round(255.0 * a_core / a))
                px[i] = px[i + 1] = px[i + 2] = level
                px[i + 3] = int(round(255.0 * min(a, 1.0)))
    return _png(strip_w, tile_px, px)


def write_sprites(plan: CursorPlan, cache_dir: str | Path) -> tuple[Path, Path | None]:
    """Materialize the two sprites, reusing whatever is already on disk.

    Named by the parameters that determine their content, so a cache hit cannot be
    stale: the only way to get a different image is to ask for a different size, and
    that is a different filename. Cheap enough to matter -- the arrow rasterizer is
    hundreds of thousands of Python operations and the export path runs per render.
    """
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    arrow = d / f"arrow-{plan.size_px}-{plan.outline_px}.png"
    if not arrow.exists():
        _atomic_write(arrow, arrow_png(plan.size_px, plan.outline_px))
    if not plan.ripples or plan.ripple_frames <= 0:
        return arrow, None
    sheet = d / f"ripple-{plan.size_px}-{plan.ripple_px}-{plan.ripple_frames}.png"
    if not sheet.exists():
        _atomic_write(sheet, ripple_png(plan.size_px, plan.ripple_px, plan.ripple_frames))
    return arrow, sheet


def _atomic_write(path: Path, data: bytes) -> None:
    """Through a temp file: two exports of the same project can run at once (an export
    and a proxy build both reach this), and ffmpeg opening a half-written PNG fails the
    render rather than waiting."""
    tmp = path.with_name(path.name + f".{struct.pack('>I', zlib.crc32(data)).hex()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
