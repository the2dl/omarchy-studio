# Engines

What actually does the work, and why each choice was made. Most of these are decisions
that could reasonably have gone the other way; the reasoning is recorded so it can be
re-examined rather than re-litigated.

---

## Capture: three backends, not one

Wayland does not let an application record whatever it likes. The three ways in have
different powers, and the recorder picks between them — the differences leak into what
the product can offer, so they are worth knowing.

| | KMS | Portal | Toplevel |
|---|---|---|---|
| **Reads** | DRM scanout, below the compositor | xdg-desktop-portal stream | one window's surface tree |
| **Frame rate** | 60 fps | 30 fps | display rate |
| **Can capture a sub-rectangle** | yes | no — whole monitor only | n/a |
| **Can hide a window** | **no** | yes | n/a — sees only its own window |
| **Used for** | monitor, area | anything needing a hidden self-view | true single-window |

**KMS** is the default and the fastest. It reads the scanout *below* the compositor,
which is exactly why it cannot honour `no_screen_share`: the compositor's decision to
hide a window has already been applied to a layer this path never sees. Anything marked
hidden is still in the frame.

**Portal** runs at half the rate but the compositor is in the loop, so windows can be
excluded. This is the backend that makes a live camera self-view possible: the bubble is
on screen for you and absent from the recording.

**Toplevel** (`hyprland_toplevel_export_v1`) renders one window's surface tree alone —
the Linux equivalent of ScreenCaptureKit's window capture. This is what "just this
window" means when it means it literally: anything stacked on top of the window is not
in the take, because those surfaces are never composited into the frame.

A region capture that also wants a self-view takes the whole monitor through the portal
and crops back at export, because only KMS can capture a sub-rectangle directly and only
the portal can hide the bubble. macOS never faces this — it excludes windows on the same
fast path whatever the capture is.

### The recorders

- **gpu-screen-recorder** drives the KMS and portal paths.
- **`contrib/omarchy-capture-window`** is a native recorder written for the toplevel
  path: dmabuf capture straight from the compositor, VAAPI encode, AAC audio, and a
  CFR clock that gap-fills when the compositor stops rendering. It is optional; without
  it, "just this window" falls back to capturing the window's *rectangle*, so anything
  on top lands in the take.

### The compositor plugin

`no_screen_share` is stock Hyprland, and it paints a **black rectangle** over an excluded
window. `contrib/hyprland-studio-screenshare` upgrades that to genuine exclusion — the
background shows through instead. Optional: without it the self-view is a black box in a
portal capture rather than invisible, and the recorder checks at record time and parks
or drops the bubble rather than spoiling the take.

---

## Input: the click and cursor track

A daemon samples the pointer at 120 Hz into `events/cursor.bin` and logs clicks to
`events/input.jsonl`. Both carry `CLOCK_MONOTONIC` timestamps, tied to video frame 0 by
the anchor in `capture.json`.

This is what makes the cursor and zoom *editable* rather than baked. The export draws a
smoothed pointer from the samples, and derives zoom moves by clustering the clicks.

**Clicks on UI are discarded.** `capture.json` carries `chrome_rects` — the compositor's
reserved edges, plus the recording HUD's own rectangle, which it writes down once it
knows where it landed. Without that second one, every take ended with a zoom lunging at
the Stop button.

---

## Zoom: derived, not authored

Zoom moves are computed from the click track at render time, not stored as keyframes:

1. Clicks land on the output timeline; clicks inside a cut are discarded.
2. Nearby clicks **cluster** into one move. Without clustering, a burst of clicks in one
   dialog produces one zoom per click, each easing out before the next eases in — the
   pump.
3. Each cluster becomes a quintic ease-in, a hold, and an ease-out.

Deleting one move stores the **source frames of its clicks**, not the move's index or
its time range — both of those shift as soon as any other edit lands. The click track is
part of the capture and never changes, so it is the one stable name.

The warp itself is ffmpeg's `perspective` filter, gated with `enable=` so it runs only
on frames a zoom is actually in flight. On a 510-frame export with two zooms, that is
434 frames that were previously paying full price for an identity warp: **56.2s → 31.2s,
output bit-identical.**

---

## Render: one graph, one process

The whole edit compiles to a single ffmpeg filtergraph, written to a file — argv dies at
about 288 KB with `E2BIG`, and `-filter_complex_script` was removed in ffmpeg 9. The
surviving syntax is `-/filter_complex <path>`.

### Composite at the delivered size

