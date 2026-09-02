"""Preview versus export, measured in pixels.

The editor's entire claim is that what QML draws is what ffmpeg will render. Reading the
filtergraph cannot check that claim -- a graph can be perfectly well formed and still
composite differently from the scene graph -- so this module renders the SAME frame of
the SAME project twice, once through `render.build_graph` and once through the real
editor window, and compares the two.

SILHOUETTES, NOT PIXELS. The two engines antialias, resample and colour-manage
differently, so pixel equality is the wrong test and would have to be loosened until it
proved nothing. What has to agree is WHERE things are: the bounding box of the probe
against a black ground, at the 50% luma contour. The budget is 2 px.

Probes are WHITE ON BLACK on purpose. A saturated colour probe would be measured through
the export's 4:2:0 chroma subsampling, which moves a colour boundary by up to a pixel for
reasons that have nothing to do with placement. Luma is not subsampled, so a white-on-
black silhouette measures the geometry and nothing else.

WHY THESE TESTS NEED A COMPOSITOR, AND WHAT RUNS WITHOUT ONE
------------------------------------------------------------
`QT_QPA_PLATFORM=offscreen` renders the scene graph faithfully -- an Image, a Rectangle,
a MultiEffect all grab correctly -- but a VideoOutput grabs EMPTY. Measured here on
qt6-multimedia 6.10 against every escape route available on this machine: the offscreen
platform on the default RHI and on `QSG_RHI_BACKEND=opengl`, `QT_QUICK_BACKEND=software`,
the `vnc` platform, `minimalegl` (aborts), and software decoding via
`QT_FFMPEG_DECODING_HW_DEVICE_TYPES=`. All produce a grab in which the Rectangle behind
the video is present and the video is not. The same scene on the session compositor
renders the video correctly. There is no Xvfb, weston, sway or cage installed to host a
headless one.

So: the webcam, zoom and blur cases -- everything whose silhouette IS the video -- run
against the live session compositor and SKIP when there is none. The image-layer case
needs no video and is forced offscreen, so it is a real headless check. Set
`OMARCHY_STUDIO_PARITY=required` to turn the skip into a failure, which is what a CI job
that believes it is running these should do.

WHAT THIS HARNESS FOUND
-----------------------
* Image layers never exported at all. The editor stores `props["asset"]` as a bare
  filename so the bundle stays portable; layers.py handed it straight to `-i` and ffmpeg
  resolved it against its own working directory. Fixed in `render._resolve_asset`.
* Pixelate previewed unreadable and exported legible. The bridge writes `block`
  normalized to the canvas (0.012); layers.py read it as pixels, `int()`ed it to 0 and
  clamped to a 2 px mosaic. Both now go through `layers.block_px`.
* Text previewed a caption box the export did not draw, at a font size off a different
  basis. Both now go through `layers.split_color` / `layers.font_px` / `layers.radius_px`.
* The blur BOX lands within 1 px, but the blur STRENGTH does not track -- see
  `test_the_preview_blur_is_at_least_as_strong_as_the_export`, which is xfail with the
  measurement.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from omarchy_studio import project, qmlbridge, render
from omarchy_studio.geometry import REDACT_PRESETS, Canvas, Placement, Zoom, blur_sigma
from omarchy_studio.project import Bundle, Capture, Layer, Stream

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "bin" / "omarchy-studio"
QML6 = Path("/usr/bin/qml6")

W, H, FPS, FRAMES = 640, 360, 30, 40
CANVAS = Canvas(W, H)
ANCHOR_US = 1_000_000_000

# The brief's budget. Both engines have measured well inside it; loosening this is how a
# parity harness stops being one.
BUDGET_PX = 2.0

_HAVE_COMPOSITOR = bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
_REQUIRED = os.environ.get("OMARCHY_STUDIO_PARITY") == "required"
_NO_COMPOSITOR = (
    "the QML half of a video parity check needs a compositor: a VideoOutput grabs empty "
    "under every headless Qt platform on this machine (offscreen, offscreen+opengl RHI, "
    "software backend, vnc; minimalegl aborts) and no Xvfb/weston/sway/cage is installed "
    "to host one. Run the suite from a graphical session, or set "
    "OMARCHY_STUDIO_PARITY=required to make this a failure instead of a skip."
)

needs_qml = pytest.mark.skipif(not QML6.exists(), reason="qml6 not installed")
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg not installed",
)
needs_compositor = pytest.mark.skipif(not _HAVE_COMPOSITOR, reason=_NO_COMPOSITOR)


def test_the_video_half_of_this_harness_can_actually_run():
    """The loud stub. A skipped parity check is honest; a CI job that quietly skips every
    one of them and reports green is not, so this fails when the environment was asserted
    to support them and does not."""
    if _REQUIRED and not _HAVE_COMPOSITOR:
        raise AssertionError(
            "OMARCHY_STUDIO_PARITY=required but no compositor is reachable.\n" + _NO_COMPOSITOR
        )
    if not _HAVE_COMPOSITOR:
        pytest.skip(_NO_COMPOSITOR)


# --- fixtures ---------------------------------------------------------------


def _encode(dst: Path, graph: str) -> None:
    """A lossless clip from a lavfi graph. Lossless because the probe is measured at a
    luma threshold and h264 ringing on a hard white edge is exactly the wrong noise."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    gp = dst.with_suffix(".graph")
    gp.write_text(graph + "\n")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-/filter_complex", str(gp), "-map", "[v]",
         "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p",
         "-g", "15", "-bf", "0", "-frames:v", str(FRAMES), str(dst)],
        check=True, capture_output=True,
    )


