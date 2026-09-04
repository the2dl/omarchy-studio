from __future__ import annotations

import subprocess

import pytest
from ffmpeg_harness import FFMPEG, FONTFILE, framehashes, needs_ffmpeg, needs_font

from omarchy_studio.geometry import Canvas, Placement, ffmpeg_blur, text_placement
from omarchy_studio import layers as layers_mod
from omarchy_studio.layers import (
    DEFAULT_FONTFILE,
    InputRegistry,
    LayerError,
    UnsupportedLayer,
    compile_layer,
    timebase_chain,
    webcam_layer,
)
from omarchy_studio.project import Layer, WebcamSettings
from omarchy_studio.timebase import CutMap, FrameRange, Timebase

CANVAS = Canvas(320, 240)
TB = Timebase(30)
TOTAL = 30


def cutmap(cuts=()) -> CutMap:
    return CutMap([FrameRange(*c) for c in cuts], TOTAL)


def registry(camera: str | None = None) -> InputRegistry:
    reg = InputRegistry()
    if camera:
        reg.bind("camera", camera)
    return reg


def compile_one(layer: Layer, *, cuts=(), camera=None, label_in="[base]"):
    return compile_layer(
        layer, CANVAS, cutmap(cuts), TB, registry(camera), label_in=label_in
    )


# -- forward compatibility ---------------------------------------------------


def test_unknown_type_is_skipped_with_a_warning():
    """A project written by a newer build must degrade to 'some overlays missing'."""
    layer = Layer(id="a", type="hologram")
    with pytest.warns(UnsupportedLayer, match="hologram"):
        assert compile_one(layer) is None


def test_deferred_arrow_is_skipped_with_a_warning():
    with pytest.warns(UnsupportedLayer, match="deferred"):
        assert compile_one(Layer(id="a", type="arrow")) is None


def test_zoom_is_not_an_overlay_and_warns_about_nothing():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert compile_one(Layer(id="z", type="zoom")) is None


def test_disabled_layer_is_skipped():
    assert compile_one(Layer(id="a", type="shape", enabled=False)) is None


def test_layer_entirely_inside_a_cut_vanishes():
    layer = Layer(id="a", type="shape", t=FrameRange(12, 18))
    assert compile_one(layer, cuts=[(10, 20)]) is None


# -- geometry comes from geometry.py -----------------------------------------


def test_overlay_position_is_the_resolved_placement():
    layer = Layer(id="a", type="shape", x=0.25, y=0.5, w=0.25, h=0.125)
    r = Placement(0.25, 0.5, 0.25, 0.125).resolve(CANVAS).to_even()
    f = compile_one(layer)
    assert f"overlay=x={int(r.x)}:y={int(r.y)}" in f.filter_chain
    assert f"s={int(r.w)}x{int(r.h)}" in f.filter_chain


def test_centre_anchor_is_honoured():
    layer = Layer(id="a", type="shape", x=0.5, y=0.5, w=0.25, h=0.25, anchor="center")
    r = Placement(0.5, 0.5, 0.25, 0.25, "center").resolve(CANVAS).to_even()
    assert f"overlay=x={int(r.x)}:y={int(r.y)}" in compile_one(layer).filter_chain


def test_text_is_centred_on_the_same_point_geometry_reports():
    """The tile origin IS the placement rect origin, so the tile-relative form has to
    land on geometry.text_placement's canvas-absolute centre. Centre anchoring measured
    0.0 px against the preview; left anchoring drifted ~7% of the string width."""
    place = Placement(0.1, 0.2, 0.5, 0.1)
    want_cx = float(
        text_placement("hi", place, CANVAS, 24)["ffmpeg_xy"]
        .split("x=")[1]
        .split("-text_w")[0]
    )
    r = place.resolve(CANVAS).to_even()
    assert r.x + r.w / 2 == pytest.approx(want_cx, abs=1.0)

    layer = Layer(id="t", type="text", x=0.1, y=0.2, w=0.5, h=0.1, props={"text": "hi"})
    assert "x=(w-text_w)/2:y=(h-text_h)/2" in compile_one(layer).filter_chain


def test_blur_sigma_comes_from_geometry():
    layer = Layer(id="b", type="blur", w=0.25, h=0.25, props={"preset": "heavy"})
    assert ffmpeg_blur("heavy") in compile_one(layer).filter_chain


