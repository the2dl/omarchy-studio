"""The backdrop library, its compatibility rules, and the filtergraph it produces.

The `gradients` filter has two traps that only show up in rendered pixels -- it rotates
its own gradient unless told not to, and it silently substitutes a RANDOM line for any
endpoint outside `[0, size-1]` -- so the graph assertions here are about specific
argument values rather than "a gradients filter is present". Both traps produce a
perfectly valid file with the wrong picture in it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import synthetic

from omarchy_studio import backgrounds, qmlbridge, render
from omarchy_studio.geometry import Canvas
from omarchy_studio.project import Bundle, BackdropSettings, Edit

RATE = "30/1"
# 16:9, 4:3, portrait and square: the padded frame a gradient is generated on is
# aspect-dependent, and the endpoint clamp has to hold for all of them.
SHAPES = [(1920, 1080), (1280, 720), (640, 480), (1080, 1920), (800, 800)]


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    """No media -- nothing here needs a frame count."""
    return Bundle(synthetic.make_bundle(tmp_path / "rec", media=False))


# --- the catalogue ----------------------------------------------------------


def test_every_id_is_unique_and_none_of_them_is_the_custom_sentinel():
    ids = [b.id for b in backgrounds.CATALOG]
    assert len(ids) == len(set(ids))
    assert backgrounds.CUSTOM not in ids
    names = [b.name for b in backgrounds.CATALOG]
    assert len(names) == len(set(names)), "two swatches with the same label"


def test_the_library_has_both_halves_and_they_are_worth_showing():
    solids = [b for b in backgrounds.CATALOG if not b.is_gradient]
    gradients = [b for b in backgrounds.CATALOG if b.is_gradient]
    assert len(solids) >= 8 and len(gradients) >= 8
    # Solids first, then gradients: `catalog()` is the display order of the swatch grid
    # and a grid that interleaves the two reads as unsorted.
    kinds = [b.kind for b in backgrounds.CATALOG]
    assert kinds == sorted(kinds, key=lambda k: k != backgrounds.SOLID)


def test_every_colour_in_the_library_is_a_hex_colour_ffmpeg_will_take():
    for b in backgrounds.CATALOG:
        for c in b.colors:
            assert backgrounds.is_color(c), f"{b.id} has {c!r}"


def test_solids_carry_one_colour_and_gradients_carry_a_ramp():
    for b in backgrounds.CATALOG:
        if b.is_gradient:
            # The upper bound is `gradients`' own: it exposes c0..c7 and refuses
            # nb_colors outside 2..8.
            assert 2 <= len(b.colors) <= backgrounds.MAX_STOPS, b.id
            assert 0.0 <= b.angle < 360.0, b.id
        else:
            assert len(b.colors) == 1, b.id


def test_the_catalogue_serializes_to_what_a_swatch_grid_needs():
    entry = backgrounds.catalog()[0]
    assert set(entry) == {"id", "name", "kind", "colors", "angle"}
    json.dumps(backgrounds.catalog())  # it crosses an HTTP boundary


def test_find_returns_the_entry_or_nothing():
    assert backgrounds.find("canvas").name == "Canvas"
    assert backgrounds.find("no-such-ground") is None
    assert backgrounds.find(backgrounds.CUSTOM) is None


# --- resolution -------------------------------------------------------------


def test_an_id_resolves_to_that_entry_verbatim():
    bd = BackdropSettings(background="terracotta", color="#ff00ff", gradient="#00ff00")
    got = backgrounds.resolve(bd, Canvas(1920, 1080))
    assert got is backgrounds.find("terracotta")
    assert got.colors == ("#3d241a", "#171113"), "the custom fields outranked the swatch"


def test_the_custom_sentinel_resolves_to_the_users_own_colour():
    solid = backgrounds.resolve(BackdropSettings(color="#123456"), Canvas(1920, 1080))
    assert solid.kind == backgrounds.SOLID and solid.colors == ("#123456",)

    grad = backgrounds.resolve(
        BackdropSettings(color="#123456", gradient="#654321"), Canvas(1920, 1080)
    )
    assert grad.kind == backgrounds.GRADIENT
    assert grad.colors == ("#123456", "#654321")


def test_a_custom_gradient_still_runs_corner_to_corner():
    """It predates the angle field and drew `x0=0:y0=0:x1=W:y1=H`. Reproduced per canvas
    rather than frozen at 135 degrees, which would rotate every backdrop already sitting
    in someone's edit.json by 15.6 degrees at 16:9."""
    bd = BackdropSettings(color="#101010", gradient="#e0e0e0")
    for w, h in SHAPES:
        angle = backgrounds.resolve(bd, Canvas(w, h)).angle
        assert backgrounds.gradient_line(angle, w, h) == pytest.approx((0, 0, w, h))


