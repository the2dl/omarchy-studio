# Hyprland patch: exclude `no_screen_share` windows instead of blacking them out

A local patch against **Hyprland v0.56.2**, required for the recorder's live
self-view (`bin/omarchy-capture-screenrecording`, the `exclude` self-view mode).

## Why this exists

The camera bubble has to be visible on screen while you record and absent from the
recording, so it stays a separate, editable stream. Two facts make that hard here,
both verified by experiment rather than assumed:

1. **The KMS backend cannot be excluded from.** `gpu-screen-recorder -w <monitor>`
   reads the DRM scanout buffer below the compositor, so nothing Hyprland does can
   hide a window from it. A/B tested: a window carrying `no_screen_share` appears in
   the recording exactly like any other.
2. **The portal backend honours the rule, but stock Hyprland paints a black
   rectangle** where the window is, rather than showing what is behind it.
   `CScreenshareFrame::renderMonitor()` blits the already-composited *mirror
   framebuffer* and then draws `Colors::BLACK` rects over `no_screen_share` windows.

A black box would be survivable if the bubble never moved, because the camera
overlay covers it at its default placement. It is not survivable once the bubble can
be moved or switched off during a recording -- which is the whole point of the
feature -- because the hole moves with it and smears through the take.

## What the patch does

Hyprland already has a second render path: when `needsUnmodifiedCopy()` is true the
main render writes every draw to two colour attachments (`SH_FEAT_MIRROR` shaders,
`GL_COLOR_ATTACHMENT1` = `m_mirrorTex`) and the mirror FB is copied from that.

The patch forces that MRT path whenever a screenshare is active *and* a visible
`no_screen_share` window exists, and masks attachment 1 (`glDrawBuffers`) around that
window's pass elements. The window -- with its popups, decorations, blur and its
close-fade snapshot -- never reaches the mirror, so whatever is behind it shows
through. Layer-surface black boxes are left alone.

Cost: MRT bandwidth, and only while a session is active and such a window is visible.
The mirror FB is invalidated (forcing a full refresh) whenever that state flips.

## Runtime kill switch

    render:screenshare_exclude_windows = false

Reverts to stock black-box behaviour without reinstalling anything. The recorder
probes this option to decide whether the `exclude` self-view mode is available at
all, so turning it off also makes the recorder fall back cleanly.

## Building

    makepkg -d -e            # -d: glaze / hyprland-protocols makedepends are not installed
                             # (CMake FetchContent's glaze; protocols come from the subproject)
    sudo pacman -U hyprland-0.56.2-1.1-x86_64.pkg.tar.zst

`pkgrel` is `1.1` so pacman reads it as newer than the repo's `0.56.2-1` and `-Syu`
leaves it alone, reporting "local is newer". **When Hyprland genuinely updates, the
patch is overwritten and the self-view silently falls back** -- the recorder's probe
sees the option is gone and drops to `off`. Rebuild against the new tag, or add
`IgnorePkg = hyprland` if you would rather be asked.

## Rollback

    sudo pacman -U /var/cache/pacman/pkg/hyprland-0.56.2-1-x86_64.pkg.tar.zst

Either way the change only takes effect at the next login; `pacman -U` swaps the file
on disk and leaves the running compositor alone.

## Status

Compiles clean and the patched binary starts and runs a full session (verified
nested, via the Wayland backend). **The exclusion itself has not been verified
against a real DRM session** -- that needs a login with the patched build. To check:
put a `no_screen_share` window over something with a distinctive colour, capture with
both `grim` and `gpu-screen-recorder -w portal`, and confirm the area shows what is
behind the window rather than black.
