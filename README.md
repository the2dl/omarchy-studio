<img src="docs/media/logo/png/mark-128.png" width="72" alt="">

# omarchy-studio

A non-destructive screen recorder for Linux, built for Hyprland.

Recording is not the hard part — `gpu-screen-recorder` already does that well. The
hard part is everything after: moving the camera bubble, retiming a zoom, cutting a
section out, and changing your mind about any of it a week later. So a recording
here is **not a file**. It is a bundle: the screen, the camera and the input events
captured as separate streams beside an immutable description of what the hardware
did.

Nothing is ever re-encoded over the top of it. `capture.json` is written once and
never touched again; every edit lives in `edit.json` and is applied at export.

```
screenrecording-2026-09-04_14-22-11/
├── capture.json     what the hardware did — immutable
├── edit.json        every decision you have made — rewritten freely
├── media/           screen.mp4, camera.mp4, and the first-frame sidecar
├── events/          cursor.bin, input.jsonl, window.jsonl
├── proxy/           editor-resolution transcodes
└── assets/          images and other layer material
```

## What it does

- **Capture** a display, a window or a hand-drawn area, at native resolution and full
  colour range. HEVC when the grid is past h264's 4096 ceiling.
- **A camera bubble** as its own stream, so it can be moved, resized, reshaped,
  turned off for part of a take, or removed entirely afterwards.
- **Cuts** stored in source time, so adding or removing one never slides an
  annotation off the thing it points at.
- **Auto-zoom** driven by real click events, recorded at 120 Hz alongside the video
  and anchored to it by first-frame timestamp rather than by guesswork.
- **Layers** — images, text, shapes, blur and pixelate redactions, captions from
  local speech-to-text.
- **Head and tail pads**, so a title card can hold before the recording starts.
- **A teleprompter** that the compositor can keep out of the take.
- **Export presets** up to native, decided at export rather than at capture, because
  capture keeps every pixel.

## What it looks like

![The editor](docs/media/editor.png)

The editor. Preview on its backdrop, the inspector on the right (zoom, cursor,
camera, backdrop), and the timeline underneath with a row per track — screen, zoom,
camera, clicks, audio. Nothing here is applied to the master; it is all `edit.json`
until you press Export.

![The pre-record setup bar](docs/media/setup.png)

Before recording. Source mode, microphone, system audio, camera shape, the
teleprompter, and `Re-frame later`. Start is not on the bar — it sits in the middle
of whatever you picked, so the confirmation is the thing itself rather than a
thumbnail of it.

## Requirements

Hyprland (tested on 0.56.2, Lua config), plus:

    gpu-screen-recorder   capture (KMS and xdg-desktop-portal backends)
    ffmpeg / ffprobe      render, proxies, probing
    qt6 (qml6)            the editor, the setup bar, the HUD
    mpv                   the live camera self-view
    slurp, jq             region picking, compositor queries

Python 3.12+. The optional caption engine (`faster-whisper`) lives in the repo venv.

## Documentation

Full docs, and a rendered version for GitHub Pages:

    docs/how-it-works.md      the model: bundles, the four stages, why the stage order
    docs/engines.md           capture backends, render, audio, redaction -- and the numbers
    docs/using-the-editor.md  the setup bar, the HUD, the timeline, every shortcut
    docs/index.html           all three as one page; what Pages serves

## Installing it on another machine

The core needs no build step -- it runs out of the checkout, on the system Python.
A venv is only for the optional caption engine; every entry point falls back to
`python3` when there is none.

    git clone https://github.com/the2dl/omarchy-studio
    sudo pacman -S --needed gpu-screen-recorder ffmpeg qt6-declarative mpv \
                            slurp jq wayland wayland-protocols

Then bind a key, as under "Using it".

Two optional native pieces, in the order they are worth having:

    contrib/omarchy-capture-window/build.sh            true single-window capture
    contrib/hyprland-studio-screenshare/install.sh     exclusion instead of blackout

Each checks its own toolchain first and names what is missing, rather than failing
somewhere in the middle of a build. Between them they want:

    sudo pacman -S --needed base-devel cmake rsync

    # aarch64 only, for the screenshare plugin -- see "On aarch64" below
    sudo pacman -S --needed capstone

Everything else they link against is already on a machine running Hyprland.

Neither is required and neither fails loudly, which is the point of listing what you
give up. Without the first, "just this window" records the window's RECTANGLE, so
anything on top of it lands in the take; the dispatch detects the missing binary and
falls back on its own. Without the second, `no_screen_share` still works -- it is
stock Hyprland -- but it paints black rectangles over excluded windows rather than
showing what is behind them, so a portal capture gets a black box where the camera
self-view was.

### On aarch64 (Apple silicon under Asahi)

Both native pieces build and run, and the recorder needs no flags to work here. Two
things differ, and neither needs anything from you beyond the extra package above.

**The screenshare plugin hooks differently.** Hyprland's own function hooking is
x86-64 ONLY -- `CFunctionHook::hook()` in `src/plugins/HookSystem.cpp` opens with

    // check for unsupported platforms
    #if !defined(__x86_64__)
        return false;
    #endif

