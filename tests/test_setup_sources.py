"""The pre-record setup window's seams: source enumeration and the stdout contract.

Two halves. The pure half (setup_sources) is tested exhaustively because it defines
the one line of JSON bin/omarchy-capture-screenrecording consumes -- a malformed
target there poisons the recorder with no UI left on screen to notice. The bridge
half drives bin/omarchy-capture-setup's real HTTP server with a fake QML client, so
the token gate and the /done validation are exercised on the wire, not by calling
the functions they guard.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from omarchy_studio import setup_sources as ss

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "omarchy-capture-setup"


# --- monitors ----------------------------------------------------------------


def mon(**over):
    base = {"name": "DP-1", "width": 5120, "height": 2880, "scale": 2.0,
            "x": 0, "y": 0, "transform": 0, "refreshRate": 60.0,
            "focused": True, "activeWorkspace": {"id": 2, "name": "2"}}
    base.update(over)
    return base


def test_monitor_logical_divides_by_scale():
    assert ss.monitor_logical(mon()) == (0, 0, 2560, 1440)


def test_monitor_logical_swaps_for_rotation():
    # Transforms 1 and 3 are 90/270 degrees: the logical rect is portrait.
    assert ss.monitor_logical(mon(transform=1)) == (0, 0, 1440, 2560)
    assert ss.monitor_logical(mon(transform=3)) == (0, 0, 1440, 2560)
    assert ss.monitor_logical(mon(transform=2)) == (0, 0, 2560, 1440)


def test_monitor_logical_floors_fractional_scale():
    # 1920 / 1.25 = 1536 exactly; 1000/1.5 floors to 666 as capture-region does.
    m = mon(width=1000, height=900, scale=1.5)
    assert ss.monitor_logical(m)[2:] == (666, 600)


def test_monitors_targets_and_refresh():
    out = ss.monitors([mon(), mon(name="HDMI-A-1", x=2560, focused=False,
                                  refreshRate=143.996)])
    assert [m["target"] for m in out] == ["monitor:DP-1", "monitor:HDMI-A-1"]
    assert out[1]["refresh"] == 144
    assert out[1]["x"] == 2560


def test_active_workspace_is_focused_monitors():
    mons = [mon(focused=False, activeWorkspace={"id": 7, "name": "7"}), mon()]
    assert ss.active_workspace(mons) == 2
    assert ss.active_workspace([mon(focused=False)]) is None


# --- windows -----------------------------------------------------------------


def client(**over):
    base = {"class": "foot", "title": "shell", "at": [11, 45],
            "size": [2538, 686], "workspace": {"id": 2}, "hidden": False}
    base.update(over)
    return base


def test_windows_filters_workspace_and_hidden():
    clients = [
        client(),
        client(title="elsewhere", workspace={"id": 1}),
        client(title="special", workspace={"id": -98}),
        client(title="ghost", hidden=True, at=[0, 0]),
    ]
    out = ss.windows(clients, 2)
    assert [w["title"] for w in out] == ["shell"]
    assert out[0]["target"] == "region:2538x686+11+45"


def test_windows_dedupes_identical_geometry_keeping_first():
    # Stacked/grouped windows at one rectangle are one capture choice; the first
    # client wins, exactly as capture-region's `unique` collapse behaves.
    out = ss.windows([client(title="a"), client(title="b")], 2)
    assert [w["title"] for w in out] == ["a"]


def test_windows_none_workspace_yields_nothing():
    assert ss.windows([client()], None) == []


def test_windows_drops_degenerate_rects():
    assert ss.windows([client(size=[0, 686])], 2) == []


# --- cameras -----------------------------------------------------------------


def test_cameras_parses_webcam_list_output():
    listing = "/dev/video0  Studio Display: Studio Display  (usb-0000:0d:00.0-5.4):\n"
    cams = ss.cameras(listing)
    assert cams[0]["device"] == "/dev/video0"
    assert cams[0]["name"].startswith("Studio Display")


def test_cameras_ignores_noise():
    assert ss.cameras("no cameras\n\n/dev/media0  not a video node\n") == []


# --- targets -----------------------------------------------------------------


def test_region_target_passes_monitors_through():
    assert ss.region_target("monitor:DP-1\n") == "monitor:DP-1"


def test_region_target_rewrites_slurp_geometry():
    assert ss.region_target("200,200 1600x900") == "region:1600x900+200+200"
    # Multi-monitor layouts put monitors at negative origins.
    assert ss.region_target("-1920,0 1920x1080") == "region:1920x1080+-1920+0"


@pytest.mark.parametrize("bad", ["", "monitor:", "1600x900", "x,y wxh",
                                 "200,200 1600×900"])
def test_region_target_rejects_noise(bad):
    with pytest.raises(ValueError):
        ss.region_target(bad)


def test_parse_target_round_trips_every_form():
    assert ss.parse_target("monitor:DP-1") == {"kind": "monitor", "name": "DP-1"}
    assert ss.parse_target("region:1600x900+200+200") == {
        "kind": "region", "x": 200, "y": 200, "width": 1600, "height": 900}
    assert ss.parse_target("region:1920x1080+-1920+0")["x"] == -1920
    assert ss.parse_target("camera:/dev/video0") == {
        "kind": "camera", "device": "/dev/video0"}


@pytest.mark.parametrize("bad", ["", "monitor:", "camera:", "camera:/dev/null",
                                 "region:0x0+0+0", "region:ax9+0+0", "desktop"])
def test_parse_target_rejects_malformed(bad):
    with pytest.raises(ValueError):
        ss.parse_target(bad)


# --- the stdout contract ------------------------------------------------------


def test_config_has_every_key_always():
    c = ss.config("monitor:DP-1", mic=True, desktop_audio=False,
                  camera="off", camera_device=None)
    assert c == {"target": "monitor:DP-1", "mic": True, "desktop_audio": False,
                 "camera": "off", "camera_device": None, "countdown": 3}


def test_config_carries_the_countdown():
    c = ss.config("monitor:DP-1", True, False, "off", None, countdown=0)
    assert c["countdown"] == 0


@pytest.mark.parametrize("bad", [-1, 11, 2.5, "3"])
def test_config_rejects_bad_countdown(bad):
    # The countdown is a timing promise to the consumer (surfaces gone within N
    # seconds of the printed line); a nonsense value breaks trims downstream.
    with pytest.raises(ValueError):
        ss.config("monitor:DP-1", True, False, "off", None, countdown=bad)


def test_config_rejects_bad_camera_mode():
    with pytest.raises(ValueError):
        ss.config("monitor:DP-1", True, False, "bubble", None)


def test_config_rejects_overlay_without_device():
    with pytest.raises(ValueError):
        ss.config("monitor:DP-1", True, False, "circle", None)


def test_config_forces_overlay_off_for_camera_target():
    # Recording the camera full-frame and overlaying it on itself is meaningless;
    # the contract guards it even if the UI regresses.
    c = ss.config("camera:/dev/video1", False, False, "corner", "/dev/video0")
    assert c["camera"] == "off"
    assert c["camera_device"] == "/dev/video1"


def test_config_is_one_json_line():
    c = ss.config("region:1600x900+200+200", True, True, "corner", "/dev/video0")
    line = json.dumps(c)
    assert "\n" not in line
    assert json.loads(line) == c


# --- the bridge, on the wire --------------------------------------------------


def _load_cli():
    """Import the launcher, which has no .py suffix because it is a command."""
    spec = importlib.util.spec_from_loader(
        "omarchy_capture_setup",
        importlib.machinery.SourceFileLoader("omarchy_capture_setup", str(SCRIPT)),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["omarchy_capture_setup"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def cli():
    return _load_cli()


@pytest.fixture()
def bridge(cli, tmp_path):
    session = cli.SetupSession(tmp_path, countdown=3)
    session.sources = {"monitors": [{"name": "DP-1"}], "windows": [],
                       "cameras": [], "mic": None}
    server = cli.serve(session, 0)
    yield session, server.server_port
    server.shutdown()


def _call(port, path, token, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-Studio-Token": token, "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_bridge_requires_the_token(bridge):
    session, port = bridge
    with pytest.raises(urllib.error.HTTPError) as e:
        _call(port, "/sources", "wrong")
    assert e.value.code == 403
    assert not session.done.is_set()


def test_bridge_serves_sources_and_mic(bridge):
    session, port = bridge
    assert _call(port, "/sources", session.token)["monitors"][0]["name"] == "DP-1"
    m = _call(port, "/mic", session.token)
    assert set(m) == {"level", "db", "alive"}


def test_bridge_done_records_a_valid_config(bridge):
    session, port = bridge
    reply = _call(port, "/done", session.token,
                  {"target": "monitor:DP-1", "mic": True, "desktop_audio": False,
                   "camera": "off", "camera_device": None})
    assert reply == {"ok": True}
    assert session.done.is_set()
    assert session.result["target"] == "monitor:DP-1"
    # The session's own countdown rides into the contract line: the consumer
    # learns how long until every setup surface is gone.
    assert session.result["countdown"] == 3


def test_bridge_done_rejects_a_bad_config_without_finishing(bridge):
    # The window stays up on a 400 (main.qml re-shows itself), so the session must
    # not look finished -- otherwise the launcher would print nothing and exit 0's
    # consumer would hang on an empty line.
    session, port = bridge
    with pytest.raises(urllib.error.HTTPError) as e:
        _call(port, "/done", session.token, {"target": "desktop", "camera": "off"})
    assert e.value.code == 400
    assert not session.done.is_set()
    assert session.result is None


def test_bridge_cancel_finishes_with_no_result(bridge):
    session, port = bridge
    assert _call(port, "/cancel", session.token, {}) == {"ok": True}
    assert session.done.is_set()
    assert session.result is None