# --- old projects -----------------------------------------------------------


def test_an_edit_json_written_before_the_library_loads_unchanged():
    old = {
        "version": 1,
        "backdrop": {"enabled": True, "color": "#101010", "gradient": "#2b3040",
                     "padding": 0.06, "corner_radius": 0.03, "shadow": False},
    }
    bd = Edit.from_dict(old).backdrop
    assert bd.background == backgrounds.CUSTOM, "a swatch would outrank the stored colour"
    assert (bd.color, bd.gradient) == ("#101010", "#2b3040")
    assert (bd.padding, bd.corner_radius, bd.shadow) == (0.06, 0.03, False)


def test_the_headless_editors_solid_form_survives():
    """`--backdrop=color:#101010` writes an EMPTY gradient, not a missing one."""
    bd = Edit.from_dict({"backdrop": {"color": "#101010", "gradient": ""}}).backdrop
    assert bd.background == backgrounds.CUSTOM
    assert not backgrounds.resolve(bd, Canvas(1280, 720)).is_gradient


def test_an_id_from_a_newer_build_degrades_instead_of_refusing_to_open():
    """Forward compatibility is a stated property of the format: a project loses the
    ground it asked for, it does not fail to load."""
    bd = Edit.from_dict({"backdrop": {"background": "hologram", "color": "#101010"}}).backdrop
    assert bd.background == backgrounds.CUSTOM
    assert backgrounds.resolve(bd, Canvas(1280, 720)).colors == ("#101010",)


def test_a_chosen_swatch_survives_the_undo_round_trip():
    """Undo restores whole-Edit snapshots through to_dict/from_dict, so anything that
    does not survive that pair silently reverts on the first Ctrl+Z."""
    e = Edit()
    e.backdrop.background = "moss"
    assert Edit.from_dict(e.to_dict()).backdrop.background == "moss"


# --- gradient geometry ------------------------------------------------------


def test_the_gradient_line_is_the_css_construction():
    # 180 degrees is straight down the middle, top to bottom, like CSS.
    assert backgrounds.gradient_line(180, 400, 200) == pytest.approx((200, 0, 200, 200))
    # 90 degrees is left to right.
    assert backgrounds.gradient_line(90, 400, 200) == pytest.approx((0, 100, 400, 100))
    # 0 degrees points up, so the line runs bottom to top.
    assert backgrounds.gradient_line(0, 400, 200) == pytest.approx((200, 200, 200, 0))


def test_no_planned_endpoint_can_be_replaced_by_a_random_one():
    """`gradients` tests each endpoint with `v < 0 || v >= size` and substitutes a
    random value when it matches, so an out-of-frame line does not fail -- it renders a
    different gradient on every export. Six runs of the shipped `x1=W:y1=H` gave six
    different frames; the same command with `W-1`/`H-1` gave one.
    """
    for w, h in SHAPES:
        for angle in range(0, 360, 5):
            p = backgrounds.gradient_plan(angle, w, h)
            for v, size in ((p.x0, p.width), (p.x1, p.width),
                            (p.y0, p.height), (p.y1, p.height)):
                assert 0 <= v < size, f"{angle} deg on {w}x{h}: {v} outside 0..{size - 1}"


def test_the_crop_always_lands_inside_the_generated_frame():
    for w, h in SHAPES:
        for angle in range(0, 360, 5):
            p = backgrounds.gradient_plan(angle, w, h)
            crop = p.crop or (w, h, 0, 0)
            assert crop[0] == w and crop[1] == h
            assert crop[2] + w <= p.width and crop[3] + h <= p.height