def test_an_unknown_redaction_preset_raises_rather_than_defaulting():
    """Falling back to the weakest setting on a typo is the failure the preset ladder
    exists to prevent -- it would silently ship the least-obscured option."""
    layer = Layer(id="b", type="blur", w=0.2, h=0.2, props={"preset": "medium"})
    with pytest.raises(ValueError):
        compile_one(layer)


def test_blur_uses_gblur_not_boxblur():
    """Same Gaussian kernel family as Qt's MultiEffect, so preview and export track
    each other as strength varies."""
    chain = compile_one(Layer(id="b", type="blur", w=0.2, h=0.2)).filter_chain
    assert "gblur=" in chain and "boxblur" not in chain


def test_redaction_crop_is_clamped_inside_the_canvas():
    """`crop` outside the frame is a hard error, unlike `overlay`."""
    layer = Layer(id="b", type="blur", x=0.9, y=0.9, w=0.5, h=0.5)
    chain = compile_one(layer).filter_chain
    crop = chain.split("crop=")[1].split(",")[0]
    parts = dict(p.split("=") for p in crop.split(":"))
    assert int(parts["x"]) + int(parts["w"]) <= CANVAS.width
    assert int(parts["y"]) + int(parts["h"]) <= CANVAS.height


def test_overlay_is_not_clamped():
    """An overlay may legitimately hang off the edge; `overlay` accepts that."""
    layer = Layer(id="a", type="shape", x=0.9, y=0.9, w=0.5, h=0.5)
    chain = compile_one(layer).filter_chain
    assert "overlay=x=288:y=216" in chain


# -- gating ------------------------------------------------------------------


def test_gate_is_remapped_through_the_cutmap():
    layer = Layer(id="a", type="shape", t=FrameRange(20, 26))
    chain = compile_one(layer, cuts=[(5, 15)]).filter_chain
    # Source [20,26) with 10 frames removed before it becomes output [10,16).
    assert "enable='gte(n,10)*lt(n,16)'" in chain


def test_a_layer_straddling_a_cut_gets_one_merged_gate():
    """CutMap.remap merges adjacent output intervals, which is what keeps generated
    gates small enough to stay under the expression budget."""
    layer = Layer(id="a", type="shape", t=FrameRange(0, 30))
    chain = compile_one(layer, cuts=[(10, 20)]).filter_chain
    assert "enable='gte(n,0)*lt(n,20)'" in chain


def test_gate_is_on_the_frame_index_never_on_time():
    chain = compile_one(Layer(id="a", type="shape")).filter_chain
    assert "gte(n," in chain
    assert "between(" not in chain and "gte(t," not in chain


# -- the one primitive -------------------------------------------------------


def test_every_overlay_type_ends_in_the_same_overlay():
    reg = InputRegistry()
    reg.bind("camera", "[cam]")
    common = "overlay=x=0:y=0:enable='gte(n,0)*lt(n,30)':eof_action=repeat:shortest=0"
    for layer in (
        Layer(id="i", type="image", w=0.5, h=0.5, props={"path": "/tmp/x.png"}),
        Layer(id="t", type="text", w=0.5, h=0.5, props={"text": "x"}),
        Layer(id="s", type="shape", w=0.5, h=0.5),
        Layer(id="w", type="webcam", w=0.5, h=0.5),
    ):
        f = compile_layer(layer, CANVAS, cutmap(), TB, reg, label_in="[base]")
        assert common in f.filter_chain, layer.type
        assert f.label_in == "[base]"
        assert f.label_out.endswith("_o]")


def test_a_static_tile_source_produces_one_frame():
    chain = compile_one(Layer(id="a", type="shape", w=0.5, h=0.5)).filter_chain
    assert ":r=1:d=1," in chain
    faded = compile_one(
        Layer(id="a", type="shape", w=0.5, h=0.5, fade_frames=4)
    ).filter_chain
    assert ":r=30/1:d=1.000000," in faded


def test_fade_is_frame_indexed():
    layer = Layer(id="a", type="shape", t=FrameRange(0, 30), fade_frames=6)
    chain = compile_one(layer).filter_chain
    assert "fade=t=in:alpha=1:s=0:n=6" in chain
    assert "fade=t=out:alpha=1:s=24:n=6" in chain