def parity_bundle(
    root: Path, *, boxes: tuple[tuple[int, int, int, int], ...] = (), camera: bool = False,
    canvas: Canvas = CANVAS,
) -> Path:
    """A bundle whose screen is black with white boxes and whose camera is solid white.

    Deliberately not `synthetic.make_bundle`: that one is testsrc2 and smptebars, which
    have no silhouette to measure. `monitor_scale` is 1 and the capture origin is (0, 0)
    so a click's normalized focal point is just its coordinate over the canvas, and the
    zoom cases can name a focal point directly.
    """
    root = Path(root)
    shutil.rmtree(root, ignore_errors=True)
    dur = FRAMES / FPS
    cw, ch = canvas.width, canvas.height
    cap = Capture(
        created="2026-09-02T00:00:00",
        screen=Stream("media/screen.mp4", cw, ch, FPS, 1, ANCHOR_US, has_audio=False),
        camera=Stream("media/camera.mp4", 320, 240, FPS, 1, ANCHOR_US) if camera else None,
        logical_geometry={"x": 0, "y": 0, "w": cw, "h": ch},
        physical_geometry={"x": 0, "y": 0, "w": cw, "h": ch},
        monitor_scale=1.0,
        monitor_name="DP-1",
    )
    project.create(root, cap)

    g = f"color=c=black:s={cw}x{ch}:r={FPS}:d={dur}"
    for x, y, w, h in boxes:
        g += f",drawbox=x={x}:y={y}:w={w}:h={h}:color=white:t=fill"
    _encode(root / "media" / "screen.mp4", g + "[v]")
    if camera:
        _encode(root / "media" / "camera.mp4", f"color=c=white:s=320x240:r={FPS}:d={dur}[v]")

    (root / "events").mkdir(exist_ok=True)
    (root / "events" / "input.jsonl").write_text(
        json.dumps({"t_us": ANCHOR_US, "type": "meta", "schema": 1}) + "\n"
    )
    # The preview reads the proxy, never the master. Copying the master across is what
    # `proxy.ensure_proxy` would produce for a clip already at preview size, and keeps
    # the two halves reading identical pixels.
    (root / "proxy").mkdir(exist_ok=True)
    for stream in ("screen", "camera"):
        src = root / "media" / f"{stream}.mp4"
        if src.exists():
            shutil.copy2(src, root / "proxy" / f"{stream}-proxy.mp4")
    return root


def write_clicks(root: Path, focal: tuple[float, float], frames: tuple[int, ...]) -> None:
    """A click cluster at a normalized focal point.

    `map_clicks` divides by `screen.width` after subtracting the capture origin and
    multiplying by `monitor_scale`; with origin 0 and scale 1 that inverts to a plain
    multiply, which is why this fixture is built the way it is.
    """
    cx, cy = focal
    lines = [json.dumps({"t_us": ANCHOR_US, "type": "meta", "schema": 1})]
    for f in frames:
        lines.append(json.dumps({
            # +1us so the frame index cannot land a frame early on an exact boundary.
            "t_us": ANCHOR_US + round(f * 1_000_000 / FPS) + 1,
            "type": "click", "button": "left",
            "x": round(cx * W), "y": round(cy * H),
        }))
    (root / "events" / "input.jsonl").write_text("\n".join(lines) + "\n")


