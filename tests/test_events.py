"""The input-event track: binary round-trip, crash tolerance, and the frame mapping.

The mapping tests are the important ones. A click placed on the wrong frame or the
wrong pixel still produces a zoom that looks like a zoom, so nothing downstream can
notice the mistake -- these assertions are the only place the three required
transforms (anchor, calibration, scale) are checked independently of each other.
"""

import json
import math

import pytest

from omarchy_studio.events import (
    CURSOR_HEADER,
    Click,
    CursorSample,
    CursorWriter,
    EventsError,
    InputWriter,
    Scroll,
    clicks_to_frames,
    map_clicks,
    read_chapters,
    read_clicks,
    read_cursor,
    read_cursor_track,
    read_events,
    read_scrolls,
    write_cursor,
)
from omarchy_studio.project import Capture, Stream
from omarchy_studio.timebase import Timebase

HZ = 120.0
PERIOD_US = 1_000_000 // 120


def track(n: int, *, t0: int = 5_000_000, jitter: int = 0) -> list[CursorSample]:
    """A synthetic paced track. `jitter` shifts each interval deterministically so the
    dt residuals are non-zero, which is the case the varint sizing depends on."""
    out = []
    t = t0
    for i in range(n):
        out.append(CursorSample(t, 100 + i * 3, 200 - i))
        t += PERIOD_US + (jitter * ((i % 7) - 3) if jitter else 0)
    return out


# --- binary round-trip ------------------------------------------------------


def test_round_trip_is_exact(tmp_path):
    p = tmp_path / "cursor.bin"
    samples = track(500, jitter=90)
    write_cursor(p, samples, hz=HZ, scale=2.0)
    assert read_cursor(p) == samples


def test_header_fields_round_trip(tmp_path):
    p = tmp_path / "cursor.bin"
    samples = track(10, t0=123_456_789)
    write_cursor(p, samples, hz=HZ, scale=1.25)
    t = read_cursor_track(p)
    assert t.hz == HZ
    # Thousandths, not float32: 1.25 is exact either way but 1.6 is not, and a
    # compositor scale of 1.6 must not come back as 1.5999999.
    assert t.scale == 1.25
    assert t.t0_us == 123_456_789
    assert t.finalized is True
    assert len(t) == 10


def test_scale_of_1_6_survives(tmp_path):
    p = tmp_path / "cursor.bin"
    write_cursor(p, track(3), hz=HZ, scale=1.6)
    assert read_cursor_track(p).scale == 1.6


def test_large_and_negative_deltas_round_trip(tmp_path):
    """A pointer warp across a 5K desktop, a backwards jump, and a stalled sample."""
    p = tmp_path / "cursor.bin"
    samples = [
        CursorSample(1_000_000, 0, 0),
        CursorSample(1_008_333, 5119, 2879),
        CursorSample(1_016_666, -1200, -30),  # a monitor left of the origin
        CursorSample(2_000_000, 0, 0),  # a one-second gap
    ]
    write_cursor(p, samples)
    assert read_cursor(p) == samples


def test_single_sample_track(tmp_path):
    p = tmp_path / "cursor.bin"
    write_cursor(p, [CursorSample(7, 8, 9)])
    assert read_cursor(p) == [CursorSample(7, 8, 9)]


def test_empty_track(tmp_path):
    p = tmp_path / "cursor.bin"
    write_cursor(p, [])
    t = read_cursor_track(p)
    assert t.samples == []
    assert t.finalized is True


def test_not_a_cursor_file(tmp_path):
    p = tmp_path / "cursor.bin"
    p.write_bytes(b"NOPE" + bytes(CURSOR_HEADER.size - 4))
    with pytest.raises(EventsError, match="not a cursor.bin"):
        read_cursor(p)


def test_short_file(tmp_path):
    p = tmp_path / "cursor.bin"
    p.write_bytes(b"OSCU")
    with pytest.raises(EventsError, match="shorter than"):
        read_cursor(p)


def test_future_version_is_refused(tmp_path):
    p = tmp_path / "cursor.bin"
    write_cursor(p, track(3))
    data = bytearray(p.read_bytes())
    data[4] = 99
    p.write_bytes(bytes(data))
    with pytest.raises(EventsError, match="version 99"):
        read_cursor(p)


