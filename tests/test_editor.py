"""The editor, run for real.

Two things are worth testing about a QML app from Python, and only two:

1. that it loads at all. qml6 reports a missing type as a runtime warning and can still
   exit 0, and the Qt 5.15 `qml` binary fails with nothing but "Did not load any objects,
   exiting" -- so "the launcher returned 0" is not enough, and these tests read the
   self-test line the window prints before it quits.

2. that what it draws lands where geometry.py says it should. The preview's whole claim
   is that it is the export's twin, and the way that claim breaks is silent: an
   `anchors.fill` overriding an explicit x/y, a transformOrigin that is not TopLeft. So
   the stage is grabbed at full canvas resolution and the drawn rectangle is measured
   against Placement.resolve.

These run headless (QT_QPA_PLATFORM=offscreen) and leave no window behind.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import synthetic

from omarchy_studio.geometry import Canvas, Placement, Zoom
from omarchy_studio.project import Bundle

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "bin" / "omarchy-studio"
QML6 = Path("/usr/bin/qml6")

needs_qml = pytest.mark.skipif(not QML6.exists(), reason="qml6 not installed")
needs_ffmpeg = pytest.mark.skipif(not synthetic.have_ffmpeg(), reason="ffmpeg not installed")

# A colour no theme uses, so the bounding box found in the grab can only be the layer.
PROBE_COLOR = "#00ff00"


def run_editor(bundle: Path, *extra: str, timeout: int = 90) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run(
        [str(LAUNCHER), str(bundle), "--selftest", "4000", *extra],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(REPO),
    )


# The expected zoom track is computed in a FRESH interpreter, the way the editor's own
# bridge computes it. Deriving it in the pytest process instead couples this test to
# whatever else the suite has done to the shared events/zoom modules, and the failure
# looks like a preview bug rather than the test-order artefact it is.
_TRACK_SNIPPET = """
import json, sys
from pathlib import Path
from omarchy_studio.project import Bundle
from omarchy_studio import qmlbridge
print(json.dumps(qmlbridge.zoom_track(Bundle(Path(sys.argv[1])))))
"""


def zoom_track_of(bundle_root: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "lib")
    out = subprocess.run(
        [sys.executable, "-c", _TRACK_SNIPPET, str(bundle_root)],
        capture_output=True, text=True, check=True, env=env, cwd=str(REPO),
    ).stdout
    return json.loads(out)


def selftest_fields(stderr: str) -> dict:
    for line in stderr.splitlines():
        if "SELFTEST {" in line:
            return json.loads(line.split("SELFTEST ", 1)[1])
    raise AssertionError(f"no SELFTEST line in:\n{stderr[-3000:]}")


def raw_rgb(png: Path) -> tuple[bytes, int, int]:
    """Decode with ffmpeg rather than adding an image library for one test."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(png)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = (int(v) for v in probe.split(","))
    data = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(png), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return data, w, h


def bbox_of(data: bytes, w: int, h: int, rgb: tuple[int, int, int], tol: int = 40):
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = y * w * 3
        for x in range(w):
            i = row + x * 3
            if (abs(data[i] - rgb[0]) <= tol
                    and abs(data[i + 1] - rgb[1]) <= tol
                    and abs(data[i + 2] - rgb[2]) <= tol):
                x0, y0 = min(x0, x), min(y0, y)
                x1, y1 = max(x1, x), max(y1, y)
    if x1 < 0:
        return None
    return x0, y0, x1 + 1, y1 + 1


@needs_qml
def test_editor_starts_and_reads_the_project(tmp_path):
    root = synthetic.make_bundle(tmp_path / "rec", media=False)
    r = run_editor(root, "--no-proxy")
    f = selftest_fields(r.stderr)
    assert r.returncode == 0, r.stderr[-3000:]
    assert f["connected"] is True
    assert f["canvas"] == "1280x720"
    assert f["clicks"] == 4
    assert f["webcamEditable"] is True
    assert "Did not load any objects" not in r.stderr
    # A missing type is a warning, not an error, and the app still runs without it.
    assert "unavailable" not in r.stderr
    assert "Cannot override FINAL" not in r.stderr


@needs_qml
def test_burned_in_recording_reports_a_disabled_webcam(tmp_path):
    root = synthetic.make_bundle(tmp_path / "rec", burned_in=True, media=False)
    f = selftest_fields(run_editor(root, "--no-proxy").stderr)
    assert f["webcamEditable"] is False


@needs_qml
@needs_ffmpeg
def test_drawn_layer_lands_where_placement_resolve_says(tmp_path):
    """The preview's core claim, measured in pixels.

    A shape layer at a known normalized placement is drawn into a full-resolution grab of
    the stage, and its bounding box has to match the rectangle geometry.py resolves. This
    is the test that fails if someone anchors a transformed item or reintroduces
    placement maths in JavaScript.
    """
    root = synthetic.make_bundle(tmp_path / "rec", media=False)
    place = Placement(0.2, 0.35, 0.25, 0.3)
    bundle = Bundle(root)
    edit = json.loads(json.dumps(bundle.edit.to_dict()))
    edit["layers"] = [{
        "id": "probe", "type": "shape",
        "x": place.x, "y": place.y, "w": place.w, "h": place.h,
        "anchor": "top-left", "opacity": 1.0, "fade_frames": 0, "z": 1,
        "props": {"color": PROBE_COLOR, "radius": 0.0}, "enabled": True, "t": None,
    }]
    bundle.edit_path.write_text(json.dumps(edit, indent=2))

    png = tmp_path / "stage.png"
    r = run_editor(root, "--no-proxy", "--grab", str(png))
    assert r.returncode == 0, r.stderr[-3000:]
    assert png.exists(), r.stderr[-3000:]

    data, w, h = raw_rgb(png)
    canvas = Canvas(1280, 720)
    assert (w, h) == (canvas.width, canvas.height)
    box = bbox_of(data, w, h, (0, 255, 0))
    assert box is not None, "the probe layer was not drawn at all"
    want = place.resolve(canvas)
    assert abs(box[0] - want.x) <= 2
    assert abs(box[1] - want.y) <= 2
    assert abs((box[2] - box[0]) - want.w) <= 2
    assert abs((box[3] - box[1]) - want.h) <= 2