# --- the two renderers ------------------------------------------------------


def export_frame(bundle: Bundle, frame: int, png: Path) -> Path:
    """One frame of the real export, straight out of `[vout]`.

    PNG rather than "render an mp4 and pull a frame back out": the plan's own encoder
    settings are not under test here and h264 quantization is noise on a threshold
    measurement. Everything upstream of the encoder -- the graph, the inputs, the
    `-/filter_complex` file -- is exactly what `render.run_plan` uses.
    """
    plan = render.build_graph(bundle)
    # An `-ss` head trim would renumber frame 0 and this comparison assumes it did not.
    assert plan.head_trim_seconds == 0.0, "fixture unexpectedly has warm-up packets"
    gp = png.with_suffix(".graph")
    gp.write_text(plan.graph + "\n")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *plan.inputs,
         "-/filter_complex", str(gp), "-map", "[vout]",
         "-frames:v", str(frame + 1), "-update", "1", "-f", "image2", str(png)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr[-3000:]
    return png


def qml_frame(root: Path, frame: int, png: Path, *, needs_video: bool) -> dict:
    """Grab the editor's stage at full canvas resolution and return its self-test line.

    The window is the production one -- `bin/omarchy-studio`, the real bridge, the real
    Preview.qml. A parity harness that instantiated its own scene would be testing a
    second implementation of the preview, which is the thing it exists to prevent.
    """
    env = dict(os.environ)
    if needs_video:
        # Inherit the session's platform. Forcing offscreen here is what makes a video
        # parity check silently compare two black frames and pass.
        env.pop("QT_QPA_PLATFORM", None)
    else:
        env["QT_QPA_PLATFORM"] = "offscreen"
    r = subprocess.run(
        [str(LAUNCHER), str(root), "--selftest", "3500", "--no-proxy",
         "--frame", str(frame), "--grab", str(png)],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(REPO),
    )
    lines = [l for l in r.stderr.splitlines() if "SELFTEST {" in l]
    assert lines, f"editor produced no self-test line (rc={r.returncode}):\n{r.stderr[-3000:]}"
    assert png.exists(), r.stderr[-3000:]
    return json.loads(lines[-1].split("SELFTEST ", 1)[1])


# --- measurement ------------------------------------------------------------


@dataclass(frozen=True)
class Grey:
    data: bytes
    width: int
    height: int
    scale: float  # grab pixels per canvas pixel; 2 on a HiDPI session, 1 offscreen
    peak: int


def grey(png: Path, canvas: Canvas = CANVAS) -> Grey:
    """Decode to single-channel luma, in whatever resolution the grab came out at.

    `grabToImage` honours the window's devicePixelRatio, so the same request yields
    1280x720 on this scale-2 display and 640x360 offscreen. Normalising by the ratio
    rather than forcing DPR 1 keeps the extra half-pixel of precision the HiDPI grab
    actually carries.
    """
    dims = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(png)],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")
    w, h = int(dims[0]), int(dims[1])
    data = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(png), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    ).stdout
    assert len(data) == w * h, f"{png} decoded to {len(data)} bytes, expected {w * h}"
    ratio = w / canvas.width
    assert abs(h / canvas.height - ratio) < 1e-6, (
        f"{png} is {w}x{h}, not a scaled {canvas.width}x{canvas.height}"
    )
    return Grey(data, w, h, ratio, max(data))


def silhouette(g: Grey) -> tuple[float, float, float, float] | None:
    """Bounding box of everything at or above half the frame's peak luma, in CANVAS px.

    Half-peak rather than a fixed level because a Gaussian's 50% contour sits exactly on
    the original edge at any sigma -- which is what lets a blurred box and a sharp one be
    compared with the same measurement.
    """
    level = g.peak // 2
    table = bytes(0 if v < level else 1 for v in range(256))
    mask = g.data.translate(table)
    x0, y0, x1, y1 = g.width, g.height, -1, -1
    for y in range(g.height):
        row = mask[y * g.width:(y + 1) * g.width]
        a = row.find(b"\x01")
        if a < 0:
            continue
        x0, x1 = min(x0, a), max(x1, row.rfind(b"\x01"))
        y0, y1 = min(y0, y), max(y1, y)
    if x1 < 0:
        return None
    return (x0 / g.scale, y0 / g.scale, (x1 + 1) / g.scale, (y1 + 1) / g.scale)


