"""A window that moves mid-recording is a framing decision, not a ruined take.

gsr's window capture is X11-only (`-w window ... (X11 only)`), so on Hyprland a
"window" target is a REGION: the rectangle the window happened to occupy when the
user picked it. Move the window after that and the recording is of the desktop it
left behind, silently, with nothing recoverable.

macOS does not have this problem -- ScreenCaptureKit takes a window as the content
filter, so the capture follows the window itself. The equivalent here has to be
built out of what Wayland does give us: record the monitor, log where the window
went, and decide the framing afterwards.

That "afterwards" is the point. Following becomes a toggle the user flips after
watching the take, it can be turned back off, and a window that never moved renders
byte-for-byte what it rendered before.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import needs_ffmpeg

from omarchy_studio import follow, probe, render
from omarchy_studio.events import WindowSample, WindowTrack, WindowWriter, read_window_track
from omarchy_studio.project import Bundle, Capture, Stream

REPO_ROOT = Path(__file__).resolve().parents[1]

ANCHOR = synthetic.ANCHOR_US


def _cap(**kw) -> Capture:
    kw.setdefault("physical_geometry", {"x": 400, "y": 200, "w": 1000, "h": 600})
    kw.setdefault("monitor_scale", 2.0)
    kw.setdefault("source_crop", {"x": 40, "y": 20, "width": 1000, "height": 600})
    return Capture(
        screen=Stream(path="screen.mp4", width=2560, height=1440,
                      fps_num=30, fps_den=1, anchor_us=ANCHOR),
        **kw,
    )


def _track(*rects, hz: float = 10.0) -> WindowTrack:
    """rects are (t_s, x, y, w, h) in LOGICAL desktop pixels."""
    return WindowTrack(
        address="0xabc", title="w", hz=hz,
        samples=[WindowSample(ANCHOR + int(t * 1e6), x, y, w, h)
                 for t, x, y, w, h in rects],
    )


# --- the track ---------------------------------------------------------------


def test_the_track_stores_changes_not_ticks(tmp_path):
    """A window holds still for seconds at a time. Storing a row per poll would spend
    thousands of samples restating that nothing happened."""
    p = tmp_path / "window.jsonl"
    with WindowWriter(p) as w:
        w.meta(address="0xabc", title="foot", hz=10.0)
        assert w.sample(0, 10, 20, 300, 200) is True
        assert w.sample(100_000, 10, 20, 300, 200) is False, "no move, no row"
        assert w.sample(200_000, 90, 20, 300, 200) is True
    assert len(read_window_track(p).samples) == 2


def test_a_rect_is_held_between_samples(tmp_path):
    p = tmp_path / "w.jsonl"
    with WindowWriter(p) as w:
        w.sample(0, 10, 20, 300, 200)
        w.sample(5_000_000, 90, 20, 300, 200)
    t = read_window_track(p)
    assert t.rect_at(4_999_999) == (10, 20, 300, 200), "holds the last value"
    assert t.rect_at(5_000_000) == (90, 20, 300, 200)
    assert t.rect_at(10**12) == (90, 20, 300, 200), "past the end is where it last was"
    assert t.rect_at(-1) is None, "before the first sample there is no answer"


def test_a_closed_window_is_marked_rather_than_forgotten(tmp_path):
    """"The window is gone" and "the window is where it last was" have to stay
    distinguishable -- a follow that keeps panning to a dead window's rect is a bug,
    and one that snaps to the origin is a worse one."""
    p = tmp_path / "w.jsonl"
    with WindowWriter(p) as w:
        w.sample(0, 10, 20, 300, 200)
        w.gone(3_000_000)
    t = read_window_track(p)
    assert t.gone_us == 3_000_000
    assert t.rect_at(9_000_000) == (10, 20, 300, 200)


def test_a_missing_track_is_an_empty_track_not_an_error(tmp_path):
    """Most recordings have no window to follow, and the editor asks unconditionally."""
    assert read_window_track(tmp_path / "nope.jsonl").samples == []


def test_a_partial_last_line_costs_only_that_line(tmp_path):
    p = tmp_path / "w.jsonl"
    p.write_text('{"t_us":0,"x":1,"y":2,"w":3,"h":4}\n{"t_us":1,"x":5,')
    assert len(read_window_track(p).samples) == 1


# --- when there is nothing to follow -----------------------------------------


def test_an_empty_track_plans_nothing():
    assert follow.plan(WindowTrack(), _cap()) is None


def test_a_capture_with_no_surrounding_pixels_plans_nothing():
    """The stream IS the frame, so there is nowhere to pan to."""
    assert follow.plan(_track((0, 100, 50, 400, 300)), _cap(source_crop={})) is None


def test_an_unanchored_stream_plans_nothing():
    """Without frame 0 the track's CLOCK_MONOTONIC stamps place the moves at
    plausible-looking wrong times, which is worse than not moving at all."""
    c = _cap()
    c.screen.anchor_us = None
    assert follow.plan(_track((0, 100, 50, 400, 300)), c) is None


def test_a_still_window_plans_a_static_crop():
    p = follow.plan(_track((0, 100, 50, 400, 300)), _cap())
    assert p is not None and p.static
    assert p.rect() == (0, 0, 800, 600), "the window, in stream pixels, clamped"


def test_a_one_pixel_nudge_is_not_a_move():
    """A window manager settling a border must not pan the whole frame."""
    p = follow.plan(_track((0, 100, 50, 400, 300), (2, 102, 50, 400, 300)), _cap())
    assert p is not None and p.static


# --- following ---------------------------------------------------------------


def test_a_moved_window_becomes_a_move():
    p = follow.plan(_track((0, 100, 50, 400, 300), (2, 500, 50, 400, 300)), _cap())
    assert p is not None and not p.static
    assert len(p.moves) == 1
    m = p.moves[0]
    assert m.t_s == pytest.approx(2.0)
    assert m.to[0] > m.frm[0], "it moved right, so the crop moves right"


def test_the_crop_size_is_the_largest_the_window_ever_was():
    """A video stream has one frame size, so a window that grew mid-take cannot grow
    the output. The size is the largest it reached; a smaller moment shows some
    desktop around it, which is what framing this by hand would do."""
    p = follow.plan(_track((0, 100, 50, 400, 300), (2, 100, 50, 600, 400)), _cap())
    assert (p.w, p.h) == (1200, 800)


def test_a_drag_is_one_settle_not_thirty():
    """A drag emits a sample every 100ms. Panning through every one of them
    reproduces the user's wobble instead of reading as camera work."""
    rects = [(i * 0.1, 100 + i * 20, 50, 400, 300) for i in range(20)]
    p = follow.plan(_track(*rects), _cap())
    assert len(p.moves) == 1, f"expected one settle, got {len(p.moves)}"
    assert p.moves[0].to[0] == pytest.approx(2 * (100 + 19 * 20) - 360, abs=2)


