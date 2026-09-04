"""Output-only time at the ends, so a title or end card has somewhere to live.

Cuts can only REMOVE time -- output_frames was the sum of kept source frames -- so
before this the output could never be longer than the capture and a card could only
ever cover the recording rather than precede it.

The other half of the same ask is the covering case, which needed nothing new: a
full-frame image over the FIRST SECONDS OF THE RECORDING keeps camera and audio,
because those are real recorded footage. A pad has neither, and cannot: nothing was
recorded there. The two are different features and this file is only the first.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import needs_ffmpeg

from omarchy_studio import backgrounds, qmlbridge, render
from omarchy_studio.project import BackdropSettings, Bundle, Edit, Layer
from omarchy_studio.timebase import CutMap, FrameRange


# --- the time model ----------------------------------------------------------


def test_pads_make_the_output_longer_than_the_capture():
    cm = CutMap([], 100, head_pad=30, tail_pad=20)
    assert cm.output_frames == 150
    assert cm.kept_frames == 100


def test_a_pad_shifts_every_recorded_frame_later():
    cm = CutMap([], 100, head_pad=30)
    assert cm.to_output(0) == 30
    assert cm.to_output(99) == 129


def test_pads_compose_with_cuts():
    """The recording keeps its own indices; only where it LANDS moves."""
    cm = CutMap([FrameRange(50, 70)], 100, head_pad=30, tail_pad=20)
    assert cm.output_frames == 30 + 80 + 20
    assert cm.to_output(70) == 80          # first frame after the cut
    assert [(r.start, r.end) for r in cm.remap(FrameRange(0, 50))] == [(30, 80)]


def test_no_pads_is_exactly_what_it_was():
    """Every existing project has to render identically."""
    cm = CutMap([FrameRange(10, 20)], 100)
    assert cm.output_frames == 90
    assert cm.to_output(0) == 0
    assert [(r.start, r.end) for r in cm.remap(FrameRange(0, 10))] == [(0, 10)]


def test_pad_frames_are_their_own_coordinate_space():
    """Layer ranges are SOURCE frames so a cut never slides an annotation off what it
    annotates -- and a pad has no source frames, so it needs its own space."""
    cm = CutMap([], 100, head_pad=30, tail_pad=20)
    assert [(r.start, r.end) for r in cm.remap_pad("head", FrameRange(0, 30))] == [(0, 30)]
    assert [(r.start, r.end) for r in cm.remap_pad("tail", FrameRange(0, 20))] == [(130, 150)]


def test_a_pad_range_is_clamped_to_its_pad():
    cm = CutMap([], 100, head_pad=30)
    assert [(r.start, r.end) for r in cm.remap_pad("head", FrameRange(0, 999))] == [(0, 30)]
    assert cm.remap_pad("tail", FrameRange(0, 10)) == []      # no tail pad at all


# --- the ground --------------------------------------------------------------


def test_the_pad_takes_the_backdrop_colour():
    """The user's choice: whatever colour is selected, with a default -- not a black
    band nothing else in the project uses."""
    assert backgrounds.pad_colour(BackdropSettings(color="#ff8800")) == "0xff8800"


def test_an_unusable_colour_falls_back_rather_than_breaking_the_render():
    assert backgrounds.pad_colour(BackdropSettings(color="nonsense")) == \
        backgrounds.DEFAULT_PAD_COLOUR


# --- persistence -------------------------------------------------------------


def test_pads_round_trip_through_edit_json():
    e = Edit()
    e.head_pad_frames, e.tail_pad_frames = 60, 30
    back = Edit.from_dict(e.to_dict())
    assert (back.head_pad_frames, back.tail_pad_frames) == (60, 30)


def test_a_project_written_before_pads_existed_has_none():
    assert (Edit.from_dict({}).head_pad_frames, Edit.from_dict({}).tail_pad_frames) == (0, 0)


def test_a_negative_pad_is_refused_at_the_door():
    assert Edit.from_dict({"head_pad_frames": -30}).head_pad_frames == 0


# --- through the bridge ------------------------------------------------------


@needs_ffmpeg
def test_shrinking_a_pad_clamps_the_layer_living_in_it(tmp_path):
    """A card cannot outlive the pad it sits in -- it would be a layer gated on output
    frames that no longer exist."""
    root = tmp_path / "clamp"
    synthetic.make_bundle(root, seconds=2.0, width=320, height=180, camera=False)
    b = Bundle(root)
    qmlbridge.apply_op(b, "set_pads", {"head_ms": 2000})
    b.edit.layers.append(Layer(id="card", type="shape", pad="head", t=FrameRange(0, 60)))

    qmlbridge.apply_op(b, "set_pads", {"head_ms": 500})
    card = b.edit.layers[0]
    assert card.t.end <= b.edit.head_pad_frames

    qmlbridge.apply_op(b, "set_pads", {"head_ms": 0})
    assert card.pad == "" and card.t is None, "the card was left in a pad that is gone"


# --- rendered ----------------------------------------------------------------


@needs_ffmpeg
def test_a_padded_export_is_longer_than_its_capture(tmp_path):
    root = tmp_path / "render"
    synthetic.make_bundle(root, seconds=2.0, width=320, height=180, camera=False)
    b = Bundle(root)
    source = b.source_frames()
    b.edit.head_pad_frames, b.edit.tail_pad_frames = 30, 15
    b.save_edit()

    out = root / "out.mp4"
    render.render(b, out)
    from omarchy_studio import probe
    assert probe.frame_count(out) == source + 45


@needs_ffmpeg
def test_a_card_in_the_head_pad_renders_before_the_recording(tmp_path):
    """And the recording still renders after it -- the pad adds time rather than
    replacing any."""
    root = tmp_path / "card"
    synthetic.make_bundle(root, seconds=2.0, width=320, height=180, camera=False)
    b = Bundle(root)
    b.edit.head_pad_frames = 30
    b.edit.layers.append(
        Layer(id="card", type="shape", pad="head", t=FrameRange(0, 30),
              x=0.0, y=0.0, w=1.0, h=1.0,
              props={"fill": "#ff0000"}))
    b.save_edit()
    g = render.build_graph(b).graph
    assert "tpad=start=30:start_mode=add" in g
    # The card's gate lives in the pad, at output frames 0..30.
    assert "gte(n,0)" in g and "lt(n,30)" in g