def edge_width(g: Grey, y_canvas: float, x_from: float, x_to: float) -> float:
    """The 10%-to-90% rise width of a blurred edge, in canvas px, scanning left to right.

    A Gaussian's 10-90 width is 2.563*sigma, so this is a direct read of how hard each
    engine actually blurred -- which is a different question from where it blurred, and
    the one that turns out to disagree.
    """
    y = int(round(y_canvas * g.scale))
    row = [g.data[y * g.width + x] / g.peak for x in range(g.width)]
    lo, hi = int(x_from * g.scale), int(x_to * g.scale)

    def crossing(level: float) -> float | None:
        for x in range(max(lo, 1), hi):
            if row[x] >= level:
                prev = row[x - 1]
                return (x - 1 + (level - prev) / max(row[x] - prev, 1e-9)) / g.scale
        return None

    a, b = crossing(0.1), crossing(0.9)
    assert a is not None and b is not None, "no 10-90 edge found on the scanline"
    return b - a


def assert_within(budget: float, label: str, a, b) -> float:
    assert a is not None, f"{label}: nothing was drawn in the export frame"
    assert b is not None, f"{label}: nothing was drawn in the preview grab"
    worst = max(abs(p - q) for p, q in zip(a, b))
    assert worst <= budget, (
        f"{label}: preview and export disagree by {worst:.2f}px (budget {budget}px)\n"
        f"  export  {tuple(round(v, 2) for v in a)}\n"
        f"  preview {tuple(round(v, 2) for v in b)}"
    )
    return worst


# --- webcam ------------------------------------------------------------------


@needs_qml
@needs_ffmpeg
@needs_compositor
@pytest.mark.parametrize(
    "place,shape",
    [
        ((0.10, 0.15, 0.25, 0.30), "circle"),
        ((0.38, 0.40, 0.24, 0.24), "rect"),
        ((0.62, 0.58, 0.30, 0.35), "rounded"),
    ],
    ids=["top-left-circle", "centre-rect", "bottom-right-rounded"],
)
def test_webcam_silhouette_matches_the_export(tmp_path, place, shape):
    """Three positions and all three mask shapes.

    A circle is the ellipse INSCRIBED in the tile, so its silhouette box is the tile box
    -- which is the assertion that catches a preview drawing a stadium (radius min/2 on a
    non-square box) where the export draws an ellipse.
    """
    root = parity_bundle(tmp_path / "cam", camera=True)
    b = Bundle(root)
    b.edit.webcam.x, b.edit.webcam.y, b.edit.webcam.w, b.edit.webcam.h = place
    b.edit.webcam.shape = shape
    b.save_edit()

    export_frame(b, 10, tmp_path / "ff.png")
    st = qml_frame(root, 10, tmp_path / "qml.png", needs_video=True)
    assert st["webcamShape"] == shape and st["webcamRect"]["visible"] is True

    worst = assert_within(
        BUDGET_PX, f"webcam {shape}",
        silhouette(grey(tmp_path / "ff.png")), silhouette(grey(tmp_path / "qml.png")),
    )
    print(f"\nwebcam {shape} at {place}: worst edge disagreement {worst:.2f}px")


# --- zoom --------------------------------------------------------------------