def test_the_box_diagonal_is_the_one_angle_that_needs_no_room():
    """It is also the case the old custom gradient hit, so the compatibility path is the
    cheapest one rather than the most padded."""
    p = backgrounds.gradient_plan(backgrounds.diagonal_angle(1920, 1080), 1920, 1080)
    assert (p.pad_x, p.pad_y) == (0, 0)
    assert (p.x0, p.y0, p.x1, p.y1) == (0, 0, 1920, 1080)
    # One pixel of growth, purely so the far endpoint is a valid index.
    assert (p.width, p.height) == (1921, 1081)


# --- the filtergraph --------------------------------------------------------


def test_a_solid_is_a_plain_colour_source_at_the_project_rate():
    chain = render._ground(backgrounds.find("ink"), 1920, 1080, RATE)
    assert chain == "color=c=0x0d0e10:s=1920x1080:r=30/1"


def test_a_gradient_names_every_stop_and_never_moves():
    """The rate is the PROJECT's: leaving a source at lavfi's default 25 silently
    resampled a 30 fps timeline and lost 8 frames of a 52-frame render."""
    chain = render._ground(backgrounds.find("nocturne"), 1920, 1080, RATE)
    assert chain.startswith("gradients=")
    assert "c0=0x1d2440" in chain and "c1=0x141726" in chain and "c2=0x0b0c12" in chain
    assert "nb_colors=3" in chain
    assert "r=30/1" in chain
    # Without this the ground rotates: 0.01 radians per FRAME by default.
    assert ":speed=0" in chain


def test_an_angled_gradient_is_generated_oversized_and_cropped_back():
    chain = render._ground(backgrounds.find("canvas"), 1920, 1080, RATE)
    p = backgrounds.gradient_plan(backgrounds.find("canvas").angle, 1920, 1080)
    assert f"s={p.width}x{p.height}" in chain
    assert chain.endswith(f",crop=1920:1080:{p.pad_x}:{p.pad_y}")
    assert p.height > 1080, "150 degrees is near the worst case for vertical extent"


def test_more_stops_than_gradients_can_hold_is_refused():
    too_many = backgrounds.Background(
        "wide", "Wide", backgrounds.GRADIENT, tuple(["#101010"] * 9), 90
    )
    with pytest.raises(render.RenderError):
        render._ground(too_many, 640, 480, RATE)


def _backdrop_graph(bd: BackdropSettings, canvas: Canvas) -> str:
    g = render._Graph()
    render._backdrop(g, "[base]", canvas, _Tb(), bd)
    return g.text()


class _Tb:
    fps_num, fps_den = 30, 1


def test_the_gradient_ground_keeps_the_padding_radius_and_shadow():
    """The ground is one stage of the backdrop, not the whole of it: swapping a colour
    for a gradient must not disturb the inset, the rounded mask or the drop shadow."""
    bd = BackdropSettings(enabled=True, background="dusk", padding=0.05,
                          corner_radius=0.02, shadow=True)
    graph = _backdrop_graph(bd, Canvas(1920, 1080))
    assert "gradients=" in graph
    assert "geq=lum=" in graph, "the rounded-corner mask went missing"
    assert "gblur=" in graph, "the drop shadow went missing"
    # 0.05 * min(1920,1080) = 54 px a side, rounded to an even inset.
    assert "scale=1812:972" in graph


def test_a_gradient_reaches_the_overlay_as_the_infinite_main_input():
    """A source used as the overlay MAIN input drags its own timebase into the output --
    a 6.9 s clip once came out 208 seconds long -- so the real video reaches it through
    `shortest=1` and not the other way round."""
    graph = _backdrop_graph(
        BackdropSettings(enabled=True, background="harbour"), Canvas(1280, 720)
    )
    # The content reaches the overlay through a PTS normalisation now (see
    # render._backdrop): concat and tpad each leave the last frame's timing slightly
    # off and the overlay's shortest=1 then ended a frame early. The claim under test
    # is unchanged -- the gradient is the overlay's main input, ahead of the content.
    assert graph.index("gradients=") < graph.index("]overlay=")
    assert "[bg][content_cfr]overlay=" in graph
    assert "shortest=1" in graph


