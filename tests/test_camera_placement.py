"""Where the live self-view goes when Record is pressed.

The pre-record bubble can be dragged anywhere and resized by its grip, and that
rectangle is what the export composites the camera into. The live bubble used to
ignore it entirely: `find_park_spot` computed a corner at a size preset, so pressing
Record threw away the placement the user had just made, on screen, every take.

It is honoured only where it CAN be. On the portal backend the compositor keeps a
no_screen_share window out of its frames, so the bubble may sit anywhere -- including
inside the capture, which is the whole point. On KMS nothing hides it (the backend
reads the DRM scanout below the compositor), so an honoured rect there would be a
bubble welded into the take; that case still parks, and the export still puts the
camera where the user asked, because the same rectangle went into edit.json.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "omarchy-capture-camera"

# One 2560x1440 logical display at the origin, and a second to its right.
ONE = "DP-1\t0\t0\t2560\t1440"
TWO = ONE + "\nHDMI-A-1\t2560\t0\t1920\t1080"


def place(rect: str, *, size: str = "medium", exclude: str = "yes",
          outside: str = "2560x1440+0+0", monitors: str = ONE) -> dict | None:
    """Run start_preview against a stubbed compositor and report where it decided.

    None when it refused outright -- which for a KMS capture with nowhere to park is
    the correct answer: no self-view beats a self-view burned into the frames.
    """
    script = f"""
    set -- status
    source {SCRIPT} >/dev/null 2>&1
    monitors_logical() {{ printf '%b\\n' {json.dumps(monitors)}; }}
    SIZE={size}; EXCLUDE={exclude}; OUTSIDE={json.dumps(outside)}
    REQUEST_RECT={json.dumps(rect)}
    apply_self_view_rule() {{ return 0; }}   # ends in hyprctl; geometry is the subject
    FIFO=$(mktemp -u)
    if start_preview >/dev/null 2>&1; then
      printf '{{"x":%s,"y":%s,"w":%s,"h":%s,"mon":"%s"}}\\n' \
        "$PARK_X" "$PARK_Y" "$PRESET_W" "$PRESET_H" "$PARK_MON"
    fi
    rm -f "$FIFO"
    """
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else None


# --- honoured ----------------------------------------------------------------


def test_the_bubble_maps_where_the_user_left_it():
    """The bug in one assertion: this used to be the bottom-right corner."""
    assert place("380x380+700+300") == {"x": 700, "y": 300, "w": 380, "h": 380,
                                        "mon": "DP-1"}


def test_the_size_comes_from_the_grip_not_the_preset():
    """`medium` on a 1440-high display is 320. The user resized to 380 and meant it."""
    for size in ("small", "medium", "large"):
        assert place("380x380+700+300", size=size)["w"] == 380


def test_a_bubble_on_the_second_display_belongs_to_that_display():
    """`move` in the window rule is relative to the window's own monitor, so the
    wrong monitor puts the bubble a whole screen away from where it was asked for."""
    got = place("300x300+3000+400", monitors=TWO)
    assert got["mon"] == "HDMI-A-1" and (got["x"], got["y"]) == (3000, 400)


def test_a_bubble_straddling_an_edge_goes_to_the_display_showing_most_of_it():
    got = place("300x300+2500+400", monitors=TWO)     # centre at 2650 -> HDMI-A-1
    assert got["mon"] == "HDMI-A-1"


# --- not honoured ------------------------------------------------------------


def test_kms_parks_instead_because_nothing_can_hide_the_bubble():
    """An honoured rect on KMS is a bubble welded into every frame. The placement is
    not lost -- the export reads the same rectangle out of edit.json."""
    got = place("380x380+700+300", exclude="no", outside="1400x760+300+200")
    assert got is not None
    assert (got["x"], got["y"]) != (700, 300), "parked, not honoured"


@pytest.mark.parametrize("bad", ["", "garbage", "0x0+10+10", "380x380+99999+99999",
                                 "-5x380+700+300", "380x380"])
def test_an_unusable_rect_falls_back_to_parking(bad):
    """A recording must not fail over a preference. Anything that is not a rectangle
    on a real display parks, which is what happened before this existed."""
    got = place(bad)
    assert got is not None
    assert (got["x"], got["y"]) != (700, 300)


def test_no_rect_at_all_is_exactly_the_old_behaviour():
    """The property that made this safe to add."""
    assert place("") == place("garbage")


# --- the recorder hands it over ----------------------------------------------


def test_the_recorder_passes_the_setup_rect_to_the_camera():
    """The rect reaches edit.json through `begin` and the live bubble through here.
    Only one of those was wired, which is why the export was right and the preview
    was not."""
    src = (REPO / "bin" / "omarchy-capture-screenrecording").read_text()
    assert '--rect="$SETUP_CAMERA_RECT"' in src


def test_the_camera_script_documents_the_option():
    src = SCRIPT.read_text()
    assert "--rect=<WxH+X+Y>" in src, "an undocumented flag is an unfindable one"
