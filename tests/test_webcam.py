"""The camera bubble's drop shadow.

The shadow is emitted as its OWN overlay against the base, under the bubble's tile,
rather than composited into the tile. Two earlier attempts at the latter are why:
a still plate used as the overlay's main input dragged its 1/1 timebase into the
output and froze the camera on frame one, and making that plate infinite instead
ended the tile one frame early through `shortest=1`. Both traps are documented in
render._backdrop; a still plate held by `eof_action=repeat` against the finite base
avoids them, and is what this file already does for every other static tile.
"""

from __future__ import annotations


def _cam(shape="circle", shadow=True, w=200, h=200):
    """The webcam tile's chains, its tile label, and its shadow plate (or None)."""
    from omarchy_studio.geometry import Rect
    from omarchy_studio.layers import InputRegistry, _tile_webcam
    from omarchy_studio.project import Layer

    layer = Layer(id="webcam", type="webcam", x=0.0, y=0.0, w=0.5, h=0.5,
                  props={"shape": shape, "mirror": True, "shadow": shadow})
    reg = InputRegistry()
    reg.bind("camera", "[cam]")
    chains, tile, sh = _tile_webcam(layer, "webcam", Rect(0, 0, w, h), reg)
    return ";".join(chains), tile, sh


def test_the_bubble_gets_a_shadow_in_every_shape():
    """A rect bubble has no mask to reuse, which is the case that breaks if the shadow
    is ever wired onto the masking path instead of alongside it."""
    for shape in ("circle", "rounded", "rect"):
        g, _tile, sh = _cam(shape=shape, shadow=True)
        assert "gblur" in g, shape
        assert sh is not None, shape
        assert sh[0].endswith("_shadow]"), shape


def test_no_shadow_leaves_the_tile_exactly_as_it_was():
    """Off is the old graph, not a graph with a transparent shadow in it."""
    for shape in ("circle", "rounded", "rect"):
        g, _tile, sh = _cam(shape=shape, shadow=False)
        assert "gblur" not in g, shape
        assert sh is None, shape


def test_the_shadow_never_becomes_the_tile():
    """The tile stays the bubble, at the bubble's own rect. The shadow is a separate
    overlay placed behind it -- if it ever ends up inside the tile again, the camera
    freezes or the take loses its last frame."""
    for shadow in (True, False):
        _g, tile, _sh = _cam(shadow=shadow)
        assert tile == "[webcam_s]"


def test_the_shadow_sits_up_and_left_of_the_bubble_and_falls_below_it():
    """The plate is bigger than the bubble on every side, so it starts a margin earlier;
    inside it the silhouette is padded DOWN, which is what makes the shadow read as a
    shadow rather than a halo."""
    from omarchy_studio.layers import _shadow_margin

    w = h = 240
    m, dy = _shadow_margin(w, h)
    g, _tile, sh = _cam(shadow=True, w=w, h=h)
    assert sh[1] == -m and sh[2] == -m
    assert f"pad={w + 2 * m}:{h + 2 * m + dy}:{m}:{m + dy}:" in g
    assert dy > 0


def test_the_shadow_scales_with_the_bubble():
    """A fixed blur reads as lifted on a big bubble and as a smudge under a small one,
    and the size is a slider people move."""
    from omarchy_studio.layers import _shadow_margin

    small, _ = _shadow_margin(120, 120)
    large, _ = _shadow_margin(600, 600)
    assert large > small * 3


def test_the_shape_is_only_evaluated_once():
    """The mask feeds both the bubble's alpha and the shadow's silhouette, and ffmpeg
    hands a labelled pad to exactly one consumer -- so it is split, not recomputed."""
    g, _tile, _sh = _cam(shape="circle", shadow=True)
    assert g.count("geq=lum=") == 1
    assert "split=2" in g


def test_the_shadow_plate_is_a_single_frame():
    """Held by the overlay's eof_action=repeat, like every other static tile here. An
    infinite plate is what ended the tile a frame early last time."""
    g, _tile, _sh = _cam(shadow=True)
    assert "r=1:d=1" in g
    assert "_shadow]" in g


def test_the_shadow_is_drawn_under_the_bubble_not_over_it():
    """Order in the emitted graph is the z-order on screen."""
    from omarchy_studio.geometry import Canvas
    from omarchy_studio.project import Edit
    from omarchy_studio import layers as L
    from omarchy_studio.timebase import CutMap, Timebase

    edit = Edit()
    edit.webcam.enabled = True
    edit.webcam.shadow = True
    canvas = Canvas(1920, 1080)
    layer = L.webcam_layer(edit.webcam, canvas)
    reg = L.InputRegistry()
    reg.bind("camera", "[cam]")
    frag = L.compile_layer(layer, canvas, CutMap([], 100),
                           Timebase(fps_num=30, fps_den=1), reg, label_in="[base]")
    g = frag.filter_chain
    assert g.index("_sh]") < g.index("_o]"), "the shadow must be composited first"