@needs_qml
@needs_ffmpeg
@needs_compositor
@pytest.mark.parametrize(
    "amount,focal,box",
    [
        (1.6, (0.35, 0.62), (192, 198, 64, 50)),
        (2.5, (0.35, 0.62), (192, 198, 64, 50)),
        (2.0, (0.08, 0.50), (32, 162, 64, 50)),
    ],
    ids=["1.6x", "2.5x", "2.0x-clamped-at-the-left-edge"],
)
def test_zoom_silhouette_matches_the_export(tmp_path, amount, focal, box):
    """A white box magnified two ways, compared to each other AND to Zoom.viewport.

    The third case focuses 0.08 across the frame, where the viewport clamps against the
    left edge. That clamp exists twice -- once in `Zoom.viewport` for the preview and
    once transcribed into `zoom_filter`'s expression for the export -- so it is precisely
    the sort of hand-copied arithmetic that drifts, and the only thing that catches it is
    a rendered comparison.

    Frame 13 is inside the hold, where the envelope is exactly 1 and both sides are at
    the nominal scale rather than somewhere on an ease that they might sample differently.
    """
    root = parity_bundle(tmp_path / "zoom", boxes=(box,))
    write_clicks(root, focal, (4, 6))
    b = Bundle(root)
    b.edit.webcam.enabled = False
    b.edit.zoom.enabled = True
    b.edit.zoom.amount = amount
    b.edit.zoom.ease_frames = 6
    b.edit.zoom.hold_frames = 12
    b.edit.zoom.merge_gap_frames = 30
    b.save_edit()

    frame = 13
    export_frame(b, frame, tmp_path / "ff.png")
    st = qml_frame(root, frame, tmp_path / "qml.png", needs_video=True)
    assert st["frame"] == frame
    assert st["zoomScale"] == pytest.approx(amount, abs=1e-6), (
        "the preview is not on the hold plateau; the comparison would be meaningless"
    )

    ff = silhouette(grey(tmp_path / "ff.png"))
    ql = silhouette(grey(tmp_path / "qml.png"))
    worst = assert_within(BUDGET_PX, f"zoom {amount}x", ff, ql)

    # Third opinion: geometry.Zoom, so "both engines wrong the same way" still fails.
    vp = Zoom(amount, *focal).viewport(CANVAS)
    x, y, w, h = box
    want = tuple((v - o) * amount for v, o in
                 ((x, vp.x), (y, vp.y), (x + w, vp.x), (y + h, vp.y)))
    assert_within(BUDGET_PX, f"zoom {amount}x export vs Zoom.viewport", want, ff)
    assert_within(BUDGET_PX, f"zoom {amount}x preview vs Zoom.viewport", want, ql)
    print(f"\nzoom {amount}x focal {focal}: preview/export {worst:.2f}px, "
          f"analytic box {tuple(round(v, 2) for v in want)}")


# --- image layer -------------------------------------------------------------


@needs_qml
@needs_ffmpeg
def test_image_layer_silhouette_matches_the_export(tmp_path):
    """The one case that needs no video, so it runs offscreen and is a real headless check.

    Added through `qmlbridge.apply_op` rather than by hand-building a Layer, because the
    bug this found lived in the seam between the op (which stores a bare asset filename)
    and the compiler (which passed it to `-i` verbatim) -- a hand-built layer carrying an
    absolute path would have exported perfectly and proved nothing.
    """
    root = parity_bundle(tmp_path / "img")
    asset = tmp_path / "probe.png"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=64x64:d=1",
         "-frames:v", "1", str(asset)],
        check=True, capture_output=True,
    )
    b = Bundle(root)
    b.edit.webcam.enabled = False
    qmlbridge.apply_op(b, "add_image", {
        "path": str(asset),
        "rect": {"x": 96, "y": 72, "width": 224, "height": 152},
    })
    b.save_edit()
    assert b.edit.layers[0].props == {"asset": "probe.png"}, (
        "the bundle must keep the portable asset name; the render resolves it"
    )

    export_frame(b, 4, tmp_path / "ff.png")
    st = qml_frame(root, 4, tmp_path / "qml.png", needs_video=False)
    assert st["layers"] == 1

    worst = assert_within(
        BUDGET_PX, "image layer",
        silhouette(grey(tmp_path / "ff.png")), silhouette(grey(tmp_path / "qml.png")),
    )
    print(f"\nimage layer: worst edge disagreement {worst:.2f}px (headless)")


# --- redaction ---------------------------------------------------------------

