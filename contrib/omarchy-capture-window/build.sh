#!/usr/bin/env bash
# Build the native single-window recorder.
#
# It is OPTIONAL. Without it the dispatch falls back to capturing the window's
# rectangle -- see "is not built; falling back to a rectangle capture" in
# bin/omarchy-capture-screenrecording. What you lose is true isolation: a rectangle
# capture records whatever is on top of the window, this records the window's own
# surface tree and nothing else.
#
# The protocol glue is generated rather than committed. The Hyprland extension ships
# in this repo because it is not in wayland-protocols; linux-dmabuf comes from the
# system, so it tracks whatever wayland-protocols is installed instead of going stale
# in a checkout.
#
# wlr-stub.c is not optional and not obvious: hyprland-toplevel-export-v1 references
# zwlr_foreign_toplevel_handle_v1 in its XML, so the generated code needs that symbol
# to link even though this recorder addresses windows by address and never binds the
# foreign-toplevel protocol at all.
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

need() {
  command -v "$1" >/dev/null || { echo "missing: $1 ($2)" >&2; exit 1; }
}
need gcc "base-devel"
need pkg-config "pkgconf"
need wayland-scanner "wayland"

DMABUF=$(ls /usr/share/wayland-protocols/*/linux-dmabuf/linux-dmabuf-v1.xml 2>/dev/null | head -1)
[[ -n $DMABUF ]] || { echo "missing: linux-dmabuf-v1.xml (wayland-protocols)" >&2; exit 1; }

PKGS="wayland-client gbm libavcodec libavformat libavutil libavfilter libavdevice"
pkg-config --exists $PKGS || {
  echo "missing pkg-config deps; need: $PKGS (wayland, mesa, ffmpeg)" >&2
  exit 1
}

wayland-scanner client-header protocols/hyprland-toplevel-export-v1.xml \
  hyprland-toplevel-export-v1-client-protocol.h
wayland-scanner private-code protocols/hyprland-toplevel-export-v1.xml \
  hyprland-toplevel-export-v1-protocol.c
wayland-scanner client-header "$DMABUF" linux-dmabuf-v1-client-protocol.h
wayland-scanner private-code "$DMABUF" linux-dmabuf-v1-protocol.c

gcc -O2 -o omarchy-capture-window \
  record.c \
  hyprland-toplevel-export-v1-protocol.c \
  linux-dmabuf-v1-protocol.c \
  wlr-stub.c \
  $(pkg-config --cflags --libs $PKGS) \
  -lpthread

echo "built: $(pwd)/omarchy-capture-window"
