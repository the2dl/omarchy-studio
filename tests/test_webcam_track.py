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
    assert _ids(e) == [("webcam1", 0, 170), ("webcam2", 170, TOTAL)]


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
    e.layers = [l for l in e.layers if l.id != "webcam2"]
    assert _ids(e) == [("webcam1", 0, 170)]


def test_a_new_segment_inherits_the_look_of_its_nearest_neighbour():
    """"Bring my head back" should bring back the SAME head, so only its position is
    worth changing afterwards."""
    e = Edit()
    layers_mod.split_webcam(e, CANVAS, TOTAL, 170)
    e.layers = [l for l in e.layers if l.id != "webcam2"]
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
    seg = next(l for l in e.layers if l.id == "webcam2")
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
    qmlbridge.apply_op(b, "delete_layer", {"id": "webcam2"})
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

    qmlbridge.apply_op(b, "set_webcam", {"id": "webcam2", "shape": "rect"})
    seg = next(l for l in b.edit.layers if l.id == "webcam2")
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
