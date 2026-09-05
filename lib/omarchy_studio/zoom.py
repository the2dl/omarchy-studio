"""Auto-zoom: click track -> zoom segments -> a per-frame viewport expression.

THE FILTER IS `perspective`, NOT `crop`+`scale`, AND NOT `zoompan`.

That is a correction to the original plan, and it is forced by two measurements on the
installed ffmpeg (n9.0.1):

1. `crop`'s width and height are evaluated ONCE, at init. The `eval` option that used to
   select per-frame evaluation is gone from the filter in this build; only `x` and `y`
   are re-read per frame. A crop chain gated on the frame index therefore holds whatever
   size the expression produced at n=0 for the entire render -- a zoom that never zooms,
   with no warning. Verified: `crop=w='if(gt(n,10),320,640)'` emits 640-wide frames for
   all 30 frames, and the flipped predicate emits 320 for all 30.

2. `crop` cannot pan smoothly either, for exactly the reason `zoompan` was rejected. Its
   x/y are integers, so a sub-pixel pan staircases. Panning 0.25 px/frame across a ramp
   whose luma rises 8 per pixel, sampling one column: crop gives 32, 32, 32, 40, 40, 40,
   48, 48 -- three frames of stall then a 1 px jump. `perspective` over the same ramp
   gives 18, 20, 22, 24, 26, 28, 30, 32: exactly +2 per frame, which is exactly 0.25 px.

`perspective` with `sense=source:eval=frame` maps a source quad onto the output corners,
which for an axis-aligned quad IS crop-and-scale -- done per frame, with sub-pixel
resampling. It is geometrically exact: magnifying a `lum=X*4` ramp 2x about the centre
predicts 64, 66, 68, 70, ... and produces 64, 66, 68, 70, ... The cost is real and is the
price of the correctness: 8.4 ms/frame at 2560x1440 in yuv420p against 0.9 ms for the
(broken) crop+scale, so the zoom is applied while the base is still yuv420p, before the
backdrop converts to rgba -- in gbrap it costs 14.9 ms/frame instead.

THE FRAME VARIABLE IS `(on-1)`. `perspective` exposes `in` and `on`, not `n`, and both
are ONE-BASED: with `x0='on*1'` on a 64px ramp the first output frame is already shifted
by one pixel, and frames 1..5 read as var = 1, 2, 3, 4, 5. Writing `on` where `n` was
meant puts the whole zoom envelope one frame early.
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import MappedClick
from .exprs import balanced_sum, frame_gate
from .geometry import Canvas, Zoom
from .project import ZoomSettings
from .timebase import CutMap, FrameRange, Timebase

# `perspective`'s frame counters start at 1 on the first output frame (measured), so the
# 0-based index every other expression in this project uses is one less.
FRAME = "(on-1)"

# Registers 0 and 1 are scratch inside a single envelope; 5..9 carry the viewport. They
# must not collide: `st(5, <tree of envelopes>)` re-enters 0 and 1 many times while it
# evaluates, and reading ld(6) after another envelope has run would otherwise return an
# envelope's leftovers. ffmpeg gives each expression its own zeroed register file
# (av_expr_eval builds a fresh Parser), so the numbering only has to be consistent here.
_R_ENV_IN, _R_ENV_OUT = 0, 1
_R_SCALE, _R_VW, _R_VH, _R_VX, _R_VY = 5, 6, 7, 8, 9

# A zoom shorter than this is a strobe rather than a move: the ease never resolves and
# the frame just twitches. Segments below it are dropped instead of rendered.
_MIN_SEGMENT_SECONDS = 0.1


class ZoomError(ValueError):
    pass


@dataclass(frozen=True)
class ZoomSegment:
    """One zoom move on the OUTPUT timeline.

    Output, not source: the zoom is applied after the cut chain, so the frame index the
    filter sees has already had the cuts removed. `zoom_segments` does that remapping,
    which is why it takes a CutMap -- `zoom_filter` never sees source time at all.

    `t` spans the whole move including both eases. The envelope is 0 at `t.start`,
    reaches 1 after `ease_in` frames, holds, then returns to 0 at `t.end`.
    """

    t: FrameRange
    zoom: Zoom
    ease_in: int
    ease_out: int
    # SOURCE frame of the first click in the cluster. The stable name of this move: the
    # output range shifts whenever a cut is added, and the index in the list shifts
    # whenever any earlier move appears or goes, so neither can address a zoom across an
    # edit. The click track is part of the capture and never changes.
    anchor: int = -1
    # SOURCE frames of every click in the cluster, not just the first. Deleting the move
    # suppresses all of them, which is what makes the deletion survive re-clustering.
    sources: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.ease_in < 0 or self.ease_out < 0:
            raise ZoomError(f"negative ease on {self.t}")
        if self.ease_in + self.ease_out > len(self.t):
            raise ZoomError(
                f"eases ({self.ease_in}+{self.ease_out}) exceed the segment {self.t}"
            )

    @property
    def hold_start(self) -> int:
        return self.t.start + self.ease_in

    @property
    def hold_end(self) -> int:
        """First frame of the ease-out."""
        return self.t.end - self.ease_out

    def envelope(self, frame: int) -> float:
        """The 0..1 envelope at an output frame. The Python twin of what the filter
        computes; `zoom_at` uses it, and the tests compare rendered geometry against it."""
        rise = _smootherstep(_ramp(frame, self.t.start, self.ease_in))
        fall = _smootherstep(_ramp(frame, self.hold_end, self.ease_out))
        return rise * (1.0 - fall)


def _ramp(frame: int, start: int, span: int) -> float:
    """clip((frame-start)/span, 0, 1), with span 0 meaning an instant step."""
    if span <= 0:
        return 1.0 if frame >= start else 0.0
    return min(max((frame - start) / span, 0.0), 1.0)


def _smootherstep(t: float) -> float:
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


# --- clicks -> segments -----------------------------------------------------


def zoom_segments(
    clicks: list[MappedClick],
    settings: ZoomSettings,
    tb: Timebase,
    cutmap: CutMap,
) -> list[ZoomSegment]:
    """Cluster the click track into non-overlapping zoom moves on the output timeline.

    Clicks land on the output timeline first, and clicks inside a cut are discarded --
    zooming toward something the viewer never sees is worse than not zooming, and it also
    keeps `merge_gap_frames` meaning what the viewer experiences rather than what the raw
    recording contained.

    Clustering is what stops the pump. Without it a burst of clicks in one dialogue
    produces one zoom move per click, each easing out before the next eases in.
    """
    if not settings.enabled:
        return []
    if settings.amount <= 1.0:
        return []

    # Deleted moves are dropped HERE, one click at a time, before anything is clustered.
    #
    # The first version of this suppressed whole clusters by their anchor, and that was
    # a bug: changing merge_gap_frames re-clusters the clicks, the anchor stops being an
    # anchor, and a move the user deleted quietly came back. A deletion that undeletes
    # itself is worse than no deletion. "Do not zoom here" is a fact about the CLICKS,
    # so it is stored and applied against the clicks, and no amount of re-clustering can
    # rebuild a move out of clicks that are not there.
    suppressed = set(settings.suppressed)
    # (output frame, cx, cy, SOURCE frame) -- the source frame rides along so the
    # cluster can be named by something that survives a cut.
    pts: list[tuple[int, float, float, int]] = []
    for c in clicks:
        if c.frame in suppressed:
            continue
        out = cutmap.to_output(c.frame)
        if out is not None:
            pts.append((out, c.cx, c.cy, c.frame))
    if not pts:
        return []
    pts.sort(key=lambda p: p[0])

    clusters: list[list[tuple[int, float, float, int]]] = []
    for p in pts:
        if clusters and p[0] - clusters[-1][-1][0] <= settings.merge_gap_frames:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    # A gap wider than merge_gap_frames can still leave two moves overlapping, because a
    # move outlives its last click by hold+ease. Overlapping envelopes would sum, so the
    # focal blend would land between two unrelated targets; merging the clusters instead
    # keeps every envelope disjoint, which is what lets the filter expression be a plain
    # sum with no denominator.
    while True:
        merged: list[list[tuple[int, float, float, int]]] = [clusters[0]]
        collided = False
        for c in clusters[1:]:
            if _span(merged[-1], settings)[1] > _span(c, settings)[0]:
                merged[-1] = merged[-1] + c
                collided = True
            else:
                merged.append(c)
        clusters = merged
        if not collided:
            break

    min_len = max(2, round(tb.fps * _MIN_SEGMENT_SECONDS))
    total = cutmap.output_frames
    segments: list[ZoomSegment] = []
    for c in clusters:
        anchor = min(p[3] for p in c)
        start, end = _span(c, settings)
        end = min(end, total)
        if end - start < min_len:
            continue
        ease_in = min(settings.ease_frames, end - start)
        ease_out = min(settings.ease_frames, end - start - ease_in)
        cx = sum(p[1] for p in c) / len(c)
        cy = sum(p[2] for p in c) / len(c)
        segments.append(
            ZoomSegment(
                t=FrameRange(start, end),
                zoom=Zoom(
                    scale=settings.amount,
                    cx=min(max(cx, 0.0), 1.0),
                    cy=min(max(cy, 0.0), 1.0),
                ),
                ease_in=ease_in,
                ease_out=ease_out,
                anchor=anchor,
                sources=tuple(sorted(p[3] for p in c)),
            )
        )
    return segments


def _span(cluster: list[tuple[int, float, float, int]], s: ZoomSettings) -> tuple[int, int]:
    """[start, end) of the move a cluster produces, before clamping.

    The ease-in starts ON the first click rather than before it: the frames leading up to
    a click are the ones showing what the user is about to click, and pre-zooming them
    means the zoom is already committed before the click that justifies it.
    """
    first, last = cluster[0][0], cluster[-1][0]
    hold_end = max(first + s.ease_frames, last + s.hold_frames)
    return first, hold_end + s.ease_frames


def zoom_at(segments: list[ZoomSegment], frame: int) -> Zoom:
    """The Zoom in force at an output frame.

    The Python side of the same arithmetic the filter expression performs, so the QML
    preview can call `Zoom.to_qml` on this and land on the pixel the export produces.
    Segments are disjoint, so at most one term is ever non-zero; the sum form is kept
    rather than a search because it is the expression, verbatim.
    """
    scale = 1.0
    cx = cy = 0.5
    for s in segments:
        e = s.envelope(frame)
        if e:
            scale += (s.zoom.scale - 1.0) * e
            cx += (s.zoom.cx - 0.5) * e
            cy += (s.zoom.cy - 0.5) * e
    return Zoom(scale=scale, cx=cx, cy=cy)


# --- segments -> filter -----------------------------------------------------


def _reg(i: int) -> str:
    return f"ld({i})"


def _quintic(reg: int) -> str:
    """smootherstep of a register: 6t^5-15t^4+10t^3, in Horner form.

    Read from a register rather than substituted five times: the substituted form
    multiplies the length of every envelope by five, and the envelope already appears
    eight times over (once per `perspective` corner).
    """
    r = _reg(reg)
    return f"{r}*{r}*{r}*({r}*({r}*6-15)+10)"


def _ramp_expr(start: int, span: int) -> str:
    """The filter twin of `_ramp`.

    A zero-length ease is a step, and it has to be written as one. Substituting a span
    of 1 to dodge the division would put the step a frame late -- clip((n-s)/1) is still
    0 AT n=s -- which is exactly the off-by-one this module exists to avoid.
    """
    if span <= 0:
        return f"gte({FRAME},{start})"
    return f"clip(({FRAME}-{start})/{span},0,1)"


def _envelope_expr(s: ZoomSegment) -> str:
    """A parenthesised sequence evaluating to this segment's 0..1 envelope.

    No `enable` gate and no frame comparison: the rise is already pinned to 0 before the
    segment and the fall to 1 after it, so the product is exactly 0 everywhere outside
    [t.start, t.end). That is what keeps the terms summable.
    """
    return (
        f"(st({_R_ENV_IN},{_ramp_expr(s.t.start, s.ease_in)});"
        f"st({_R_ENV_OUT},{_ramp_expr(s.hold_end, s.ease_out)});"
        f"{_quintic(_R_ENV_IN)}*(1-{_quintic(_R_ENV_OUT)}))"
    )


def zoom_filter(segments: list[ZoomSegment], canvas: Canvas, tb: Timebase) -> str:
    """The `perspective` filter for a whole timeline, or "" when there is no zoom.

    "" rather than an identity filter: `perspective` costs its full 8.4 ms/frame even
    when the quad is the frame. It is gated with `enable` so that it costs that only on
    the frames a zoom is actually in flight -- `ffmpeg -filters` lists it `TS`, and the
    `T` is timeline support, so a disabled frame never enters the filter at all.

    This docstring used to claim the opposite -- "`enable` would not help, a gated filter
    still processes every frame" -- and that was simply wrong. It is what kept the filter
    running over every frame of the take: on a 510-frame export with two zooms, 434
    frames were paying full price for a warp that is the identity. Gating took the whole
    export from 56.2s to 31.2s with the output bit-identical (framemd5).

    The gate is inclusive of both ends of `t`. The envelope is 0 AT `t.start` and 0 again
    AT `t.end`, so those two frames are already the identity and need not be inside the
    gate at all; they are included because a gate that is one frame too wide costs one
    identity warp, and a gate one frame too narrow drops a real one.

    The emitted expression uses `perspective`'s own `W`/`H` rather than the canvas
    dimensions, so the identical string is correct against the 1080p preview proxy and
    the 1440p master. `canvas` is still required, and used, to reject a segment whose
    viewport would collapse.
    """
    live = [s for s in segments if _viewport_is_sane(s, canvas)]
    if not live:
        return ""

    envs = [_envelope_expr(s) for s in live]
    scale = "1+" + balanced_sum(
        [f"({s.zoom.scale - 1.0:.6f}*{e})" for s, e in zip(live, envs)]
    )
    cx = "0.5+" + balanced_sum(
        [f"({s.zoom.cx - 0.5:.6f}*{e})" for s, e in zip(live, envs)]
    )
    cy = "0.5+" + balanced_sum(
        [f"({s.zoom.cy - 0.5:.6f}*{e})" for s, e in zip(live, envs)]
    )

    # geometry.Zoom.viewport, transcribed: w = W/scale, x = clamp(cx*W - w/2, 0, W-w).
    # The clamp is what stops a click near an edge panning past it, and it has to be
    # here rather than baked into the segment because the viewport widens as the ease
    # runs, so which side is clamped changes from frame to frame.
    # The x and y halves are separate expressions so a corner evaluates the envelope
    # tree twice rather than three times; `perspective` re-reads all eight per frame.
    pre_x = (
        f"st({_R_SCALE},{scale});"
        f"st({_R_VW},W/{_reg(_R_SCALE)});"
        f"st({_R_VX},clip(({cx})*W-{_reg(_R_VW)}/2,0,W-{_reg(_R_VW)}))"
    )
    pre_y = (
        f"st({_R_SCALE},{scale});"
        f"st({_R_VH},H/{_reg(_R_SCALE)});"
        f"st({_R_VY},clip(({cy})*H-{_reg(_R_VH)}/2,0,H-{_reg(_R_VH)}))"
    )
    left = f"{pre_x};{_reg(_R_VX)}"
    right = f"{pre_x};{_reg(_R_VX)}+{_reg(_R_VW)}"
    top = f"{pre_y};{_reg(_R_VY)}"
    bottom = f"{pre_y};{_reg(_R_VY)}+{_reg(_R_VH)}"

    # sense=source: the quad names the region of the SOURCE that fills the output, which
    # is precisely Zoom.viewport. Measured against the analytic prediction on a luma
    # ramp, the mapping is source_x = x0 + dest_x*(x1-x0)/W -- W, not W-1.
    return (
        "perspective="
        f"x0='{left}':y0='{top}':"
        f"x1='{right}':y1='{top}':"
        f"x2='{left}':y2='{bottom}':"
        f"x3='{right}':y3='{bottom}':"
        "sense=source:eval=frame:interpolation=cubic"
        f":enable='{_gate_expr(live)}'"
    )


def _gate_expr(live: list[ZoomSegment]) -> str:
    """The frames on which `perspective` has anything to do.

    `n` here is the filter's own 0-based input count, and the quad expressions are
    written against `(on-1)`, the 0-based OUTPUT count. For a 1:1 filter those are the
    same number, including across disabled frames -- `on` keeps counting through them,
    which is what makes the gate safe rather than a source of drift.

    Through `frame_gate` rather than joining the terms here: it emits a BALANCED tree,
    and a flat left-deep `a+b+c+...` is what ffmpeg's expression parser cannot allocate.
    A 150-click project has 150 segments, and a hand-rolled join took the whole graph
    down with "Cannot allocate memory" -- the same trap `balanced_sum` exists for, three
    functions up. The suite caught it.

    `t.end + 1` because `frame_gate` is half-open and the envelope only returns to 0 AT
    `t.end`: half-open would stop one frame short of the frame that closes the move.
    """
    return frame_gate([FrameRange(s.t.start, s.t.end + 1) for s in live])


def _viewport_is_sane(s: ZoomSegment, canvas: Canvas) -> bool:
    """Reject a peak viewport too small to resample. Goes through Zoom.viewport so the
    single geometry seam decides, rather than a second copy of the same arithmetic."""
    vp = s.zoom.viewport(canvas)
    return vp.w >= 16.0 and vp.h >= 16.0
