"""Boundary behaviour of the frame-index time model.

The walk test exists because the seconds-based version of this code leaked an extra
frame on 318 of 899 boundaries -- 35% -- and did so while keeping the frame *count*
correct, so it could not be caught by counting. Any change that reintroduces float
time will fail here.
"""

import pytest

from omarchy_studio.timebase import (
    CutMap,
    FrameRange,
    Timebase,
    TimebaseError,
    normalize,
)


@pytest.fixture
def tb():
    return Timebase(60)


def test_ntsc_rates_round_trip():
    assert Timebase.from_fps(29.97) == Timebase(30000, 1001)
    assert Timebase.from_fps(60) == Timebase(60, 1)
    with pytest.raises(TimebaseError):
        Timebase.from_fps(31.4159)


def test_frame_seconds_round_trip(tb):
    for n in range(0, 5000, 7):
        assert tb.to_frame(tb.to_seconds(n)) == n


def test_midpoint_stays_inside_its_frame(tb):
    for n in range(0, 1000):
        mid = tb.frame_midpoint(n)
        assert tb.to_seconds(n) < mid < tb.to_seconds(n + 1)


def test_boundary_walk_is_exact(tb):
    """Every boundary from 1..900 must gate exactly its own frames.

    This is the regression that the `%.6f` seconds representation failed.
    """
    for b in range(1, 901):
        r = FrameRange(b, b + 10)
        assert len(r) == 10
        assert b in r
        assert (b - 1) not in r
        assert (b + 9) in r
        assert (b + 10) not in r


def test_range_rejects_empty_and_inverted():
    with pytest.raises(TimebaseError):
        FrameRange(10, 10)
    with pytest.raises(TimebaseError):
        FrameRange(10, 5)
    with pytest.raises(TimebaseError):
        FrameRange(-1, 5)


def test_normalize_merges_touching_ranges():
    got = normalize([FrameRange(10, 20), FrameRange(20, 30), FrameRange(50, 60)])
    assert got == [FrameRange(10, 30), FrameRange(50, 60)]


def test_normalize_merges_overlapping_and_contained():
    got = normalize([FrameRange(0, 100), FrameRange(20, 30), FrameRange(90, 140)])
    assert got == [FrameRange(0, 140)]


# --- cuts -------------------------------------------------------------------


def test_cutmap_without_cuts_is_identity():
    cm = CutMap([], 600)
    assert cm.output_frames == 600
    for n in (0, 1, 299, 599):
        assert cm.to_output(n) == n
        assert cm.to_source(n) == n


def test_cutmap_removes_exactly_the_cut_frames():
    cm = CutMap([FrameRange(100, 200)], 600)
    assert cm.output_frames == 500
    assert cm.to_output(99) == 99
    assert cm.to_output(150) is None
    assert cm.to_output(200) == 100
    assert cm.to_source(100) == 200


def test_cutmap_is_a_bijection_on_kept_frames():
    cm = CutMap([FrameRange(50, 75), FrameRange(300, 340)], 900)
    for n in range(900):
        out = cm.to_output(n)
        if out is None:
            assert cm.is_cut(n)
        else:
            assert cm.to_source(out) == n


def test_multiple_cuts_accumulate_correctly():
    cm = CutMap([FrameRange(10, 20), FrameRange(30, 40), FrameRange(50, 60)], 100)
    assert cm.output_frames == 70
    assert cm.to_output(20) == 10
    assert cm.to_output(40) == 20
    assert cm.to_output(60) == 30
    assert cm.to_output(99) == 69


def test_cut_past_the_end_is_rejected():
    with pytest.raises(TimebaseError):
        CutMap([FrameRange(500, 700)], 600)


def test_adjacent_cuts_merge_before_mapping():
    cm = CutMap([FrameRange(10, 20), FrameRange(20, 30)], 100)
    assert cm.cuts == [FrameRange(10, 30)]
    assert cm.output_frames == 80


# --- the remap, which is what makes source time safe ------------------------


def test_remap_of_range_before_a_cut_is_unchanged():
    cm = CutMap([FrameRange(200, 300)], 600)
    assert cm.remap(FrameRange(10, 50)) == [FrameRange(10, 50)]


def test_remap_of_range_after_a_cut_shifts_back():
    cm = CutMap([FrameRange(200, 300)], 600)
    assert cm.remap(FrameRange(400, 450)) == [FrameRange(300, 350)]


def test_remap_of_range_straddling_a_cut_splits_then_merges():
    cm = CutMap([FrameRange(200, 300)], 600)
    # [150,350) loses [200,300); the two survivors are contiguous in output time,
    # so they must come back merged rather than as two gates.
    assert cm.remap(FrameRange(150, 350)) == [FrameRange(150, 250)]


def test_remap_of_range_entirely_inside_a_cut_vanishes():
    cm = CutMap([FrameRange(200, 300)], 600)
    assert cm.remap(FrameRange(220, 280)) == []


def test_remap_preserves_total_visible_length():
    cm = CutMap([FrameRange(100, 150), FrameRange(400, 420)], 900)
    r = FrameRange(0, 900)
    assert sum(len(p) for p in cm.remap(r)) == cm.output_frames


def test_layer_spanning_many_cuts_collapses_to_few_gates():
    """A layer over 30 small cuts must not emit 30 separate gate terms -- ffmpeg's
    expression parser has a 100-level budget and the naive form walks straight into it.
    """
    cuts = [FrameRange(i * 20, i * 20 + 5) for i in range(30)]
    cm = CutMap(cuts, 1000)
    pieces = cm.remap(FrameRange(0, 600))
    assert len(pieces) == 1, f"expected one merged interval, got {len(pieces)}"