def test_opacity_becomes_an_alpha_multiplier():
    chain = compile_one(Layer(id="a", type="shape", opacity=0.5)).filter_chain
    assert "colorchannelmixer=aa=0.500000" in chain


def test_redaction_never_fades():
    """A partially transparent blur box leaks the pixels it exists to hide."""
    layer = Layer(id="b", type="blur", w=0.3, h=0.3, fade_frames=10, opacity=0.4)
    chain = compile_one(layer).filter_chain
    assert "fade=" not in chain and "colorchannelmixer" not in chain


# -- inputs ------------------------------------------------------------------


def test_a_static_image_is_decoded_once():
    """A tile that never changes is generated once and held by overlay's
    eof_action=repeat -- 0.129 s against 1.294 s for 40 full-length tile sources over 60
    frames at 1080p, bit-identical output."""
    reg = InputRegistry()
    layer = Layer(id="i", type="image", w=0.5, h=0.5, props={"path": "/tmp/logo.png"})
    f = compile_layer(layer, CANVAS, cutmap([(0, 6)]), TB, reg, label_in="[base]")
    assert f.extra_inputs == [["-i", "/tmp/logo.png"]]
    assert reg.argv() == ["-i", "/tmp/logo.png"]


def test_a_faded_image_is_looped_for_the_output_duration():
    """`fade` counts frames, so a fading tile needs real ones."""
    reg = InputRegistry()
    layer = Layer(
        id="i", type="image", w=0.5, h=0.5, fade_frames=3,
        props={"path": "/tmp/logo.png"},
    )
    f = compile_layer(layer, CANVAS, cutmap([(0, 6)]), TB, reg, label_in="[base]")
    args = f.extra_inputs[0]
    assert args[:2] == ["-loop", "1"]
    assert args[-2:] == ["-i", "/tmp/logo.png"]
    # 24 output frames at 30 fps.
    assert args[args.index("-t") + 1] == "0.800000"


def test_image_without_a_usable_path_is_skipped_not_fatal():
    """It used to raise, which took the whole export down with it.

    render._resolve_asset now empties `path` when the asset name escapes the bundle --
    a crafted edit.json could otherwise point ffmpeg at any file or URL -- and an
    unrenderable layer must not stop the export, the same rule this compiler already
    follows for a range a cut removed entirely.
    """
    with pytest.warns(UnsupportedLayer):
        assert compile_one(Layer(id="i", type="image", props={})) is None


def test_webcam_needs_a_bound_camera_stream():
    with pytest.raises(LayerError, match="camera"):
        compile_one(Layer(id="w", type="webcam"))


def test_webcam_reads_the_bound_label_and_never_resamples_it():
    """`trim` counts frames on its input's own grid, so the camera is put on the project
    frame grid and cut upstream; doing it again here would double-resample."""
    chain = compile_one(Layer(id="w", type="webcam"), camera="[cam_cut]").filter_chain
    assert chain.startswith("[cam_cut]")
    assert "fps=" not in chain


def test_timebase_chain_runs_before_the_cut():
    assert timebase_chain("[1:v]", TB, "[cam]") == "[1:v]fps=30/1,setsar=1[cam]"


def test_webcam_layer_adapts_the_settings():
    layer = webcam_layer(WebcamSettings(x=0.5, y=0.25, shape="rounded"), CANVAS)
    assert layer.type == "webcam" and layer.x == 0.5 and layer.y == 0.25
    assert layer.props["shape"] == "rounded"


# -- `rounded`, the superellipse ----------------------------------------------
#
# A superellipse, not a rounded rectangle with a generous radius. It has to survive
# three separate implementations of the same outline -- this filtergraph mask,
# editor/SquircleShape.qml, and the setup bar's self-view -- so the properties that
# make it that shape are asserted here rather than left to the eye.


def test_rounded_gets_the_square_centre_crop_like_the_circle():
    # Without it the camera's 16:9 frame is stretched into a square box, and every
    # face in it is 33% too wide.
    chain = compile_one(
        Layer(id="w", type="webcam", props={"shape": "rounded"}), camera="[cam]"
    ).filter_chain
    assert "crop=w='min(iw,ih)'" in chain


