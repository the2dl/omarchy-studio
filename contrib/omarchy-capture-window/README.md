# omarchy-capture-window

Capturing ONE window's own pixels, via Hyprland's `hyprland_toplevel_export_v1`.

## Why this exists

A "window" recording today is a RECTANGLE of the finished screen. Anything drawn
over that rectangle is in the take, and on the KMS backend nothing can be hidden at
all -- `no_screen_share` is ignored, because KMS reads the DRM scanout below the
compositor. A user recording one window and pressing SUPER+S gets Omarchy's quake
console in their video, plus the 60% `dim_special` darkening it applies to
everything underneath. No crop can fix either.

macOS does not have this problem: ScreenCaptureKit takes the window as the content
filter. `hyprland_toplevel_export_v1` is the equivalent here -- the compositor
renders that window's surface tree alone, no other window, no decoration, no dim.

## Why not the portal

The portal advertises window capture (`AvailableSourceTypes = 7`) and gsr asks for
it (`SelectSources` with `types = 7`, verified by dbus-monitor). But the ScreenCast
portal deliberately does not let an APP name the window -- the user picks in the
portal's own UI. Three ways around that were checked and all are closed:

  - The `[HC>]{class}[HT>]{title}[HE>]{address}[HA>]` selection format is the
    picker's environment INPUT, not a D-Bus API. Only a process named by
    `custom_picker_binary` can answer it -- which is the user's portal config, the
    thing that must not be a dependency.
  - Restore tokens degrade to "first window with this class" (xdph resolves
    `handleFromClass` when the internal handle does not match), and the handle is an
    xdph pointer that does not survive a restart. Two `foot` windows is enough to
    make it wrong.
  - There is no app-facing Hyprland extension to the portal. The extension IS this
    protocol.

So: talk the protocol directly. No portal, no picker, no restore token, no
dependency on `allow_token_by_default`, and the user's other screensharing is
untouched.

## spike.c -- milestone 0, the measurement

Answers the one question that could have killed the approach: can this machine
render an extra window pass per vblank? Deliberately takes the CHEAP path (wl_shm,
one buffer, a `readPixels` per frame) so the answer is a floor rather than a
best case.

Measured 2026-09-04, Hyprland 0.56.2, DP-1 5120x2880@2x, Granite Ridge iGPU:

    window        5076x2768 (a maximised Chrome at 2x)
    frames        224 in 5.001s, 0 failed
    rate          44.79 fps

**44.8, not 60.** So the DMA-BUF path is a requirement, not an optimisation:
buffers allocated through GBM and imported with `zwp_linux_dmabuf_v1` remove the
readback entirely. Single-buffered and fully serialised here (capture -> copy ->
ready -> capture), so double-buffering is worth something too.

## spike_dmabuf.c -- milestone 1, no readback

GBM-allocated buffers handed to the compositor through `zwp_linux_dmabuf_v1`, two
of them ping-ponged. The frame never leaves the GPU, and the DRM PRIME fd is what
an encoder wants anyway -- it maps into VAAPI without a copy.

Same window, same session, back to back:

    shm      5076x2768   40.54 fps
    dmabuf   5076x2768   59.57 fps      AR24, 2 buffers, 0 failed

Repeats: 55.55, 50.32, 59.34 on the maximised window; 59.78 on a 5072x1356 one.

**Display rate, so window capture can be offered at the same 60 the KMS path
gives.** The variance is not a ceiling: frames are copied on output commit, and
Hyprland renders on damage, so a still window simply produces fewer. That is the
pacing hazard in the list below, and the answer is the recorder's own CFR clock
repeating the last frame -- which is what gsr does too, and makes a duplicate
frame mean "nothing changed" rather than "we missed one".

Exclusion verified at the same instant, with the quake console open over the target:
`grim` of the monitor showed the console and the dim; the toplevel capture showed
the window alone, clean and undimmed.

    wayland-scanner client-header protocols/hyprland-toplevel-export-v1.xml \
        hyprland-toplevel-export-v1-client-protocol.h
    wayland-scanner private-code protocols/hyprland-toplevel-export-v1.xml \
        hyprland-toplevel-export-v1-protocol.c
    gcc -O2 -o spike spike.c hyprland-toplevel-export-v1-protocol.c wlr-stub.c \
        $(pkg-config --cflags --libs wayland-client)
    ./spike 0x<address> <seconds> [raw.out]

`handle` is the window's Hyprland address truncated to its low 32 bits -- the same
truncation xdph does when it builds its picker list.

## record.c -- the recorder

Works end to end: captures one window, encodes h264/hevc through VAAPI, muxes an
mp4, writes gsr's two-column first-frame sidecar, and finalises on SIGINT/SIGTERM.

    ./omarchy-capture-window -w 0x<address> -o out.mp4 -f 60 \
        [-k auto|h264_vaapi|hevc_vaapi] [-q QP] [-cursor yes|no] \
        [-write-first-frame-ts FILE]

Flags mirror gpu-screen-recorder on purpose: the capture script already branches on
`kms|portal`, and this is meant to be a third value rather than a second way of
doing everything.

**Measured 35-37 fps, which is SLOWER than the shm spike, and the reason is worth
recording.** VAAPI on this driver refuses to import a BGRA DRM object:

    Failed to create surface from DRM object: 2 (resource allocation failed)
    Failed to map frame: -5

Tried with tiled and with linear buffers; the refusal is the format, not the
modifier. VA surfaces here accept an UPLOAD from BGRA and convert on the GPU, they
just will not take an import -- so the current path allocates linear, maps the
buffer, and uploads. That is two copies where the shm spike had one, which is
exactly why it measures worse than 44.8.

So the standing order of preference is:

    1. dmabuf import   59.6 fps   blocked on the VAAPI import above
    2. shm             44.8 fps   one copy, and better than what ships today
    3. map + upload    35-37 fps  what ships today

Fixing (1) is the real work -- EGL/GL or Vulkan import, or a driver path that takes
RGB surfaces. Falling back to (2) is a smaller change and an immediate win. Neither
is done.

## What is still missing

Still to build: a 60Hz CFR clock repeating the last frame (the compositor only renders on
damage, so there is no frame while the screen is idle), the `.ts` first-frame
sidecar `capture.read_gsr_ts` expects, SIGINT/IPC stop parity with gsr, and audio.
Known hazards: mid-recording resize renegotiates the buffer size against a
fixed-size encoder; `overlay_cursor` only draws while the window is focused;
a translucent window records as premultiplied alpha over nothing.