# --- crash tolerance --------------------------------------------------------


def test_unfinalized_file_still_reads(tmp_path):
    """A SIGKILLed recorder never runs close(), so the count stays 0 and the finalized
    flag stays clear. The samples that reached disk must still come back."""
    p = tmp_path / "cursor.bin"
    w = CursorWriter(p, hz=HZ, flush_every=1)
    samples = track(50)
    for s in samples:
        w.append(s)
    # No close(): the process died here.
    del w

    t = read_cursor_track(p)
    assert t.finalized is False
    assert t.samples == samples
    with pytest.raises(EventsError, match="never finalized"):
        read_cursor_track(p, strict=True)


def test_killed_before_the_first_sample_leaves_a_valid_empty_file(tmp_path):
    p = tmp_path / "cursor.bin"
    CursorWriter(p, hz=HZ)  # opened, then the process died
    t = read_cursor_track(p)
    assert t.samples == []
    assert t.finalized is False
    assert t.hz == HZ  # the header written at open is already self-describing


def test_torn_trailing_record_drops_only_that_record(tmp_path):
    p = tmp_path / "cursor.bin"
    samples = track(40, jitter=90)
    write_cursor(p, samples)
    data = p.read_bytes()
    p.write_bytes(data[:-1])  # a write interrupted one byte from the end

    got = read_cursor(p)
    assert got == samples[:-1]
    with pytest.raises(EventsError, match="torn trailing record"):
        read_cursor_track(p, strict=True)


# --- size -------------------------------------------------------------------


def _track_at(px_per_s: float, seconds: float = 60.0, hz: float = HZ) -> list[CursorSample]:
    """A hand-driven pointer path at a given speed, on realistic sample intervals.

    Intervals carry +/-40us of pacing error, which is the distribution a live 120Hz run
    on this machine actually produced (mean 2.7us, p95 7.3us, max 143us over 720
    samples). The spike's compression figure came from a parked cursor on a perfect
    grid, and both halves of that are unrepresentative.
    """
    n = int(seconds * hz)
    step = px_per_s / hz
    out = []
    t, x, y = 0, 1280.0, 720.0
    for i in range(n):
        heading = 2.6 * math.sin(i / 190.0) + 1.3 * math.sin(i / 47.0)
        x = min(max(x + step * math.cos(heading), 0), 2559)
        y = min(max(y + step * math.sin(heading), 0), 1439)
        out.append(CursorSample(t, int(x), int(y)))
        t += int(1_000_000 / hz) + int(40 * math.sin(i * 2.399))
    return out


def test_bytes_per_minute_under_continuous_motion(tmp_path, capsys):
    """The headline size, measured at a speed a person actually moves a mouse."""
    p = tmp_path / "cursor.bin"
    samples = _track_at(900.0)
    size = write_cursor(p, samples, hz=HZ, scale=2.0)
    with capsys.disabled():
        print(f"\ncursor.bin: {size:,} bytes/min at 900 px/s "
              f"({size / len(samples):.2f} bytes/sample)")
    # JSON at ~45 bytes a sample is 324000/min. Three bytes a sample is 21653; the
    # ceiling here catches a regression to fixed-width without pinning the exact bytes.
    assert size < 30_000, f"{size} bytes for one minute of motion"
    assert read_cursor(p) == samples


def test_size_does_not_depend_on_how_much_the_mouse_moves(tmp_path):
    """At 120Hz a zigzag varint absorbs the whole delta, so motion is free.

    A one-byte zigzag varint covers -64..63. Even a flick across a 5K desktop is about
    42 logical px between samples at 120Hz, so dx and dy cost one byte whatever the
    user does, and the track is 3 bytes a sample from parked to frantic. Anyone tempted
    to add a "compress the static parts" optimisation should read this test first:
    there is nothing left to win.
    """
    sizes = {
        speed: write_cursor(tmp_path / f"{speed}.bin", _track_at(speed), hz=HZ)
        for speed in (0, 300, 900, 2000, 5000)
    }
    assert len(set(sizes.values())) == 1, sizes


