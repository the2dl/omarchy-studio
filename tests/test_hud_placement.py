"""Where the recording HUD puts itself, and why it is not always the spec's answer.

Spec §1c says bottom centre, 26px from the edge. That is only correct when the capture
cannot see the HUD. On a KMS capture it can: gpu-screen-recorder's KMS backend reads the
DRM scanout BELOW the compositor, so `no_screen_share` does nothing to it -- verified by
finding the HUD, Stop button and all, burned into a window recording. On that backend the
pill has to be physically outside the captured rectangle.

These test the geometry only. `place()` ends in an hyprctl dispatch, so the tests drive
it with a stubbed compositor and assert on the coordinates it asks for.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "omarchy-recording-hud"

MONITOR = [{"name": "DP-1", "x": 0, "y": 0, "width": 5120, "height": 2880,
            "scale": 2.0, "focused": True}]          # 2560x1440 logical


@pytest.fixture(scope="module")
def hud():
    spec = importlib.util.spec_from_loader(
        "omarchy_hud", importlib.machinery.SourceFileLoader("omarchy_hud", str(SCRIPT)))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["omarchy_hud"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def placed(hud, monkeypatch):
    """Run place() against a stub compositor and return the (x, y) it asked for."""
    asked = {}

    def fake_hypr(*args):
        return json.dumps(MONITOR) if args[:2] == ("monitors", "-j") else ""

    def fake_run(argv, **kwargs):
        asked["argv"] = argv
        return None

    monkeypatch.setattr(hud, "hypr", fake_hypr)
    monkeypatch.setattr(hud.subprocess, "run", fake_run)
    monkeypatch.setattr(hud, "notify", lambda *a, **k: asked.setdefault("notified", True))

    def go(**kwargs):
        asked.clear()
        hud.place("0xabc", 322, 56, **kwargs)
        cmd = asked["argv"][-1]
        x = int(cmd.split("x = ")[1].split(",")[0])
        y = int(cmd.split("y = ")[1].split(",")[0])
        return x, y, asked.get("notified", False)

    return go


def test_rect_parsing(hud):
    assert hud.parse_rect("1263x1384+11+45") == (11, 45, 1263, 1384)
    # Negative origins are legal and normal: a monitor left of or above the primary one
    # has them, and the capture rect is in absolute logical desktop coordinates.
    assert hud.parse_rect("800x600+-100+-50") == (-100, -50, 800, 600)
    assert hud.parse_rect("") is None
    assert hud.parse_rect("garbage") is None


def test_without_a_capture_rect_it_is_the_specs_bottom_centre(placed):
    x, y, _ = placed()
    assert x == (2560 - 322) // 2
    assert y == 1440 - 56 - 26


def test_the_portal_backend_leaves_it_where_the_spec_wants_it(placed):
    """The compositor keeps a no_screen_share window out of portal frames, so there is
    nothing to dodge -- moving it would be worse placement for no reason."""
    x, y, _ = placed(backend="portal", capture_rect="1263x1384+11+45", monitor="DP-1")
    assert (x, y) == ((2560 - 322) // 2, 1440 - 56 - 26)


def test_a_kms_capture_pushes_it_below_the_captured_rectangle(placed):
    # A window occupying the top 900px of the display leaves room underneath.
    x, y, notified = placed(backend="kms", capture_rect="1263x900+11+45", monitor="DP-1")
    assert y == 45 + 900 + 26, "the pill must sit under the capture, not over it"
    assert y + 56 <= 1440
    assert not notified


def test_it_goes_above_when_there_is_no_room_below(placed):
    # A capture pinned to the bottom of the display.
    x, y, notified = placed(backend="kms", capture_rect="1263x1200+11+240", monitor="DP-1")
    assert y + 56 <= 240, "with nothing below, the pill belongs above the capture"
    assert not notified


def test_a_full_screen_kms_capture_says_so_rather_than_hiding_it(placed):
    """Nowhere to put it. The HUD stays anyway -- a recording with no stop button is
    worse than one with a pill in the corner -- but the user is told, because the
    alternative is finding out in the export."""
    x, y, notified = placed(backend="kms", capture_rect="2560x1440+0+0", monitor="DP-1")
    assert notified
    assert 0 <= y <= 1440 - 56, "it must still be on screen"


def test_it_never_leaves_the_monitor(placed):
    for rect in ("1263x900+11+45", "1263x1200+11+240", "2560x1440+0+0", "400x300+2100+1100"):
        x, y, _ = placed(backend="kms", capture_rect=rect, monitor="DP-1")
        assert 0 <= x <= 2560 - 322, rect
        assert 0 <= y <= 1440 - 56, rect
