"""The headless re-render entry point.

The parsing half is pure and cheap, so it is tested exhaustively. The rendering half
runs real ffmpeg against a lavfi bundle, and every assertion about it is made on
extracted PIXELS rather than on the filtergraph text: the whole class of bug this CLI
can introduce -- an option that is parsed, stored, and then quietly not applied -- is
invisible to a graph-shaped assertion.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import synthetic

from omarchy_studio.geometry import Canvas
from omarchy_studio.project import Bundle
from omarchy_studio.timebase import FrameRange, Timebase

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "omarchy-capture-screenrecording-edit"


def _load():
    """Import the CLI, which has no .py suffix because it is a user-facing command."""
    spec = importlib.util.spec_from_loader(
        "omarchy_edit_cli",
        importlib.machinery.SourceFileLoader("omarchy_edit_cli", str(SCRIPT)),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["omarchy_edit_cli"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


cli = _load()
TB = Timebase(30, 1)


# --- times ------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,frame",
    [
        ("0", 0),
        ("1", 30),
        ("1.5", 45),
        ("0:02", 60),
        ("1:05", 30 * 65),
        ("0:01:05", 30 * 65),
        ("1:02:03", 30 * 3723),
        ("90f", 90),
        ("0f", 0),
    ],
)
def test_human_times_land_on_the_frame_grid(spec, frame):
    assert cli.parse_time(spec, TB) == frame


def test_every_time_goes_through_the_timebase():
    """Not a style point: 29.97 rounds differently from 30, and a second seconds->frames
    path is how a boundary ends up off the grid."""
    ntsc = Timebase(30000, 1001)
    assert cli.parse_time("100", ntsc) == ntsc.to_frame(100.0) == 2997
    assert cli.parse_time("100", TB) == 3000


def test_a_frame_index_skips_the_conversion_entirely():
    ntsc = Timebase(30000, 1001)
    assert cli.parse_time("299f", ntsc) == 299


@pytest.mark.parametrize("spec", ["", "abc", "-1", "1:2:3:4", "1.2.3", "xf", "-5f"])
def test_bad_times_are_usage_errors_not_tracebacks(spec):
    with pytest.raises(cli.UsageError):
        cli.parse_time(spec, TB)


def test_a_cut_is_a_half_open_frame_range():
    assert cli.parse_cut("1-2", TB) == FrameRange(30, 60)
    assert cli.parse_cut("0:01-0:02", TB) == FrameRange(30, 60)
    assert cli.parse_cut("30f-60f", TB) == FrameRange(30, 60)


@pytest.mark.parametrize("spec", ["1", "2-1", "1-1", "1-2-3", "a-b"])
def test_bad_cuts_are_refused(spec):
    with pytest.raises(cli.UsageError):
        cli.parse_cut(spec, TB)


# --- webcam -----------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,frac", [("s", 0.18), ("m", 0.24), ("large", 0.32), ("25%", 0.25), ("0.4", 0.4)]
)
def test_webcam_sizes(spec, frac):
    assert cli.parse_size(spec) == pytest.approx(frac)


@pytest.mark.parametrize("spec", ["0", "0%", "120%", "1.5", "huge", ""])
def test_bad_webcam_sizes_are_refused(spec):
    with pytest.raises(cli.UsageError):
        cli.parse_size(spec)


def test_webcam_positions_accept_corners_and_coordinates():
    assert cli.parse_position("bottom-left") == "bottom-left"
    assert cli.parse_position("BR") == "br"
    assert cli.parse_position("0.1,0.2") == (0.1, 0.2)
    with pytest.raises(cli.UsageError):
        cli.parse_position("middle-ish")


def test_the_tile_is_square_in_pixels_not_in_normalized_units():
    """A circle mask is generated at the tile's pixel size, so a normalized square on a
    16:9 canvas would render an ellipse."""
    canvas = Canvas(1280, 720)
    x, y, w, h = cli.webcam_rect(canvas, frac=0.24, position="bottom-left")
    assert w * canvas.width == pytest.approx(h * canvas.height)
    assert w != pytest.approx(h)


def test_corners_put_the_tile_in_the_corner_they_name():
    canvas = Canvas(1280, 720)
    side = 0.24 * 720
    margin = cli.WEBCAM_MARGIN * 720
    for corner, want in (
        ("top-left", (margin, margin)),
        ("top-right", (1280 - side - margin, margin)),
        ("bottom-left", (margin, 720 - side - margin)),
        ("bottom-right", (1280 - side - margin, 720 - side - margin)),
    ):
        x, y, w, h = cli.webcam_rect(canvas, frac=0.24, position=corner)
        assert (x * 1280, y * 720) == pytest.approx(want, abs=0.5)


def test_a_tile_pushed_off_canvas_is_clamped_back_on():
    canvas = Canvas(1280, 720)
    x, y, w, h = cli.webcam_rect(canvas, frac=0.5, position=(0.95, 0.95))
    assert x + w == pytest.approx(1.0)
    assert y + h == pytest.approx(1.0)


def test_backdrop_modes():
    assert cli.parse_backdrop("off") == (False, None, None)
    assert cli.parse_backdrop("color") == (True, None, "")
    assert cli.parse_backdrop("color:#101010") == (True, "#101010", "")
    on, c, g = cli.parse_backdrop("gradient")
    assert (on, c, g) == (True, None, cli.DEFAULT_GRADIENT)
    assert cli.parse_backdrop("gradient:#111111,#222222") == (True, "#111111", "#222222")
    with pytest.raises(cli.UsageError):
        cli.parse_backdrop("stripes")


# --- applying to a bundle ---------------------------------------------------


@pytest.fixture
def bundle(tmp_path):
    synthetic.make_bundle(tmp_path / "b", media=False, seconds=2.0)
    return Bundle(tmp_path / "b")


def _args(**kw):
    ns = cli.build_parser().parse_args([])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_a_corner_and_a_size_become_webcam_settings(bundle):
    cli.apply_options(bundle, _args(webcam="bottom-left", webcam_size="m"))
    cam = bundle.edit.webcam
    assert cam.enabled
    assert cam.x * 1280 == pytest.approx(cli.WEBCAM_MARGIN * 720, abs=0.5)
    assert cam.w * 1280 == pytest.approx(0.24 * 720, abs=0.5)


def test_resizing_without_a_position_keeps_the_top_left_it_had(bundle):
    cli.apply_options(bundle, _args(webcam="top-left", webcam_size="s"))
    before = (bundle.edit.webcam.x, bundle.edit.webcam.y)
    cli.apply_options(bundle, _args(webcam_size="l"))
    assert (bundle.edit.webcam.x, bundle.edit.webcam.y) == before
    assert bundle.edit.webcam.w * 1280 == pytest.approx(0.32 * 720, abs=0.5)


def test_growing_a_far_corner_tile_stays_on_canvas(bundle):
    """Growing from the stored top-left would push a bottom-right tile off the frame;
    webcam_rect clamps instead, so it ends up flush with the edge rather than cropped."""
    cli.apply_options(bundle, _args(webcam="bottom-right", webcam_size="s"))
    cli.apply_options(bundle, _args(webcam_size="l"))
    cam = bundle.edit.webcam
    assert cam.x + cam.w == pytest.approx(1.0, abs=1e-9)
    assert cam.y + cam.h == pytest.approx(1.0, abs=1e-9)


def test_no_webcam_disables_without_moving_anything(bundle):
    before = (bundle.edit.webcam.x, bundle.edit.webcam.y, bundle.edit.webcam.w)
    cli.apply_options(bundle, _args(no_webcam=True))
    assert bundle.edit.webcam.enabled is False
    assert (bundle.edit.webcam.x, bundle.edit.webcam.y, bundle.edit.webcam.w) == before


def test_a_burned_in_camera_refuses_to_be_moved(tmp_path):
    """The pixels are already composited; offering a control that silently does nothing
    is worse than the error."""
    synthetic.make_bundle(tmp_path / "b", media=False, burned_in=True)
    b = Bundle(tmp_path / "b")
    with pytest.raises(cli.UsageError):
        cli.apply_options(b, _args(webcam="bottom-left"))


def test_zoom_hold_is_seconds_on_the_command_line_and_frames_in_the_model(bundle):
    cli.apply_options(bundle, _args(zoom="on", zoom_hold=0.8, zoom_amount=2.5))
    assert bundle.edit.zoom.enabled is True
    assert bundle.edit.zoom.hold_frames == 24
    assert bundle.edit.zoom.amount == 2.5


def test_a_zoom_amount_below_one_is_clamped_not_raised(bundle):
    """Zoom refuses to construct below 1.0; a script sweeping the amount must not die on
    its first step."""
    cli.apply_options(bundle, _args(zoom_amount=0.5))
    assert bundle.edit.zoom.amount == 1.0


def test_cuts_are_merged_on_insert(bundle):
    """A layer spanning ~30 separate output intervals approaches ffmpeg's 100-term
    expression budget; merging on insert is what keeps generated gates small."""
    cli.apply_options(bundle, _args(cut=["1-2", "2-3", "0.2-0.4"]))
    assert bundle.edit.cuts == [FrameRange(6, 12), FrameRange(30, 90)]


def test_reset_then_apply_starts_from_defaults(bundle):
    cli.apply_options(bundle, _args(cut=["1-2"], zoom="on"))
    bundle.save_edit()
    assert bundle.edit_path.exists()
    cli.apply_options(bundle, _args(reset=True, zoom="off"))
    assert bundle.edit.cuts == []
    assert bundle.edit.zoom.enabled is False


def test_backdrop_gradient_reaches_the_settings(bundle):
    cli.apply_options(bundle, _args(backdrop="gradient:#101010,#404040", padding=0.06, radius=0.03))
    bd = bundle.edit.backdrop
    assert (bd.enabled, bd.color, bd.gradient) == (True, "#101010", "#404040")
    assert (bd.padding, bd.corner_radius) == (0.06, 0.03)
    cli.apply_options(bundle, _args(backdrop="color"))
    assert bundle.edit.backdrop.gradient is None


# --- discovery --------------------------------------------------------------


def test_latest_picks_the_newest_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("OMARCHY_SCREENRECORD_DIR", str(tmp_path))
    for name, when in (("screenrecording-a", 1000), ("screenrecording-b", 2000)):
        synthetic.make_bundle(tmp_path / name, media=False)
        (tmp_path / name / "capture.json").touch()
        import os

        os.utime(tmp_path / name / "capture.json", (when, when))
    assert cli.locate_latest() == tmp_path / "screenrecording-b"


def test_locate_exits_one_when_there_are_no_recordings(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OMARCHY_SCREENRECORD_DIR", str(tmp_path))
    assert cli.main(["--locate"]) == 1
    assert capsys.readouterr().out == ""


def test_notify_is_a_no_op_without_notify_send(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    called = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: called.append(a))
    cli.notify("t", "b")
    assert called == []


def test_notify_shells_out_when_notify_send_exists(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/notify-send")
    seen = []
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: seen.append(argv))
    cli.notify("Recording exported", "/tmp/x.mp4")
    assert seen and seen[0][0] == "notify-send" and seen[0][-1] == "/tmp/x.mp4"


# --- end to end -------------------------------------------------------------

pytestmark_ffmpeg = pytest.mark.skipif(not synthetic.have_ffmpeg(), reason="needs ffmpeg")


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """One small bundle, rendered several ways. Module-scoped because each render is a
    real ffmpeg run and the assertions below only read the results."""
    if not synthetic.have_ffmpeg():
        pytest.skip("needs ffmpeg")
    root = tmp_path_factory.mktemp("cli")
    src = root / "bundle"
    synthetic.make_bundle(src, seconds=2.0, width=320, height=240, clicks=(0.4, 1.2))
    out = {}
    variants = {
        "bl": ["--webcam=bottom-left", "--webcam-size=m", "--zoom=off"],
        "tr": ["--webcam=top-right", "--webcam-size=m", "--zoom=off"],
        "nocam": ["--no-webcam", "--zoom=off"],
        "zoom": ["--no-webcam", "--zoom=on", "--zoom-amount=2.0"],
        "cut": ["--no-webcam", "--zoom=off", "--cut=0:00.5-0:01"],
    }
    for name, opts in variants.items():
        dst = root / f"{name}.mp4"
        rc = cli.main([str(src), "--reset", *opts, "-o", str(dst), "--json"])
        assert rc == 0, name
        out[name] = dst
    return root, src, out


def _frame(path: Path, frame: int, x: int, y: int, w: int, h: int) -> bytes:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-an",
         "-vf", f"select=eq(n\\,{frame}),crop={w}:{h}:{x}:{y},format=rgb24",
         "-fps_mode", "passthrough", "-frames:v", "1", "-f", "rawvideo", "-"],
        check=True, capture_output=True).stdout
    assert len(raw) == w * h * 3, f"{path} frame {frame}: {len(raw)} bytes"
    return raw


def _mad(a: bytes, b: bytes) -> float:
    """Mean absolute difference per channel byte. x264 puts a couple of units of noise on
    otherwise identical regions, so the threshold between 'same' and 'different' is a
    factor of ten, not zero."""
    return sum(abs(p - q) for p, q in zip(a, b)) / len(a)


def _tile_boxes() -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Sample boxes inside the bottom-left and top-right tiles of the 320x240 renders."""
    side = int(0.24 * 240)
    m = int(0.04 * 240)
    box = (side - 5) & ~1
    return (m + 2, 240 - side - m + 2, box, box), (320 - side - m + 2, m + 2, box, box)


