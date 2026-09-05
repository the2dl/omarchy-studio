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
import threading
import time
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


def test_windows_skips_our_own_surfaces_by_title_prefix():
    # Only matters once enumeration repeats: the first pass runs before qml6 exists,
    # every later one runs with our monitor-sized sheets mapped over everything.
    clients = [
        client(title="shell"),
        client(title="omarchy-setup-sheet-DP-1", at=[0, 0], size=[2560, 1440]),
        client(title="omarchy-studio-teleprompter", at=[40, 40], size=[600, 300]),
    ]
    out = ss.windows(clients, 2, ("omarchy-setup-sheet-", "omarchy-studio-teleprompter"))
    assert [w["title"] for w in out] == ["shell"]


def test_windows_keeps_everything_when_no_prefixes_are_given():
    clients = [client(title="omarchy-setup-sheet-DP-1")]
    assert len(ss.windows(clients, 2)) == 1


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
    assert c == {"target": "monitor:DP-1", "mic": True, "mic_device": None,
                 "desktop_audio": False, "camera": "off", "camera_device": None,
                 "camera_rect": None, "window": None, "window_isolated": False,
                 "full_monitor": False, "countdown": 3}


def test_only_a_real_window_can_be_isolated():
    """An area is a rectangle by definition; there is no toplevel behind it to
    export, so asking for one is not a thing that can be honoured."""
    assert ss.config("region:800x600+10+20", True, False, "off",
                     None, window_isolated=True)["window_isolated"] is False
    assert ss.config("region:800x600+10+20", True, False, "off", None,
                     window="0x5f2a", window_isolated=True)["window_isolated"] is True


def test_isolating_and_reframing_cannot_both_be_true():
    """Re-framing needs pixels around the frame; isolating means the stream IS the
    window and there are none. Both at once describes nothing."""
    c = ss.config("region:800x600+10+20", True, False, "off", None,
                  window="0x5f2a", window_isolated=True, full_monitor=True)
    assert c["window_isolated"] is True and c["full_monitor"] is False


def test_recording_more_than_the_selection_is_opt_in():
    """Picking one window is frequently a decision NOT to record the rest of the
    screen. Recording it anyway -- onto disk, in the bundle, where an export can
    reach it -- is not a default anyone should get without asking."""
    assert ss.config("region:800x600+10+20", True, False, "off",
                     None)["full_monitor"] is False


def test_asking_for_it_gets_it():
    assert ss.config("region:800x600+10+20", True, False, "off", None,
                     full_monitor=True)["full_monitor"] is True


def test_a_display_capture_has_nothing_more_to_record():
    """The choice would be between a thing and itself."""
    assert ss.config("monitor:DP-1", True, False, "off", None,
                     full_monitor=True)["full_monitor"] is False


def test_a_window_pick_carries_which_window_it_was_cut_from():
    """The target is a rectangle, and a rectangle does not move when the window
    inside it does. The address is what lets the recorder log where it went."""
    c = ss.config("region:800x600+10+20", True, False, "off", None, window="0x5f2a")
    assert c["window"] == "0x5f2a"


def test_a_monitor_target_has_no_window_to_follow():
    """Only a region has an outside to re-frame into; keeping the address on a
    full-monitor capture would make the bundle claim a capability it lacks."""
    assert ss.config("monitor:DP-1", True, False, "off", None,
                     window="0x5f2a")["window"] is None


@pytest.mark.parametrize("bad", ["; rm -rf /", "0xZZZ", "5f2a", "0x" + "a" * 40, ""])
def test_a_bad_window_address_is_refused(bad):
    """It is compared against compositor output and rides through a shell, so it is
    constrained to exactly the shape Hyprland emits."""
    with pytest.raises(ValueError):
        ss.config("region:800x600+10+20", True, False, "off", None, window=bad)


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
    c = ss.config("camera:/dev/video1", False, False, "rounded", "/dev/video0")
    assert c["camera"] == "off"
    assert c["camera_device"] == "/dev/video1"


def test_config_carries_where_the_self_view_was_left():
    rect = {"x": 1800, "y": 900, "width": 400, "height": 400}
    c = ss.config("monitor:DP-1", True, False, "circle", "/dev/video0",
                  camera_rect=rect)
    assert c["camera_rect"] == rect


def test_config_drops_the_placement_when_there_is_no_camera():
    # Nothing downstream should have to ask "is this placement for a camera that is
    # not being recorded?" -- the answer is always no.
    c = ss.config("monitor:DP-1", True, False, "off", None,
                  camera_rect={"x": 10, "y": 10, "width": 100, "height": 100})
    assert c["camera_rect"] is None


@pytest.mark.parametrize("bad", [
    {"x": 0, "y": 0, "width": 0, "height": 100},      # degenerate
    {"x": 0, "y": 0, "width": 100},                    # missing a side
    {"x": "left", "y": 0, "width": 100, "height": 100},
    "400x400+10+10",                                   # a rect, but not this shape
])
def test_config_rejects_a_malformed_placement(bad):
    with pytest.raises(ValueError):
        ss.config("monitor:DP-1", True, False, "circle", "/dev/video0",
                  camera_rect=bad)


