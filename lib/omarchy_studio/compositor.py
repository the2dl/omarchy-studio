"""What the compositor can do for a recording, asked once and in one place.

Only one question so far, but it is asked from three surfaces (the recorder, the HUD
and the teleprompter) and it used to be three copies of one `hyprctl getoption` line.
When the answer moved from a patched Hyprland to a plugin, all three had to change --
which is the argument for it living here.
"""

from __future__ import annotations

import json
import subprocess

# The plugin that provides the behaviour today: contrib/hyprland-studio-screenshare.
EXCLUDE_PLUGIN = "omarchy-studio-screenshare"

# The config option the RETIRED out-of-tree patch added
# (contrib/hyprland-screenshare-exclude). Still probed so a machine that has not been
# migrated off the patched package keeps working instead of silently losing the
# self-view; delete this arm once no such machine is left.
LEGACY_OPTION = "render:screenshare_exclude_windows"


def _hypr(*args: str) -> str:
    try:
        return subprocess.run(["hyprctl", *args], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def exclude_plugin_loaded() -> bool:
    try:
        return any(p.get("name") == EXCLUDE_PLUGIN
                   for p in json.loads(_hypr("-j", "plugin", "list") or "[]"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False


def self_view_exclusion_available() -> bool:
    """Can this compositor leave a `no_screen_share` window OUT of a screenshare --
    showing what is behind it -- rather than covering it with a black box?

    Stock Hyprland cannot: it paints Colors::BLACK over the window, and a black hole
    that follows a draggable camera bubble through a take is worse than no self-view.
    So this gates the `exclude` self-view mode, and a false answer falls back to
    parking the bubble outside the capture, or to no live preview at all.
    """
    return exclude_plugin_loaded() or _hypr("getoption", LEGACY_OPTION).startswith("bool: true")
