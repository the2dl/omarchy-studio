# Using it

Three surfaces: the **setup bar** before you record, the **HUD** while you record, and
the **editor** afterwards. Plus a **library** for finding old takes.

---

## Before recording — the setup bar

```
bin/omarchy-capture-screenrecording        # opens the setup bar; run again to stop
```

Worth binding to a key. There is no PrintScreen on every keyboard, and the stock Omarchy
capture bindings assume there is:

```lua
o.bind("SUPER + SHIFT + code:13", "Screenrecording (studio)",
       "/path/to/omarchy-studio/bin/omarchy-capture-screenrecording")
```

A bar appears at the bottom of the screen with four choices:

| | |
|---|---|
| **Mode** | display, window, or area |
| **Mic** | which microphone, or none |
| **Camera** | which camera, or none |
| **System audio** | on or off |

**Start is not on the bar.** Each display gets an overlay showing its name, resolution
and frame rate, with the Start button in the centre — so pressing it is itself proof you
picked the right monitor.

Two options worth understanding:

- **Just this window** captures only that window's own surfaces. Anything stacked on top
  is not in the take. Needs the native window recorder built; without it you get the
  window's *rectangle* instead, and whatever is over it.
- **Follow window** records the whole display and carries your selection as a crop, so a
  window that moves mid-recording can be followed afterwards. It puts the whole screen
  on disk, which is a privacy consideration: everything beside the window is in the
  bundle where an export could reach it.

Press Start and a countdown runs. Capture is already initialising during it, and every
setup surface is destroyed before it ends.

---

## While recording — the HUD

A floating pill with elapsed time, a live microphone level, and three buttons:

| | |
|---|---|
| **Pause** | stop and resume without ending the take |
| **Discard** | end it and delete the bundle |
| **Stop** | finish and open the editor |

It is kept out of the recording by the compositor, and it refuses to appear at all
rather than be recorded — if there is nowhere to put it outside the capture and the
compositor cannot hide it, it closes itself and tells you the keyboard shortcut instead.

Clicking Stop does not end up in your video: the HUD's rectangle is recorded as UI, and
clicks inside it are dropped from the click track.

There is also a **teleprompter** (`bin/omarchy-teleprompter`) — a floating script you
can read from while recording, held out of the take the same way.

---

## After recording — the editor

```
bin/omarchy-studio <bundle>        # opens a recording
bin/omarchy-recordings             # the library
```

The window is a canvas with a timeline underneath, a settings panel on the right, and a
layer list on the left.

### The timeline

Five rows, top to bottom:

| row | what it shows |
|---|---|
| **screen** | the take, as uniform film cells |
| **layers** | one sub-row per layer, front-most first, each spanning its time range |
| **zoom** | the zoom moves — the row you actually work in, so its label is accented |
| **clicks** | every recorded click; the ones that produced a zoom read brighter |
| **audio** | the audio track |

**Cuts are a fold, not a hole.** A cut span collapses to a 16-pixel seam that spans every
row at once — the x-axis itself folds, so one cut is one object rather than a gap per
row. Click a seam to expand it back and see what is inside.

### Making a cut

1. Press **C** to drop a mark at the playhead, or drag on the ruler.
2. Move to the other end.
3. Press **C** again to commit it. The range folds to a seam.

**Backspace** removes a cut, but only one you have expanded — so a stray Backspace with
nothing open cannot take time out of the movie.

### Zoom

Zoom is automatic, derived from your clicks. The panel gives you **amount**, **hold**,
**ease** and **merge gap** (how close two clicks have to be to become one move instead
of two).

To remove a single unwanted move — the click on a menu you would rather not zoom at —
select it on the zoom row and press **Delete**, or use the Delete button beside its name.
It stays deleted through every later edit. When any are deleted, the zoom section grows a
**restore N deleted** button, because a deleted move leaves no gap on the timeline to
click.

### Layers

Add text, images, captions, shapes, and redactions. Each has an inspector with its own
controls plus a shared **when** section for its time range.

Draw tools live on the canvas: **select**, **text**, **blur** and **pixelate**. With a
tool armed, drag on the canvas to place it. **Escape** disarms back to select.

Redactions are deliberately blunt: no fade, no opacity. A partially transparent blur box
leaks the pixels it exists to hide. What the preview shows is what exports.

### The camera

The camera is its own stream, so the bubble can be moved, resized, reshaped (circle,
rounded, rectangle) or mirrored at any time. Press **S** to split the camera segment at
the playhead — deleting the right-hand half is how a head that is on for the whole take
becomes one that goes away partway.

### Backdrop

A colour or gradient behind the video, with rounded corners and a drop shadow. The
padding, corner radius and background are all adjustable.

### Export

Pick a size — **1080p**, **1440p**, **4k** or **native** — and press Export. A preset is
a ceiling, never a target: asking for 1440p from a 1080p capture gives you 1080p, because
upscaling costs time and file size to make the picture no better.

When the render finishes, your file manager opens at the result.

---

## Keyboard shortcuts

| key | does |
|---|---|
| `Space` | play / pause |
| `←` `→` | step one frame |
| `C` | mark a cut, then commit it |
| `Backspace` | remove the expanded cut |
| `Delete` | delete the selected layer, or the selected zoom |
| `S` | split the camera segment at the playhead |
| `P` | preview mode — hide every editing handle |
| `L` | show or hide the layer list |
| `Escape` | disarm the draw tool, clear a selection, or leave preview mode |
| `Ctrl+S` | save |
| `Ctrl+Z` | undo |
| `Ctrl+Shift+Z` | redo |

Every letter shortcut is suppressed while a text field has focus, so typing a layer's
name cannot fire an edit.

---

## Things worth knowing

**Nothing is rendered until you export.** The editor plays a proxy — a fast draft copy
of the master — and applies your edit live over it. Scrubbing stays responsive on a 5K
recording because you are never touching the master.

**Undo is a whole-project snapshot**, not a per-operation inverse, and drags coalesce
into one step. So one Ctrl+Z undoes one *gesture*, not sixty frames of a drag.

**Saving is atomic.** A crash mid-save cannot leave an unparseable `edit.json`.

**Deleting `edit.json` gives you the raw recording back.** Every edit lives in that one
small file; the master is never touched.

**A take survives being stopped by something else.** If the recorder is killed from
outside — the stock bar indicator, a `pkill`, a crash — the bundle is finalised anyway,
either by a watcher or the next time the dispatch runs.

---

## Captions

`bin/omarchy-studio-transcribe` generates captions from the audio. The engine
(`faster-whisper`) is optional and lives in the repo venv; without it, captions can still
be added and edited by hand.
