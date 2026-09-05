"""The export size is chosen at EXPORT, not at capture.

Capture runs at the panel's native grid (capture.capture_size returns None for a 5K
panel and capture_codec names hevc for it), which is right for a master and wrong as a
default for a deliverable: h264 above 4096 is refused by many players and the encode is
punishing. So the size is a preset resolved against the canvas here.
"""
from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import needs_ffmpeg

from omarchy_studio.project import Bundle

from omarchy_studio import render
from omarchy_studio.geometry import Canvas
from omarchy_studio.project import (DEFAULT_EXPORT_PRESET, EXPORT_PRESETS, Edit)


FIVE_K = Canvas(5120, 2880)
FULL_HD = Canvas(1920, 1080)


@pytest.mark.parametrize("preset,expected", [
    ("1080p", 1080),
    ("1440p", 1440),
    ("4k", 2160),
    ("native", None),
])
def test_a_preset_resolves_to_its_height_against_a_5k_capture(preset, expected):
    assert render.export_height(preset, FIVE_K) == expected


@pytest.mark.parametrize("preset", EXPORT_PRESETS)
def test_a_preset_never_upscales(preset):
    """A preset is a ceiling, not a target.

    Asking for 1440p from a 1080p capture has to give 1080p: inventing pixels costs
    render time and file size to make the picture no better.
    """
    assert render.export_height(preset, FULL_HD) is None


def test_an_unknown_preset_exports_native_rather_than_raising():
    """A bad string must not be able to stop an export -- same rule as Edit.from_dict."""
    assert render.export_height("720p-ish", FIVE_K) is None


def test_the_default_is_a_size_every_player_takes():
    assert DEFAULT_EXPORT_PRESET in EXPORT_PRESETS
    assert render.export_height(DEFAULT_EXPORT_PRESET, FIVE_K) == 1440


def test_the_preset_survives_a_round_trip_through_edit_json():
    e = Edit()
    e.export_preset = "4k"
    assert Edit.from_dict(e.to_dict()).export_preset == "4k"


def test_a_bundle_written_before_presets_existed_gets_the_default():
    assert Edit.from_dict({}).export_preset == DEFAULT_EXPORT_PRESET


def test_a_hand_edited_nonsense_preset_falls_back_instead_of_raising():
    assert Edit.from_dict({"export_preset": "huge"}).export_preset == DEFAULT_EXPORT_PRESET


# --- the filtergraph --------------------------------------------------------


def _graph(tmp_path, preset: str, *, width: int, height: int) -> str:
    root = tmp_path / preset
    synthetic.make_bundle(root, seconds=1.0, width=width, height=height, camera=False)
    b = Bundle(root)
    b.edit.export_preset = preset
    return render.build_graph(b).graph


@needs_ffmpeg
def test_the_picture_is_resampled_exactly_once(tmp_path):
    """The invariant that matters for text is ONE resample, not a late one.

    This used to assert the downscale was the last filter before the encoder, on the
    theory that composing at the master's resolution and squeezing at the end was the
    way to keep text crisp. It was not even true: `_backdrop` scales the inset as well,
    so with a backdrop on -- the default -- every frame went through two resamples.
    Composing at the delivered size means the one remaining scale takes the zoomed
    master straight to its final size.
    """
    g = _graph(tmp_path, "1080p", width=3840, height=2160)
    assert g.count("lanczos") == 1, g
    assert "scale=1920:1080:flags=lanczos" in g
    assert "format=yuv420p[vout]" in g
    # and nothing rescales it afterwards
    assert "scale" not in g.split("[fitted]")[-1]


@needs_ffmpeg
def test_the_backdrop_inset_is_the_only_scale_when_there_is_a_backdrop(tmp_path):
    """With a backdrop the inset scale IS the downscale; adding another would resample
    the picture twice, which is the bug this replaced."""
    root = tmp_path / "bd"
    synthetic.make_bundle(root, seconds=1.0, width=3840, height=2160, camera=False)
    b = Bundle(root)
    b.edit.export_preset = "1080p"
    b.edit.backdrop.enabled = True
    g = render.build_graph(b).graph
    assert "[fitted]" not in g, "a second downscale crept back in"
    assert g.count("flags=lanczos") == 1, g


@needs_ffmpeg
def test_a_redaction_hides_the_same_pixels_at_any_composite_size(tmp_path):
    """The blur sigma is in pixels while the composite canvas is not, so it has to be
    scaled or a 1440p composite blurs twice as much of the frame as the 5K master did --
    and the proxy and the export would disagree about what a redaction hides."""
    from omarchy_studio.geometry import ffmpeg_blur

    full = ffmpeg_blur("strong", 1.0)
    half = ffmpeg_blur("strong", 0.5)
    assert full != half
    sig = lambda f: float(f.split("sigma=")[1].split(":")[0])
    assert abs(sig(half) * 2 - sig(full)) < 1e-6


@needs_ffmpeg
def test_native_emits_no_scale_at_all(tmp_path):
    g = _graph(tmp_path, "native", width=3840, height=2160)
    assert "lanczos" not in g
    assert "format=yuv420p[vout]" in g


@needs_ffmpeg
def test_a_capture_smaller_than_the_preset_emits_no_scale(tmp_path):
    g = _graph(tmp_path, "4k", width=1280, height=720)
    assert "lanczos" not in g