def test_config_is_one_json_line():
    c = ss.config("region:1600x900+200+200", True, True, "rounded", "/dev/video0")
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


# --- microphones --------------------------------------------------------------

# One real input, its monitor, and an output's monitor: the shape pactl actually
# returns, where monitors outnumber the inputs.
PACTL_SOURCES = [
    {"name": "alsa_output.pci-0000_10_00.6.analog-stereo.monitor",
     "description": "Analog Stereo Monitor"},
    {"name": "alsa_input.usb-Blue_Microphones-00.analog-stereo",
     "description": "Blue Microphones Analog Stereo"},
    {"name": "alsa_input.pci-0000_10_00.6.analog-stereo",
     "description": "Ryzen HD Audio Analog Stereo"},
]


def test_mics_drops_monitor_sources():
    """A monitor source recorded as "the mic" records the desktop into the voice
    track, so they must never reach the picker."""
    got = ss.mics(PACTL_SOURCES)
    assert [m["name"] for m in got] == [
        "alsa_input.usb-Blue_Microphones-00.analog-stereo",
        "alsa_input.pci-0000_10_00.6.analog-stereo",
    ]
    assert got[0]["label"] == "Blue Microphones Analog Stereo"


def test_mics_marks_the_default():
    got = ss.mics(PACTL_SOURCES, "alsa_input.pci-0000_10_00.6.analog-stereo")
    assert [m["default"] for m in got] == [False, True]


def test_mics_falls_back_to_the_name_when_a_source_has_no_description():
    got = ss.mics([{"name": "alsa_input.thing"}])
    assert got[0]["label"] == "alsa_input.thing"


def test_config_carries_the_picked_mic():
    c = ss.config("monitor:DP-1", mic=True, desktop_audio=False, camera="off",
                  camera_device=None, mic_device="alsa_input.usb-Blue-00.analog")
    assert c["mic_device"] == "alsa_input.usb-Blue-00.analog"


def test_config_drops_the_mic_device_when_the_mic_is_off():
    """Otherwise the recorder is handed a source to open for a track nobody asked
    for, and an unplugged device would fail a recording that wanted no audio."""
    c = ss.config("monitor:DP-1", mic=False, desktop_audio=False, camera="off",
                  camera_device=None, mic_device="alsa_input.usb-Blue-00.analog")
    assert c["mic_device"] is None


@pytest.mark.parametrize("bad", ["has space", "semi;colon", "quote'd", "new\nline"])
def test_config_rejects_a_mic_device_that_could_not_be_a_source_name(bad):
    """The name reaches a shell as one gsr -a argument."""
    with pytest.raises(ValueError):
        ss.config("monitor:DP-1", mic=True, desktop_audio=False, camera="off",
                  camera_device=None, mic_device=bad)


def test_the_bar_offers_the_same_three_shapes_as_the_editor():
    # One vocabulary end to end. The bar used to say "squircle" and "corner" for shapes
    # the model called "squircle" and "rounded", and the editor panel offered a third
    # set of names again -- so the shape you set up was not the shape you got.
    assert ss.CAMERA_MODES == ("off", "circle", "rounded", "rect")
    for shape in ("circle", "rounded", "rect"):
        assert ss.config("monitor:DP-1", True, False, shape, "/dev/video0")["camera"] == shape


# --- hot-plug: sources that appear after the bar is already up -----------------


class _FakeMeter:
    """Stands in for MicMeter so the tests never spawn parec."""

    def __init__(self) -> None:
        self.device = ""
        self.level = 0.0
        self.db = -120.0
        self.alive = False
        self.retargets: list[str] = []

    def start(self, device: str = "") -> None:
        self.device = device

    def retarget(self, device: str) -> None:
        self.retargets.append(device)
        self.device = device


@pytest.fixture()
def hotplug(cli, tmp_path, monkeypatch):
    """A session whose view of the system is a mutable dict the test edits.

    The whole bug this covers is that enumeration used to happen exactly once,
    before qml6 spawned, so anything plugged in afterwards did not exist as far as
    the bar was concerned -- the camera chip stayed "no camera" with the shape
    control disabled beside it for the life of the process.
    """
    world = {"cameras": "", "nodes": (), "scans": 0, "mics": [], "clients": [],
             "monitors": [{"name": "DP-1", "x": 0, "y": 0, "width": 2560,
                           "height": 1440, "scale": 1.0, "focused": True,
                           "activeWorkspace": {"id": 2}}]}

    def fake_json(cmd):
        return world["monitors"] if "monitors" in cmd else world["clients"]

    def fake_scan(cmd):
        world["scans"] += 1
        return world["cameras"]

    monkeypatch.setattr(cli, "_run_json", fake_json)
    monkeypatch.setattr(cli, "_run_text", fake_scan)
    monkeypatch.setattr(cli, "video_nodes", lambda: world["nodes"])
    monkeypatch.setattr(cli, "list_mics", lambda: list(world["mics"]))

    session = cli.SetupSession(tmp_path, countdown=3)
    session.meter = _FakeMeter()
    server = cli.serve(session, 0)
    yield world, session, server.server_port
    server.shutdown()


