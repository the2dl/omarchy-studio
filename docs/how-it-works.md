# How omarchy-studio works

The short version: **recording captures pixels, editing captures intent, and export is
the only place the two meet.** Nothing you do in the editor touches what was recorded,
and nothing about how you recorded limits what you can decide afterwards.

That one rule explains most of the design, so it is worth being precise about.

---

## The bundle

A recording is not a file. It is a directory:

```
screenrecording-2026-09-05_13-23-43/
├── capture.json          what the hardware did          IMMUTABLE
├── edit.json             what you decided               MUTABLE
├── media/
│   ├── screen.mp4        the master, at capture resolution
│   ├── screen.mp4.ts     first-frame timestamp sidecar
│   ├── cam.mp4           the camera, recorded separately
│   └── cam.tsv           camera frame timestamps
├── events/
│   ├── input.jsonl       clicks, with timestamps
│   ├── cursor.bin        pointer positions, 120 Hz
│   └── chrome.json       rectangles that are UI, not content
├── proxy/                fast draft copies, rebuilt on demand
└── assets/               images you added
```

`capture.json` is written once and describes only facts: resolution, frame rate, the
monitor and its scale, the clock anchor that ties events to frames. It is never edited,
because it is the record of what actually happened. If it were mutable, every later
question ("where was the cursor at 4.2s?") would have two possible answers.

`edit.json` is everything you chose: cuts, zooms, layers, the backdrop, the camera
bubble, the export size. It is small, it is JSON, and it is the only thing an edit
writes. Delete it and you have the raw recording back, exactly as captured.

**The camera is a separate stream, not burned in.** That is what lets you move the
bubble, resize it, cut it out for part of the take, or drop it entirely — after the
fact. A recorder that composites the camera into the frame has thrown that away before
you ever see it.

---

## The four stages

### 1. Setup — decide what to record

A bar appears at the bottom of the screen, and an overlay on each display shows its
name, resolution and frame rate with a **Start** button in the middle. The button is on
the target itself, which doubles as proof you picked the right monitor.

You choose: a display, a window, or an area; whether the camera is on; which microphone;
and whether system audio is included.

Pressing Start prints the configuration and begins a countdown. Capture initialisation
runs *during* the countdown rather than after it, so the two waits overlap — and every
setup surface is destroyed before the countdown ends, so no frame of the UI can reach
the recording.

### 2. Record — pixels and intent, in parallel

Three things run at once:

- **The screen**, through one of three capture backends (see [engines](engines.md)).
- **The camera**, to its own file with its own timestamps.
- **An input daemon**, sampling the pointer at 120 Hz and logging clicks.

That third one is why the editor can offer automatic zoom. The clicks are recorded as
*data*, not as an effect — so the zoom they imply can be tuned, or deleted, or turned
off entirely, long after the take is over.

A floating HUD gives you elapsed time, a microphone level, pause, discard and stop. It
is kept out of the recording by the compositor, and clicks on it are discarded from the
click track — otherwise every take would end with the zoom lunging at a Stop button the
viewer cannot see.

### 3. Edit — non-destructively

The editor plays a **proxy**: a fast, lower-resolution copy of the master, built in the
background so scrubbing stays responsive on a 5K recording. Everything you see is the
edit applied live over that proxy. Nothing is rendered until you export.

### 4. Export — one ffmpeg process

The whole edit compiles to a single ffmpeg filtergraph. There is no intermediate file,
no round trip, no generation loss. The graph is written to a file rather than passed on
the command line, because a long project's graph exceeds the kernel's argument limit.

---

## Why the stage order is what it is

Export applies the edit in a fixed order: **cut → cursor → zoom → backdrop → layers.**
The order is not arbitrary; each position was measured.

**Cut first.** Everything downstream then runs over fewer frames — 2.20s against 2.83s
on the same project. Layer timings are remapped into the shortened timeline, and
cut-first with remapped ranges produces bit-identical frames to cut-last with verbatim
ranges. So it is provably free.

**Cursor before the zoom**, so the zoom magnifies the pointer along with the pixels
underneath it. Drawn after the zoom, the pointer would need the zoom's per-frame
viewport inverted into its own position maths — and any disagreement between the two
copies shows up as the pointer sliding across the thing it is pointing at.

**Zoom second**, while the frame is still planar YUV. The same warp costs 8.4 ms/frame
at 1440p on `yuv420p` and 14.9 ms/frame once the backdrop has converted the stream to
RGBA. Identical pixels, nearly double the price.

**Backdrop third**, and it is the stage that fixes the output length. The background is
an infinite source; the real video reaches it through `shortest=1`. The other way round,
the background's own timebase governs the output — a 6.9s clip once came out 208
seconds long.

**Layers last.** Their cost scales with how many exist, not how long each is visible
(2.75s against 2.70s for a layer visible 1s of 20 versus throughout), so there is
nothing to gain by ordering them.

---

## What "non-destructive" actually buys you

- **Re-export at any size.** The master is kept at capture resolution. A preset is a
  ceiling, never a target — asking for 1440p from a 1080p capture gives 1080p, because
  inventing pixels costs time and file size to make the picture no better.
- **Change your mind about the camera** — position, size, shape, or whether it appears
  at all, in any part of the take.
- **Delete an automatic zoom** you did not want, without turning the feature off.
- **Reframe after the fact.** A window that moved mid-recording can be followed, because
  the window's position was recorded alongside the pixels.
- **Redact later.** Blur and pixelate are applied at export, at a strength calibrated so
  the preview shows exactly what ships.

The cost is that a bundle is bigger than a video file, and export takes real time. Both
are deliberate trades.

---

## Where to go next

- **[Engines](engines.md)** — the capture backends, the render pipeline, the audio
  chain, and why each is the way it is.
- **[Using the editor](using-the-editor.md)** — the setup bar, the HUD, the timeline,
  and every keyboard shortcut.