def _frames(path: Path) -> int:
    return int(subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True).stdout.strip())


@pytestmark_ffmpeg
def test_print_plan_emits_a_graph_and_renders_nothing(rendered, capsys):
    root, src, _ = rendered
    never = root / "never.mp4"
    rc = cli.main([str(src), "--no-webcam", "--print-plan", "--json", "-o", str(never)])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    assert "fps=30/1" in emitted["graph"]
    assert emitted["graph"].rstrip().endswith("[aout]")
    # The graph is passed as a FILE: argv dies at ~288KB with E2BIG, and ffmpeg 9.0.1
    # has removed -filter_complex_script.
    assert "-/filter_complex" in emitted["argv"]
    assert not never.exists()


@pytestmark_ffmpeg
def test_the_render_completes_and_decodes(rendered):
    _, _, out = rendered
    for name, path in out.items():
        assert path.stat().st_size > 0, name
        subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                       check=True, capture_output=True)


@pytestmark_ffmpeg
def test_webcam_corners_put_the_camera_in_different_places(rendered):
    """Pixels, not settings. An option that is stored and then not applied looks correct
    in edit.json and wrong on screen."""
    root, src, out = rendered
    # A sample box just inside each tile, EVEN-sized: crop snaps odd dimensions down on
    # subsampled formats, and a short read is a confusing way to fail.
    bl, tr = _tile_boxes()
    f = 30
    bl_with = _mad(_frame(out["bl"], f, *bl), _frame(out["nocam"], f, *bl))
    bl_without = _mad(_frame(out["tr"], f, *bl), _frame(out["nocam"], f, *bl))
    tr_with = _mad(_frame(out["tr"], f, *tr), _frame(out["nocam"], f, *tr))
    tr_without = _mad(_frame(out["bl"], f, *tr), _frame(out["nocam"], f, *tr))
    assert bl_with > 10 * max(bl_without, 1.0)
    assert tr_with > 10 * max(tr_without, 1.0)


