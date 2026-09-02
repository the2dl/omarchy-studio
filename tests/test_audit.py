"""Cross-component gaps, found by auditing the four components against each other.

Each component's own suite tests it against its own assumptions. These are the cases
that only exist in the seams: what the editor writes versus what the compiler reads,
what `render.build_graph` does with a bundle none of the unit fixtures produce, and the
production argv path that the graph-level tests reach around.

The property tests here are cheap. The end-to-end renders are deliberately tiny -- the
question is whether the stack survives the shape of the input, not how it looks, and
`test_parity.py` is where appearance is measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import synthetic

from omarchy_studio import layers as layers_mod, probe, qmlbridge, render
from omarchy_studio.geometry import Canvas
from omarchy_studio.project import Bundle, Layer
from omarchy_studio.timebase import FrameRange

needs_ffmpeg = pytest.mark.skipif(
    not synthetic.have_ffmpeg(), reason="ffmpeg not installed"
)


def _tiny(tmp_path: Path, name: str, **kw) -> Bundle:
    """A bundle small enough that a full render is a test-suite-sized cost.

    The media is always built: `render.build_graph` probes the container for an exact
    frame count -- gsr writes VFR and duration*fps disagrees with the packet count in
    practice -- so there is no such thing as planning a render against a bundle whose
    master is absent.
    """
    kw.setdefault("seconds", 1.0)
    kw.setdefault("width", 192)
    kw.setdefault("height", 108)
    return Bundle(synthetic.make_bundle(tmp_path / name, **kw))


# --- a bundle with no camera at all -----------------------------------------


def test_a_camera_less_bundle_builds_a_graph_that_never_mentions_a_camera(tmp_path):
    """`capture.camera is None` is a different case from a burned-in or disabled camera,
    and it is the one no fixture in the other suites produces. The webcam settings are
    still enabled by default, so the guard that has to hold is 'no camera stream, no
    webcam layer' rather than 'the user turned it off'."""
    b = _tiny(tmp_path, "rec", camera=False)
    assert b.capture.camera is None
    assert b.edit.webcam.enabled is True

    plan = render.build_graph(b)
    assert plan.inputs.count("-i") == 1, plan.inputs
    assert "webcam" not in plan.graph
    assert "[cam" not in plan.graph


def test_an_explicit_webcam_layer_is_dropped_when_there_is_no_camera(tmp_path):
    """A project authored on a recording that had a camera, opened against one that did
    not -- or a bundle whose camera media was deleted. The webcam layer asks the input
    registry for a stream nobody bound, and an unhandled LayerError there is an export
    that dies on a project the user can still perfectly well render."""
    b = _tiny(tmp_path, "rec", camera=False)
    b.edit.layers.append(layers_mod.webcam_layer(b.edit.webcam))
    b.edit.layers.append(
        Layer(id="s1", type="shape", x=0.1, y=0.1, w=0.2, h=0.2, z=1,
              props={"color": "#ffffff"})
    )
    plan = render.build_graph(b)
    assert "webcam" not in plan.graph
    assert "[s1_o]" in plan.graph, "the surviving layers must still compile"


def test_the_editor_opens_a_camera_less_bundle_without_a_webcam(tmp_path):
    # media=False on purpose: the editor has to open a bundle whose master is still
    # being written, or is on another machine.
    b = _tiny(tmp_path, "rec", camera=False, media=False)
    state = qmlbridge.project_state(b)
    assert state["media"]["camera"] is None
    assert state["webcam"]["editable"] is False
    assert state["webcam"]["disabled_reason"]


@needs_ffmpeg
def test_a_camera_less_bundle_renders_end_to_end(tmp_path):
    b = _tiny(tmp_path, "rec", camera=False)
    out = render.render(b, tmp_path / "out.mp4")
    assert probe.frame_count(out) == b.source_frames()


# --- cuts at the very edges of the timeline ---------------------------------