because the mechanism is an inline trampoline assembled from raw x86 opcodes, with
the displaced prologue decoded by the udis86 *x86* disassembler. None of it ports:
aarch64 has fixed 4-byte instructions, no absolute branch, a pile of PC-relative
forms that need re-encoding when moved, and a non-coherent i-cache. Upstream is not
going to fix it (hyprwm/Hyprland#15684 is low prio with nobody on it).

So on aarch64 the plugin hooks with **funchook** instead, vendored at
`contrib/hyprland-studio-screenshare/vendor/funchook/` (arm64+unix+capstone subset
only, GPL-2 with a linking exception). It links the SYSTEM capstone, which is the one
extra package. x86-64 is untouched and still uses `HyprlandAPI::createFunctionHook`.
The CMake guard is on `CMAKE_SYSTEM_PROCESSOR`, so the same `install.sh` does the
right thing on either architecture with no flags.

**The camera encodes on the CPU.** There is no hardware video encoder reachable from
Linux on Apple silicon: no libva driver for the GPU, no Vulkan video-encode queue, no
V4L2 M2M encoder, and `apple_avd` is a *decoder*. Asahi's own feature table lists
Video Encoder as TBA. `bin/omarchy-capture-camera` probes for VAAPI once and falls
back to libx264 in the same one-graph shape -- measured at 110-129% of one core at
1920x1440@30 with the live preview running. The screen side already did this via
gpu-screen-recorder's `-fallback-cpu-encoding`.

The plugin's install script deliberately stops short of loading it. Read the comment
at the top of that script before running it; the first load wants a session you can
afford to lose.

A Hyprland update leaves that plugin stale until it is rebuilt, and the built-in
answer -- `hyprpm update` -- rebuilds EVERY registered repository, which is how a
working hyprexpo build was destroyed here. So there is a hook that rebuilds only this
one, for `exec_on_start` alongside the usual `hyprpm reload`:

    o.exec_on_start(".../contrib/hyprland-studio-screenshare/ensure-loaded.sh")

Loaded already, which is every login but the first after an update, it is one IPC
call and an exit (7ms). Otherwise it reloads, and rebuilds only if that was not
enough. Skipping it costs the live camera preview and nothing else: the recorder asks
the compositor at record time and parks or drops the self-view when exclusion is
missing, so a stale plugin cannot reach a recording.

## Using it

    bin/omarchy-capture-screenrecording      # opens the setup bar; again to stop
    bin/omarchy-studio <bundle>              # open a recording in the editor
    bin/omarchy-recordings                   # the library

Bind the first to a key. There is no PrintScreen on every keyboard, and the stock
Omarchy capture bindings assume there is:

```lua
o.bind("SUPER + SHIFT + code:13", "Screenrecording (studio)",
       "/path/to/omarchy-studio/bin/omarchy-capture-screenrecording")
```

## How capture actually works, and why it is not one code path

Wayland does not let an application record whatever it likes, and the three ways in
have different powers. The recorder picks between them, and the reasons are worth
knowing because they leak into what the product can offer:

**KMS** (`-w <monitor|region>`) reads the DRM scanout *below* the compositor. It is
fast, it does 60 fps, and it can capture a sub-rectangle directly. It also cannot be
told to hide anything: `no_screen_share` is invisible to it, which is why the
recording HUD and the teleprompter are physically *moved* out of the captured
rectangle rather than marked, and why they step aside entirely when there is nowhere
to move them to.

**The portal** (`-w portal`) goes through the compositor, so it *can* hide a window —
that is how the camera self-view sits inside the frame without being recorded. But
it only ever hands over a whole monitor, so a region that wants a live self-view
records the display and carries the selection as a crop. It also runs at 30: a 60 fps
CFR master off this path was measured duplicating 15–50% of its frames at 5K, and an
honest 30 beats a 60 that is lying about half of them.

**`hyprland_toplevel_export_v1`** renders one window's surface tree alone — no
occluding windows, no `dim_special`, no decoration. This is the ScreenCaptureKit
equivalent and it is [in progress](contrib/omarchy-capture-window/). Measured at
**59.6 fps** on DMA-BUF for a 5076×2768 window, against 44.8 on the shm path.

Because a window pick is otherwise a *rectangle*, and a rectangle records whatever is
drawn on top of it.

## Design rules

The codebase is opinionated about a few things, and they are load-bearing:

- **One seam per concern.** One geometry module, one cut map, one timebase. The
  editor and the export must never compute the same thing twice — they agree right
  up until they don't, and the disagreement ships as a preview that is subtly not the
  video.
- **Capture keeps everything; the edit decides.** Export size, framing, camera
  placement and loudness are all decisions made after the take, not before it.
- **Defaults must not cost the user something they did not ask for.** Recording more
  than the selection is a checkbox, because picking one window is frequently a
  decision *not* to record the rest of the screen.
- **A control that would visibly do nothing is worse than no control.**

## The mark

Three nested frames receding to a filled core: a zoom push-in, which is the editor's
signature move, and a screen within a screen. `docs/media/logo/` carries the SVGs, the
rasterised PNGs and the handoff notes -- including the two rules worth knowing before
using it anywhere:

- **Below 24px, use the small mark.** It is not an optimisation. The full mark's
  receding strokes thin out and its opacity ramp turns to grey mush at that size, so
  the small form drops the middle frame and thickens the outer one. The editor's own
  title bar is a 20px instance and uses it.
- **Single colour, always.** The three frames differ in opacity, never in hue. No
  gradient, no glow, no second colour.

## Tests

    .venv/bin/python -m pytest -q          # 881 passing
    /usr/lib/qt6/bin/qmltestrunner -input editor/setup/tst_reframe.qml

The QML suites are not optional decoration. QML fails *quietly*: a property read off
the wrong object resolves to `undefined`, an anchor inside a positioner is undefined
behaviour, and neither raises anything at runtime. `qmllint` and the `tst_*.qml`
cases catch exactly that class, and each one in the tree exists because something
shipped broken.

## Status

Usable, and used. The native single-window recorder is the current build; the
`contrib/omarchy-capture-window/` README documents what is proven and what is left.