def test_a_delta_over_63_pixels_costs_a_second_byte(tmp_path):
    """The boundary the previous test relies on, asserted directly."""
    base = [CursorSample(0, 0, 0), CursorSample(PERIOD_US, 63, 0)]
    over = [CursorSample(0, 0, 0), CursorSample(PERIOD_US, 64, 0)]
    assert write_cursor(tmp_path / "b.bin", base, hz=HZ) + 1 == write_cursor(
        tmp_path / "o.bin", over, hz=HZ
    )


def test_a_dropped_sample_costs_only_its_own_record(tmp_path):
    """A socket error skips a sample. The gap must not re-baseline the whole track."""
    clean = [CursorSample(i * PERIOD_US, 500, 500) for i in range(200)]
    gapped = [s for i, s in enumerate(clean) if i != 100]
    a = write_cursor(tmp_path / "a.bin", clean, hz=HZ)
    b = write_cursor(tmp_path / "b.bin", gapped, hz=HZ)
    assert read_cursor(tmp_path / "b.bin") == gapped
    assert b < a  # one record fewer, even though its dt residual needs three bytes


# --- input.jsonl ------------------------------------------------------------


def test_input_writer_and_readers_round_trip(tmp_path):
    p = tmp_path / "input.jsonl"
    with InputWriter(p) as w:
        w.meta(t_us=10, hz=HZ)
        w.click(1_000_000, "left", 400, 300)
        w.scroll(1_100_000, "down", 401, 305)
        w.chapter(1_200_000, "the good bit")
        w.click(1_300_000, "right", 10, 20)

    assert read_clicks(p) == [
        Click(1_000_000, "left", 400, 300),
        Click(1_300_000, "right", 10, 20),
    ]
    assert read_scrolls(p) == [Scroll(1_100_000, "down", 401, 305)]
    assert [c.label for c in read_chapters(p)] == ["the good bit"]
    assert read_events(p)[0]["type"] == "meta"


def test_malformed_line_does_not_cost_the_others(tmp_path):
    p = tmp_path / "input.jsonl"
    p.write_text(
        json.dumps({"t_us": 1, "type": "click", "button": "left", "x": 1, "y": 2})
        + "\n{ this is not json\n"
        + json.dumps({"t_us": 2, "type": "click", "button": "left", "x": 3, "y": 4})
        + "\n"
        + '{"t_us": 3, "type": "cli'  # a partial write from a live recorder
    )
    assert [c.t_us for c in read_clicks(p)] == [1, 2]


def test_missing_file_reads_as_empty(tmp_path):
    assert read_clicks(tmp_path / "nope.jsonl") == []


# --- the three-part mapping -------------------------------------------------

ANCHOR = 1_000_000_000  # CLOCK_MONOTONIC us of video frame 0


def make_capture(**over) -> Capture:
    """A scale-2 region capture: `-w 1600x900+200+200` recorded as 3200x1800."""
    fields = dict(
        screen=Stream(
            path="media/screen.mp4",
            width=3200,
            height=1800,
            fps_num=60,
            anchor_us=ANCHOR,
        ),
        logical_geometry={"x": 200, "y": 200, "w": 1600, "h": 900},
        physical_geometry={"x": 400, "y": 400, "w": 3200, "h": 1800},
        monitor_scale=2.0,
        calibration_c_ms=40.0,
    )
    fields.update(over)
    return Capture(**fields)


@pytest.fixture
def tb():
    return Timebase(60)


def test_all_three_transforms_land_the_click(tb):
    cap = make_capture()
    # Two seconds after frame 0, at the exact centre of the captured region.
    c = Click(ANCHOR + 2_000_000, "left", 1000, 650)
    (m,) = map_clicks([c], cap, tb)
    assert m.frame == 122  # 2.000s + 40ms calibration = 2.040s * 60fps
    assert (m.px, m.py) == (1600.0, 900.0)  # logical 800,450 from the origin, x2
    assert (m.cx, m.cy) == (0.5, 0.5)


def test_dropping_the_anchor_would_shift_every_frame(tb):
    """(a). Moving frame 0 one second later must move the click one second earlier."""
    c = Click(ANCHOR + 2_000_000, "left", 1000, 650)
    early = map_clicks([c], make_capture(), tb)[0].frame
    late_anchor = make_capture(
        screen=Stream("media/screen.mp4", 3200, 1800, 60, anchor_us=ANCHOR + 1_000_000)
    )
    assert early - map_clicks([c], late_anchor, tb)[0].frame == 60