@needs_qml
@needs_ffmpeg
def test_zoom_transform_is_applied_not_overridden(tmp_path):
    """Reads the transform back off the item, because the failure mode is a binding that
    is silently overridden -- anchors do that with no warning, and the symptom is a zoom
    that scales but never pans."""
    root = synthetic.make_bundle(tmp_path / "rec", seconds=2.0)
    bundle = Bundle(root)
    bundle.edit.zoom.enabled = True
    bundle.edit.zoom.amount = 2.0
    bundle.edit.zoom.ease_frames = 10
    bundle.edit.zoom.hold_frames = 40
    bundle.save_edit()

    track = zoom_track_of(root)
    peak = track["scale"].index(max(track["scale"]))
    frame = track["frames"][peak]

    f = selftest_fields(run_editor(root, "--no-proxy", "--frame", str(frame)).stderr)
    assert f["frame"] == frame
    assert f["zoomScale"] == pytest.approx(track["scale"][peak], abs=1e-6)
    assert f["zoomX"] == pytest.approx(track["x"][peak], abs=1e-6)
    assert f["zoomY"] == pytest.approx(track["y"][peak], abs=1e-6)
    # The scale really is a zoom-in and the translation is the viewport's, not a
    # rounding QML invented.
    assert track["scale"][peak] > 1.0
    assert f["zoomX"] <= 0 and f["zoomY"] <= 0


@needs_qml
def test_editor_leaves_no_processes_behind(tmp_path):
    """Scoped to this bundle's path: the machine may legitimately have another editor
    open, and a test that kills or fails on someone else's window is worse than no
    test."""
    root = synthetic.make_bundle(tmp_path / "rec", media=False)
    run_editor(root, "--no-proxy")
    out = subprocess.run(["pgrep", "-af", str(root)], capture_output=True, text=True).stdout
    assert out.strip() == "", f"a process outlived the editor: {out}"


@needs_qml
def test_an_intent_from_the_ui_round_trips_through_the_bridge(tmp_path):
    """Proves the whole loop the UI depends on: XHR POST with the token, the bridge
    mutating the Edit, and the reply re-rendering the view -- without saving, because an
    intent must not write the project."""
    root = synthetic.make_bundle(tmp_path / "rec", media=False)
    op = json.dumps({"op": "add_blur",
                     "args": {"rect": {"x": 128, "y": 72, "width": 256, "height": 144}}})
    f = selftest_fields(run_editor(root, "--no-proxy", "--selftest-op", op).stderr)
    assert f["error"] == ""
    assert f["layers"] == 1
    assert f["dirty"] is True
    assert not (root / "edit.json").exists()


@needs_qml
def test_a_rejected_intent_reaches_the_ui_as_an_error(tmp_path):
    root = synthetic.make_bundle(tmp_path / "rec", burned_in=True, media=False)
    op = json.dumps({"op": "set_webcam", "args": {"shape": "rect"}})
    f = selftest_fields(run_editor(root, "--no-proxy", "--selftest-op", op).stderr)
    assert "burned" in f["error"]
    assert f["webcamShape"] == "circle"  # the view snapped back to the truth


@needs_qml
def test_webcam_box_is_placement_resolve(tmp_path):
    """The overlay's geometry read back off the item, so a stray anchor or a JavaScript
    re-derivation of the box shows up here rather than in an exported video."""
    root = synthetic.make_bundle(tmp_path / "rec", media=False)
    bundle = Bundle(root)
    cam = bundle.edit.webcam
    f = selftest_fields(run_editor(root, "--no-proxy").stderr)
    # Through WebcamSettings.placement, not the raw w/h: a circle is square in PIXELS
    # and derives its height, so the stored h is not the box. Asserting the raw values
    # here is what let a 282x158 "circle" pass every test in the suite.
    canvas = Canvas(1280, 720)
    want = cam.placement(canvas).resolve(canvas)
    got = f["webcamRect"]
    assert got["visible"] is True
    assert got["x"] == pytest.approx(want.x)
    assert got["y"] == pytest.approx(want.y)
    assert got["width"] == pytest.approx(want.w)
    assert got["height"] == pytest.approx(want.h)


def test_qmldir_declares_both_singletons_and_no_module():
    """Fails in a second, where the alternative fails in five minutes.

    The launcher runs `qml6 -I editor editor/main.qml`, so main.qml finds its siblings
    through the implicit import of its own directory. A `module` line turns that
    directory into a named module, the implicit import stops resolving, and the only
    symptom is a window that never appears. Dropping the Bridge singleton does the same
    thing: every component binds to Bridge.state.
    """
    text = (REPO / "editor" / "qmldir").read_text()
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    assert "singleton Theme 1.0 Theme.qml" in lines
    assert "singleton Bridge 1.0 Bridge.qml" in lines
    assert not any(l.startswith("module ") for l in lines)
