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
def test_the_downscale_is_the_last_thing_before_the_encoder(tmp_path):
    """Everything above it -- the zoom especially -- composes at full resolution.

    Scaling earlier would magnify an already-resampled frame, which is the whole reason
    capturing native is worth it.
    """
    g = _graph(tmp_path, "1080p", width=3840, height=2160)
    assert "scale=-2:1080:flags=lanczos,format=yuv420p[vout]" in g


@needs_ffmpeg
def test_native_emits_no_scale_at_all(tmp_path):
    g = _graph(tmp_path, "native", width=3840, height=2160)
    assert "lanczos" not in g
    assert "format=yuv420p[vout]" in g


@needs_ffmpeg
def test_a_capture_smaller_than_the_preset_emits_no_scale(tmp_path):
    g = _graph(tmp_path, "4k", width=1280, height=720)
    assert "lanczos" not in g
