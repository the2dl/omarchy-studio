#!/bin/bash
# Install (or update) the plugin through hyprpm.
#
# hyprpm only takes a git repo with hyprpm.toml at its root, and this directory is a
# subdirectory of the omarchy-studio checkout. So the plugin is staged into a small git
# repo of its own under ~/.local/share and hyprpm is pointed at that. Re-running after
# editing the source commits the changes there and runs `hyprpm update`.
#
# This does NOT load the plugin into the running compositor. That is `hyprpm reload`,
# left to you on purpose: hyprpm builds against STOCK headers, and while the locally
# patched Hyprland package is still installed the version gate cannot tell the two
# apart (same commit hash). Do the first load from a session you can afford to lose,
# or after going back to the stock package.
set -euo pipefail

# Preflight, because everything below is `set -e` and the failures are otherwise
# bare "command not found" from the middle of a staging step, or -- worse -- a cmake
# error from inside hyprpm's own build output, where it reads as the plugin being
# broken rather than the toolchain being absent.
#
# hyprpm supplies the Hyprland HEADERS itself (it builds against stock ones, on
# purpose -- see hyprpm.toml). The pkg-config modules CMakeLists asks for beyond that
# are Hyprland's own runtime dependencies, so a machine running Hyprland already has
# them; they are not re-checked here.
need() {
  command -v "$1" >/dev/null || { echo "missing: $1  ($2)" >&2; exit 1; }
}
need rsync "rsync -- staging into the hyprpm repo"
need git "git -- hyprpm only accepts a git repository"
need hyprpm "hyprpm -- ships with hyprland"
need cmake "cmake -- the plugin's build, see hyprpm.toml"
need g++ "base-devel"

# The plugin needs function hooking, and Hyprland's own is x86_64-ONLY:
# CFunctionHook::hook() returns false outright on every other architecture. So the
# three cases differ and each has to say something different.
#
#   x86_64            stock HyprlandAPI hooks, nothing to check.
#   aarch64/arm64     the vendored funchook does the hooking instead (see
#                     vendor/funchook/README.omarchy-studio.md), which wants a C
#                     compiler, an assembler for prehook-arm64-gas.S and the SYSTEM
#                     capstone. Checked here so a missing package is a named failure
#                     rather than a cmake error from inside hyprpm's build output.
#   anything else     funchook has no backend either, so this still builds, installs,
#                     and then dies in init. Say so rather than leaving someone to
#                     work out why an installed, enabled plugin never loads.
case "$(uname -m)" in
x86_64) ;;
aarch64 | arm64)
  need cc "base-devel -- compiles the vendored funchook on aarch64"
  need pkg-config "pkgconf -- locates capstone"
  pkg-config --exists capstone || {
    echo "missing: capstone  (pacman -S capstone -- funchook's disassembler on aarch64)" >&2
    exit 1
  }
  ;;
*)
  echo "note: Hyprland hooks only work on x86_64, and this plugin only carries a" >&2
  echo "      funchook backend for aarch64 -- on $(uname -m) it will build and" >&2
  echo "      install, but will not load. The recorder handles its absence: the" >&2
  echo "      self-view parks outside the capture, or is dropped." >&2
  ;;
esac

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
STAGE=${OMARCHY_STUDIO_HYPRPM_STAGE:-"$HOME/.local/share/omarchy-studio/hyprpm/omarchy-studio-screenshare"}
NAME=omarchy-studio-screenshare

mkdir -p "$STAGE"
rsync -a --delete --exclude build --exclude .git --exclude test "$SRC/" "$STAGE/"

cd "$STAGE"
if [[ ! -d .git ]]; then
  git init -q -b main
  git -c user.name=omarchy-studio -c user.email=omarchy-studio@localhost add -A
  git -c user.name=omarchy-studio -c user.email=omarchy-studio@localhost commit -q -m "omarchy-studio-screenshare"
elif ! git diff --quiet || [[ -n $(git status --porcelain) ]]; then
  git -c user.name=omarchy-studio -c user.email=omarchy-studio@localhost add -A
  git -c user.name=omarchy-studio -c user.email=omarchy-studio@localhost commit -q -m "update $(date -Is)"
fi

# NEVER `hyprpm update` here: it updates EVERY registered repository, and a foreign
# plugin whose upstream no longer builds for this Hyprland gets unloaded from the live
# session as collateral (this happened with hyprexpo). Re-register only our repo.
if hyprpm list 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -q "Repository omarchy-studio "; then
  echo "== hyprpm remove omarchy-studio (re-adding from $STAGE)"
  hyprpm remove omarchy-studio </dev/null
fi
echo "== hyprpm add $STAGE"
hyprpm add "$STAGE" </dev/null

hyprpm enable "$NAME" </dev/null
echo
echo "Installed and enabled. Not loaded into the running compositor -- when ready:"
echo "    hyprpm reload"
echo "Then confirm with: hyprctl -j plugin list | jq '.[].name'"
