from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import needs_ffmpeg

from omarchy_studio import probe, render
from omarchy_studio.project import Bundle, FrameRange, Layer

W, H, SECONDS = 320, 240, 2.0
FPS = synthetic.FPS
SOURCE_FRAMES = int(SECONDS * FPS)


@pytest.fixture(scope="module")
def recording(tmp_path_factory) -> Path:
    """One synthetic recording, reused: the lavfi encode is the slow part."""
    root = tmp_path_factory.mktemp("rec")
    synthetic.make_bundle(root, seconds=SECONDS, width=W, height=H)
    return root


@pytest.fixture
def bundle(recording, tmp_path) -> Bundle:
    """A private copy, so a test that writes edit.json cannot affect another."""
    dest = tmp_path / "rec"
    dest.mkdir()
    for sub in ("media", "events", "assets"):
        (dest / sub).mkdir(exist_ok=True)
    for p in Path(recording).rglob("*"):
        if p.is_file():
            target = dest / p.relative_to(recording)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())
    return Bundle(dest)


def set_clicks(bundle: Bundle, frames_and_pos) -> None:
    """Write an input.jsonl the events reader actually accepts (t_us, not t)."""
    anchor = bundle.capture.screen.anchor_us
    lines = [
        json.dumps({
            "t_us": anchor + round(f * 1e6 / FPS),
            "type": "click", "button": "left", "x": x, "y": y,
        })
        for f, x, y in frames_and_pos
    ]
    (bundle.events_dir / "input.jsonl").write_text("\n".join(lines) + "\n")


# --- plan assembly ----------------------------------------------------------


def test_a_plain_bundle_produces_a_runnable_plan(bundle):
    plan = render.build_graph(bundle)
    assert plan.total_frames == SOURCE_FRAMES
    assert "-i" in plan.inputs
    assert plan.maps == ["-map", "[vout]", "-map", "[aout]"]
    assert "[vout]" in plan.graph
    assert "libx264" in plan.output_args
    assert "h264_vaapi" not in plan.output_args


def test_the_screen_is_input_zero_and_the_camera_input_one(bundle):
    plan = render.build_graph(bundle)
    assert plan.input_specs[0][-1].endswith("screen.mp4")
    assert plan.input_specs[1][-1].endswith("camera.mp4")


def test_a_burned_in_camera_is_never_opened(tmp_path):
    root = tmp_path / "burned"
    synthetic.make_bundle(root, seconds=SECONDS, width=W, height=H, burned_in=True)
    plan = render.build_graph(Bundle(root))
    assert len(plan.input_specs) == 1
    assert "camera" not in plan.graph


def test_disabling_the_webcam_drops_the_camera_input(bundle):
    bundle.edit.webcam.enabled = False
    plan = render.build_graph(bundle)
    assert len(plan.input_specs) == 1


# --- ordering ---------------------------------------------------------------


def test_the_cut_comes_before_the_zoom_and_the_layers(bundle):
    """2.20 s against 2.83 s on the same project: every stage downstream of the cut
    then runs over fewer frames."""
    bundle.edit.cuts = [FrameRange(10, 20)]
    bundle.edit.zoom.enabled = True
    set_clicks(bundle, [(40, 280, 180)])
    bundle.edit.layers = [Layer(id="box", type="shape", x=0.1, y=0.1, w=0.2, h=0.2)]
    g = render.build_graph(bundle).graph
    assert g.index("trim=start_frame=") < g.index("perspective")
    assert g.index("perspective") < g.index("overlay")


def test_every_time_varying_input_is_cut_including_the_camera(bundle):
    """An uncut camera drifts by exactly the total cut duration -- measured at exactly
    -90 and -150 frames for 3 s and 5 s of cuts."""
    bundle.edit.cuts = [FrameRange(10, 20)]
    g = render.build_graph(bundle).graph
    assert g.count("concat=n=") >= 2, "the camera was not put through the cut"
    assert "cam" in g


def test_no_cuts_means_no_cut_chain_at_all(bundle):
    g = render.build_graph(bundle).graph
    assert "trim=start_frame=" not in g
    assert "concat=n=" not in g


# --- camera alignment -------------------------------------------------------


def test_a_late_camera_is_padded_by_whole_frames(bundle):
    """The camera anchor is 120 ms after the screen's, which is 3.6 frames at 30 fps.

    `tpad`/`trim` rather than `-itsoffset`: measured on this build, `-itsoffset 0.2`
    into `fps=30` delays by exactly the 6 frames asked for, but `-itsoffset -0.2`
    advances by 4 rather than 6.
    """
    assert bundle.camera_offset_frames() == 4
    g = render.build_graph(bundle).graph
    assert "tpad=start=4:start_mode=clone" in g
    assert "-itsoffset" not in render.build_graph(bundle).inputs