def test_two_separate_moves_stay_two_moves():
    p = follow.plan(
        _track((0, 100, 50, 400, 300), (2, 500, 50, 400, 300), (6, 900, 50, 400, 300)),
        _cap(),
    )
    assert len(p.moves) == 2


def test_the_crop_never_leaves_the_stream():
    """A crop running past the frame makes ffmpeg refuse the whole graph, so a window
    dragged half off-screen clamps instead."""
    p = follow.plan(
        _track((0, 100, 50, 400, 300), (2, 5000, 5000, 400, 300)), _cap()
    )
    for m in p.moves:
        for x, y in (m.frm, m.to):
            assert 0 <= x <= 2560 - p.w
            assert 0 <= y <= 1440 - p.h


# --- the expression ----------------------------------------------------------


def test_the_expression_holds_then_eases_then_holds():
    p = follow.plan(_track((0, 100, 50, 400, 300), (2, 500, 50, 400, 300)), _cap())
    x = p.x_expr()
    a, b = p.moves[0].frm[0], p.moves[0].to[0]
    assert _ev(x, 0.0) == pytest.approx(a, abs=2)
    assert _ev(x, 1.9) == pytest.approx(a, abs=2), "still, before the move"
    assert _ev(x, 9.0) == pytest.approx(b, abs=2), "arrived, and stays"
    mid = _ev(x, 2.0 + p.moves[0].dur_s / 2)
    assert min(a, b) < mid < max(a, b), "and it is somewhere in between partway"


def test_the_easing_is_smooth_at_both_ends():
    """A linear ramp starts and stops with a visible jerk. Smoothstep is why this
    reads as a camera move rather than as a slide."""
    p = follow.plan(_track((0, 100, 50, 400, 300), (2, 900, 50, 400, 300)), _cap())
    d = p.moves[0].dur_s
    x = p.x_expr()
    v = [_ev(x, 2.0 + f * d) for f in (0.0, 0.05, 0.5, 0.95, 1.0)]
    assert abs(v[1] - v[0]) < abs(v[2] - v[1]), "eases in"
    assert abs(v[4] - v[3]) < abs(v[2] - v[1]), "eases out"