@pytestmark_ffmpeg
def test_no_webcam_leaves_the_camera_out_of_both_corners(rendered):
    """--no-webcam has to differ from BOTH placements: matching one of them would mean
    the flag had merely moved the tile somewhere else."""
    root, src, out = rendered
    bl, tr = _tile_boxes()
    f = 30
    assert _mad(_frame(out["nocam"], f, *bl), _frame(out["bl"], f, *bl)) > 10
    assert _mad(_frame(out["nocam"], f, *tr), _frame(out["tr"], f, *tr)) > 10
    assert _frames(out["nocam"]) == _frames(out["bl"])
    assert Bundle(src).edit.webcam.enabled is False


@pytestmark_ffmpeg
def test_the_zoom_that_renders_is_the_zoom_the_python_twin_predicts(rendered):
    """zoom_at is what the QML preview calls, so a divergence here is a preview that
    lies about the export."""
    from omarchy_studio import render as render_mod, zoom as zoom_mod

    root, src, out = rendered
    b = Bundle(src)
    b.edit.zoom.enabled = True
    b.edit.zoom.amount = 2.0
    b.edit.cuts = []
    segs = render_mod._segments(b, render_mod.effective_cutmap(b))
    assert segs, "the click track produced no zoom"
    on = off = 0
    for f in range(0, _frames(out["zoom"]), 3):
        scale = zoom_mod.zoom_at(segs, f).scale
        d = _mad(_frame(out["zoom"], f, 0, 0, 320, 240), _frame(out["nocam"], f, 0, 0, 320, 240))
        # The middle of the ease is deliberately unasserted: a 5% magnification is a real
        # difference but not a large one, and a threshold there would be arbitrary.
        if scale <= 1.005:
            assert d < 5, f"frame {f}: identity predicted, diff {d:.2f}"
            off += 1
        elif scale >= 1.3:
            assert d > 20, f"frame {f}: scale {scale:.2f} predicted, diff {d:.2f}"
            on += 1
    assert on > 3 and off > 3


