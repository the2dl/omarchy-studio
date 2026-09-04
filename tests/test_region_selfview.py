"""A region capture can have a live self-view, by recording more than it shows.

KMS is the only backend that captures a sub-rectangle directly, and KMS reads the DRM
scanout BELOW the compositor -- so `no_screen_share` is invisible to it and the bubble
would be welded into every frame. The portal CAN hide it, but only ever hands over a
whole monitor.

So a region that wants a self-view records the MONITOR through the portal and carries
the region as a crop. The renderer crops before anything else, and every stage after it
sees exactly the frame the user chose. macOS never faces this: it excludes windows on
the same fast path whatever the capture is, which is why the reference design just shows the
bubble.

The side effect is worth as much as the fix: the whole monitor is on disk, so the
framing stops being a decision made before recording.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import needs_ffmpeg

from omarchy_studio import capture as capture_mod, probe, render
from omarchy_studio.project import Bundle, Capture, Stream


def _cap(**kw) -> Capture:
    return Capture(screen=Stream(path="screen.mp4", width=5120, height=2880,
                                 fps_num=30, fps_den=1), **kw)


# --- the manifest ------------------------------------------------------------


def test_without_a_crop_the_canvas_is_the_stream():
    c = _cap()
    assert (c.canvas.width, c.canvas.height) == (5120, 2880)
    assert c.crop_rect() is None


def test_the_crop_is_the_canvas():
    """Everything downstream composes on the canvas, so making the crop the canvas is
    what keeps a portal-and-crop region identical to a direct one from there on."""
    c = _cap(source_crop={"x": 22, "y": 90, "width": 5076, "height": 2768})
    assert (c.canvas.width, c.canvas.height) == (5076, 2768)
    assert c.crop_rect() == (22, 90, 5076, 2768)


def test_a_crop_running_past_the_stream_is_clamped():
    """ffmpeg refuses the whole graph over a crop bigger than its input, which would
    turn a bad manifest into an export that cannot run at all."""
    c = _cap(source_crop={"x": 5000, "y": 2800, "width": 9999, "height": 9999})
    x, y, w, h = c.crop_rect()
    assert x + w <= 5120 and y + h <= 2880


def test_an_unusable_crop_is_ignored_rather_than_fatal():
    for bad in ({"nonsense": 1}, {"x": "a", "y": 0, "width": 10, "height": 10},
                {"x": 0, "y": 0, "width": 0, "height": 10}):
        assert _cap(source_crop=bad).crop_rect() is None


def test_the_crop_is_even_for_chroma_siting():
    c = _cap(source_crop={"x": 11, "y": 45, "width": 2539, "height": 1385})
    x, y, w, h = c.crop_rect()
    assert x % 2 == 0 and y % 2 == 0 and w % 2 == 0 and h % 2 == 0


# --- begin -------------------------------------------------------------------


def test_begin_records_where_the_frame_sits_in_the_stream(tmp_path):
    b = capture_mod.begin(
        tmp_path / "rec",
        logical_geometry={"x": 11, "y": 45, "width": 2538, "height": 1384},
        monitor_name="DP-1", monitor_scale=2.0,
        source_logical={"x": 0, "y": 0, "width": 2560, "height": 1440},
    )
    # Offsets are absolute desktop coordinates, so the crop is measured from the
    # STREAM's origin, not the desktop's.
    assert b.capture.source_crop == {"x": 22, "y": 90, "width": 5076, "height": 2768}
    # The frame the user chose is still what everything maps against.
    assert b.capture.logical_geometry["width"] == 2538


def test_begin_without_a_larger_source_records_no_crop(tmp_path):
    b = capture_mod.begin(
        tmp_path / "rec2",
        logical_geometry={"x": 0, "y": 0, "width": 2560, "height": 1440},
        monitor_name="DP-1", monitor_scale=2.0,
        source_logical={"x": 0, "y": 0, "width": 2560, "height": 1440},
    )
    assert b.capture.source_crop == {}, "an identical source is not a crop"


# --- the render --------------------------------------------------------------


@needs_ffmpeg
def test_the_crop_runs_before_the_frame_grid(tmp_path):
    """Before the timebase, the cuts, the zoom and the layers: cropping first is what
    means there is no second coordinate system anywhere downstream."""
    root = tmp_path / "crop"
    synthetic.make_bundle(root, seconds=1.0, width=640, height=360, camera=False)
    b = Bundle(root)
    b.capture.source_crop = {"x": 40, "y": 20, "width": 500, "height": 300}
    g = render.build_graph(b).graph
    assert "[0:v]crop=500:300:40:20[cropped]" in g
    assert g.index("crop=500:300") < g.index("fps=")


@needs_ffmpeg
def test_the_export_is_the_frame_not_the_stream(tmp_path):
    root = tmp_path / "render"
    synthetic.make_bundle(root, seconds=1.0, width=640, height=360, camera=False)
    b = Bundle(root)
    b.capture.source_crop = {"x": 40, "y": 20, "width": 500, "height": 300}
    b.edit.export_preset = "native"
    b.save_edit()
    out = root / "o.mp4"
    render.render(b, out)
    assert probe.dimensions(out) == (500, 300)


@needs_ffmpeg
def test_an_uncropped_bundle_renders_exactly_as_before(tmp_path):
    """The property that made this safe to add: no crop, no change."""
    root = tmp_path / "plain"
    synthetic.make_bundle(root, seconds=1.0, width=640, height=360, camera=False)
    g = render.build_graph(Bundle(root)).graph
    assert "crop=" not in g.split("[base]")[0]