@needs_ffmpeg
@pytest.mark.parametrize(
    "cuts",
    [
        [FrameRange(0, 6)],
        [FrameRange(24, 30)],
        [FrameRange(0, 4), FrameRange(26, 30)],
    ],
    ids=["head", "tail", "both-edges"],
)
def test_a_cut_on_the_first_or_last_frame_renders_the_whole_stack(tmp_path, cuts):
    """cuts.py proves the right frames survive; this proves the rest of the stack copes.

    An edge cut is where the remapping arithmetic has no slack: a layer whose range
    starts at source frame 0 lands at output frame 0 only if the head cut was subtracted,
    a click inside the head cut has to be dropped rather than remapped to a negative
    frame, and the camera has to be trimmed by the same segments as the base or it drifts
    by exactly the cut length.
    """
    b = _tiny(tmp_path, "rec", seconds=1.0)
    total = b.source_frames()
    assert total == 30, total
    b.edit.cuts = cuts
    b.edit.zoom.enabled = True
    b.edit.layers.append(
        Layer(id="cap", type="shape", x=0.1, y=0.1, w=0.3, h=0.3,
              t=FrameRange(0, total), props={"color": "#ffffff"})
    )
    b.save_edit()

    cutmap = render.effective_cutmap(b)
    plan = render.build_graph(b)
    out = render.render(b, tmp_path / "out.mp4")
    assert probe.frame_count(out) == cutmap.output_frames
    assert plan.total_frames == cutmap.output_frames

    # The trims name SOURCE frames -- the kept segments -- and the camera rides on
    # exactly the same ones, which is the property an uncut camera breaks by drifting
    # the whole cut duration.
    for k in cutmap.kept:
        trim = f"trim=start_frame={k.start}:end_frame={k.end}"
        assert plan.graph.count(trim) == 2, (
            f"{trim} appears {plan.graph.count(trim)} times; the base and the camera "
            "must both be cut by it"
        )

    # The layer spans the whole recording, so its gate must open at output frame 0 and
    # close at the output length -- never at a source index the output no longer has.
    assert f"gte(n,0)*lt(n,{cutmap.output_frames})" in plan.graph


@needs_ffmpeg
def test_a_click_inside_a_head_cut_is_dropped_rather_than_remapped(tmp_path):
    """`CutMap.to_output` returns None inside a cut. Treating that as 0 would put a zoom
    on the first frame of the edited video aimed at something the viewer never sees."""
    b = _tiny(tmp_path, "rec", clicks=(0.05, 0.9))
    b.edit.cuts = [FrameRange(0, 10)]
    b.edit.zoom.enabled = True
    b.edit.zoom.hold_frames = 6
    b.edit.zoom.ease_frames = 3
    b.save_edit()
    segments = render._segments(b, render.effective_cutmap(b))
    assert len(segments) == 1, "the click inside the head cut should have gone"
    assert segments[0].t.start > 0


# --- the >100-click expression through the production argv path -------------


@needs_ffmpeg
def test_a_hundred_plus_click_project_renders_through_the_real_plan(tmp_path):
    """The balanced-tree and graph-file constraints, together, on the path that ships.

    test_zoom proves the expression parses when handed to a bare `run_graph`. What is not
    otherwise covered is `RenderPlan.argv`: a graph this size is far past argv's ~288 KB
    E2BIG limit, ffmpeg 9.0.1 has removed `-filter_complex_script`, and the only thing
    standing between this project and that failure is the leading slash in
    `-/filter_complex`. The canvas is deliberately tiny -- `perspective` is 8.4 ms/frame
    at 1440p and this render is 480 frames.
    """
    root = tmp_path / "rec"
    b = Bundle(synthetic.make_bundle(
        root, seconds=16.0, width=96, height=54,
        clicks=tuple(0.2 + 0.13 * i for i in range(120)),
    ))
    b.edit.zoom.enabled = True
    b.edit.zoom.amount = 1.5
    b.edit.zoom.hold_frames = 2
    b.edit.zoom.ease_frames = 1
    b.edit.zoom.merge_gap_frames = 0
    b.save_edit()

    segments = render._segments(b, render.effective_cutmap(b))
    assert len(segments) >= 100, f"only {len(segments)} segments; the test lost its teeth"
    plan = render.build_graph(b)
    assert len(plan.graph) > 288_000, (
        f"graph is only {len(plan.graph)} bytes; it no longer exceeds argv's limit and "
        "this test would pass even if the graph were passed inline"
    )
    out = render.render(b, tmp_path / "out.mp4")
    assert probe.frame_count(out) == plan.total_frames


def test_the_graph_is_never_passed_inline(tmp_path):
    """The one-line guard for the same constraint, with no render behind it: whatever
    else changes, the graph must reach ffmpeg as a file reference."""
    b = _tiny(tmp_path, "rec")
    argv = render.build_graph(b).argv(Path("/tmp/g.txt"), Path("/tmp/o.mp4"))
    assert "-/filter_complex" in argv
    assert "-filter_complex" not in argv
    assert "-filter_complex_script" not in argv
    assert argv[argv.index("-/filter_complex") + 1] == "/tmp/g.txt"