def test_the_expression_offsets_stay_even():
    """An odd x on yuv420 is not a rounding detail; ffmpeg refuses it."""
    p = follow.plan(_track((0, 101, 51, 401, 301), (2, 503, 53, 401, 301)), _cap())
    for t in (0.0, 2.1, 2.3, 2.5, 9.0):
        assert _ev(p.x_expr(), t) % 2 == 0
        assert _ev(p.y_expr(), t) % 2 == 0


def _ev(expr: str, t: float) -> float:
    """Evaluate an ffmpeg expression the way ffmpeg would, for the subset used here."""
    import math

    py = expr.replace("if(", "_if(").replace("lt(", "_lt(").replace("floor(", "math.floor(")
    return eval(py, {"math": math, "t": t,
                     "_if": lambda c, a, b: a if c else b,
                     "_lt": lambda a, b: a < b})


# --- the bundle --------------------------------------------------------------


def _bundle_with_track(root, *rects, seconds=4.0) -> Bundle:
    synthetic.make_bundle(root, seconds=seconds, width=1280, height=720, camera=False)
    b = Bundle(root)
    b.capture.source_crop = {"x": 40, "y": 20, "width": 600, "height": 400}
    b.capture.physical_geometry = {"x": 400, "y": 200, "w": 600, "h": 400}
    b.capture.monitor_scale = 2.0
    with WindowWriter(follow.track_path(b)) as w:
        w.meta(address="0xabc", title="foot", hz=10.0)
        for t, x, y, ww, hh in rects:
            w.sample(ANCHOR + int(t * 1e6), x, y, ww, hh)
    return b


def test_following_is_off_until_asked(tmp_path):
    """Turning it on changes the framing of every frame; that is a decision the user
    makes after seeing the take, not one inherited silently."""
    b = _bundle_with_track(tmp_path / "a", (0, 200, 100, 300, 200), (2, 400, 100, 300, 200))
    assert b.edit.follow_window is False
    assert follow.for_bundle(b) is None
    assert follow.has_track(b) is True, "but it is offerable"


def test_a_recording_with_nothing_to_follow_is_not_offered(tmp_path):
    b = _bundle_with_track(tmp_path / "b", (0, 200, 100, 300, 200))
    assert follow.has_track(b) is False


def test_the_canvas_is_the_plan_not_the_picked_rectangle(tmp_path):
    """Two answers here would put every overlay at the wrong place at once."""
    b = _bundle_with_track(tmp_path / "c", (0, 200, 100, 300, 200), (2, 400, 100, 340, 240))
    before = b.canvas
    b.edit.follow_window = True
    b._follow_cache = None
    assert (b.canvas.width, b.canvas.height) == (680, 480)
    assert (before.width, before.height) == (600, 400)


# --- the render --------------------------------------------------------------


@needs_ffmpeg
def test_a_still_window_renders_exactly_what_it_rendered_before(tmp_path):
    """The property that makes this safe to add."""
    b = _bundle_with_track(tmp_path / "d", (0, 200, 100, 300, 200))
    plain = render.build_graph(b).graph
    b.edit.follow_window = True
    b._follow_cache = None
    followed = render.build_graph(b).graph
    assert "x='" not in followed, "a still window needs no expression"
    assert plain.count("crop=") == followed.count("crop=")


@needs_ffmpeg
def test_following_emits_a_moving_crop(tmp_path):
    b = _bundle_with_track(tmp_path / "e", (0, 200, 100, 300, 200), (2, 500, 100, 300, 200))
    b.edit.follow_window = True
    b._follow_cache = None
    g = render.build_graph(b).graph
    assert "crop=600:400:x='" in g
    assert g.index("crop=600:400") < g.index("fps="), "still before the frame grid"


@needs_ffmpeg
def test_a_followed_export_is_the_size_of_the_frame(tmp_path):
    b = _bundle_with_track(tmp_path / "f", (0, 200, 100, 300, 200), (2, 500, 100, 300, 200),
                           seconds=4.0)
    b.edit.follow_window = True
    b.edit.export_preset = "native"
    b._follow_cache = None
    b.save_edit()
    out = tmp_path / "f" / "o.mp4"
    render.render(b, out)
    assert probe.dimensions(out) == (600, 400)