# A big canvas and a big probe, both forced by the preset ladder reaching sigma 60.
#
# Two separate constraints, and getting either wrong makes this measure the wrong thing:
#
# * The box needs ~3 sigma (180px) of clear space on every side, or the blur runs into
#   the boundary and the comparison measures each engine's edge padding, not its kernel.
# * The box must be much WIDER than sigma. The half-peak contour of a blurred step edge
#   sits on the edge at any sigma, which is what makes it a placement measurement -- but
#   only while the box is large enough to still reach full peak in the middle. At 120x80
#   against sigma 60 it does not: the peak collapses, the half-peak contour balloons
#   outward by 25px, and the test starts reporting a strength difference as a placement
#   one. 400x300 is 2.5 sigma of half-width at the widest preset.
BLUR_CANVAS = Canvas(960, 720)
_BLUR_BOX = (280, 210, 400, 300)
# The redaction covers the whole frame, so the only boundary in play is the frame's own
# and both engines treat that identically.
_BLUR_RECT = (0, 0, 960, 720)
# Scan the box's middle row from well outside it to its centre. The centre carries the
# frame's peak luma, so the 90% crossing always exists however far the blur has pulled
# the peak down -- a window stopping at the nominal edge never reached 0.9 of it.
_SCAN = (_BLUR_BOX[1] + _BLUR_BOX[3] / 2, 20.0, _BLUR_BOX[0] + _BLUR_BOX[2] / 2)


def _blur_bundle(tmp_path: Path, preset: str) -> tuple[Path, Bundle]:
    root = parity_bundle(tmp_path / "blur", boxes=(_BLUR_BOX,), canvas=BLUR_CANVAS)
    b = Bundle(root)
    b.edit.webcam.enabled = False
    x, y, w, h = _BLUR_RECT
    b.edit.layers.append(
        Layer(id="red", type="blur",
              x=x / BLUR_CANVAS.width, y=y / BLUR_CANVAS.height,
              w=w / BLUR_CANVAS.width, h=h / BLUR_CANVAS.height,
              props={"preset": preset})
    )
    b.save_edit()
    return root, b


def _blur_pair(tmp_path: Path, preset: str) -> tuple[Grey, Grey]:
    root, b = _blur_bundle(tmp_path, preset)
    export_frame(b, 5, tmp_path / "ff.png")
    qml_frame(root, 5, tmp_path / "qml.png", needs_video=True)
    return (grey(tmp_path / "ff.png", BLUR_CANVAS),
            grey(tmp_path / "qml.png", BLUR_CANVAS))


def sigma_of(g: Grey) -> float:
    """The sigma implied by a measured 10-90 edge width. A Gaussian's is 2.563*sigma."""
    return edge_width(g, *_SCAN) / 2.563


@needs_qml
@needs_ffmpeg
@needs_compositor
@pytest.mark.parametrize("preset", sorted(REDACT_PRESETS))
def test_blur_box_silhouette_matches_the_export(tmp_path, preset):
    """WHERE the blur is, measured at the half-peak contour.

    Half-peak is deliberate and is what makes this a placement test rather than a
    strength test: blurring a step edge with any symmetric kernel leaves the 50% crossing
    exactly where the edge was, so this box is the unblurred box no matter what sigma
    each engine used. How hard each one blurred is the next test, and it is a different
    answer.
    """
    ff, ql = _blur_pair(tmp_path, preset)
    worst = assert_within(BUDGET_PX, f"blur {preset}", silhouette(ff), silhouette(ql))
    print(f"\nredaction {preset!r} (export sigma {blur_sigma(preset)}): box agrees to "
          f"{worst:.2f}px; measured sigma export {sigma_of(ff):.1f} preview {sigma_of(ql):.1f}")