# --- unknown and deferred layer types ---------------------------------------


def test_an_unknown_layer_type_is_skipped_by_both_engines(tmp_path):
    """Forward compatibility is a stated property of the format. The export warns and
    drops it; the preview must keep it, marked, so the user is told an annotation is
    there rather than silently losing it on the next save."""
    b = _tiny(tmp_path, "rec")
    b.edit.layers.append(Layer(id="x1", type="hologram", x=0.1, y=0.1, w=0.2, h=0.2))
    b.edit.layers.append(
        Layer(id="s1", type="shape", x=0.5, y=0.5, w=0.2, h=0.2, z=2,
              props={"color": "#ffffff"})
    )

    with pytest.warns(layers_mod.UnsupportedLayer, match="hologram"):
        plan = render.build_graph(b)
    assert "hologram" not in plan.graph
    assert "[s1_o]" in plan.graph

    d = qmlbridge.resolve_layer(b.edit.layers[0], b.canvas, b)
    assert d["type"] == "hologram" and d["supported"] is False
    assert json.loads(json.dumps(b.edit.to_dict()))["layers"][0]["type"] == "hologram"


def test_an_unknown_layer_type_never_breaks_the_chain(tmp_path):
    """`compile_layer` returning None means 'carry your label forward'. A caller that
    took the fragment's label anyway would dangle a reference to a label nothing
    produced, and ffmpeg would reject the whole graph -- so the failure of the skip is
    not a missing overlay, it is an export that will not start."""
    b = _tiny(tmp_path, "rec")
    b.edit.layers = [
        Layer(id="a", type="shape", x=0.1, y=0.1, w=0.2, h=0.2, z=1, props={"color": "#fff"}),
        Layer(id="b", type="hologram", x=0.2, y=0.2, w=0.2, h=0.2, z=2),
        Layer(id="c", type="shape", x=0.3, y=0.3, w=0.2, h=0.2, z=3, props={"color": "#fff"}),
    ]
    with pytest.warns(layers_mod.UnsupportedLayer):
        graph = render.build_graph(b).graph
    # c reads a's output, because b contributed nothing at all.
    assert "[a_o][c_s]overlay" in graph or "[a_o]" in graph.split("[c_")[0]
    assert "[b_o]" not in graph


@needs_ffmpeg
def test_a_project_of_nothing_but_unknown_layers_still_renders(tmp_path):
    b = _tiny(tmp_path, "rec")
    b.edit.layers = [Layer(id="x1", type="hologram", x=0.1, y=0.1, w=0.2, h=0.2)]
    b.save_edit()
    with pytest.warns(layers_mod.UnsupportedLayer):
        out = render.render(b, tmp_path / "out.mp4")
    assert probe.frame_count(out) == b.source_frames()


# --- regressions for the seams the parity harness found ---------------------


def test_an_image_layers_asset_is_resolved_against_the_bundle(tmp_path):
    """The editor stores a bare filename so the bundle stays portable. Handed straight
    to `-i`, ffmpeg resolved it against its own working directory and every image layer
    added through the editor died with 'No such file or directory'."""
    b = _tiny(tmp_path, "rec")
    src = tmp_path / "logo.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    qmlbridge.apply_op(b, "add_image", {"path": str(src)})
    assert b.edit.layers[0].props == {"asset": "logo.png"}

    plan = render.build_graph(b)
    assert str(b.assets_dir / "logo.png") in plan.inputs
    assert "logo.png" not in [a for a in plan.inputs if not a.startswith("/")]
    # And the portable name is what stays on disk.
    b.save_edit()
    assert json.loads(b.edit_path.read_text())["layers"][0]["props"] == {"asset": "logo.png"}


def test_an_explicit_image_path_still_wins_over_an_asset_name(tmp_path):
    b = _tiny(tmp_path, "rec")
    b.edit.layers.append(
        Layer(id="i1", type="image", x=0.1, y=0.1, w=0.2, h=0.2,
              props={"path": "/somewhere/else.png", "asset": "logo.png"})
    )
    assert "/somewhere/else.png" in render.build_graph(b).inputs