@pytestmark_ffmpeg
def test_a_cut_removes_exactly_the_frames_it_names(rendered):
    root, src, out = rendered
    full, cut = _frames(out["nocam"]), _frames(out["cut"])
    assert full - cut == 15  # 0.5s at 30fps
    # And it removes the RIGHT frames: output frame 15 must be source frame 30.
    for out_f, src_f in ((0, 0), (14, 14), (15, 30), (30, 45)):
        same = _mad(_frame(out["cut"], out_f, 0, 0, 320, 240),
                    _frame(out["nocam"], src_f, 0, 0, 320, 240))
        other = _mad(_frame(out["cut"], out_f, 0, 0, 320, 240),
                     _frame(out["nocam"], src_f + 1, 0, 0, 320, 240))
        assert same < other, f"output {out_f} did not land on source {src_f}"


@pytestmark_ffmpeg
def test_json_status_is_machine_readable_and_reports_the_length(rendered, capsys, tmp_path):
    root, src, out = rendered
    rc = cli.main([str(src), "--no-render", "--json"])
    assert rc == 0
    state = json.loads(capsys.readouterr().out)
    assert state["ok"] is True
    assert state["canvas"] == {"width": 320, "height": 240}
    assert state["rendered"] is False


@pytestmark_ffmpeg
def test_an_impossible_edit_fails_loudly_in_json(rendered, capsys):
    root, src, out = rendered
    rc = cli.main([str(src), "--reset", "--cut=0-10", "--json", "-o", str(root / "x.mp4")])
    err = json.loads(capsys.readouterr().out)
    assert rc == 1 and err["ok"] is False and err["error"]
