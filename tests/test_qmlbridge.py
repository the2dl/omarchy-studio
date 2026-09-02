"""The bridge is the only place placement maths happens, so these tests are mostly
"does the resolved value equal what geometry.py says", plus the inverse round trip that
a drag depends on.

The HTTP tests run against a real loopback server because the editor's whole model layer
is that server: a unit test of apply_op that never goes through a socket would not catch
a route that returns 404 or a token check that rejects every request.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import synthetic

from omarchy_studio import qmlbridge
from omarchy_studio.geometry import Canvas, Placement, Zoom
from omarchy_studio.project import Bundle, Layer
from omarchy_studio.timebase import FrameRange

REPO = Path(__file__).resolve().parents[1]
needs_ffmpeg = pytest.mark.skipif(not synthetic.have_ffmpeg(), reason="ffmpeg/ffprobe not installed")


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    """No media: every path here that needs a frame count degrades to 0 on purpose."""
    return Bundle(synthetic.make_bundle(tmp_path / "rec", media=False))


@pytest.fixture
def media_bundle(tmp_path: Path) -> Bundle:
    return Bundle(synthetic.make_bundle(tmp_path / "rec", seconds=2.0))


# --- resolution --------------------------------------------------------------


def test_resolved_placement_is_geometry_verbatim(bundle):
    canvas = bundle.canvas
    p = Placement(0.25, 0.5, 0.2, 0.3)
    assert qmlbridge.resolve_placement(p, canvas)["rect"] == p.to_qml(canvas)


def test_placement_round_trips_through_pixels(bundle):
    canvas = bundle.canvas
    for anchor in ("top-left", "center"):
        p = Placement(0.31, 0.42, 0.25, 0.18, anchor)
        r = p.resolve(canvas)
        back = qmlbridge.placement_from_rect(r.x, r.y, r.w, r.h, canvas, anchor)
        assert back.x == pytest.approx(p.x)
        assert back.y == pytest.approx(p.y)
        assert back.w == pytest.approx(p.w)
        assert back.h == pytest.approx(p.h)
        assert back.anchor == anchor


def test_drag_off_canvas_is_clamped_not_rejected(bundle):
    canvas = bundle.canvas
    p = qmlbridge.placement_from_rect(-500, -400, 300, 200, canvas)
    assert p.x == 0.0 and p.y == 0.0
    r = p.resolve(canvas)
    assert r.x == 0 and r.y == 0


def test_resolved_zoom_matches_geometry_and_carries_origin(bundle):
    canvas = bundle.canvas
    z = Zoom(2.0, 0.3, 0.7)
    d = qmlbridge.resolve_zoom(z, canvas)
    expect = z.to_qml(canvas)
    assert (d["scale"], d["x"], d["y"]) == (expect["scale"], expect["x"], expect["y"])
    assert d["transformOrigin"] == "TopLeft"


def test_blur_layer_resolves_to_multieffect_properties(bundle):
    from omarchy_studio.geometry import qml_blur

    bundle.edit.layers.append(Layer(id="b1", type="blur", x=0.1, y=0.1, w=0.2, h=0.2, props={"strength": 0.4}))
    d = qmlbridge.resolve_layer(bundle.edit.layers[-1], bundle.canvas, bundle)
    assert d["blur"] == qml_blur(0.4)
    assert d["rect"]["width"] == pytest.approx(0.2 * bundle.canvas.width)


def test_text_layer_is_centre_anchored(bundle):
    """Left-anchored text drifted ~7% of the string width between the two engines; the
    bridge must hand QML a centre, not a corner."""
    lay = Layer(id="t1", type="text", x=0.2, y=0.4, w=0.4, h=0.1, props={"text": "hi"})
    d = qmlbridge.resolve_layer(lay, bundle.canvas, bundle)
    r = lay.placement.resolve(bundle.canvas)
    assert d["text"]["cx"] == pytest.approx(r.x + r.w / 2)
    assert d["text"]["cy"] == pytest.approx(r.y + r.h / 2)
    assert d["text"]["font_file"] == qmlbridge.FONT_FILE


def test_unknown_layer_type_is_marked_not_dropped(bundle):
    lay = Layer(id="x1", type="hologram", x=0, y=0, w=0.1, h=0.1)
    d = qmlbridge.resolve_layer(lay, bundle.canvas, bundle)
    assert d["supported"] is False and d["type"] == "hologram"


# --- webcam ------------------------------------------------------------------


def test_burned_in_webcam_is_disabled_with_a_reason(tmp_path):
    b = Bundle(synthetic.make_bundle(tmp_path / "rec", burned_in=True, media=False))
    w = qmlbridge.resolve_webcam(b)
    assert w["editable"] is False
    assert "burned" in w["disabled_reason"]
    with pytest.raises(qmlbridge.BridgeError):
        qmlbridge.apply_op(b, "set_webcam", {"rect": {"x": 0, "y": 0, "width": 10, "height": 10}})


def test_webcam_without_camera_stream_is_disabled(tmp_path):
    b = Bundle(synthetic.make_bundle(tmp_path / "rec", camera=False, media=False))
    assert qmlbridge.resolve_webcam(b)["editable"] is False


def test_webcam_radius_only_applies_to_the_rounded_shape(bundle):
    """A circle is the ellipse inscribed in the tile (layers._circle_mask), so it has no
    radius at all: sending min(w,h)/2 would draw a stadium in the preview and an ellipse
    in the export the moment the box is not square -- which the 0.22x0.22 default on a
    16:9 canvas already is."""
    bundle.edit.webcam.w, bundle.edit.webcam.h = 0.3, 0.1
    assert qmlbridge.resolve_webcam(bundle)["radius"] == 0.0
    bundle.edit.webcam.shape = "rounded"
    bundle.edit.webcam.corner_radius = 0.25
    w = qmlbridge.resolve_webcam(bundle)
    # Normalized to the SHORT side, the same normalization layers._radius_px uses.
    assert w["radius"] == pytest.approx(0.25 * min(w["rect"]["width"], w["rect"]["height"]))


# --- events ------------------------------------------------------------------


def test_clicks_land_on_frames_and_normalized_coordinates(bundle):
    clicks = qmlbridge.click_events(bundle)
    assert [c["frame"] for c in clicks] == [18, 42, 45, 66]  # 30fps, anchored at frame 0
    # Logical 300,180 minus the region origin 200,100 is 100,80 logical = 200,160
    # physical on a scale-2 display, over a 1280x720 canvas.
    assert clicks[0]["cx"] == pytest.approx(200 / 1280)
    assert clicks[0]["cy"] == pytest.approx(160 / 720)


def test_clicks_before_frame_zero_clamp_to_it(tmp_path):
    """events.map_clicks clamps rather than drops, and the timeline shows what it is
    given: the recorder arms its binds before the encoder produces its first frame, so an
    early click is a real click on content the video does start with."""
    root = synthetic.make_bundle(tmp_path / "rec", media=False, clicks=())
    p = root / "events" / "input.jsonl"
    p.write_text(
        json.dumps({"t_us": int(synthetic.ANCHOR_US - 500_000), "type": "click",
                    "button": "left", "x": 300, "y": 180})
        + "\n"
    )
    assert [c["frame"] for c in qmlbridge.click_events(Bundle(root))] == [0]


def test_truncated_event_log_does_not_raise(tmp_path):
    root = synthetic.make_bundle(tmp_path / "rec", media=False)
    p = root / "events" / "input.jsonl"
    p.write_text(p.read_text() + '{"t_us": 1000000, "type": "cli')
    assert len(qmlbridge.click_events(Bundle(root))) == 4


def test_nearby_clicks_merge_into_one_zoom_gesture(media_bundle):
    """The preview's gestures come from zoom.zoom_segments -- the function the
    filtergraph is generated from -- so a merge rule that changes there changes here."""
    from omarchy_studio import zoom as zoom_mod

    media_bundle.edit.zoom.enabled = True
    media_bundle.edit.zoom.merge_gap_frames = 90
    segs = zoom_mod.zoom_segments(
        qmlbridge._events.map_clicks(
            qmlbridge._events.read_clicks(media_bundle.events_dir / "input.jsonl"),
            media_bundle.capture,
            media_bundle.timebase,
        ),
        media_bundle.edit.zoom,
        media_bundle.timebase,
        media_bundle.cutmap(),
    )
    # Frames 18/42/45/66 are each within 90 of their predecessor: one gesture, else the
    # frame pumps on every double click.
    assert len(segs) == 1


def test_zoom_track_is_empty_when_zoom_is_off(bundle):
    assert qmlbridge.zoom_track(bundle)["frames"] == []


@needs_ffmpeg
def test_zoom_track_eases_from_identity_to_the_full_amount(media_bundle):
    bundle = media_bundle
    z = bundle.edit.zoom
    z.enabled, z.amount, z.ease_frames, z.hold_frames = True, 2.0, 10, 30
    tr = qmlbridge.zoom_track(bundle)
    assert tr["frames"], "zoom enabled with clicks must produce samples"
    assert max(tr["scale"]) == pytest.approx(2.0)
    assert min(tr["scale"]) < 1.2  # the ease actually starts near identity
    assert tr["frames"] == sorted(tr["frames"])
    # Every sample is Zoom.to_qml verbatim, so x/y are the viewport translation for the
    # scale and focal point at that frame rather than anything QML could round.
    i = tr["scale"].index(max(tr["scale"]))
    assert tr["x"][i] <= 0 and tr["y"][i] <= 0


# --- intents -----------------------------------------------------------------


def test_set_webcam_from_a_dragged_rect(bundle):
    canvas = bundle.canvas
    qmlbridge.apply_op(bundle, "set_webcam", {"rect": {"x": 640, "y": 360, "width": 256, "height": 144}})
    cam = bundle.edit.webcam
    assert cam.x == pytest.approx(0.5) and cam.y == pytest.approx(0.5)
    assert cam.w == pytest.approx(0.2) and cam.h == pytest.approx(0.2)


def test_zoom_amount_below_one_is_clamped_not_thrown(bundle):
    qmlbridge.apply_op(bundle, "set_zoom", {"amount": 0.4})
    assert bundle.edit.zoom.amount == 1.0
    Zoom(bundle.edit.zoom.amount)  # would raise if the clamp had not happened


def test_zoom_timings_arrive_in_ms_and_are_snapped_to_frames(bundle):
    qmlbridge.apply_op(bundle, "set_zoom", {"hold_ms": 1200, "ease_ms": 300})
    assert bundle.edit.zoom.hold_frames == 36  # 1.2s at 30fps
    assert bundle.edit.zoom.ease_frames == 9


def test_add_blur_layer_and_update_it(bundle):
    qmlbridge.apply_op(bundle, "add_blur", {"rect": {"x": 128, "y": 72, "width": 256, "height": 144}})
    lay = bundle.edit.layers[0]
    assert lay.type == "blur" and lay.x == pytest.approx(0.1)
    qmlbridge.apply_op(bundle, "update_layer", {"id": lay.id, "props": {"strength": 0.9}, "opacity": 0.5})
    assert lay.props["strength"] == 0.9 and lay.opacity == 0.5
    qmlbridge.apply_op(bundle, "delete_layer", {"id": lay.id})
    assert bundle.edit.layers == []


def test_layer_ids_do_not_collide(bundle):
    for _ in range(3):
        qmlbridge.apply_op(bundle, "add_blur", {"rect": {"x": 0, "y": 0, "width": 100, "height": 100}})
    assert len({l.id for l in bundle.edit.layers}) == 3


def test_added_image_is_copied_into_the_bundle(bundle, tmp_path):
    src = tmp_path / "logo.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    qmlbridge.apply_op(bundle, "add_image", {"path": str(src), "rect": {"x": 0, "y": 0, "width": 200, "height": 200}})
    lay = bundle.edit.layers[0]
    assert (bundle.assets_dir / lay.props["asset"]).exists()
    assert qmlbridge.resolve_layer(lay, bundle.canvas, bundle)["source"].startswith("file://")


def test_cuts_snap_to_frames_and_merge(bundle):
    qmlbridge.apply_op(bundle, "add_cut", {"start_ms": 1000, "end_ms": 2000})
    qmlbridge.apply_op(bundle, "add_cut", {"start_ms": 1900, "end_ms": 2500})
    assert bundle.edit.cuts == [FrameRange(30, 75)]
    qmlbridge.apply_op(bundle, "delete_cut", {"index": 0})
    assert bundle.edit.cuts == []


def test_zero_length_cut_becomes_one_frame(bundle):
    qmlbridge.apply_op(bundle, "add_cut", {"start_ms": 1000, "end_ms": 1000})
    assert bundle.edit.cuts == [FrameRange(30, 31)]


def test_unknown_op_is_rejected(bundle):
    with pytest.raises(qmlbridge.BridgeError):
        qmlbridge.apply_op(bundle, "rm -rf", {})


# --- the state dump ----------------------------------------------------------


def test_state_is_json_serializable_and_complete(bundle):
    s = json.loads(json.dumps(qmlbridge.project_state(bundle)))
    for key in ("canvas", "timebase", "media", "edit", "webcam", "layers", "clicks", "cuts", "proxy"):
        assert key in s
    assert s["timebase"]["ms_per_frame"] == pytest.approx(1000 / 30)
    assert s["capture"]["monitor_scale"] == 2.0


def test_state_survives_missing_media(bundle):
    s = qmlbridge.project_state(bundle)
    assert s["source_frames"] == 0
    assert s["media"]["screen"] is None  # nothing to play, and the UI can say so


@needs_ffmpeg
def test_preview_never_gets_the_master_url(media_bundle):
    s = qmlbridge.project_state(media_bundle)
    assert s["media"]["screen"]["ready"] is False
    assert s["media"]["screen"]["url"] == ""  # the 5K master is never handed to a player
    synthetic.make_proxies(media_bundle.root)
    s = qmlbridge.project_state(media_bundle)
    assert s["media"]["screen"]["ready"] is True
    assert s["media"]["screen"]["url"].endswith("proxy/screen-proxy.mp4")


@needs_ffmpeg
def test_camera_offset_is_reported_in_ms(media_bundle):
    # The fixture puts the camera 120ms after the screen: 3.6 frames at 30fps -> 4.
    assert media_bundle.camera_offset_frames() == 4
    assert qmlbridge.project_state(media_bundle)["media"]["camera_offset_ms"] == pytest.approx(400 / 3)


# --- the server --------------------------------------------------------------


@pytest.fixture
def server(bundle):
    session = qmlbridge.Session(bundle, REPO)
    srv = qmlbridge.serve(session, 0)
    yield session, srv.server_port
    srv.shutdown()


def _call(port: int, path: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-Studio-Token": token, "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_server_serves_state_and_applies_ops(server):
    session, port = server
    s = _call(port, "/state", session.token)
    assert s["canvas"]["width"] == 1280 and s["dirty"] is False
    s = _call(port, "/op", session.token, {"op": "set_webcam", "args": {"shape": "rect"}})
    assert s["webcam"]["shape"] == "rect" and s["dirty"] is True
    assert not session.bundle.edit_path.exists()  # an op must not write the project
    s = _call(port, "/save", session.token, {})
    assert s["dirty"] is False and session.bundle.edit_path.exists()
    assert json.loads(session.bundle.edit_path.read_text())["webcam"]["shape"] == "rect"


def test_server_rejects_a_bad_token(server):
    _, port = server
    with pytest.raises(urllib.error.HTTPError) as e:
        _call(port, "/state", "not-the-token")
    assert e.value.code == 403


def test_rejected_intent_returns_the_message_and_the_unchanged_state(tmp_path):
    b = Bundle(synthetic.make_bundle(tmp_path / "rec", burned_in=True, media=False))
    session = qmlbridge.Session(b, REPO)
    srv = qmlbridge.serve(session, 0)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _call(srv.server_port, "/op", session.token, {"op": "set_webcam", "args": {"shape": "rect"}})
        body = json.loads(e.value.read())
        assert "burned" in body["error"] and body["state"]["webcam"]["shape"] == "circle"
    finally:
        srv.shutdown()


def test_export_without_a_renderer_reports_the_failure(server):
    """render.py may not exist yet; the editor must show that, not hang on a progress
    bar that never moves."""
    session, port = server
    session.exporter.MODULE = "omarchy_studio.definitely_not_a_module"
    _call(port, "/export", session.token, {"output": str(session.bundle.root / "out.mp4")})
    for _ in range(200):
        st = _call(port, "/export", session.token)
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["state"] == "error" and "definitely_not_a_module" in st["message"]


def test_zoom_track_is_omitted_from_a_drag_reply(server):
    """A drag POSTs an intent per frame; re-serializing the whole track each time is
    what would make the drag stutter. Absent means unchanged."""
    session, port = server
    s = _call(port, "/op", session.token, {"op": "set_webcam", "args": {"shape": "rect"}})
    assert "zoom_track" not in s
    s = _call(port, "/op", session.token, {"op": "set_zoom", "args": {"enabled": True}})
    assert "zoom_track" in s
    assert "zoom_track" in _call(port, "/state", session.token)


def test_backdrop_inset_is_resolved_through_placement(bundle):
    from omarchy_studio.geometry import Placement as P

    canvas = bundle.canvas
    pad = 0.05 * min(canvas.width, canvas.height)
    bundle.edit.backdrop.padding = 0.05
    d = qmlbridge.resolve_backdrop(bundle)
    expect = P(pad / canvas.width, pad / canvas.height,
               (canvas.width - 2 * pad) / canvas.width,
               (canvas.height - 2 * pad) / canvas.height)
    assert d["rect"] == expect.to_qml(canvas)


@needs_ffmpeg
def test_export_drives_the_real_renderer_to_completion(media_bundle):
    """The progress contract, against the renderer that actually ships: the child's
    callback has to reach the UI as a fraction that ends at 1.0."""
    pytest.importorskip("omarchy_studio.render")
    qmlbridge.apply_op(media_bundle, "add_cut", {"start_ms": 500, "end_ms": 900})
    media_bundle.save_edit()
    ex = qmlbridge.Exporter(media_bundle, REPO)
    out = media_bundle.root / "out.mp4"
    ex.start(str(out))
    for _ in range(600):
        st = ex.snapshot()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.25)
    assert st["state"] == "done", st
    assert st["progress"] == 1.0
    assert out.exists() and out.stat().st_size > 0


def test_backdrop_padding_is_measured_against_the_short_side(bundle):
    """render._backdrop pads by `padding * min(W,H)` on BOTH axes. Resolving the padding
    per-axis instead would give the preview the wrong margin on the long one."""
    bundle.edit.backdrop.padding = 0.05
    bundle.edit.backdrop.corner_radius = 0.02
    d = qmlbridge.resolve_backdrop(bundle)
    short = min(bundle.canvas.width, bundle.canvas.height)
    assert d["rect"]["x"] == pytest.approx(0.05 * short)
    assert d["rect"]["y"] == pytest.approx(0.05 * short)
    assert d["rect"]["width"] == pytest.approx(bundle.canvas.width - 0.1 * short)
    assert d["rect"]["height"] == pytest.approx(bundle.canvas.height - 0.1 * short)
    assert d["radius"] == pytest.approx(0.02 * short)
    # The preview scales the whole zoomed canvas into the inset, as the export does.
    assert d["content_scale"]["x"] == pytest.approx(d["rect"]["width"] / bundle.canvas.width)
    assert d["content_scale"]["y"] == pytest.approx(d["rect"]["height"] / bundle.canvas.height)