def test_a_camera_plugged_in_after_launch_reaches_the_bar(hotplug):
    world, session, port = hotplug
    session.enumerate()
    assert _call(port, "/sources", session.token)["cameras"] == []

    world["cameras"] = "/dev/video2  Logitech StreamCam\n"
    world["nodes"] = ("/dev/video2",)
    session.enumerate()

    cams = _call(port, "/sources", session.token)["cameras"]
    assert [c["device"] for c in cams] == ["/dev/video2"]


def test_the_camera_scan_is_skipped_while_the_video_nodes_are_unchanged(hotplug):
    """v4l2-ctl costs ~86ms of the ~98ms an enumeration takes -- most of it, and all
    of it wasted when nothing plugged in. The /dev/videoN set moves if and only if a
    camera did, so it gates the scan exactly rather than approximately."""
    world, session, port = hotplug
    world["nodes"] = ("/dev/video0",)
    world["cameras"] = "/dev/video0  Built-in\n"
    session.enumerate()
    assert world["scans"] == 1

    session.enumerate()
    session.enumerate()
    assert world["scans"] == 1
    assert _call(port, "/sources", session.token)["cameras"][0]["device"] == "/dev/video0"

    world["nodes"] = ("/dev/video0", "/dev/video2")
    session.enumerate()
    assert world["scans"] == 2


def test_a_mic_plugged_in_after_launch_reaches_the_bar_and_the_meter(hotplug):
    world, session, port = hotplug
    session.enumerate()
    assert _call(port, "/sources", session.token)["mic"] is None

    world["mics"] = [{"name": "alsa_input.usb-Blue", "label": "Yeti", "default": True}]
    session.enumerate()

    assert _call(port, "/sources", session.token)["mic"]["name"] == "alsa_input.usb-Blue"
    # The meter was started dead (no mic existed); the rescan has to re-point it or
    # the level stays flat next to a mic that is plainly working.
    assert session.meter.device == "alsa_input.usb-Blue"


def test_a_rescan_does_not_yank_the_meter_off_a_mic_the_user_picked(hotplug):
    world, session, port = hotplug
    world["mics"] = [{"name": "built-in", "label": "Built-in", "default": True}]
    session.enumerate()

    _call(port, "/mic-device", session.token, {"device": "alsa_input.usb-Blue"})
    assert session.meter.device == "alsa_input.usb-Blue"

    # Another device shows up and becomes the system default. The user's pick wins:
    # the meter is answering "is THIS mic working?" about a mic they named.
    world["mics"] = [{"name": "hdmi-thing", "label": "HDMI", "default": True}]
    session.enumerate()
    assert session.meter.device == "alsa_input.usb-Blue"


def test_a_rescan_does_not_offer_our_own_sheets_as_capture_targets(hotplug):
    world, session, port = hotplug
    world["clients"] = [
        {"class": "foot", "title": "shell", "at": [11, 45], "size": [2538, 686],
         "workspace": {"id": 2}, "hidden": False},
        {"class": "org.qt-project.qml", "title": "omarchy-setup-sheet-DP-1",
         "at": [0, 0], "size": [2560, 1440], "workspace": {"id": 2}, "hidden": False},
    ]
    session.enumerate()
    wins = _call(port, "/sources", session.token)["windows"]
    assert [w["title"] for w in wins] == ["shell"]


def test_the_rescan_pauses_while_the_area_picker_owns_the_screen(cli, hotplug,
                                                                 monkeypatch):
    """slurp has frozen a copy of the desktop with our windows unmapped; four
    subprocesses describing that world are four subprocesses wasted -- and it must
    pick itself back up when the pick is over, without anyone restarting it."""
    world, session, port = hotplug
    monkeypatch.setattr(cli, "RESCAN_SECONDS", 0.01)
    calls: list[int] = []
    session.enumerate = lambda: calls.append(1)   # type: ignore[method-assign]

    session.picking = True
    t = threading.Thread(target=session.rescan_forever, daemon=True)
    t.start()
    time.sleep(0.15)
    assert calls == []

    session.picking = False
    time.sleep(0.15)
    session.done.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert calls


def test_a_display_that_appears_gets_its_sheet_repositioned(hotplug):
    """The sheet itself comes for free (the Repeater is bound to the list); being
    moved onto the new output does not."""
    world, session, port = hotplug
    moved = []
    session.reposition = lambda: moved.append(1)
    session.enumerate()
    assert moved == []          # same one monitor as the fixture built

    world["monitors"] = world["monitors"] + [
        {"name": "HDMI-A-1", "x": 2560, "y": 0, "width": 1920, "height": 1080,
         "scale": 1.0, "focused": False, "activeWorkspace": {"id": 3}}]
    session.enumerate()
    for _ in range(20):         # reposition runs on its own thread
        if moved:
            break
        time.sleep(0.02)
    assert moved == [1]
    assert [m["name"] for m in _call(port, "/sources", session.token)["monitors"]] \
        == ["DP-1", "HDMI-A-1"]