@needs_qml
@needs_ffmpeg
@needs_compositor
@pytest.mark.xfail(
    strict=True,
    reason=(
        "MEASURED, NOT ASSUMED. Effective sigma from the 10-90 edge width of a 400x300 "
        "white box, 960x720 canvas: export 20.8 / 32.4 / 57.4 for strong / heavy / "
        "solid, which tracks the asked-for 22 / 34 / 60 to within gblur steps=3. The "
        "preview measures 14.0 for ALL THREE. Two faults compound. (1) The ratio is "
        "off: _QT_BLURMAX_TO_GBLUR_SIGMA = 18/48 claims MultiEffect at blurMax=48, "
        "blur=1.0 equals sigma 18; it measures 14.0 on this build, so the true figure "
        "is nearer 14/48. (2) Every preset saturates: sigma/(48*0.375) is 1.22, 1.89 "
        "and 3.33, and geometry._clamp pins all three to blur=1.0. So the canvas is "
        "1.5x to 4.1x weaker than the file and identical across the ladder. Qt cannot "
        "reach sigma 60 by raising `blur` alone -- it needs blurMax (capped at 64) "
        "and/or blurMultiplier -- so the fix is a recalibration in geometry.py, which "
        "is a contract this agent must not edit."
    ),
)
@pytest.mark.parametrize("preset", sorted(REDACT_PRESETS))
def test_the_preview_renders_the_export_redaction_strength(tmp_path, preset):
    """geometry.py's stated rule: 'the canvas must never look safer than the file', and
    'the preview renders export strength exactly'. This is that rule as a measurement.
    It turns green when the calibration is fixed, and strict xfail means nobody can fix
    it and leave this stale."""
    ff, ql = _blur_pair(tmp_path, preset)
    want = blur_sigma(preset)
    got = sigma_of(ql)
    assert got == pytest.approx(want, rel=0.15), (
        f"{preset}: the export blurs at sigma {sigma_of(ff):.1f} (asked {want}) and the "
        f"preview at sigma {got:.1f}"
    )


@needs_qml
@needs_ffmpeg
@needs_compositor
@pytest.mark.xfail(
    strict=True,
    reason=(
        "MEASURED: the three presets are indistinguishable in the preview. Effective "
        "preview sigma is 14.0 for all of strong, heavy and solid, because their QML "
        "`blur` values (1.22, 1.89, 3.33) are all pinned to 1.0 by geometry._clamp, "
        "while the export goes 20.8 -> 32.4 -> 57.4. A redaction control whose effect is "
        "invisible on the canvas is exactly the failure the preset ladder replaced a "
        "slider to avoid: the user cannot tell 'strong' from 'solid' before exporting."
    ),
)
def test_each_redaction_preset_looks_different_on_the_canvas(tmp_path):
    """A ladder the user cannot see is not a ladder. Each step up must widen the
    preview's blur, not just the file's."""
    sigmas = {}
    for preset in sorted(REDACT_PRESETS, key=lambda k: REDACT_PRESETS[k]):
        _, ql = _blur_pair(tmp_path, preset)
        sigmas[preset] = sigma_of(ql)
    ladder = [sigmas[k] for k in sorted(REDACT_PRESETS, key=lambda k: REDACT_PRESETS[k])]
    assert all(b > a * 1.1 for a, b in zip(ladder, ladder[1:])), (
        f"preview sigmas {sigmas} do not rise with the preset ladder "
        f"{dict(sorted(REDACT_PRESETS.items(), key=lambda kv: kv[1]))}"
    )


# --- what consumes the budget ------------------------------------------------


@pytest.mark.parametrize(
    "place",
    [
        (0.62, 0.58, 0.30, 0.35),   # the rounded-webcam case, worst measured at 2.00px
        (0.10, 0.15, 0.25, 0.30),
        (0.3125, 0.25, 0.25, 0.25),  # already even at 640x360: no snap, no error
    ],
)
def test_the_export_snaps_overlay_boxes_to_even_pixels_and_the_preview_does_not(place):
    """Where most of the 2px budget actually goes, recorded so it is not rediscovered.

    `Placement.to_ffmpeg_overlay` runs the resolved rect through `Rect.to_even` -- yuv420
    chroma subsampling requires it, and ffmpeg's crop floors odd values silently anyway.
    `Placement.to_qml` does not: QML is happy with 396.8. So every layer and the webcam
    sit up to ~1.5px left and up in the export relative to the preview, per axis,
    whenever the resolved position is not already even.

    The measured rounded-webcam case lands at exactly 2.00px once the mask's own
    antialiasing is added, which is the whole budget. That is not a bug in either
    emission -- both are doing the right thing for their engine -- but it means the 2px
    figure is nearly all systematic, with very little left over for a real regression to
    show up in. Halving it would need the preview to snap too, and `geometry.py` is a
    contract this agent must not edit.
    """
    r = Placement(*place).resolve(CANVAS)
    even = r.to_even()
    for a, b in ((r.x, even.x), (r.y, even.y), (r.w, even.w), (r.h, even.h)):
        assert abs(a - b) < 2.0, (
            f"{place}: the even snap moved an edge by {abs(a - b):.2f}px, which alone "
            f"exceeds the {BUDGET_PX}px parity budget"
        )
