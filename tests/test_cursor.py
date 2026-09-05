"""The synthetic cursor: where it lands, how it moves, and what happens when it can't.

The mapping tests are the important ones, for the same reason they are in
test_events.py: a pointer drawn at the wrong pixel still looks like a pointer. It
follows the mouse, it has an outline, it ripples on click -- and it is eight hundred
pixels from the thing the user was actually clicking, on every frame, and nothing
downstream can tell. `test_a_capped_capture_does_not_double_the_coordinate` is the one
that pins the failure this project has already shipped once.

The smoothing tests assert measurements rather than adjectives. "Looks smooth" is not
checkable; "the RMS second difference falls by 20x while a step still crosses halfway on
the step frame" is, and it is also the property that actually matters -- a filter that
smooths by lagging detaches the pointer from the click that moved it.
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from ffmpeg_harness import FFMPEG, needs_ffmpeg

from omarchy_studio import cursor as cur
from omarchy_studio import qmlbridge, render
from omarchy_studio.events import Click, CursorSample, CursorTrack, map_clicks, write_cursor
from omarchy_studio.geometry import Canvas
from omarchy_studio.project import Bundle, Capture, CursorSettings, Edit, Stream, create
from omarchy_studio.timebase import CutMap, FrameRange, Timebase

FPS = 60
ANCHOR = 1_000_000_000
HZ = 120.0
PERIOD_US = 1_000_000 // 120
TB = Timebase(FPS)


# --- fixtures ---------------------------------------------------------------


def capture_of(
    *,
    video: tuple[int, int] = (2560, 1440),
    region: tuple[int, int, int, int] = (0, 0, 2560, 1440),
    scale: float = 1.0,
    latency_ms: float = 0.0,
    fps: int = FPS,
) -> Capture:
    x, y, w, h = region
    return Capture(
        created="2026-09-02T00:00:00",
        screen=Stream("media/screen.mp4", video[0], video[1], fps, 1, ANCHOR),
        logical_geometry={"x": x, "y": y, "w": w, "h": h},
        physical_geometry={
            "x": int(x * scale), "y": int(y * scale),
            "w": int(w * scale), "h": int(h * scale),
        },
        monitor_scale=scale,
        calibration_c_ms=latency_ms,
    )


def straight_track(n: int, *, x0: int = 0, y0: int = 0, dx: float = 1.0, dy: float = 0.0,
                   t0: int = ANCHOR) -> CursorTrack:
    """A paced 120Hz track walking in a straight line."""
    return CursorTrack(
        samples=[
            CursorSample(t0 + i * PERIOD_US, int(x0 + dx * i), int(y0 + dy * i))
            for i in range(n)
        ],
        hz=HZ,
        scale=1.0,
        t0_us=t0,
        finalized=True,
    )


def cutmap_of(total: int, cuts: list[FrameRange] | None = None) -> CutMap:
    return CutMap(cuts or [], total)


def plan_for(track, capture, *, cuts=None, total=None, settings=None, clicks=(), canvas=None):
    c = canvas or Canvas(capture.screen.width, capture.screen.height)
    total = total if total is not None else 120
    return cur.build_plan(
        track,
        list(clicks),
        capture,
        Timebase(capture.screen.fps_num, capture.screen.fps_den),
        cutmap_of(total, cuts),
        settings or CursorSettings(smoothing=0.0),
        c,
    )


# --- the coordinate mapping -------------------------------------------------


def test_a_logical_sample_normalizes_against_the_capture_rectangle():
    cap = capture_of(video=(1600, 900), region=(200, 100, 800, 450))
    # Dead centre of the captured region, whatever its origin or the video's size.
    assert cur.to_canvas(600, 325, cap) == pytest.approx((800.0, 450.0))
    assert cur.to_canvas(200, 100, cap) == pytest.approx((0.0, 0.0))
    assert cur.to_canvas(1000, 550, cap) == pytest.approx((1600.0, 900.0))


def test_a_capped_capture_does_not_double_the_coordinate():
    """The bug this project has already had once, in `map_clicks`.

    A 5120x2880 display on a scale-2 monitor has a 2560x1440 logical desktop, and
    `capture.capture_size` caps the encode at 4096, so the video comes out 2560x1440 --
    the SAME size as the logical region, not twice it. Multiplying by monitor_scale
    puts every position at double its coordinate, and a pointer at 92% across the
    screen lands off the canvas entirely.
    """
    cap = capture_of(video=(2560, 1440), region=(0, 0, 2560, 1440), scale=2.0)
    x, y = cur.to_canvas(2355, 1300, cap)
    assert (x, y) == pytest.approx((2355.0, 1300.0))
    assert x < cap.screen.width  # not 4710, which is what * monitor_scale gives
    assert x != pytest.approx(2355 * cap.monitor_scale)


def test_an_uncapped_scaled_capture_still_scales():
    """The other half of the same rule: when the cap did NOT bite, the ratio reproduces
    monitor_scale exactly. One expression covers both cases; that is why it is the one
    used."""
    cap = capture_of(video=(1600, 900), region=(0, 0, 800, 450), scale=2.0)
    assert cur.to_canvas(400, 225, cap) == pytest.approx((800.0, 450.0))


def test_the_cursor_and_a_click_at_the_same_place_land_on_the_same_pixel():
    """Pins the two mappings together. They are separate implementations -- events.py
    owns clicks, cursor.py owns samples -- and if they ever disagree the ripple stops
    being centred on the pointer that caused it, which is the artefact nobody reports
    because it just looks slightly wrong."""
    cap = capture_of(video=(2560, 1440), region=(200, 100, 2560, 1440), scale=2.0)
    click = Click(t_us=ANCHOR + 500_000, button="left", x=1480, y=820)
    m = map_clicks([click], cap, TB)[0]
    assert cur.to_canvas(click.x, click.y, cap) == pytest.approx((m.px, m.py))


def test_a_capture_without_geometry_is_refused_rather_than_guessed():
    cap = capture_of()
    cap.logical_geometry = {}
    with pytest.raises(cur.CursorError):
        cur.to_canvas(10, 10, cap)


def test_a_capture_without_an_anchor_is_refused():
    cap = capture_of()
    cap.screen.anchor_us = None
    with pytest.raises(cur.CursorError):
        cur.source_positions(straight_track(10), cap, TB, 10)


# --- time: resampling onto the frame grid -----------------------------------


def test_the_sample_time_of_a_frame_inverts_the_click_mapping():
    """The calibration offset is ADDED when placing a click, so it must be SUBTRACTED
    when asking what instant a frame shows. With the sign the same in both places the
    ring would trail the pointer by twice the latency -- 80ms here, five frames.

    The track carries its own timestamp in its x coordinate (logical x = milliseconds
    since the anchor), so a resampled position reports exactly which instant the frame
    was filled from, and the assertion needs no tolerance argument to be convincing.
    """
    cap = capture_of(video=(1000, 500), region=(0, 0, 1000, 500), latency_ms=40.0)
    samples = [
        CursorSample(ANCHOR + i * PERIOD_US, round(i * PERIOD_US / 1000.0), 0)
        for i in range(240)
    ]
    pos = cur.source_positions(CursorTrack(samples, HZ, 1.0, ANCHOR, True), cap, TB, 100)
    for frame in (12, 30, 61):
        ms_shown = pos[frame][0]
        assert ms_shown == pytest.approx(frame * 1000.0 / FPS - 40.0, abs=0.5)
    # And a click stamped at the instant a frame shows lands on that frame.
    frame = 61
    t = ANCHOR + round(pos[frame][0] * 1000.0)
    assert map_clicks([Click(t, "left", 0, 0)], cap, TB)[0].frame == frame


def test_positions_interpolate_between_samples():
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    # Two samples 100ms apart: 6 frames at 60fps, so the frames between them are
    # genuinely interpolated rather than snapped to a neighbour.
    track = CursorTrack(
        [CursorSample(ANCHOR, 0, 0), CursorSample(ANCHOR + 100_000, 600, 0)],
        HZ, 1.0, ANCHOR, True,
    )
    pos = cur.source_positions(track, cap, TB, 12)
    assert pos[0] == pytest.approx((0.0, 0.0))
    assert pos[3] == pytest.approx((300.0, 0.0))  # halfway in time, halfway in space
    assert pos[6] == pytest.approx((600.0, 0.0))
    assert pos[7] is None  # past the last sample


def test_frames_outside_the_track_are_not_covered():
    """None, not (0,0). The recorder arms its sampler after the encoder starts, so a
    real recording has uncovered frames at both ends, and a pointer parked at the origin
    through them is a visible lie about where the mouse was."""
    cap = capture_of(video=(800, 600), region=(0, 0, 800, 600))
    track = straight_track(120, t0=ANCHOR + 500_000)  # starts half a second in
    pos = cur.source_positions(track, cap, TB, 120)
    assert pos[0] is None
    assert pos[29] is None
    assert pos[31] is not None


# --- smoothing ---------------------------------------------------------------


def rms_second_difference(v: list[float]) -> float:
    a = [v[i + 1] - 2 * v[i] + v[i - 1] for i in range(1, len(v) - 1)]
    return math.sqrt(sum(x * x for x in a) / len(a))


def noisy_ramp(n: int = 600, *, slope: float = 12.0, noise: float = 1.5) -> list[float]:
    """A steady drag plus the jitter a 120Hz sampler produces. Deterministic: an
    LCG rather than `random`, so the measured ratios below are the same on every run
    and a regression is a real change rather than a reseed."""
    out = []
    state = 12345
    for i in range(n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        u1 = ((state >> 8) & 0xFFFF) / 65535.0 or 1e-9
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        u2 = ((state >> 8) & 0xFFFF) / 65535.0
        g = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        out.append(slope * i + noise * g)
    return out


def test_smoothing_removes_an_order_of_magnitude_of_jitter():
    raw = noisy_ramp()
    smoothed = cur.smooth(raw, 2.4)  # the shipped default at 60fps
    before, after = rms_second_difference(raw), rms_second_difference(smoothed)
    assert after < before / 10.0, f"jitter only fell from {before:.3f} to {after:.3f}"


def test_smoothing_does_not_bend_a_straight_drag():
    """The point of a zero-phase kernel: a constant-velocity move comes out the same
    move, not a shortened one. A causal filter would deliver it late by its own lag,
    which is the position error that makes a pointer look detached from its click."""
    straight = [12.0 * i for i in range(400)]
    smoothed = cur.smooth(straight, 2.4)
    interior = range(20, 380)
    assert max(abs(smoothed[i] - straight[i]) for i in interior) < 1e-6


def test_a_step_arrives_on_time_and_settles_within_five_frames():
    step = [0.0] * 100 + [100.0] * 100
    s = cur.smooth(step, 2.4)
    # Halfway ON the step frame -- lag zero. A one-pole EMA matched to the same jitter
    # reduction does not reach halfway until frame +9 (measured; see cursor.smooth).
    assert s[100] >= 50.0
    assert s[99] <= 50.0
    settled = next(i for i in range(100, 200) if s[i] >= 95.0)
    assert settled - 100 <= 5, f"took {settled - 100} frames to settle"


def test_smoothing_zero_is_the_identity():
    raw = noisy_ramp(60)
    assert cur.smooth(raw, cur.sigma_frames(CursorSettings(smoothing=0.0), TB)) == raw


def test_the_same_strength_smooths_the_same_amount_at_any_frame_rate():
    """Stored in seconds and converted per project. A sigma stored in frames would make
    a 120fps recording four times as sluggish as a 30fps one for the same slider."""
    s = CursorSettings(smoothing=0.5)
    assert cur.sigma_frames(s, Timebase(60)) == pytest.approx(2.4)
    assert cur.sigma_frames(s, Timebase(30)) == pytest.approx(1.2)
    assert cur.sigma_frames(s, Timebase(120)) == pytest.approx(4.8)


def test_smoothing_leaves_uncovered_frames_uncovered():
    pts = [None] * 10 + [(float(i), 0.0) for i in range(30)] + [None] * 10
    out = cur.smooth_path(pts, 2.0)
    assert out[:10] == [None] * 10
    assert out[-10:] == [None] * 10
    assert all(p is not None for p in out[10:40])


def test_the_ends_of_the_path_are_held_not_faded_toward_the_origin():
    """A kernel running off into zeros drags the first frames toward 0, which reads as
    the pointer sliding in from the corner of the frame at the start of every export."""
    pts = [(500.0, 500.0)] * 40
    out = cur.smooth_path(pts, 4.0)
    assert out[0] == pytest.approx((500.0, 500.0))
    assert out[-1] == pytest.approx((500.0, 500.0))


# --- cuts ---------------------------------------------------------------------


def test_a_cut_remaps_the_path_rather_than_delaying_it():
    """Without the remap the pointer replays the motion the cut removed, so it drifts
    further behind the content for every cut in the project."""
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    track = straight_track(240, dx=1.0)  # 1 logical px per 120Hz sample = 2 px/frame
    uncut = plan_for(track, cap, total=120)
    cut = plan_for(track, cap, total=120, cuts=[FrameRange(10, 20)])
    assert len(cut.positions) == 110
    # Output frame 10 is source frame 20: the ten cut frames of motion are gone, not
    # deferred.
    assert cut.positions[10] == pytest.approx(uncut.positions[20])
    assert cut.positions[9] == pytest.approx(uncut.positions[9])


def test_smoothing_runs_before_the_cut_so_a_join_is_not_smeared():
    """Source time, not output time. Smoothed after the cut, the frames on either side
    of a join would be averaged into each other -- a pointer that slides across a jump
    the picture makes instantly."""
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    track = straight_track(240, dx=1.0)
    s = CursorSettings(smoothing=0.5)
    cut = plan_for(track, cap, total=120, cuts=[FrameRange(40, 80)], settings=s)
    uncut = plan_for(track, cap, total=120, settings=s)
    # Every surviving output frame carries exactly the position its source frame had.
    cm = cutmap_of(120, [FrameRange(40, 80)])
    for f in range(cm.output_frames):
        assert cut.positions[f] == pytest.approx(uncut.positions[cm.to_source(f)])


# --- the plan -----------------------------------------------------------------


def test_a_plan_covers_every_output_frame():
    cap = capture_of(video=(1280, 720), region=(0, 0, 1280, 720))
    plan = plan_for(straight_track(400), cap, total=90)
    assert len(plan.positions) == 90
    assert plan.visible_frames > 0


def test_the_sprite_size_follows_the_canvas():
    cap = capture_of(video=(2560, 1440), region=(0, 0, 2560, 1440))
    plan = plan_for(straight_track(300), cap)
    assert plan.size_px == 32  # the 0.022 default, at 1440p
    small = plan_for(straight_track(300), cap, settings=CursorSettings(size=0.011))
    assert small.size_px == 16


def test_positions_are_clamped_to_a_margin_around_the_canvas():
    """The pointer genuinely leaves a region capture -- it is one desktop and the
    recording is a window on it -- and an excursion to x=9000 would spend keyframes
    describing motion nobody can see."""
    cap = capture_of(video=(800, 600), region=(0, 0, 800, 600))
    track = CursorTrack(
        [CursorSample(ANCHOR + i * PERIOD_US, 5000 + i, -4000) for i in range(200)],
        HZ, 1.0, ANCHOR, True,
    )
    plan = plan_for(track, cap, total=60)
    xs = [p[0] for p in plan.positions if p is not None]
    assert xs and max(xs) <= 800 + 2 * plan.sprite_w + 1


# --- graceful degradation ------------------------------------------------------


def test_no_track_is_no_plan():
    cap = capture_of()
    assert plan_for(None, cap) is None


def test_an_empty_track_is_no_plan():
    cap = capture_of()
    assert plan_for(CursorTrack([], HZ, 1.0, 0, True), cap) is None


def test_a_track_that_covers_no_surviving_frame_is_no_plan():
    """A short track entirely inside a cut. The alternative -- an empty plan -- would
    have the renderer build an overlay that draws a parked pointer for the length of
    the video."""
    cap = capture_of(video=(800, 600), region=(0, 0, 800, 600))
    track = straight_track(20, t0=ANCHOR + 200_000)  # frames 12..21
    assert plan_for(track, cap, total=60, cuts=[FrameRange(10, 30)]) is None


def test_a_disabled_cursor_is_no_plan():
    cap = capture_of(video=(800, 600), region=(0, 0, 800, 600))
    off = CursorSettings(enabled=False)
    assert plan_for(straight_track(300), cap, settings=off) is None


def test_a_track_that_ends_early_still_produces_a_plan():
    """A SIGKILLed recorder leaves a track shorter than the video. The pointer stops
    being drawn; the render does not stop being produced."""
    cap = capture_of(video=(800, 600), region=(0, 0, 800, 600))
    track = CursorTrack(straight_track(60).samples, HZ, 1.0, ANCHOR, finalized=False)
    plan = plan_for(track, cap, total=120)
    assert plan is not None
    assert plan.positions[0] is not None
    assert plan.positions[-1] is None
    # And the expression parks it rather than dropping the last frames of the video.
    x = cur.overlay_position_exprs(plan)[0]
    assert x


# --- the ripple ----------------------------------------------------------------


def clicks_at(frames, *, x=100, y=100, fps=FPS):
    return [
        Click(ANCHOR + round(f * 1_000_000 / fps), "left", x + i, y + i)
        for i, f in enumerate(frames)
    ]


def test_a_ripple_is_placed_on_the_click_frame():
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    plan = plan_for(straight_track(300), cap, total=120, clicks=clicks_at([10, 60]))
    assert [r.frame for r in plan.ripples] == [10, 60]


def test_a_ripple_lands_where_the_click_did():
    """Through events.map_clicks, so the ring and the auto-zoom aim at the same pixel."""
    cap = capture_of(video=(1000, 500), region=(100, 50, 500, 250))
    clicks = clicks_at([12], x=350, y=175)
    plan = plan_for(straight_track(300), cap, total=120, clicks=clicks)
    m = map_clicks(clicks, cap, TB)[0]
    assert (plan.ripples[0].x, plan.ripples[0].y) == pytest.approx((m.px, m.py))


def test_overlapping_ripples_are_thinned_to_one():
    """The tile index is a SUM of gated terms, so two live rings would add their stages
    together and select a tile off the end of the filmstrip -- which is a hard crop
    error, not a transparent frame. A double-click therefore ripples once."""
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    s = CursorSettings(ripple_frames=21)
    plan = plan_for(
        straight_track(400), cap, total=200, settings=s, clicks=clicks_at([10, 14, 18, 90])
    )
    assert [r.frame for r in plan.ripples] == [10, 90]


def test_a_click_inside_a_cut_does_not_ripple():
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    plan = plan_for(
        straight_track(400), cap, total=200,
        cuts=[FrameRange(50, 70)], clicks=clicks_at([10, 60, 120]),
    )
    # 120 survives, shifted 20 frames earlier by the cut in front of it.
    assert [r.frame for r in plan.ripples] == [10, 100]


def test_disabling_the_ripple_removes_the_filmstrip_entirely():
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    s = CursorSettings(click_ripple=False)
    plan = plan_for(straight_track(300), cap, total=120, settings=s, clicks=clicks_at([10]))
    assert plan.ripples == []
    assert plan.ripple_frames == 0
    assert cur.ripple_stage_expr(plan) == "0"


def test_the_stage_expression_is_gated_and_clipped():
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    s = CursorSettings(ripple_frames=8)
    plan = plan_for(straight_track(300), cap, total=120, settings=s, clicks=clicks_at([10]))
    e = cur.ripple_stage_expr(plan)
    # Frame-index gates, never time: `between(t,A,B)` is inclusive at both ends and
    # lights one extra frame (exprs.py).
    assert "gte(n,10)" in e and "lt(n,18)" in e
    assert e.startswith("clip(") and e.endswith(",0,8)")


# --- expressions ----------------------------------------------------------------


def evaluate_ramp_expr(expr: str, frames: int) -> list[float]:
    """A Python twin of the ramp-sum form, for asserting against the plan.

    Deliberately a *parser* of the emitted string rather than a second generator: the
    thing under test is the text ffmpeg will evaluate, so re-deriving it from the same
    keyframes would test nothing.
    """
    import re

    out = []
    consts = re.findall(r"^\(?(-?\d+(?:\.\d+)?)\+", expr)
    base = float(consts[0]) if consts else float(expr)
    ramps = [
        (int(a), int(b), float(d))
        for a, b, d in re.findall(
            r"clip\(\(\(n-1\)-(\d+)\)/(\d+),0,1\)\*(-?\d+(?:\.\d+)?)", expr
        )
    ]
    steps = [
        (int(a), float(d))
        for a, d in re.findall(r"gte\(\(n-1\),(\d+)\)\*(-?\d+(?:\.\d+)?)", expr)
    ]
    for n in range(frames):
        v = base
        for start, span, delta in ramps:
            v += min(max((n - start) / span, 0.0), 1.0) * delta
        for start, delta in steps:
            v += delta if n >= start else 0.0
        out.append(v)
    return out


def test_the_position_expression_reproduces_the_plan():
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    plan = plan_for(straight_track(400, dx=2.0, dy=1.0), cap, total=100)
    ex, ey = cur.overlay_position_exprs(plan)
    got_x = evaluate_ramp_expr(ex, len(plan.positions))
    got_y = evaluate_ramp_expr(ey, len(plan.positions))
    hx, hy = plan.hotspot
    for i, p in enumerate(plan.positions):
        if p is None:
            continue
        assert got_x[i] == pytest.approx(p[0] - hx, abs=1.0)
        assert got_y[i] == pytest.approx(p[1] - hy, abs=1.0)


def test_simplification_stays_inside_its_tolerance():
    values = noisy_ramp(2000, slope=0.7, noise=6.0)
    keys = cur.simplify(values, 1.0)
    assert keys[0] == 0 and keys[-1] == len(values) - 1
    for a, b in zip(keys, keys[1:]):
        slope = (values[b] - values[a]) / (b - a)
        for i in range(a, b + 1):
            assert abs(values[i] - (values[a] + slope * (i - a))) <= 1.0 + 1e-9


def test_a_long_wandering_path_stays_inside_the_term_budget():
    """The budget is a rendering cost, not a correctness limit, so it degrades by
    coarsening the path rather than by refusing the render: losing someone's take
    because their mouse moved a lot is not a defensible failure."""
    values = noisy_ramp(40000, slope=0.0, noise=40.0)
    expr = cur._keyframe_expr(values, cur.FRAME_OVERLAY)
    assert expr.count("clip(") + expr.count("gte(") <= cur._MAX_RAMPS


def test_a_stationary_pointer_compiles_to_a_constant():
    """No ramps at all when nothing moves -- 'the mouse did not move' is the common case
    in a slide-deck recording and it should not cost an expression."""
    cap = capture_of(video=(1000, 1000), region=(0, 0, 1000, 1000))
    plan = plan_for(straight_track(400, dx=0.0, dy=0.0), cap, total=100)
    ex, _ = cur.overlay_position_exprs(plan)
    assert "clip(" not in ex and "gte(" not in ex


def test_the_overlay_and_the_crop_use_different_frame_variables():
    """Measured on ffmpeg n9.0.1: `overlay`'s n is one-based, `crop`'s is zero-based, in
    the same graph. Writing one where the other was meant puts the pointer a frame ahead
    of the content it is pointing at."""
    assert cur.FRAME_OVERLAY == "(n-1)"
    assert cur.FRAME_CROP == "n"


# --- sprites --------------------------------------------------------------------


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def png_rgba(data: bytes) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """Decode our own 8-bit RGBA, filter-0 PNG. Only what these tests need."""
    w, h = png_size(data)
    i = 8
    idat = b""
    while i < len(data):
        (length,) = struct.unpack(">I", data[i : i + 4])
        tag = data[i + 4 : i + 8]
        if tag == b"IDAT":
            idat += data[i + 8 : i + 8 + length]
        i += 12 + length
    raw = zlib.decompress(idat)
    stride = w * 4
    px = []
    for y in range(h):
        row = raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)]
        for x in range(w):
            px.append(tuple(row[x * 4 : x * 4 + 4]))
    return w, h, px


def test_the_arrow_is_generated_not_loaded_from_a_theme():
    """No file on disk is consulted. A render that silently loses its pointer because a
    cursor theme moved is worse than one that never had a pointer."""
    data = cur.arrow_png(32, 2)
    w, h, px = png_rgba(data)
    assert (w, h) == (24, 36)
    # The far corners are outside the arrow and outside its outline. The top-LEFT corner
    # is deliberately not checked: the tip is the hotspot and its outline wraps around it,
    # so that corner is legitimately part-covered.
    assert px[w - 1][3] == 0, "top-right corner must be transparent"
    assert px[(h - 1) * w + w - 1][3] == 0, "bottom-right corner must be transparent"
    # The tip is the hotspot: opaque within a pixel or two of the sprite's origin.
    assert any(px[y * w + x][3] > 200 for y in range(2, 8) for x in range(1, 6))


def test_the_arrow_is_white_inside_and_dark_at_its_edge():
    """Legible over both a dark terminal and a white document, which a single-colour
    pointer is not."""
    w, h, px = png_rgba(cur.arrow_png(48, 3))
    opaque = [(i % w, i // w, p) for i, p in enumerate(px) if p[3] > 240]
    assert any(p[0] > 230 for _, _, p in opaque), "no white core"
    assert any(p[0] < 40 for _, _, p in opaque), "no dark outline"


def test_the_arrow_sprite_matches_the_size_the_plan_publishes():
    """The plan tells the graph generator where the hotspot sits inside the sprite. A
    rasterizer that rounded its width differently would offset the pointer by a pixel on
    every frame."""
    cap = capture_of(video=(2560, 1440), region=(0, 0, 2560, 1440))
    plan = plan_for(straight_track(300), cap)
    w, h = png_size(cur.arrow_png(plan.size_px, plan.outline_px))
    assert (w, h) == (plan.sprite_w, plan.sprite_h)


def test_the_filmstrips_first_tile_is_empty():
    """Tile 0 is what 'no ripple' selects. If it were not transparent every frame of the
    export would carry a ring at the origin."""
    s = 40
    w, h, px = png_rgba(cur.ripple_png(12, s, 6))
    assert (w, h) == (s * 7, s)
    assert all(px[y * w + x][3] == 0 for y in range(s) for x in range(s))


def test_the_ripple_expands_and_fades():
    s = cur._ripple_tile_px(24)
    frames = 10
    w, h, px = png_rgba(cur.ripple_png(24, s, frames))

    def ring(k):
        row = h // 2
        xs = [x for x in range(s) if px[row * w + k * s + x][3] > 3]
        alphas = [px[row * w + k * s + x][3] for x in range(s)]
        return (max(xs) - min(xs)) if xs else 0, max(alphas)

    early_w, early_a = ring(1)
    late_w, late_a = ring(frames)
    assert late_w > early_w, "the ring must expand"
    assert late_a < early_a, "and fade as it goes"
    # The LAST tile still carries a ring. It faded to nothing once, which meant the
    # strip's final frame was blank and the ripple ended before its own gate did.
    assert late_a > 0


def test_sprites_are_cached_by_the_parameters_that_determine_them(tmp_path):
    cap = capture_of(video=(1280, 720), region=(0, 0, 1280, 720))
    plan = plan_for(straight_track(300), cap, total=60, clicks=clicks_at([5]))
    arrow, sheet = cur.write_sprites(plan, tmp_path)
    assert arrow.exists() and sheet is not None and sheet.exists()
    stamp = arrow.stat().st_mtime_ns
    again, _ = cur.write_sprites(plan, tmp_path)
    assert again == arrow and arrow.stat().st_mtime_ns == stamp
    assert str(plan.size_px) in arrow.name


def test_no_filmstrip_is_written_when_nothing_ripples(tmp_path):
    cap = capture_of(video=(1280, 720), region=(0, 0, 1280, 720))
    plan = plan_for(straight_track(300), cap, total=60)
    arrow, sheet = cur.write_sprites(plan, tmp_path)
    assert sheet is None
    assert list(tmp_path.glob("ripple-*")) == []


# --- settings -------------------------------------------------------------------


def test_an_edit_without_a_cursor_key_gets_the_defaults():
    """Every project written before this existed. The pointer is ON by default, because
    the screen was captured with the hardware cursor off and the alternative is that
    every one of those projects renders a mouse-driven demo with no mouse in it."""
    e = Edit.from_dict({"version": 1, "cuts": [], "layers": []})
    assert e.cursor.enabled is True
    assert e.cursor.size == pytest.approx(0.022)
    assert e.cursor.smoothing == pytest.approx(0.5)
    assert e.cursor.click_ripple is True


def test_cursor_settings_round_trip_through_json(tmp_path):
    root = tmp_path / "rec"
    create(root, capture_of(video=(1280, 720), region=(0, 0, 1280, 720)))
    b = Bundle(root)
    b.edit.cursor.size = 0.03
    b.edit.cursor.smoothing = 0.8
    b.edit.cursor.click_ripple = False
    b.edit.cursor.ripple_frames = 12
    b.save_edit()
    again = Bundle(root).edit.cursor
    assert (again.size, again.smoothing, again.click_ripple, again.ripple_frames) == (
        pytest.approx(0.03), pytest.approx(0.8), False, 12
    )
    assert json.loads((root / "edit.json").read_text())["cursor"]["size"] == 0.03


def test_a_hand_edited_size_is_clamped_rather_than_refused():
    assert CursorSettings(size=5.0).size == CursorSettings.MAX_SIZE
    assert CursorSettings(size=0.0).size == CursorSettings.MIN_SIZE
    assert CursorSettings(smoothing=9.0).smoothing == 1.0
    assert CursorSettings(smoothing=-1.0).smoothing == 0.0
    assert CursorSettings(ripple_frames=-4).ripple_frames == 0


def test_a_setting_from_a_newer_build_is_dropped_not_fatal():
    """Same forward-compatibility rule Layer follows for unknown types: a project written
    by a newer build loses the setting it asked for, it does not refuse to open."""
    s = CursorSettings.from_dict({"size": 0.03, "trail_length": 7})
    assert s.size == pytest.approx(0.03)
    assert not hasattr(s, "trail_length")


# --- the bridge -------------------------------------------------------------------


@pytest.fixture
def bridge_bundle(tmp_path) -> Bundle:
    root = tmp_path / "rec"
    create(root, capture_of(video=(2560, 1440), region=(0, 0, 2560, 1440)))
    return Bundle(root)


def test_resolve_cursor_reports_pixels_as_well_as_the_stored_fraction(bridge_bundle):
    write_cursor(bridge_bundle.events_dir / "cursor.bin", straight_track(240).samples)
    d = qmlbridge.resolve_cursor(bridge_bundle)
    assert d["enabled"] is True
    assert d["size"] == pytest.approx(0.022)
    assert d["size_px"] == pytest.approx(0.022 * 1440)
    assert d["smoothing_ms"] == pytest.approx(40.0)  # 0.5 * SMOOTH_MAX_SECONDS
    assert d["samples"] == 240
    assert d["editable"] is True
    assert d["disabled_reason"] == ""


def test_resolve_cursor_says_why_the_controls_are_dead(bridge_bundle):
    """A recording with no cursor track has nothing to draw. Silently inert controls
    read as a bug in the editor, which is the same reason a burned-in webcam explains
    itself."""
    d = qmlbridge.resolve_cursor(bridge_bundle)
    assert d["editable"] is False
    assert "no cursor track" in d["disabled_reason"]
    assert d["samples"] == 0


def test_resolve_cursor_survives_an_unreadable_track(bridge_bundle):
    (bridge_bundle.events_dir / "cursor.bin").write_bytes(b"not a cursor track at all!!!")
    d = qmlbridge.resolve_cursor(bridge_bundle)
    assert d["editable"] is False
    assert d["disabled_reason"]


def test_resolve_cursor_flags_a_track_the_recorder_never_finalized(bridge_bundle):
    p = bridge_bundle.events_dir / "cursor.bin"
    write_cursor(p, straight_track(60).samples)
    data = bytearray(p.read_bytes())
    data[5] &= ~0x02  # clear _FLAG_FINALIZED, as a SIGKILLed recorder leaves it
    p.write_bytes(bytes(data))
    assert qmlbridge.resolve_cursor(bridge_bundle)["truncated"] is True


def test_set_cursor_writes_every_control(bridge_bundle):
    qmlbridge.apply_op(bridge_bundle, "set_cursor", {
        "enabled": False, "size": 0.04, "smoothing": 0.25,
        "click_ripple": False, "ripple_ms": 500.0,
    })
    c = bridge_bundle.edit.cursor
    assert c.enabled is False
    assert c.size == pytest.approx(0.04)
    assert c.smoothing == pytest.approx(0.25)
    assert c.click_ripple is False
    assert c.ripple_frames == 30  # 500ms at 60fps, snapped by Timebase


def test_set_cursor_clamps_through_the_model(bridge_bundle):
    """The clamps live in CursorSettings, not here: a slider that can reach 0.09 must not
    be able to write a pointer a tenth of the frame tall, and one place decides that."""
    qmlbridge.apply_op(bridge_bundle, "set_cursor", {"size": 9.0, "smoothing": 4.0})
    assert bridge_bundle.edit.cursor.size == CursorSettings.MAX_SIZE
    assert bridge_bundle.edit.cursor.smoothing == 1.0


def test_project_state_carries_the_cursor(bridge_bundle):
    state = qmlbridge.project_state(bridge_bundle, include_zoom_track=False)
    assert "cursor" in state
    assert state["edit"]["cursor"]["enabled"] is True


def test_a_cursor_drag_coalesces_into_one_undo_step(bridge_bundle):
    """A slider fires an op per frame; without coalescing one drag is 60 undo steps and
    Ctrl+Z appears broken."""
    s = qmlbridge.Session(bridge_bundle, Path("."))
    for v in (0.02, 0.025, 0.03):
        s.op("set_cursor", {"size": v})
    s.undo()
    assert bridge_bundle.edit.cursor.size == pytest.approx(0.022)


# --- through the real renderer -----------------------------------------------------


def raw_frames(graph: str, inputs, w: int, h: int, frames: int, tmp_path: Path):
    """Render a graph to raw 8-bit greyscale and return one bytes object per frame."""
    gp = tmp_path / "graph.txt"
    gp.write_text(graph + "\n")
    out = tmp_path / "out.raw"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    for group in inputs:
        cmd += group
    cmd += ["-/filter_complex", str(gp), "-map", "[vout]", "-frames:v", str(frames),
            "-f", "rawvideo", "-pix_fmt", "gray", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = out.read_bytes()
    n = w * h
    return [data[i * n : (i + 1) * n] for i in range(len(data) // n)]


def bright_bbox(frame: bytes, w: int, h: int, threshold: int = 40):
    xs = [i % w for i, v in enumerate(frame) if v > threshold]
    ys = [i // w for i, v in enumerate(frame) if v > threshold]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


@needs_ffmpeg
def test_the_rendered_pointer_lands_on_the_frame_the_plan_predicts(tmp_path):
    """The whole chain, through the real filter: plan -> expression -> pixels.

    Asserted on the sprite's top-left corner rather than on a hash, because the failure
    this catches is a POSITION error -- a one-frame offset, or the `n` convention
    confused between overlay and crop -- and a hash says only that something differs.
    """
    W, H, N = 320, 240, 24
    cap = capture_of(video=(W, H), region=(0, 0, W, H), fps=30)
    track = CursorTrack(
        [CursorSample(ANCHOR + i * PERIOD_US, 20 + i, 30 + i // 2) for i in range(400)],
        HZ, 1.0, ANCHOR, True,
    )
    plan = cur.build_plan(
        track, [], cap, Timebase(30), cutmap_of(N),
        CursorSettings(size=0.06, smoothing=0.0, click_ripple=False), Canvas(W, H),
    )
    arrow, _ = cur.write_sprites(plan, tmp_path)
    ex, ey = cur.overlay_position_exprs(plan)
    graph = (
        f"color=c=black:s={W}x{H}:r=30,format=yuv420p,trim=end_frame={N}[base];\n"
        f"[base][0:v]overlay=x='{ex}':y='{ey}':format=yuv444[vout]"
    )
    frames = raw_frames(graph, [["-i", str(arrow)]], W, H, N, tmp_path)
    assert len(frames) == N
    hx, hy = plan.hotspot
    for i, frame in enumerate(frames):
        box = bright_bbox(frame, W, H)
        assert box is not None, f"frame {i} has no pointer"
        p = plan.positions[i]
        assert p is not None
        # Within a pixel: overlay rounds its position to an integer, and the sprite's
        # own antialiased edge can start a pixel in from the box corner.
        assert abs(box[0] - (p[0] - hx)) <= 1.5, f"frame {i}: x {box[0]} vs {p[0] - hx}"
        assert abs(box[1] - (p[1] - hy)) <= 1.5, f"frame {i}: y {box[1]} vs {p[1] - hy}"


@needs_ffmpeg
def test_an_uncovered_frame_draws_no_pointer_at_all(tmp_path):
    """Parked off canvas, not gated: a gated overlay still processes every frame, and a
    position expression is already being evaluated. What matters is that nothing shows."""
    W, H, N = 200, 150, 20
    cap = capture_of(video=(W, H), region=(0, 0, W, H), fps=30)
    # Samples only for the middle of the clip.
    track = CursorTrack(
        [CursorSample(ANCHOR + 300_000 + i * PERIOD_US, 60, 60) for i in range(24)],
        HZ, 1.0, ANCHOR + 300_000, True,
    )
    plan = cur.build_plan(
        track, [], cap, Timebase(30), cutmap_of(N),
        CursorSettings(size=0.1, smoothing=0.0, click_ripple=False), Canvas(W, H),
    )
    arrow, _ = cur.write_sprites(plan, tmp_path)
    ex, ey = cur.overlay_position_exprs(plan)
    graph = (
        f"color=c=black:s={W}x{H}:r=30,format=yuv420p,trim=end_frame={N}[base];\n"
        f"[base][0:v]overlay=x='{ex}':y='{ey}':format=yuv444[vout]"
    )
    frames = raw_frames(graph, [["-i", str(arrow)]], W, H, N, tmp_path)
    for i, frame in enumerate(frames):
        drawn = bright_bbox(frame, W, H) is not None
        assert drawn == (plan.positions[i] is not None), f"frame {i}"


@needs_ffmpeg
def test_the_filmstrip_selects_the_right_tile_on_the_right_frame(tmp_path):
    """`crop`'s x IS re-evaluated per frame (its w/h are not), and its frame counter is
    zero-based where `overlay`'s is one-based. Both halves of that are load-bearing and
    neither is visible in the graph text."""
    W, H, N = 200, 160, 24
    cap = capture_of(video=(W, H), region=(0, 0, W, H), fps=30)
    click_frame = 6
    ripple_len = 8
    plan = cur.build_plan(
        straight_track(400), clicks_at([click_frame], x=100, y=80, fps=30), cap,
        Timebase(30), cutmap_of(N),
        CursorSettings(size=0.15, ripple_frames=ripple_len), Canvas(W, H),
    )
    assert [r.frame for r in plan.ripples] == [click_frame]
    _, sheet = cur.write_sprites(plan, tmp_path)
    s = plan.ripple_px
    rx, ry = cur.ripple_position_exprs(plan)
    graph = (
        f"color=c=black:s={W}x{H}:r=30,format=yuv420p,trim=end_frame={N}[base];\n"
        f"[0:v]crop=w={s}:h={s}:x='{cur.ripple_stage_expr(plan)}*{s}':y=0[tile];\n"
        f"[base][tile]overlay=x='{rx}':y='{ry}':shortest=1:format=yuv444[vout]"
    )
    inputs = [["-loop", "1", "-framerate", "30", "-i", str(sheet)]]
    frames = raw_frames(graph, inputs, W, H, N, tmp_path)
    widths = []
    for i, frame in enumerate(frames):
        # A low threshold: the ring is deliberately faint by the end of its run, and what
        # this test is about is WHEN it is drawn, not how bright.
        box = bright_bbox(frame, W, H, threshold=6)
        live = click_frame <= i < click_frame + ripple_len
        if not live:
            assert box is None, f"frame {i}: the ring leaked outside its gate"
        elif box:
            widths.append(box[2] - box[0])
    # Drawn on the click frame, and it is a ripple rather than a blink: the ring is
    # bigger at the end of its run than at the start.
    assert bright_bbox(frames[click_frame], W, H, threshold=6) is not None
    assert len(widths) >= ripple_len - 1
    assert widths[-1] > widths[0]


# --- the whole renderer ------------------------------------------------------------


@pytest.fixture(scope="module")
def recording(tmp_path_factory) -> Path:
    import synthetic

    root = tmp_path_factory.mktemp("cursorrec")
    synthetic.make_bundle(root, seconds=1.5, width=320, height=240)
    return root


@pytest.fixture
def bundle(recording, tmp_path) -> Bundle:
    dest = tmp_path / "rec"
    for p in Path(recording).rglob("*"):
        if p.is_file():
            target = dest / p.relative_to(recording)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())
    return Bundle(dest)


def write_track(bundle: Bundle, n: int = 400) -> None:
    """A track over the captured region, which synthetic.py puts at 200,100."""
    write_cursor(
        bundle.events_dir / "cursor.bin",
        [
            CursorSample(bundle.capture.screen.anchor_us + i * PERIOD_US,
                         200 + 20 + i // 4, 100 + 20 + i // 8)
            for i in range(n)
        ],
    )


@needs_ffmpeg
def test_a_bundle_without_a_cursor_track_renders_normally(bundle):
    """Not an error and not a stub overlay. The recording is still perfectly good video
    and refusing to export it would be the worse failure."""
    assert not (bundle.events_dir / "cursor.bin").exists()
    plan = render.build_graph(bundle)
    assert "[cursored]" not in plan.graph


@needs_ffmpeg
def test_a_corrupt_cursor_track_does_not_take_the_render_with_it(bundle):
    (bundle.events_dir / "cursor.bin").write_bytes(b"OSCU" + b"\x00" * 40)
    plan = render.build_graph(bundle)
    assert "[cursored]" not in plan.graph


@needs_ffmpeg
def test_the_cursor_stage_sits_between_the_cut_and_the_zoom(bundle):
    """Order, asserted on the graph rather than trusted: composited after the zoom the
    pointer would slide across the content it is pointing at."""
    write_track(bundle)
    bundle.edit.zoom.enabled = True
    plan = render.build_graph(bundle)
    g = plan.graph
    assert "[cursored]" in g
    assert g.index("[cursored]") < g.index("perspective=")


@needs_ffmpeg
def test_the_cursor_overlay_does_not_run_in_yuv420(bundle):
    """yuv420 snaps overlay x/y to EVEN pixels -- measured 0,2,2,4,4,6 against yuv444's
    0,1,2,3,4,5 -- which is the staircase the smoothing exists to remove."""
    write_track(bundle)
    plan = render.build_graph(bundle)
    overlays = [c for c in plan.graph.split(";") if "overlay=" in c and "[cur" in c]
    assert overlays
    for chain in overlays:
        assert "format=yuv444" in chain


@needs_ffmpeg
def test_a_disabled_cursor_adds_no_input_and_no_filter(bundle):
    write_track(bundle)
    bundle.edit.cursor.enabled = False
    plan = render.build_graph(bundle)
    assert "[cursored]" not in plan.graph
    # No sprite input either: an overlay that is not composited must not still be opened.
    assert not any(a.endswith(".png") for spec in plan.input_specs for a in spec)


@needs_ffmpeg
def test_a_render_with_the_cursor_produces_a_playable_file(bundle, tmp_path):
    from omarchy_studio import probe

    write_track(bundle)
    out = tmp_path / "out.mp4"
    render.render(bundle, out)
    assert out.exists() and out.stat().st_size > 0
    assert probe.frame_count(out) == render.build_graph(bundle).total_frames


@needs_ffmpeg
def test_a_render_with_a_cut_and_a_cursor_keeps_its_length(bundle, tmp_path):
    """The cursor stage carries `shortest=1` against an infinite filmstrip. Getting that
    wrong is not subtle -- a 6.9s clip once came out 208 seconds long from the same class
    of mistake in the backdrop -- but it is invisible until something counts frames."""
    from omarchy_studio import probe

    write_track(bundle)
    bundle.edit.cuts = [FrameRange(5, 15)]
    out = tmp_path / "cut.mp4"
    plan = render.build_graph(bundle)
    render.run_plan(plan, out)
    assert probe.frame_count(out) == plan.total_frames


def test_the_cursor_overlay_drops_to_yuv420_once_the_export_downscales():
    """yuv444 buys whole-pixel overlay positioning, because yuv420 snaps x/y to EVEN
    pixels. That is worth paying for at the master's size -- but once the export
    downscales 2x or more, an even MASTER pixel is at most one OUTPUT pixel, which is
    the same granularity for free. Measured: -16s on a 1440p export from a 5K master,
    with the cursor indistinguishable at 4x.

    A `native` export does not downscale, so it keeps yuv444 and its whole-pixel steps.
    """
    from omarchy_studio.render import cursor_overlay_format

    assert cursor_overlay_format(1.0) == "yuv444"   # native: no downscale
    assert cursor_overlay_format(0.75) == "yuv444"  # not enough headroom
    assert cursor_overlay_format(0.5) == "yuv420"   # 1440p from a 5K master
    assert cursor_overlay_format(0.375) == "yuv420" # 1080p from a 5K master