def test_an_early_camera_has_its_head_dropped(bundle):
    bundle.capture.camera.anchor_us = bundle.capture.screen.anchor_us - 100_000
    assert bundle.camera_offset_frames() == -3
    g = render.build_graph(bundle).graph
    assert "trim=start_frame=3,setpts=PTS-STARTPTS" in g


def test_a_camera_on_the_same_anchor_needs_no_alignment(bundle):
    bundle.capture.camera.anchor_us = bundle.capture.screen.anchor_us
    g = render.build_graph(bundle).graph
    assert "tpad=" not in g


# --- audio ------------------------------------------------------------------


def test_the_pop_mute_runs_before_the_cut_and_loudnorm_after(bundle):
    """The pop belongs to the capture's start, not the edited timeline's, so it is
    muted in source time. loudnorm measures the material it normalizes, so it runs on
    the finished audio."""
    bundle.edit.cuts = [FrameRange(10, 20)]
    g = render.build_graph(bundle).graph
    assert g.index("afade=t=in:st=0.4") < g.index("atrim=")
    assert g.index("atrim=") < g.index("loudnorm")


def test_loudnorm_is_skipped_when_normalization_is_off(bundle):
    bundle.edit.normalize_audio = False
    g = render.build_graph(bundle).graph
    assert "loudnorm" not in g
    assert "afade=t=in:st=0.4" in g, "the pop mute is not a normalization option"


def test_aselect_is_never_used(bundle):
    """aselect quantizes audio removal to whole ~21.3 ms decoder frames regardless of
    the video grid, which accumulated ~50 ms of A/V skew over six cuts."""
    bundle.edit.cuts = [FrameRange(10, 20)]
    g = render.build_graph(bundle).graph
    assert "aselect" not in g and "select=" not in g


# --- head trim --------------------------------------------------------------


def test_the_head_trim_is_applied_only_when_there_are_warm_up_packets(bundle, monkeypatch):
    monkeypatch.setattr(probe, "has_discardable_warmup", lambda p: False)
    assert render.build_graph(bundle).head_trim_seconds == 0.0
    assert "-ss" not in render.build_graph(bundle).inputs

    monkeypatch.setattr(probe, "has_discardable_warmup", lambda p: True)
    plan = render.build_graph(bundle)
    assert plan.head_trim_seconds == pytest.approx(0.1)
    assert "-ss" in plan.inputs


def test_trim_head_frames_becomes_a_cut_not_a_seek(bundle):
    """A head trim is an excision like any other; an `-ss` would renumber frame 0 and
    slide every cut and layer index authored against the untrimmed recording."""
    bundle.edit.trim_head_frames = 9
    cutmap = render.effective_cutmap(bundle)
    assert cutmap.cuts[0] == FrameRange(0, 9)
    assert render.build_graph(bundle).total_frames == SOURCE_FRAMES - 9


# --- rendering --------------------------------------------------------------


@needs_ffmpeg
def test_a_plain_render_has_exactly_the_frames_the_cut_map_promised(bundle, tmp_path):
    out = render.render(bundle, tmp_path / "out.mp4")
    assert out.exists()
    assert probe.frame_count(out) == SOURCE_FRAMES
    assert probe.has_audio(out)
    assert probe.dimensions(out) == (W, H)


@needs_ffmpeg
def test_a_cut_shortens_the_output_by_exactly_the_cut_length(bundle, tmp_path):
    bundle.edit.cuts = [FrameRange(10, 25), FrameRange(40, 50)]
    plan = render.build_graph(bundle)
    assert plan.total_frames == SOURCE_FRAMES - 25
    out = render.render(bundle, tmp_path / "cut.mp4")
    assert probe.frame_count(out) == SOURCE_FRAMES - 25


@needs_ffmpeg
def test_the_full_stack_renders(bundle, tmp_path):
    """Cuts, auto-zoom, backdrop, webcam and an overlay in one graph."""
    bundle.edit.cuts = [FrameRange(12, 20)]
    bundle.edit.zoom.enabled = True
    set_clicks(bundle, [(35, 280, 180), (38, 285, 185)])
    bundle.edit.backdrop.enabled = True
    bundle.edit.layers = [
        Layer(id="tag", type="shape", x=0.05, y=0.05, w=0.2, h=0.1,
              t=FrameRange(5, 45), fade_frames=3),
    ]
    plan = render.build_graph(bundle)
    assert "perspective" in plan.graph
    out = render.render(bundle, tmp_path / "full.mp4")
    assert probe.frame_count(out) == plan.total_frames
    assert probe.dimensions(out) == (W, H)