@pytest.mark.parametrize("block", [None, 0.012, 0.05, 24, 40])
def test_the_pixelate_block_is_the_same_size_in_both_engines(tmp_path, block):
    """The editor writes `block` normalized to the canvas; the compiler read it as
    pixels, `int()`ed 0.012 to 0 and clamped to a 2 px mosaic. A redaction that previews
    unreadable and exports legible is the worst failure this project can have, and it is
    invisible in the filtergraph -- `pixelize=w=2:h=2` looks perfectly well formed."""
    b = _tiny(tmp_path, "rec", width=640, height=360)
    props = {} if block is None else {"block": block}
    b.edit.layers.append(
        Layer(id="p1", type="pixelate", x=0.2, y=0.2, w=0.3, h=0.3, props=props)
    )
    preview = qmlbridge.resolve_layer(b.edit.layers[0], b.canvas, b)["pixelate"]["block"]
    graph = render.build_graph(b).graph
    exported = int(graph.split("pixelize=w=")[1].split(":")[0])
    assert exported == round(preview), f"preview {preview}, export {exported}"
    assert exported >= 2


def test_a_normalized_pixelate_block_scales_with_the_canvas(tmp_path):
    """The point of normalizing: the same project must hide the same words whether it is
    rendered against the 1080p proxy or the master."""
    small = layers_mod.block_px(0.012, Canvas(1280, 720))
    large = layers_mod.block_px(0.012, Canvas(2560, 1440))
    assert large == pytest.approx(2 * small)
    # An honest pixel value is left alone, so an explicitly sized redaction is stable.
    assert layers_mod.block_px(24, Canvas(1280, 720)) == 24


def test_the_caption_box_the_preview_draws_is_the_one_drawtext_draws(tmp_path):
    """`box_color` carries its own alpha; there is no separate `box_opacity` property.
    The preview invented one, defaulting to a #101820 box at 0.85 -- so every caption
    previewed on a dark plate and exported with none."""
    b = _tiny(tmp_path, "rec", width=640, height=360)
    b.edit.layers.append(
        Layer(id="t1", type="text", x=0.1, y=0.1, w=0.4, h=0.1, props={"text": "hi"})
    )
    d = qmlbridge.resolve_layer(b.edit.layers[0], b.canvas, b)["text"]
    assert d["box_opacity"] == 0.0, "the export's default box is fully transparent"

    b.edit.layers[0].props["box_color"] = "#101820@0.85"
    d = qmlbridge.resolve_layer(b.edit.layers[0], b.canvas, b)["text"]
    assert (d["box_color"], d["box_opacity"]) == ("#101820", 0.85)
    assert ")*0.850000'" in render.build_graph(b).graph


def test_the_caption_font_is_sized_off_the_tile_in_both_engines(tmp_path):
    """drawtext sizes from the tile (0.6 of its height); the preview sized from the
    canvas height, which on a short caption box disagreed by more than 2x."""
    b = _tiny(tmp_path, "rec", width=640, height=360)
    lay = Layer(id="t1", type="text", x=0.1, y=0.1, w=0.4, h=0.25, props={"text": "hi"})
    b.edit.layers.append(lay)
    preview = qmlbridge.resolve_layer(lay, b.canvas, b)["text"]["pixelSize"]
    exported = int(render.build_graph(b).graph.split("fontsize=")[1].split(":")[0])
    assert preview == exported

    lay.props["font_px"] = 44
    assert qmlbridge.resolve_layer(lay, b.canvas, b)["text"]["pixelSize"] == 44
    assert "fontsize=44:" in render.build_graph(b).graph


def test_a_corner_radius_is_measured_against_the_tile_in_both_engines(tmp_path):
    """The preview normalized the radius against the CANVAS WIDTH and the export against
    the tile's short side. On a small shape 0.2 rounded to a lozenge in one and stayed
    all but square in the other."""
    b = _tiny(tmp_path, "rec", width=640, height=360)
    lay = Layer(id="s1", type="shape", x=0.1, y=0.1, w=0.2, h=0.1,
                props={"color": "#ff3b30@0.5", "radius": 0.2})
    b.edit.layers.append(lay)
    d = qmlbridge.resolve_layer(lay, b.canvas, b)["shape"]
    rect = lay.placement.resolve(b.canvas)
    assert d["radius"] == pytest.approx(layers_mod.radius_px(0.2, rect))
    assert d["radius"] == pytest.approx(0.2 * min(rect.w, rect.h))
    # And the alpha reaches QML as a number rather than inside an unparseable colour.
    assert (d["color"], d["opacity"]) == ("#ff3b30", 0.5)
    assert f"{layers_mod.radius_px(0.2, rect.to_even()):.4f}" in render.build_graph(b).graph