def test_rounded_is_the_superellipse_and_not_a_rounded_rectangle():
    rounded = compile_one(
        Layer(id="w", type="webcam", props={"shape": "rounded"}), camera="[cam]"
    ).filter_chain
    # The Lamé exponent is what makes it that shape; a rounded rect has no pow().
    assert "pow(" in rounded
    assert layers_mod._rounded_rect_mask(64, 64, 8) not in rounded


def test_the_old_shape_names_still_open():
    # Bundles recorded before the three names were agreed say "squircle" (the model's
    # old name) or "corner" (the setup bar's). Both meant this shape; neither should
    # open as a circle, and neither should raise.
    for legacy in ("squircle", "corner"):
        assert WebcamSettings(shape=legacy).shape == "rounded"
    assert WebcamSettings(shape="nonsense").shape == "circle"


def test_the_squircle_mask_reduces_to_the_circle_at_n_equals_two():
    """The cheapest proof the superellipse maths is right: at n=2 a Lamé curve IS an
    ellipse, so the two masks must agree pixel for pixel on a square tile."""
    import re as _re

    def sample(expr, x, y):
        # geq's variables, evaluated in Python. The expressions use only abs/pow/
        # hypot/clip, all of which mean the same thing in both languages.
        env = {"X": x, "Y": y, "abs": abs, "pow": pow,
               "hypot": __import__("math").hypot,
               "clip": lambda v, lo, hi: max(lo, min(hi, v))}
        return eval(_re.sub(r"\bclip\(", "clip(", expr), {"__builtins__": {}}, env)

    circle = layers_mod._circle_mask(64, 64)
    lame2 = layers_mod._squircle_mask(64, 64, n=2.0)
    for x, y in ((32, 32), (2, 2), (32, 1), (10, 55), (63, 63)):
        assert abs(sample(circle, x, y) - sample(lame2, x, y)) < 0.5


def test_a_rounded_camera_is_square_in_pixels():
    # Same correction the circle gets: w and h normalize against different axes, so
    # equal values are an ellipse on any non-square canvas.
    place = WebcamSettings(w=0.2, h=0.2, shape="rounded").placement(CANVAS)
    assert place.w * CANVAS.width == pytest.approx(place.h * CANVAS.height)


def test_registry_indices_are_sequential():
    reg = InputRegistry()
    assert reg.add(["-i", "a.mp4"], key="screen") == 0
    assert reg.add(["-i", "b.mp4"]) == 1
    assert reg.label("screen") == "[0:v]"
    assert reg.argv() == ["-i", "a.mp4", "-i", "b.mp4"]


# -- text ---------------------------------------------------------------------


def test_text_uses_the_one_font_file():
    chain = compile_one(Layer(id="t", type="text", props={"text": "hi"})).filter_chain
    assert f"fontfile={DEFAULT_FONTFILE}" in chain


def test_text_is_escaped():
    layer = Layer(id="t", type="text", props={"text": "50% off: it's a deal"})
    chain = compile_one(layer).filter_chain
    assert "50\\\\%\\ off\\:\\ it'\\\\\\''s\\ a\\ deal" in chain


def test_translucent_text_box_folds_its_alpha_into_the_mask():
    """alphamerge REPLACES the alpha plane, so a translucent box colour came out fully
    opaque while the QML preview blended it correctly. Found by the preview cross-check."""
    layer = Layer(
        id="t", type="text", w=0.5, h=0.2,
        props={"text": "hi", "box_color": "#101820@0.85", "radius": 0.1},
    )
    chain = compile_one(layer).filter_chain
    assert ")*0.850000'" in chain
    # drawtext comes AFTER the alphamerge so the glyphs stay at alpha 1.
    assert chain.index("alphamerge") < chain.index("drawtext")


# -- it has to survive contact with ffmpeg -----------------------------------


def _png(tmp_path):
    p = tmp_path / "logo.png"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1", "-frames:v", "1", str(p)],
        check=True, capture_output=True,
    )
    return p


def _cam(tmp_path):
    p = tmp_path / "cam.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "smptebars=s=160x120:r=30:d=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(p)],
        check=True, capture_output=True,
    )
    return p