def test_dropping_the_calibration_would_shift_by_its_own_latency(tb):
    """(b). 40ms at 60fps is 2.4 frames, which rounds to a visible 2."""
    c = Click(ANCHOR + 2_000_000, "left", 1000, 650)
    with_cal = map_clicks([c], make_capture(), tb)[0].frame
    without = map_clicks([c], make_capture(calibration_c_ms=0.0), tb)[0].frame
    assert with_cal == 122
    assert without == 120


def test_dropping_the_scale_would_halve_the_focal_point(tb):
    """(c). The failure mode is not an error -- it is a zoom on the wrong quadrant."""
    c = Click(ANCHOR + 2_000_000, "left", 1000, 650)
    unscaled = map_clicks([c], make_capture(monitor_scale=1.0), tb)[0]
    assert (unscaled.px, unscaled.py) == (800.0, 450.0)
    assert (unscaled.cx, unscaled.cy) == (0.25, 0.25)


def test_region_origin_is_subtracted(tb):
    cap = make_capture()
    top_left = Click(ANCHOR, "left", 200, 200)
    (m,) = map_clicks([top_left], cap, tb)
    assert (m.px, m.py) == (0.0, 0.0)


def test_geometry_written_as_width_height_is_accepted(tb):
    cap = make_capture(logical_geometry={"x": 200, "y": 200, "width": 1600, "height": 900})
    (m,) = map_clicks([Click(ANCHOR, "left", 1000, 650)], cap, tb)
    assert (m.px, m.py) == (1600.0, 900.0)


def test_fullscreen_capture_has_no_origin(tb):
    cap = make_capture(logical_geometry={})
    (m,) = map_clicks([Click(ANCHOR, "left", 1000, 650)], cap, tb)
    assert (m.px, m.py) == (2000.0, 1300.0)


def test_a_click_before_frame_zero_clamps(tb):
    """The binds are armed before the encoder emits frame 0, so this is reachable."""
    c = Click(ANCHOR - 500_000, "left", 1000, 650)
    assert map_clicks([c], make_capture(), tb)[0].frame == 0


def test_a_missing_anchor_is_fatal(tb):
    cap = make_capture(screen=Stream("media/screen.mp4", 3200, 1800, 60, anchor_us=None))
    with pytest.raises(EventsError, match="anchor_us"):
        map_clicks([Click(ANCHOR, "left", 1, 1)], cap, tb)


def test_a_missing_screen_is_fatal(tb):
    with pytest.raises(EventsError, match="no screen stream"):
        map_clicks([Click(ANCHOR, "left", 1, 1)], make_capture(screen=None), tb)


def test_a_degenerate_scale_is_fatal(tb):
    with pytest.raises(EventsError, match="monitor_scale"):
        map_clicks([Click(ANCHOR, "left", 1, 1)], make_capture(monitor_scale=0.0), tb)


def test_clicks_to_frames_agrees_with_map_clicks(tb):
    cap = make_capture()
    clicks = [Click(ANCHOR + i * 250_000, "left", 300 + i, 300) for i in range(20)]
    assert clicks_to_frames(clicks, cap, tb) == [m.frame for m in map_clicks(clicks, cap, tb)]


def test_frames_are_monotonic_for_monotonic_clicks(tb):
    cap = make_capture()
    clicks = [Click(ANCHOR + i * 100_000, "left", 500, 500) for i in range(120)]
    frames = clicks_to_frames(clicks, cap, tb)
    assert frames == sorted(frames)
    assert frames[-1] == 716  # (119 * 0.1s + 40ms) * 60fps = 716.4


def test_a_click_is_never_placed_off_frame_by_rounding(tb):
    """Walk every frame boundary for ten seconds. A click stamped exactly on a frame's
    leading edge must land on that frame, not the one before it -- this is the same
    float-at-the-boundary failure the timebase tests guard."""
    cap = make_capture(calibration_c_ms=0.0)
    for n in range(600):
        c = Click(ANCHOR + round(n * 1_000_000 / 60), "left", 1000, 650)
        assert map_clicks([c], cap, tb)[0].frame == n