@needs_ffmpeg
def test_the_backdrop_actually_paints_and_insets_the_video(bundle, tmp_path):
    """A correct frame count proves the timebase, not the picture. This checks the
    corner is the backdrop colour and the centre is not."""
    bundle.edit.backdrop.enabled = True
    bundle.edit.backdrop.color = "#1b1d24"
    bundle.edit.backdrop.gradient = None
    bundle.edit.webcam.enabled = False
    out = render.render(bundle, tmp_path / "bd.mp4")
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(out), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    corner = tuple(raw[0:3])
    centre = tuple(raw[((H // 2) * W + W // 2) * 3:][:3])
    assert all(abs(a - b) <= 6 for a, b in zip(corner, (0x1B, 0x1D, 0x24))), corner
    assert corner != centre, "the video was not inset onto the backdrop"


@needs_ffmpeg
def test_a_gradient_backdrop_paints_its_own_stops_and_paints_them_twice(bundle, tmp_path):
    """Two things only rendered pixels can prove about `gradients`.

    That the CSS line is right: the first and last stop land exactly ON the two extreme
    corners, which is the whole point of sizing the line by |W sin| + |H cos| instead of
    aiming it at two corners.

    And that it is the SAME gradient every time. Any endpoint outside [0, size-1] is
    silently replaced with a random one, so the shipped `x1=W:y1=H` drew a different
    backdrop on every export -- six runs, six frames -- with nothing in the log.
    """
    bundle.edit.backdrop.enabled = True
    bundle.edit.backdrop.background = "fog"  # 170 deg, #dcd8d1 -> #b4b0a9
    bundle.edit.backdrop.shadow = False  # so the corners are the ground and nothing else
    bundle.edit.webcam.enabled = False

    def corners(path):
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True,
        ).stdout
        return tuple(raw[0:3]), tuple(raw[(H * W - 1) * 3:][:3])

    first = corners(render.render(bundle, tmp_path / "grad.mp4"))
    assert all(abs(a - b) <= 8 for a, b in zip(first[0], (0xDC, 0xD8, 0xD1))), first[0]
    assert all(abs(a - b) <= 8 for a, b in zip(first[1], (0xB4, 0xB0, 0xA9))), first[1]
    assert corners(render.render(bundle, tmp_path / "grad2.mp4")) == first


@needs_ffmpeg
def test_progress_is_reported_and_finishes_at_the_total(bundle, tmp_path):
    seen: list[tuple[int, int]] = []
    render.render(bundle, tmp_path / "p.mp4", progress=lambda d, t: seen.append((d, t)))
    assert seen, "ffmpeg's -progress output was never parsed"
    assert seen[-1] == (SOURCE_FRAMES, SOURCE_FRAMES)
    assert all(0 <= d <= t for d, t in seen)


@needs_ffmpeg
def test_a_render_that_fails_says_why(bundle, tmp_path):
    plan = render.build_graph(bundle)
    plan.graph = "[0:v]nosuchfilter=1[vout]"
    with pytest.raises(render.RenderError) as excinfo:
        render.run_plan(plan, tmp_path / "bad.mp4")
    assert "nosuchfilter" in str(excinfo.value)


@needs_ffmpeg
def test_a_project_cut_to_nothing_is_refused(bundle):
    bundle.edit.cuts = [FrameRange(0, SOURCE_FRAMES)]
    with pytest.raises(render.RenderError):
        render.build_graph(bundle)


@needs_ffmpeg
def test_the_proxy_draft_is_short_gop(bundle, tmp_path):
    plan = render.build_graph(bundle, for_proxy=True)
    assert "-g" in plan.output_args
    assert plan.output_args[plan.output_args.index("-g") + 1] == "15"
    assert plan.output_args[plan.output_args.index("-bf") + 1] == "0"
    out = render.render(bundle, tmp_path / "draft.mp4", for_proxy=True)
    assert probe.frame_count(out) == SOURCE_FRAMES


@needs_ffmpeg
def test_an_unknown_layer_type_degrades_rather_than_failing(bundle, tmp_path):
    """A project written by a newer build must lose overlays, not refuse to render."""
    bundle.edit.layers = [Layer(id="future", type="hologram")]
    with pytest.warns(UserWarning):
        out = render.render(bundle, tmp_path / "fwd.mp4")
    assert probe.frame_count(out) == SOURCE_FRAMES
