# omarchy-studio

A non-destructive screen recorder for Linux, built for Hyprland.

![A recording playing back in the editor](docs/media/demo.gif)

*Recorded, edited and exported with omarchy-studio.*

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
