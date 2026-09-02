from __future__ import annotations

import errno
import subprocess

import pytest
from ffmpeg_harness import FFMPEG, framehashes, needs_ffmpeg, run_graph

from omarchy_studio.events import MappedClick
from omarchy_studio.geometry import Canvas, Zoom
from omarchy_studio.project import ZoomSettings
from omarchy_studio.timebase import CutMap, FrameRange, Timebase
from omarchy_studio.zoom import (
    FRAME,
    ZoomSegment,
    zoom_at,
    zoom_filter,
    zoom_segments,
)

TB = Timebase(30)
CANVAS = Canvas(320, 240)
SRC = ["-f", "lavfi", "-i", f"testsrc2=size={CANVAS.width}x{CANVAS.height}:rate=30:duration=8"]


def click(frame: int, cx: float = 0.5, cy: float = 0.5) -> MappedClick:
    return MappedClick(
        frame=frame, px=cx * CANVAS.width, py=cy * CANVAS.height, cx=cx, cy=cy,
        button="left",
    )


def settings(**kw) -> ZoomSettings:
    base = dict(
        enabled=True, amount=2.0, hold_frames=30, ease_frames=15, merge_gap_frames=45
    )
    base.update(kw)
    return ZoomSettings(**base)


# --- clustering -------------------------------------------------------------


def test_disabled_zoom_produces_nothing_at_all():
    segs = zoom_segments([click(30)], settings(enabled=False), TB, CutMap([], 300))
    assert segs == []
    # Not an identity filter: `perspective` costs its full per-frame price even when the
    # quad is the whole frame, so the only free zoom is an absent one.
    assert zoom_filter(segs, CANVAS, TB) == ""


def test_clicks_closer_than_the_merge_gap_become_one_segment():
    s = settings(merge_gap_frames=45)
    segs = zoom_segments([click(60), click(80), click(100)], s, TB, CutMap([], 400))
    assert len(segs) == 1
    # The move outlives its LAST click by hold + ease, not its first.
    assert segs[0].t == FrameRange(60, 100 + 30 + 15)


def test_distant_clicks_become_separate_non_overlapping_segments():
    s = settings(hold_frames=10, ease_frames=5, merge_gap_frames=5)
    segs = zoom_segments([click(20), click(200)], s, TB, CutMap([], 400))
    assert len(segs) == 2
    assert segs[0].t.end <= segs[1].t.start


def test_segments_never_overlap_even_when_the_gap_rule_allows_it():
    """A gap wider than merge_gap_frames can still leave two moves overlapping, because
    a move outlives its last click by hold+ease. Overlapping envelopes would sum."""
    s = settings(hold_frames=60, ease_frames=15, merge_gap_frames=10)
    segs = zoom_segments([click(0), click(20), click(40)], s, TB, CutMap([], 400))
    for a, b in zip(segs, segs[1:]):
        assert a.t.end <= b.t.start
    assert len(segs) == 1


def test_focal_point_is_the_mean_of_the_merged_cluster():
    s = settings()
    segs = zoom_segments([click(60, 0.2, 0.4), click(70, 0.4, 0.8)], s, TB, CutMap([], 400))
    assert len(segs) == 1
    assert segs[0].zoom.cx == pytest.approx(0.3)
    assert segs[0].zoom.cy == pytest.approx(0.6)


# --- cuts -------------------------------------------------------------------


def test_clicks_inside_a_cut_are_discarded():
    cutmap = CutMap([FrameRange(50, 100)], 400)
    segs = zoom_segments([click(60)], settings(), TB, cutmap)
    assert segs == []


def test_segments_are_placed_on_the_output_timeline():
    """The zoom runs after the cut chain, so the frame index the filter sees has already
    had the cuts removed."""
    cutmap = CutMap([FrameRange(0, 60)], 400)
    segs = zoom_segments([click(90)], settings(), TB, cutmap)
    assert len(segs) == 1
    assert segs[0].t.start == 30  # 90 in source, 60 frames removed ahead of it


