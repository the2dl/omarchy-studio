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
        ok = hud.place("0xabc", 322, 56, **kwargs)
        if not ok:
            return None, None, asked.get("notified", False)
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


def test_with_nowhere_to_go_the_hud_steps_aside(placed, monkeypatch, hud):
    """A user recording a near-fullscreen window got the Stop button welded into the
    take. settle() already answers this question the other way one line up --
    "Nothing is worth a Stop button in the video" -- and this used to disagree with
    it. The take is the irreplaceable thing; the pill is a convenience."""
    monkeypatch.setattr(hud, "stop_binding", lambda: "SUPER + SHIFT + 4")
    x, y, notified = placed(backend="kms", capture_rect="2560x1440+0+0", monitor="DP-1")
    assert (x, y) == (None, None), "it must not be placed at all"
    assert notified, "and the user is told how to stop instead"


def test_a_near_fullscreen_window_is_the_real_case(placed, monkeypatch, hud):
    """2538x1384 at 11,45 on a 2560x1440 display: eleven pixels of margin. This is
    what the user actually recorded."""
    monkeypatch.setattr(hud, "stop_binding", lambda: "SUPER + SHIFT + 4")
    x, y, _ = placed(backend="kms", capture_rect="2538x1384+11+45", monitor="DP-1")
    assert (x, y) == (None, None)


def test_without_a_way_to_stop_it_stays_and_warns(placed, monkeypatch, hud):
    """With no binding the HUD IS the only stop, and then a spoiled take beats an
    unstoppable one. The old behaviour, kept for exactly this case."""
    monkeypatch.setattr(hud, "stop_binding", lambda: "")
    x, y, notified = placed(backend="kms", capture_rect="2560x1440+0+0", monitor="DP-1")
    assert notified
    assert x is not None and 0 <= y <= 1440 - 56, "it must still be on screen"


def test_a_capture_it_can_sit_beside_is_untouched(placed, monkeypatch, hud):
    """The property that makes this safe: room means placed, as before."""
    monkeypatch.setattr(hud, "stop_binding", lambda: "SUPER + SHIFT + 4")
    x, y, notified = placed(backend="kms", capture_rect="1263x900+11+45", monitor="DP-1")
    assert x is not None and not notified


def test_it_never_leaves_the_monitor(placed, monkeypatch, hud):
    monkeypatch.setattr(hud, "stop_binding", lambda: "")
    for rect in ("1263x900+11+45", "1263x1200+11+240", "2560x1440+0+0", "400x300+2100+1100"):
        x, y, _ = placed(backend="kms", capture_rect=rect, monitor="DP-1")
        assert 0 <= x <= 2560 - 322, rect
        assert 0 <= y <= 1440 - 56, rect


# --- naming the way out ------------------------------------------------------


def _binds(*rows):
    return json.dumps([{"description": d, "key": k, "modmask": m} for d, k, m in rows])


def test_the_binding_is_found_by_description(hud, monkeypatch):
    """Matched on the description, not the command: the command is an absolute path
    that differs between a packaged install and a working tree."""
    monkeypatch.setattr(hud, "hypr",
                        lambda *a: _binds(("Screenrecording (studio)", "4", 65)))
    assert hud.stop_binding() == "SUPER + SHIFT + 4"


def test_a_keycode_only_bind_still_gets_an_answer(hud, monkeypatch):
    """Their real binding is `SUPER + SHIFT + code:13`, which reports no key name.
    Naming nothing would be worse than naming the thing they pressed."""
    monkeypatch.setattr(hud, "hypr",
                        lambda *a: _binds(("Screenrecording (studio)", "", 65)))
    assert hud.stop_binding() == "the shortcut you started with"


def test_no_recording_binding_at_all_is_an_empty_answer(hud, monkeypatch):
    monkeypatch.setattr(hud, "hypr", lambda *a: _binds(("Toggle scratchpad", "S", 64)))
    assert hud.stop_binding() == ""


def test_unparseable_binds_do_not_raise(hud, monkeypatch):
    monkeypatch.setattr(hud, "hypr", lambda *a: "not json")
    assert hud.stop_binding() == ""


# --- a stop that does not take must still close the window --------------------


def test_wait_gone_reports_exit(hud):
    """The watchdog polls rather than waits: it runs on a daemon thread, where
    proc.wait() would outlive the reason for waiting."""
    class P:
        def __init__(self, codes): self.codes = list(codes)
        def poll(self): return self.codes.pop(0) if self.codes else 0
    assert hud._wait_gone(P([None, None, 0]), 2.0) is True


def test_wait_gone_reports_still_running(hud):
    class P:
        def poll(self): return None
    assert hud._wait_gone(P(), 0.6) is False


def test_the_stop_is_detached_and_holds_no_pipe(hud):
    """The recorder's stop kills this HUD partway through, and this process reads its
    output. With a pipe, that kill closed the read end and the recorder took SIGPIPE
    before opening the editor -- the take finished and nothing appeared."""
    src = (REPO / "bin" / "omarchy-recording-hud").read_text()
    block = src[src.index("def stop_recording"):src.index("def act(")]
    # Code only. The comment above this call explains the SIGPIPE it was written to
    # prevent, and a naive substring search matches its prose instead of the call.
    code = "\n".join(l for l in block.splitlines() if not l.strip().startswith("#"))
    assert "start_new_session=True" in code
    assert code.count("subprocess.DEVNULL") >= 3, "stdin, stdout and stderr"
    assert "capture_output" not in code
    assert "subprocess.PIPE" not in code