Everything below the zoom is built at the size the export will be, not at the master's.
The background is a band-limited gradient, the shadow a Gaussian whose sigma is
proportional to its radius, the corner mask analytic coverage — all resolution-independent
by construction. Building them at four times the pixels and discarding three quarters
buys nothing.

This is also *better* quality, not a trade. The backdrop scales the video down to make
room for its own padding, so the old arrangement resampled the picture twice; now the
zoomed master goes straight to its final size in one lanczos step.

The zoom stays at the master's resolution, because `perspective` outputs at its input
size and so needs at least `export_height × deepest_zoom` pixels to keep the deepest
hold at 1:1.

Measured on a 17-second 5120×2880 capture at 1440p:

| | |
|---|---|
| decode + one downscale + encode (the floor) | 4s |
| full export, before this work | 88s |
| after compositing at delivered size | 54s |
| after gating the zoom + cursor chroma | **18.6s** |

### Encode on the CPU

VAAPI measured *slower* — 18.11s against 19.82s at 1080p, 49.43 against 52.80 at 4K.
Every filter in the graph is CPU-only, so `hwupload` only adds a copy; VAAPI offloads the
encode, which was never the bottleneck. (The native window *recorder* does use VAAPI,
because there the encode is all there is.)

---

## Audio

The chain, in order, and every step is placed deliberately:

```
afade (mute the start pop)  →  cut  →  declick  →  loudnorm  →  aresample 48k
```

- **The pop fade is before the cut**, so it belongs to the capture's start rather than
  the edited timeline's. Cut the head away and the fade goes with it, instead of
  silencing the first 450 ms of whatever now begins the video.
- **Declick is before loudnorm.** A key clack is the loudest thing in a narration track,
  so normalising first would set the gain from the noise and leave the voice quiet.
- **loudnorm is after the cut**, because it measures the material it is normalising and
  a cut that removes a loud passage changes the right answer.

### Removing keyboard clicks

RNNoise (`arnndn`) with a vendored model, followed by `afftdn`. Chosen by measurement on
a real take, scoring each candidate by the *gap* between how much it attenuates the
clack windows and how much it attenuates speech — the gap is what matters, since
loudnorm follows and makes absolute level irrelevant:

| filter | clicks | speech | gap |
|---|---|---|---|
| `adeclick` | −0.7 dB | −0.3 dB | 0.4 dB |
| `afftdn` alone | −6.5 dB | −0.5 dB | 6.0 dB |
| `arnndn` | −11.1 dB | −4.9 dB | 6.2 dB |
| **`arnndn` + `afftdn`** | **−17.9 dB** | **−5.8 dB** | **12.1 dB** |

Tone breaks the tie: measured as high-band minus low-band energy, the source sits at
−13.4 dB and this chain lands at −15.0 dB — 1.6 dB from the source, while taking 18 dB
off the clacks. Aggressive alternatives scored better on the gap and left a dulled,
underwater voice.

Both denoisers buffer, and nothing in ffmpeg compensates it: `arnndn` delays 9.94 ms and
`afftdn` 25.00 ms. Without a trim, turning the option on pushed the entire audio track
~35 ms behind the picture — a lip-sync error nobody would attribute to a denoiser. The
chain trims its own latency off the front.

`loudnorm` outputs at 192 kHz internally, so the chain resamples back to the 48 kHz the
capture side writes. Without it, every normalised export carried 96 kHz AAC off a 48 kHz
capture: twice the rate, none of the information.

---

## Redaction

Blur and pixelate are applied at export, at a strength that rides a shared preset ladder
so switching method never silently changes how much is recoverable. Both the QML preview
and the ffmpeg export compute their strength from the *same* function — Qt's MultiEffect
and ffmpeg's `gblur` are different kernels, so the preview value is the export sigma
mapped back through a measured ratio. Calibrated to match, not chosen to look right.

The blur sigma is in pixels while the mosaic block is a fraction of canvas width, so the
sigma is scaled by the composite ratio. Otherwise a 1440p export and a 1080p proxy would
disagree about how much a redaction hides — which is the failure this whole area exists
to prevent.

Redactions are never partially transparent, and never faded. A translucent blur box
leaks the pixels it exists to hide.

---

## Timing

Everything is frame indices on a single project grid, not seconds.

The camera is aligned **in the graph**, with `tpad`/`trim`, not with `-itsoffset`.
`-itsoffset` is in seconds while the cut is in frame indices, and the two do not agree
in both directions: `-itsoffset 0.2` into `fps=30` delays by exactly the 6 frames asked
for, but `-itsoffset -0.2` advances by 4 rather than 6, because frames with negative
timestamps are dropped by a rule of their own.