def test_a_segment_is_clamped_to_the_output_length():
    cutmap = CutMap([], 100)
    segs = zoom_segments([click(95)], settings(), TB, cutmap)
    assert segs[0].t.end == 100


# --- the envelope -----------------------------------------------------------


def test_envelope_is_zero_outside_and_one_across_the_hold():
    seg = ZoomSegment(FrameRange(100, 200), Zoom(2.0), ease_in=20, ease_out=20)
    assert seg.envelope(99) == 0.0
    assert seg.envelope(100) == 0.0
    assert seg.envelope(120) == pytest.approx(1.0)
    assert seg.envelope(180) == pytest.approx(1.0)
    assert seg.envelope(200) == 0.0
    assert seg.envelope(300) == 0.0


def test_envelope_is_smootherstep_not_linear():
    """6t^5-15t^4+10t^3 is 0.5 at the midpoint like a linear ramp, but its derivative is
    zero at both ends -- which is the whole point, and what distinguishes it."""
    seg = ZoomSegment(FrameRange(0, 100), Zoom(2.0), ease_in=20, ease_out=20)
    assert seg.envelope(10) == pytest.approx(0.5)
    assert seg.envelope(2) == pytest.approx(0.00856, abs=1e-4)  # linear would be 0.1
    assert seg.envelope(18) == pytest.approx(0.99144, abs=1e-4)


def test_envelope_rises_monotonically_through_the_ease():
    seg = ZoomSegment(FrameRange(0, 100), Zoom(2.0), ease_in=20, ease_out=20)
    vals = [seg.envelope(n) for n in range(0, 21)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))


def test_zoom_at_is_identity_outside_every_segment():
    segs = zoom_segments([click(60, 0.2, 0.2)], settings(), TB, CutMap([], 400))
    assert zoom_at(segs, 0).identity
    assert zoom_at(segs, 399).identity
    assert not zoom_at(segs, segs[0].hold_start).identity


def test_clamped_viewport_keeps_an_edge_click_inside_the_frame():
    segs = zoom_segments([click(60, 0.02, 0.02)], settings(), TB, CutMap([], 400))
    vp = zoom_at(segs, segs[0].hold_start).viewport(CANVAS)
    assert vp.x == 0.0 and vp.y == 0.0
    assert vp.x + vp.w <= CANVAS.width and vp.y + vp.h <= CANVAS.height


# --- the emitted expression -------------------------------------------------


def test_frame_variable_compensates_for_perspectives_one_based_counter():
    """`perspective` exposes `in`/`on`, not `n`, and both are 1-based on the first
    output frame. Writing `on` where `n` was meant puts the envelope one frame early."""
    assert FRAME == "(on-1)"


@needs_ffmpeg
def test_the_generated_expression_parses(tmp_path):
    segs = zoom_segments([click(60, 0.25, 0.75)], settings(), TB, CutMap([], 240))
    graph = f"[0:v]{zoom_filter(segs, CANVAS, TB)}[vout]"
    r = run_graph(graph, tmp_path, inputs=[SRC], frames=4)
    assert r.returncode == 0, r.stderr[-3000:]


@needs_ffmpeg
def test_rendered_geometry_matches_the_python_model_frame_for_frame(tmp_path):
    """The expression and `zoom_at` must be the same arithmetic.

    Each sampled frame is compared against the SAME frame rendered through a
    `perspective` whose quad is the literal viewport `zoom_at` reports. Same filter,
    same resampler, so any difference is the expression disagreeing with the model.
    """
    segs = zoom_segments([click(60, 0.25, 0.75)], settings(), TB, CutMap([], 240))
    dyn = framehashes(
        f"[0:v]{zoom_filter(segs, CANVAS, TB)}[vout]", tmp_path, inputs=[SRC], frames=110
    )
    for n in (0, 62, 67, 70, 75, 90, 100, 103, 106):
        vp = zoom_at(segs, n).viewport(CANVAS)
        static = (
            f"[0:v]perspective=x0={vp.x:.6f}:y0={vp.y:.6f}"
            f":x1={vp.x + vp.w:.6f}:y1={vp.y:.6f}"
            f":x2={vp.x:.6f}:y2={vp.y + vp.h:.6f}"
            f":x3={vp.x + vp.w:.6f}:y3={vp.y + vp.h:.6f}"
            ":sense=source:eval=frame:interpolation=cubic[vout]"
        )
        ref = framehashes(static, tmp_path, inputs=[SRC], frames=n + 1)
        assert dyn[n] == ref[n], f"frame {n} disagrees with zoom_at"