# --- the bridge -------------------------------------------------------------


def test_the_bridge_serves_the_whole_library_with_the_sentinel_named(bundle):
    cat = qmlbridge.background_catalog()
    assert cat["custom"] == backgrounds.CUSTOM
    assert [e["id"] for e in cat["entries"]] == [b.id for b in backgrounds.CATALOG]


def test_selecting_a_swatch_by_id(bundle):
    qmlbridge.apply_op(bundle, "set_backdrop", {"enabled": True, "background": "pine"})
    assert bundle.edit.backdrop.background == "pine"
    assert qmlbridge.resolve_backdrop(bundle)["color"] == "#14211c"


def test_an_unknown_id_is_refused(bundle):
    with pytest.raises(qmlbridge.BridgeError) as e:
        qmlbridge.apply_op(bundle, "set_backdrop", {"background": "chartreuse"})
    assert "chartreuse" in str(e.value)
    assert bundle.edit.backdrop.background == backgrounds.CUSTOM, "it was applied anyway"


def test_the_custom_sentinel_is_accepted_as_an_id(bundle):
    qmlbridge.apply_op(bundle, "set_backdrop", {"background": "fog"})
    qmlbridge.apply_op(bundle, "set_backdrop", {"background": backgrounds.CUSTOM})
    assert bundle.edit.backdrop.background == backgrounds.CUSTOM


def test_picking_a_colour_deselects_the_swatch(bundle):
    """Otherwise the swatch keeps outranking the colour and the picker reads as dead."""
    qmlbridge.apply_op(bundle, "set_backdrop", {"background": "abyss"})
    qmlbridge.apply_op(bundle, "set_backdrop", {"color": "#334455"})
    assert bundle.edit.backdrop.background == backgrounds.CUSTOM
    assert bundle.edit.backdrop.color == "#334455"


def test_a_swatch_sent_alongside_a_colour_still_wins(bundle):
    qmlbridge.apply_op(bundle, "set_backdrop", {"color": "#334455", "background": "clay"})
    assert bundle.edit.backdrop.background == "clay"


def test_a_colour_the_renderer_could_not_use_is_refused(bundle):
    for bad in ("rebeccapurple", "#fff", "0x112233", "#11223344", ""):
        with pytest.raises(qmlbridge.BridgeError):
            qmlbridge.apply_op(bundle, "set_backdrop", {"color": bad})
    assert bundle.edit.backdrop.color == "#1b1d24"


def test_clearing_the_second_stop_makes_the_custom_ground_solid(bundle):
    qmlbridge.apply_op(bundle, "set_backdrop", {"gradient": "#404040"})
    assert bundle.edit.backdrop.gradient == "#404040"
    qmlbridge.apply_op(bundle, "set_backdrop", {"gradient": None})
    assert bundle.edit.backdrop.gradient is None
    assert qmlbridge.resolve_backdrop(bundle)["gradient"] is None


def test_the_resolved_backdrop_reports_the_ground_that_will_be_rendered(bundle):
    """`color`/`gradient` are the RESOLVED pair, so a swatch reaches the preview through
    the same two keys a custom colour always did."""
    qmlbridge.apply_op(bundle, "set_backdrop", {"enabled": True, "background": "ember"})
    d = qmlbridge.resolve_backdrop(bundle)
    assert d["background"] == "ember"
    assert (d["color"], d["gradient"]) == ("#33200f", "#14100f")
    assert d["ground"]["kind"] == backgrounds.GRADIENT
    assert d["ground"]["angle"] == 150


def test_the_preview_is_handed_the_same_gradient_line_the_export_uses(bundle):
    """A second implementation of the CSS gradient construction in JavaScript is exactly
    the drift the preview exists to rule out."""
    qmlbridge.apply_op(bundle, "set_backdrop", {"enabled": True, "background": "basalt"})
    line = qmlbridge.resolve_backdrop(bundle)["ground"]["line"]
    canvas = bundle.canvas
    assert (line["x0"], line["y0"], line["x1"], line["y1"]) == pytest.approx(
        backgrounds.gradient_line(180, canvas.width, canvas.height)
    )