def _build(layers, tmp_path, camera=None):
    """Assemble the way the render driver will: one chain, layers in z order."""
    reg = InputRegistry()
    reg.add(["-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=1"], key="screen")
    chains = ["[0:v]format=rgba[base]"]
    if camera is not None:
        reg.add(["-i", str(camera)], key="camera_file")
        chains.append(timebase_chain(reg.label("camera_file"), TB, "[cam]"))
        reg.bind("camera", "[cam]")
    cur = "[base]"
    for layer in layers:
        f = compile_layer(layer, CANVAS, cutmap(), TB, reg, label_in=cur)
        if f is None:
            continue
        chains.append(f.filter_chain)
        cur = f.label_out
    chains.append(f"{cur}format=yuv420p[vout]")
    return ";".join(chains), reg.inputs


@needs_ffmpeg
@needs_font
def test_a_graph_with_every_layer_type_renders(tmp_path):
    layers = [
        Layer(id="bg", type="image", w=1.0, h=1.0, props={"path": str(_png(tmp_path))}),
        Layer(id="pip", type="image", x=0.6, y=0.05, w=0.3, h=0.3,
              props={"path": str(_png(tmp_path)), "fit": "contain"}),
        Layer(id="box", type="shape", x=0.05, y=0.7, w=0.3, h=0.15,
              props={"color": "#ff3b30@0.8", "radius": 0.2}),
        Layer(id="cap", type="text", x=0.1, y=0.1, w=0.6, h=0.12,
              t=FrameRange(5, 25), fade_frames=4, props={"text": "Hello: 100%"}),
        Layer(id="red", type="blur", x=0.4, y=0.4, w=0.25, h=0.15,
              props={"strength": 0.9}),
        Layer(id="pix", type="pixelate", x=0.1, y=0.4, w=0.2, h=0.15,
              props={"block": 12}),
        webcam_layer(WebcamSettings(x=0.7, y=0.6, w=0.2, h=0.25), CANVAS),
    ]
    graph, inputs = _build(layers, tmp_path, camera=_cam(tmp_path))
    hashes = framehashes(graph, tmp_path, inputs=inputs, frames=30)
    assert len(hashes) == 30


@needs_ffmpeg
@needs_font
def test_a_gated_layer_is_absent_outside_its_range(tmp_path):
    """The gate has to actually gate -- and on the exact half-open boundary."""
    layers = [
        Layer(id="cap", type="shape", x=0.1, y=0.1, w=0.5, h=0.2,
              t=FrameRange(4, 8), props={"color": "#ffffff"}),
    ]
    graph, inputs = _build(layers, tmp_path)
    hashes = framehashes(graph, tmp_path, inputs=inputs, frames=12)
    off, on = hashes[0], hashes[4]
    assert off != on
    assert hashes == [off] * 4 + [on] * 4 + [off] * 4


@needs_ffmpeg
def test_webcam_circle_mask_leaves_the_corners_transparent(tmp_path):
    """The mask is built once from a 1-frame lavfi source, so `geq` -- a per-pixel
    interpreter -- runs once for the whole render rather than once per frame."""
    layers = [webcam_layer(WebcamSettings(x=0.0, y=0.0, w=0.25, h=0.3333), CANVAS)]
    graph, inputs = _build(layers, tmp_path, camera=_cam(tmp_path))
    corner, middle = (
        framehashes(
            graph + f";[vout]crop=4:4:{x}:{y}[sample]",
            tmp_path, inputs=inputs, maps=["[sample]"], frames=1,
        )
        for x, y in ((1, 1), (38, 38))
    )
    black = framehashes(
        "[0:v]crop=4:4:1:1[corner]", tmp_path,
        inputs=[["-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=1"]],
        maps=["[corner]"], frames=1,
    )
    # The corner is still the black base; the middle is camera pixels. Without the
    # second assertion this would pass just as happily with no webcam at all.
    assert corner == black
    assert middle != black


@needs_ffmpeg
def test_two_hundred_layers_still_parse(tmp_path):
    """Cost is linear in layers ever added (~0.7-1.0 ms per layer per 1080p frame), but
    the graph must at least still be legal at that size."""
    layers = [
        Layer(id=f"L{i}", type="shape", x=(i % 10) / 20, y=(i % 7) / 20,
              w=0.05, h=0.05, t=FrameRange(i % 20, i % 20 + 5))
        for i in range(200)
    ]
    graph, inputs = _build(layers, tmp_path)
    hashes = framehashes(graph, tmp_path, inputs=inputs, frames=3)
    assert len(hashes) == 3