@needs_ffmpeg
def test_the_zoom_starts_on_the_frame_it_says_it_does(tmp_path):
    """The off-by-one regression. With no ease-in, frame `start-1` must still be
    untouched and frame `start` must already be at full zoom."""
    seg = ZoomSegment(
        FrameRange(5, 40), Zoom(scale=2.0, cx=0.5, cy=0.5), ease_in=0, ease_out=0,
    )
    zoomed = framehashes(
        f"[0:v]{zoom_filter([seg], CANVAS, TB)}[vout]", tmp_path, inputs=[SRC], frames=8
    )
    plain = framehashes("[0:v]null[vout]", tmp_path, inputs=[SRC], frames=8)
    assert zoomed[4] == plain[4], "frame 4 was zoomed but the segment starts at 5"
    assert zoomed[5] != plain[5], "frame 5 was not zoomed but the segment starts at 5"


@needs_ffmpeg
def test_a_150_click_expression_parses_and_renders(tmp_path):
    """The balanced-tree regression. libavutil/eval.c has a recursion budget of 100 that
    a linear chain consumes one term at a time; the same terms nested as a balanced tree
    are only log2(N) deep."""
    s = settings(hold_frames=2, ease_frames=1, merge_gap_frames=0)
    clicks = [click(4 * i, 0.3 + 0.004 * (i % 100), 0.4) for i in range(150)]
    segs = zoom_segments(clicks, s, TB, CutMap([], 700))
    assert len(segs) >= 100, f"only {len(segs)} segments; the test has lost its teeth"

    graph = f"[0:v]{zoom_filter(segs, CANVAS, TB)}[vout]"
    assert len(graph) > 100_000, "expected a graph far past argv's limit"
    r = run_graph(graph, tmp_path, inputs=[SRC], frames=3)
    assert r.returncode == 0, r.stderr[-3000:]


@needs_ffmpeg
def test_the_same_terms_in_a_linear_chain_do_fail(tmp_path):
    """Proves the tree is what saves the graph, not the term count.

    If this ever passes, ffmpeg has raised its recursion budget and the balanced-tree
    requirement should be re-measured rather than assumed.
    """
    terms = "+".join(f"gte({FRAME},{i})" for i in range(150))
    graph = f"[0:v]perspective=x0='0*({terms})':eval=frame[vout]"
    r = run_graph(graph, tmp_path, inputs=[SRC], frames=1)
    assert r.returncode != 0


@needs_ffmpeg
def test_the_graph_survives_being_passed_as_a_file(tmp_path):
    """argv dies at ~288 KB with E2BIG and ffmpeg 9.0.1 removed
    `-filter_complex_script`, so `-/filter_complex <path>` is the only way in."""
    s = settings(hold_frames=2, ease_frames=1, merge_gap_frames=0)
    segs = zoom_segments([click(4 * i, 0.3, 0.4) for i in range(150)], s, TB, CutMap([], 700))
    graph = f"[0:v]{zoom_filter(segs, CANVAS, TB)}[vout]"
    gp = tmp_path / "big.txt"
    gp.write_text(graph + "\n")
    assert gp.stat().st_size > 288_000

    # E2BIG arrives as an OSError from the exec itself, before ffmpeg exists to report
    # anything -- which is why this failure mode is so easy to mistake for a crash.
    with pytest.raises(OSError) as excinfo:
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *SRC,
             "-filter_complex", graph, "-map", "[vout]",
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True,
        )
    assert excinfo.value.errno == errno.E2BIG

    r = run_graph(graph, tmp_path, inputs=[SRC], frames=1)
    assert r.returncode == 0, r.stderr[-3000:]
