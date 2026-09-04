"""The camera as a track of segments: head on for the intro, off, back at the end.

Segments rather than keyframes -- see layers.webcam_segments for why. The model keeps a
take nobody has touched as ONE static setting, and only materializes explicit layers the
first time the track is edited, so the simple case never pays for the complicated one.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import needs_ffmpeg

from omarchy_studio import layers as layers_mod, qmlbridge, render
from omarchy_studio.geometry import Canvas
from omarchy_studio.project import Bundle, Edit
from omarchy_studio.timebase import FrameRange

CANVAS = Canvas(2560, 1440)
TOTAL = 600


def _ids(edit) -> list[tuple[str, int, int]]:
    return [(s.id, s.t.start, s.t.end)
            for s in layers_mod.webcam_segments(edit, CANVAS, TOTAL)]


# --- the model ---------------------------------------------------------------


def test_an_untouched_take_reads_as_one_whole_length_segment():
    """And writes NOTHING: the implicit segment must not become a stored layer just by
    being looked at, or opening a recording would dirty it."""
    e = Edit()
    assert _ids(e) == [("webcam", 0, TOTAL)]
    assert e.layers == []


def test_a_camera_turned_off_has_no_segments():
    e = Edit()
    e.webcam.enabled = False
    assert _ids(e) == []


def test_splitting_materializes_the_track_and_cuts_in_two():
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, TOTAL, 170)
    assert _ids(e) == [("webcam", 0, 170), ("webcam1", 170, TOTAL)]


def test_splitting_on_a_seam_is_a_no_op_rather_than_an_error():
    """The playhead sitting on a boundary is a normal place for it to be, and the one
    position a user is most likely to press split from twice."""
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, TOTAL, 170)
    assert layers_mod.split_webcam(e, CANVAS, TOTAL, 170) is None
    assert len(_ids(e)) == 2


def test_deleting_a_segment_leaves_a_gap_that_is_the_camera_being_off():
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, TOTAL, 170)
    e.layers = [l for l in e.layers if l.id != "webcam1"]
    assert _ids(e) == [("webcam", 0, 170)]


def test_a_new_segment_inherits_the_look_of_its_nearest_neighbour():
    """"Bring my head back" should bring back the SAME head, so only its position is
    worth changing afterwards."""
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, TOTAL, 170)
    e.layers = [l for l in e.layers if l.id != "webcam1"]
    e.layers[0].props["shape"] = "rect"
    fresh = layers_mod.add_webcam_segment(e, CANVAS, TOTAL, FrameRange(400, 500))
    assert fresh.props["shape"] == "rect"


def test_an_overlapping_segment_is_refused():
    """Two cameras at once is not something the pipeline draws, and silently clipping
    one would be worse than saying no."""
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, TOTAL, 170)
    assert layers_mod.add_webcam_segment(e, CANVAS, TOTAL, FrameRange(100, 300)) is None


def test_editing_a_segment_leaves_the_whole_take_default_alone():
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, TOTAL, 170)
    before = (e.webcam.x, e.webcam.shape)
    seg = next(l for l in e.layers if l.id == "webcam1")
    seg.x, seg.props["shape"] = 0.03, "rect"
    assert (e.webcam.x, e.webcam.shape) == before


# --- the filtergraph ---------------------------------------------------------


@needs_ffmpeg
def test_each_segment_gets_its_own_copy_of_the_camera(tmp_path):
    """The regression that made this whole feature look like it worked.

    ffmpeg consumes a labelled pad EXACTLY ONCE. Two webcam layers both reading the
    camera's label raised no error and produced no camera: the second reference fell
    through to the screen, so the second bubble rendered the DESKTOP inside its circular
    mask. Nothing in the graph or the exit code said so -- only looking at the pixels
    did. A split sized to the number of consumers is what makes it real.
    """
    root = tmp_path / "twoseg"
    synthetic.make_bundle(root, seconds=2.0, width=640, height=360)
    b = Bundle(root)
    layers_mod.split_webcam(b.edit, b.canvas, 60, 30)
    g = render.build_graph(b).graph

    assert "split=2[cam_seg0][cam_seg1]" in g
    assert "[cam_seg0]" in g and "[cam_seg1]" in g
    # The pre-split label must not be read by a layer any more.
    assert g.count("[cam_aligned_cut]crop") == 0


@needs_ffmpeg
def test_a_single_segment_needs_no_split(tmp_path):
    """The untouched case stays exactly the graph it was."""
    root = tmp_path / "oneseg"
    synthetic.make_bundle(root, seconds=2.0, width=640, height=360)
    g = render.build_graph(Bundle(root)).graph
    assert "cam_seg" not in g


# --- through the bridge ------------------------------------------------------


@needs_ffmpeg
def test_the_whole_gesture_through_the_bridge(tmp_path):
    """Split, delete, bring back -- the three ops the timeline row actually issues."""
    root = tmp_path / "gesture"
    synthetic.make_bundle(root, seconds=4.0, width=640, height=360)
    b = Bundle(root)
    ms = b.timebase.fps_den * 1000.0 / b.timebase.fps_num

    qmlbridge.apply_op(b, "split_webcam", {"at_ms": 30 * ms})
    qmlbridge.apply_op(b, "delete_layer", {"id": "webcam1"})
    qmlbridge.apply_op(b, "add_webcam_segment", {"start_ms": 60 * ms, "end_ms": 90 * ms})

    track = qmlbridge.project_state(b)["webcam_track"]
    assert track["explicit"] is True
    assert [(s["start"], s["end"]) for s in track["segments"]] == [(0, 30), (60, 90)]


@needs_ffmpeg
def test_the_panel_edits_the_selected_segment_not_the_default(tmp_path):
    root = tmp_path / "panel"
    synthetic.make_bundle(root, seconds=2.0, width=640, height=360)
    b = Bundle(root)
    qmlbridge.apply_op(b, "split_webcam", {"at_ms": 30 * 1000.0 / b.timebase.fps})
    before = b.edit.webcam.shape

    qmlbridge.apply_op(b, "set_webcam", {"id": "webcam1", "shape": "rect"})
    seg = next(l for l in b.edit.layers if l.id == "webcam1")
    assert seg.props["shape"] == "rect"
    assert b.edit.webcam.shape == before


@needs_ffmpeg
def test_an_overlapping_add_is_reported_rather_than_silently_dropped(tmp_path):
    root = tmp_path / "overlap"
    synthetic.make_bundle(root, seconds=2.0, width=640, height=360)
    b = Bundle(root)
    ms = 1000.0 / b.timebase.fps
    qmlbridge.apply_op(b, "split_webcam", {"at_ms": 30 * ms})
    with pytest.raises(qmlbridge.BridgeError):
        qmlbridge.apply_op(b, "add_webcam_segment", {"start_ms": 0, "end_ms": 20 * ms})


# --- the nine found by review ------------------------------------------------
#
# Every one was invisible from the outside: the feature demoed correctly and the suite
# was green. They are pinned individually because each has its own way of coming back.


@needs_ffmpeg
def test_a_segment_inside_the_head_trim_still_exports(tmp_path):
    """The one that broke exports outright, one keypress from the start of any take.

    compile_layer returns BEFORE asking for a source when a cut removes a layer's range
    entirely, so the split output it was counted for went unconsumed -- and ffmpeg
    rejects the whole graph over one unconnected pad. trim_head_frames is the recorded
    countdown and is 80-166 frames on real recordings, so splitting anywhere in the
    first few seconds put a segment inside it.
    """
    root = tmp_path / "trimmed"
    synthetic.make_bundle(root, seconds=3.0, width=320, height=180)
    b = Bundle(root)
    b.edit.trim_head_frames = 40
    layers_mod.split_webcam(b.edit, b.canvas, 90, 20)
    b.save_edit()
    render.render(b, root / "out.mp4")          # must not raise
    assert (root / "out.mp4").stat().st_size > 0


@needs_ffmpeg
def test_a_lone_segment_inside_the_head_trim_still_exports(tmp_path):
    """The single-segment case takes no split, so the CUT CAMERA LABEL dangles instead.
    A `> 1` guard on the split does not cover this."""
    root = tmp_path / "lonetrim"
    synthetic.make_bundle(root, seconds=3.0, width=320, height=180)
    b = Bundle(root)
    b.edit.trim_head_frames = 40
    layers_mod.materialize_webcam(b.edit, b.canvas, 90)
    b.edit.layers[0].t = FrameRange(0, 20)
    b.save_edit()
    render.render(b, root / "out.mp4")
    assert (root / "out.mp4").stat().st_size > 0


def test_deleting_every_segment_turns_the_camera_off(tmp_path):
    """It used to fall back to the implicit whole-take camera, so the head came back for
    the ENTIRE recording -- the track could add and move the camera but never remove it,
    which is the one thing it exists for."""
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, TOTAL, 170)
    for seg in list(e.layers):
        layers_mod.drop_webcam_segment(e, seg.id)
    assert _ids(e) == []


def test_bringing_a_segment_back_re_arms_the_camera(tmp_path):
    """...and the tombstone must be liftable, or the new segment is stored and never
    rendered."""
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, TOTAL, 170)
    for seg in list(e.layers):
        layers_mod.drop_webcam_segment(e, seg.id)
    layers_mod.add_webcam_segment(e, CANVAS, TOTAL, FrameRange(100, 200))
    assert [(s[1], s[2]) for s in _ids(e)] == [(100, 200)]


@needs_ffmpeg
def test_disabling_the_only_segment_does_not_render_the_whole_take(tmp_path):
    """_layer_list tested the ENABLED layers for a webcam, so one disabled segment read
    as "no track" and appended the whole-take camera instead."""
    root = tmp_path / "disabled"
    synthetic.make_bundle(root, seconds=2.0, width=320, height=180)
    b = Bundle(root)
    layers_mod.materialize_webcam(b.edit, b.canvas, 60)
    b.edit.layers[0].enabled = False
    assert "webcam" not in render.build_graph(b).graph


@needs_ffmpeg
def test_the_global_toggle_and_the_track_agree(tmp_path):
    """The toggle gates the camera INPUT, so a track that kept drawing segments while it
    was off promised a camera the export could not contain. Segments are kept, not
    discarded, so toggling back on restores them."""
    root = tmp_path / "toggle"
    synthetic.make_bundle(root, seconds=2.0, width=320, height=180)
    b = Bundle(root)
    layers_mod.split_webcam(b.edit, b.canvas, 60, 30)
    b.edit.webcam.enabled = False
    assert qmlbridge.project_state(b)["webcam_track"]["segments"] == []
    assert "webcam" not in render.build_graph(b).graph
    b.edit.webcam.enabled = True
    assert len(qmlbridge.project_state(b)["webcam_track"]["segments"]) == 2


@needs_ffmpeg
def test_selecting_the_untouched_camera_does_not_throw(tmp_path):
    """The implicit segment is a real id to the UI but not a stored layer, so
    materialization has to happen before the lookup -- and must not RENAME it, or the id
    the UI is holding stops existing at the moment it becomes real."""
    root = tmp_path / "implicit"
    synthetic.make_bundle(root, seconds=2.0, width=320, height=180)
    b = Bundle(root)
    seg_id = qmlbridge.project_state(b)["webcam_track"]["segments"][0]["id"]
    qmlbridge.apply_op(b, "set_webcam", {"id": seg_id, "shape": "rect"})
    assert [(l.id, l.props["shape"]) for l in b.edit.layers] == [(seg_id, "rect")]


@needs_ffmpeg
def test_the_segment_rect_is_flat_like_the_global_one(tmp_path):
    """resolve_placement wraps its result; nested, the placement grid read r.x as
    undefined and posted NaN coordinates, so the grid was dead for any selected
    segment."""
    root = tmp_path / "rect"
    synthetic.make_bundle(root, seconds=2.0, width=320, height=180)
    b = Bundle(root)
    st = qmlbridge.project_state(b)
    seg_rect = st["webcam_track"]["segments"][0]["rect"]
    assert set(seg_rect) == set(st["webcam"]["rect"])
    assert isinstance(seg_rect["x"], float)


@needs_ffmpeg
def test_a_burned_in_or_camera_less_recording_grows_no_track(tmp_path):
    """The no-id branch had this guard; the track ops did not, so `S` wrote camera
    segments into a recording with no camera stream to fill them."""
    root = tmp_path / "nocam"
    synthetic.make_bundle(root, seconds=2.0, width=320, height=180, camera=False)
    b = Bundle(root)
    for op, args in (("split_webcam", {"at_ms": 500}),
                     ("add_webcam_segment", {"start_ms": 0, "end_ms": 500})):
        with pytest.raises(qmlbridge.BridgeError):
            qmlbridge.apply_op(b, op, args)
    assert b.edit.layers == []


@needs_ffmpeg
def test_a_segments_size_follows_its_own_shape_not_the_global_one(tmp_path):
    """A circle is square in PIXELS. Deriving the height from the global shape made a
    circular segment under a rect default come out an ellipse."""
    root = tmp_path / "size"
    synthetic.make_bundle(root, seconds=2.0, width=640, height=360)
    b = Bundle(root)
    seg_id = qmlbridge.project_state(b)["webcam_track"]["segments"][0]["id"]
    qmlbridge.apply_op(b, "set_webcam", {"id": seg_id, "shape": "circle"})
    b.edit.webcam.shape = "rect"
    qmlbridge.apply_op(b, "set_webcam", {"id": seg_id, "size": 0.3})
    seg = b.edit.layers[0]
    assert round(seg.w * 640) == round(seg.h * 360)


# --- trimming and moving -----------------------------------------------------


def _three(total=90):
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, total, 30)
    layers_mod.split_webcam(e, CANVAS, total, 60)
    return e


def test_a_trim_stops_at_the_previous_segment():
    """Clamped rather than refused: a drag has an obvious nearest legal position, and
    refusing on release would snap the segment back and read as being ignored."""
    e = _three()
    mid = next(l for l in e.layers if l.t.start == 30)
    got = layers_mod.clamp_webcam_range(e, mid, FrameRange(5, 60), 90)
    assert (got.start, got.end) == (30, 60)


def test_a_trim_stops_at_the_next_segment():
    e = _three()
    mid = next(l for l in e.layers if l.t.start == 30)
    got = layers_mod.clamp_webcam_range(e, mid, FrameRange(30, 90), 90)
    assert (got.start, got.end) == (30, 60)


def test_a_move_into_a_free_gap_is_left_alone():
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, 90, 30)
    tail = next(l for l in e.layers if l.t.start == 30)
    layers_mod.drop_webcam_segment(e, tail.id)
    head = e.layers[0]
    got = layers_mod.clamp_webcam_range(e, head, FrameRange(40, 70), 90)
    assert (got.start, got.end) == (40, 70)


def test_the_clamp_always_returns_a_legal_range():
    """FrameRange refuses an inverted range in its constructor, so the clamp can never
    be handed one -- but it CAN be squeezed by neighbours on both sides, and it still
    has to produce something FrameRange will accept."""
    e = _three()
    mid = next(l for l in e.layers if l.t.start == 30)
    for want in (FrameRange(31, 32), FrameRange(30, 60), FrameRange(1, 89)):
        got = layers_mod.clamp_webcam_range(e, mid, want, 90)
        assert got.end > got.start
        assert got.start >= 30 and got.end <= 60


@needs_ffmpeg
def test_dragging_through_a_neighbour_cannot_create_an_overlap(tmp_path):
    """The invariant belongs to the MODEL, not the timeline. add_webcam_segment already
    refused overlaps, so a trim must not be the one way to make one -- the bridge is
    driven by scripts and tests as well as by the row."""
    root = tmp_path / "drag"
    synthetic.make_bundle(root, seconds=3.0, width=320, height=180)
    b = Bundle(root)
    layers_mod.split_webcam(b.edit, b.canvas, 90, 30)
    layers_mod.split_webcam(b.edit, b.canvas, 90, 60)
    ms = 1000.0 / b.timebase.fps
    mid = next(l for l in b.edit.layers if l.t.start == 30)
    for start, end in ((5, 60), (30, 90), (0, 90)):
        qmlbridge.apply_op(b, "update_layer",
                           {"id": mid.id, "start_ms": start * ms, "end_ms": end * ms})
        spans = sorted((l.t.start, l.t.end) for l in b.edit.layers)
        assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1)), spans