@needs_ffmpeg
def test_the_frame_actually_pans(tmp_path):
    """The size test proves the graph ran; this proves it MOVED.

    Before the move the followed render must be identical to the unfollowed one --
    the crop is in the same place -- and after it must differ. Both halves matter:
    one alone is satisfied by a crop that panned from the first frame, or by one
    that never panned at all.
    """
    import subprocess

    def frame_at(b, t: float, name: str):
        out = tmp_path / f"{name}.mp4"
        render.render(b, out)
        png = tmp_path / f"{name}-{t}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(out),
             "-frames:v", "1", str(png)], check=True)
        return png.read_bytes()

    root = tmp_path / "pan"
    b = _bundle_with_track(root, (0, 200, 100, 300, 200), (2, 500, 100, 300, 200),
                           seconds=4.0)
    b.edit.export_preset = "native"
    b.save_edit()
    still_early, still_late = frame_at(b, 0.5, "still"), frame_at(b, 3.5, "still")

    b.edit.follow_window = True
    b._follow_cache = None
    b.save_edit()
    moved_early, moved_late = frame_at(b, 0.5, "moved"), frame_at(b, 3.5, "moved")

    assert moved_early == still_early, "before the move, following changes nothing"
    assert moved_late != still_late, "after it, the frame is somewhere else"


# --- the encoder has to be sized to the STREAM ---------------------------------


def test_the_codec_is_chosen_for_what_is_actually_encoded(tmp_path, capsys):
    """A window on a 5K panel is a small frame cut from a huge stream.

    `-k auto` picks h264 from the hardware, and h264 stops at 4096. Sizing the
    encoder to the FRAME names a codec that cannot encode what gsr is being handed,
    and the failure lands at record time on a take that cannot be redone.
    """
    from omarchy_studio.capture import main

    main(["begin", "--root", str(tmp_path / "r"), "--logical", "600x400+100+50",
          "--monitor", "DP-1", "--scale", "2", "--source-logical", "2560x1440+0+0"])
    out = dict(l.split("=", 1) for l in capsys.readouterr().out.splitlines())
    assert out["CAPTURE_CODEC"] == "hevc", "the stream is 5120 wide, not the frame's 1200"
    assert out["SOURCE_CROP"] == "1200x800+200+100"


def test_a_capture_that_is_all_frame_still_picks_from_the_frame(tmp_path, capsys):
    from omarchy_studio.capture import main

    main(["begin", "--root", str(tmp_path / "r2"), "--logical", "600x400+0+0",
          "--monitor", "DP-1", "--scale", "2"])
    out = dict(l.split("=", 1) for l in capsys.readouterr().out.splitlines())
    assert out["CAPTURE_CODEC"] == "auto"


# --- recording more than the selection is a choice ---------------------------


def test_the_recorder_only_widens_the_capture_when_asked(tmp_path):
    """The guard, read off the script: SETUP_FULL_MONITOR gates it.

    Widening is what makes re-framing possible, and it is also what puts the rest of
    the screen on disk. Picking one window is frequently a decision not to record the
    rest, so the recorder must not decide this on the user's behalf.
    """
    src = (REPO_ROOT / "bin" / "omarchy-capture-screenrecording").read_text()
    guard = [l for l in src.splitlines() if "SOURCE_LOGICAL=$(monitor_logical_geometry)" in l]
    assert len(guard) == 2, "one guard for the self-view, one for re-framing"
    assert '${SETUP_FULL_MONITOR:-false} == "true"' in src
    # And the window track is only taken when there is somewhere to pan to.
    assert '[[ -n ${SETUP_WINDOW:-} && -n $SOURCE_LOGICAL ]]' in src


def test_a_selection_only_capture_offers_no_follow_toggle(tmp_path):
    """`plan` would answer None for it, so a toggle would sit there doing nothing --
    which is worse than no toggle."""
    b = _bundle_with_track(tmp_path / "narrow", (0, 200, 100, 300, 200),
                           (2, 400, 100, 300, 200))
    b.capture.source_crop = {}          # only the selection was recorded
    assert follow.has_track(b) is False
    b.edit.follow_window = True
    b._follow_cache = None
    assert follow.for_bundle(b) is None
