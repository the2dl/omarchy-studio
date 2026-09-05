#!/usr/bin/env bash
# Make sure the plugin is loaded, rebuilding it if a Hyprland update left it stale.
#
# For `exec_on_start` in your Hyprland config, after the usual `hyprpm reload`:
#
#     o.exec_on_start("/path/to/omarchy-studio/contrib/hyprland-studio-screenshare/ensure-loaded.sh")
#
# WHY THIS IS NOT `hyprpm update`. That is the built-in answer and it rebuilds EVERY
# registered repository. A foreign plugin whose upstream no longer builds against the
# new Hyprland gets unloaded from the live session as collateral -- which is exactly
# how a working hyprexpo build was destroyed on the machine this was written on. This
# rebuilds ONE repository: ours. Whatever else you have installed is not touched, not
# rebuilt, and not unloaded.
#
# THE FAST PATH IS THE POINT. Almost every login the plugin is already loaded, so this
# is one `hyprctl` call and an exit. The rebuild only happens after a Hyprland version
# bump, which is the only time it is needed.
#
# It is safe to not run this at all. The recorder asks the compositor at record time
# whether exclusion is available and parks the self-view outside the capture, or drops
# it, when it is not -- see self_view_exclusion_available in
# bin/omarchy-capture-screenrecording. Nothing here protects a recording; it protects
# the live camera preview.
set -uo pipefail

PLUGIN=omarchy-studio-screenshare
HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)

loaded() {
  hyprctl -j plugin list 2>/dev/null \
    | jq -e --arg n "$PLUGIN" '.[] | select(.name == $n)' >/dev/null 2>&1
}

notify() {
  command -v omarchy-notification-send >/dev/null \
    && omarchy-notification-send -u "${3:-normal}" -t 6000 "$1" "$2" || true
}

# Already there. The overwhelmingly common case, and it costs one IPC round trip.
loaded && exit 0

# `hyprpm reload` first: cheap, and enough whenever the build is still good and the
# plugin simply was not loaded into this session yet.
hyprpm reload -n >/dev/null 2>&1 || true
loaded && exit 0

# Still missing, so the build is stale for this Hyprland. Rebuild ours alone.
notify "Rebuilding the screen-recording plugin" \
       "Hyprland changed, so omarchy-studio-screenshare is being rebuilt. The live camera preview will work again once it finishes."

if ! "$HERE/install.sh" >/tmp/omarchy-studio-screenshare-rebuild.log 2>&1; then
  notify "Could not rebuild the screen-recording plugin" \
         "Recording still works; the live camera preview does not. See /tmp/omarchy-studio-screenshare-rebuild.log" \
         critical
  exit 1
fi

hyprpm reload -n >/dev/null 2>&1 || true
if loaded; then
  notify "Screen-recording plugin is back" "The live camera preview works again."
else
  notify "The screen-recording plugin did not load" \
         "It rebuilt but is not loaded. Recording still works; the live camera preview does not." \
         critical
  exit 1
fi
