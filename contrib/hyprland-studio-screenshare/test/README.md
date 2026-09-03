# Nested test rig

Verifies the plugin without risking the real session: a **stock** Hyprland runs nested
in a small window, and `grim` against it goes through the same `CScreenshareFrame` path
the portal does. If the plugin takes the compositor down, only the nested one dies.

    # stock binary, extracted from the cached package rather than the installed
    # (patched) one -- the patch is a confound even with its kill switch off
    bsdtar -xf /var/cache/pacman/pkg/hyprland-0.56.2-1-x86_64.pkg.tar.zst -C /tmp/stock usr/bin/Hyprland

    systemd-run --user --unit=nested-hypr --collect \
      --setenv=XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR --setenv=WAYLAND_DISPLAY=$WAYLAND_DISPLAY \
      /tmp/stock/usr/bin/Hyprland -c nested.lua

Then, against the nested socket (`hyprctl instances -j` gives its `wl_socket` and
`instance`; `hyprctl -i`/`HYPRLAND_INSTANCE_SIGNATURE` for the plugin load):

    foot -T excl -o main.locked-title=yes -o colors.background=ff0000 sleep 600 &
    grim -o WAYLAND-1 shot.png && probe.py shot.png

`main.locked-title=yes` matters: a shell prompt resets the title and the `^excl$` rule
silently stops matching. `probe.py` reads three pixels:

    green = background showed through   -> EXCLUDED (what we want)
    red   = the window was captured     -> no exclusion
    black = a black box was painted     -> stock behaviour

Read `../README.md` "Test rig gotchas" first -- three of them each cost a debugging
round: config reloads dropping dynamic rules, `.so` caching by path, and testing
against the patched binary.
