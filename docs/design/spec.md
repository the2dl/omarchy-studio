# Screen recorder — UI spec

Handoff for implementation. `screens.png` is the rendered target for all eight
screens; ids below match the badges in that image and in `mockup.dc.html`
(open it in a browser to inspect exact markup and measure anything not listed).

Platform target: Linux / Hyprland. **No window chrome** — the app draws its own
frame, full bleed, 16px corner radius.

---

## 1. Tokens

### Color

| Token | Value | Used for |
|---|---|---|
| `bg` | `#0d0e10` | app background, panel fill |
| `bg-deep` | `#0a0b0d` | timeline tray (recessed) |
| `bg-float` | `rgba(13,14,16,0.92)` | floating panels over wallpaper, with blur |
| `canvas` | `linear-gradient(150deg, #1d1a17, #121316)` | editor canvas backdrop |
| `accent` | `#f2a25a` | primary action, selection, active state |
| `accent-dim` | `#c9854a` | secondary meter segments |
| `accent-on` | `#1a1006` | text/icons **on** accent fills |
| `live` | `#e0523f` | recording indicator — nothing else |
| `text` | `#f6f4f2` | primary |
| `text-2` | `#eceae7` | body |
| `text-3` | `#b3aea8` | labels, secondary values |
| `text-4` | `#837e79` | inactive segment labels |
| `text-5` | `#625d58` | section captions, uppercase labels |
| `text-6` | `#55514d` / `#4d4945` | hints, timeline ruler |
| `hairline` | `rgba(255,255,255,0.07)` | dividers |
| `fill-subtle` | `rgba(255,255,255,0.05)` | select/input fill |
| `fill-hover` | `rgba(255,255,255,0.07)` | active segment, hover row |
| `track` | `rgba(255,255,255,0.10)` | slider track |

Greys are warm-shifted (a touch of red/yellow) to sit with the amber. Do not
substitute neutral greys.

Red appears exactly once in the product, on the live dot. Every other
"important" thing is amber.

### Type

JetBrains Mono (JetBrainsMono Nerd Font), weight 400/500 only.

| Role | Size |
|---|---|
| screen title / primary | 15px |
| row label, value, button | 12–13px |
| uppercase section caption | 11px, `letter-spacing: 0.08em`, uppercase |
| hint, ruler, meter readout | 10–11px |

### Geometry

| Thing | Value |
|---|---|
| app window radius | 16px |
| floating panel radius | 15–18px |
| row / control radius | 9–11px |
| segment chip radius | 7px |
| panel width (inspector) | 320px |
| hairline | 1px |
| panel shadow | `0 20px 52px rgba(0,0,0,0.60)` + `inset 0 1px 0 rgba(255,255,255,0.07)` |
| floating over wallpaper | add `backdrop-filter: blur(26px)` |

### Controls

- **Slider** — 3px track, 10px round thumb, filled portion `accent` when the
  property is the panel's subject, `text-3` when it's secondary. Value sits
  right-aligned on the caption line, never under the track.
- **Toggle** — 30×17px pill, 13px knob, 2px inset. On: `accent` track,
  `accent-on` knob. Off: `track` fill, `#6d6863` knob.
- **Segmented** — equal-width chips in a row, 4px gap; active gets
  `fill-hover` + `text-2`, others `text-4`. No borders.
- **Primary button** — `accent` fill, `accent-on` label, 10px radius,
  `0 6px 18px rgba(242,162,90,0.22)`.
- **Selected card / keyframe** — `inset 0 0 0 1.5px accent` plus a 9–28% accent
  wash. Selection is always a hairline ring, never a heavy fill.

---

## 2. Screens

### 1a — Menubar dropdown (300px)
Status header (state caption + current source) → primary actions → input
section. `Start recording` is the only accented row. Shortcut hints right-aligned
in `text-5`.

### 1b — Pre-record setup (620px)
Tabs (Screen / Window / Area / Camera) → source thumbnails, two-up, live
wallpaper preview inside each, selected one ringed in accent → mic row with a
12-segment level meter → camera row with Off/Circle/Corner → footer with settings
summary in `text-5` and the Record button right.

Meter segments: 2px gap, heights vary per level; hot segments `accent`, mid
`accent-dim`, tail `#4a423a` → `#2a2825`.

### 1c — Recording HUD
Floating pill, bottom center, 26px from the edge. Left to right: live dot →
elapsed time → divider → 6-bar mic meter → pause / edit / delete → divider →
Stop.

Live dot: 9px, `live`, pulsing halo —
`@keyframes` from `box-shadow: 0 0 0 0 rgba(224,82,63,0.5)` to
`0 0 0 5px rgba(224,82,63,0)`, 1.8s ease-in-out infinite.

### 1d — Editor (1560×880 shown)
Four regions:
1. **Top bar, 46px** — file icon, name, duration/format in `text-6`, then
   undo/redo, Preview, Export (primary).
2. **Left rail, 56px** — 6 tool glyphs, 19px, active one gets an accent tile
   (9px radius, `accent-on` glyph).
3. **Canvas** — recording sits on `canvas` gradient with 46px padding and its own
   10px radius + `0 28px 64px rgba(0,0,0,0.6)`. The active zoom region draws as an
   accent hairline rect with `0 0 0 9999px rgba(0,0,0,0.28)` dimming everything
   outside it, and a small uppercase label chip above it. Webcam bubble sits
   bottom-right with four accent corner handles when selected.
4. **Right inspector, 268px** — contextual to the selected object (here: Zoom 3).
5. **Timeline tray** — recessed `bg-deep`, top hairline. Transport row, then a
   ruler, then four rows with 74px uppercase gutter labels:

| Row | Height | Content |
|---|---|---|
| screen | 40px | filmstrip thumbnails, 2px gaps, 50% opacity |
| zoom | 26px | 18px keyframe blocks, accent wash + hairline; selected is brighter with 1.5px ring |
| clicks | 16px | 6px dots; ones that produced a zoom are `text-3`, the rest `#6d6863` |
| audio | 30px | waveform bars, `#4a4640`, center-aligned |

Only the `zoom` gutter label is accented — it's the row you actually work in.

### 1e — Zoom & cursor inspector (320px)
Header names the selected keyframe with its time range. Zoom: scale slider,
easing segmented (Smooth / Snap / Linear), two follow toggles. Divider. Cursor:
size, smoothing, click ripple. Zoom properties use accent sliders, cursor
properties use `text-3` sliders — accent marks what the panel is *about*.

### 1g — Camera (320px)
Toggle lives in the header, so the whole panel can be switched off from one
place. Device select → shape segmented → **3×3 placement grid** (30×22px cells,
3px gap, selected cell accent-ringed with a centered dot) with size and inset
sliders beside it → Mirror / Hide while zoomed / Blur background.

The grid is the primary placement control; dragging the bubble on canvas is the
secondary one. Note under the grid says so.

### 1h — Click zoom (320px)
Master toggle in the header with a count ("14 zooms placed") under the title.
Sensitivity segmented (Sparse / Medium / Every click) with a one-line
explanation of the current setting → max scale → hold after click → Zoom on
scroll / Zoom on typing toggles → `Re-detect from clicks` action with a warning
line about hand-edited zooms being preserved.

When the master toggle is off, everything below the header dims to `text-4`/
`text-6` and stops responding — it does not disappear.

### 1f — Export (480px, rendered as a pane — see Decisions)
Title bar with close → format segmented (MP4 selected: accent wash + ring) →
resolution and frame rate selects, two-up → quality slider with a live size
estimate → divider → render progress with percent in accent and time remaining.

The progress section only exists while rendering; before that the footer holds
the Export button.

### 2a — Layers (editor, layer tool active)

The left rail stays 56px; selecting the layers tool slides out a **236px layer
list** beside it on `bg-deep`, hairline on both sides. The canvas and inspector
shrink to fit; the timeline is unaffected.

**List = z-order.** Top of the list is front-most. `front` and `back` sit as 10px
`text-6` captions at the top and bottom of the list so the direction is never
ambiguous. Each row: drag handle → type glyph → name + time range → visibility
eye. Rows are 9px/10px padded, 9px radius; selected row gets the accent wash +
1.5px accent ring (same as any selection in this UI).

Layer types and glyphs: image `image`, text `title`, shape `rectangle`,
blur/pixelate `blur_on`. Arrows are out of scope.

A hidden layer keeps its row but drops name to `text-5`, glyph to `#6d6863`, and
shows `visibility_off` at `#3d3936`.

**Add** is the `add` glyph in the list header (menu: Image, Text, Shape, Redact).
**Delete** is the `delete` glyph at the list footer, acting on selection; also
Backspace. **Drop target**: a dashed-equivalent (accent 1.5px ring at 28% on a 5%
accent wash) panel pinned to the bottom of the list, always visible, reading
"Drop an image / lands at the playhead". Dropping anywhere on the canvas works
too and creates the layer at the drop point.

**Canvas representation.** Selected layer draws 9px accent corner handles at the
four corners and a small type chip above the top-left corner
(`blur_on` + "redact · strong"). Unselected layers draw nothing.

**Timeline representation.** A `layers` group row, gutter label accented like
`zoom`, containing one 18px sub-row per layer in list order. Each layer's bar
spans its time range: 12px tall, 4px radius, `text-3` at 16-22% with a matching
hairline; the selected layer's bar switches to the accent treatment. A layer
covering the whole recording spans the full width — that is the intended read for
"always on".

### 2b — Layer inspectors

Same 268px column and header pattern as the zoom inspector. Sections in order:
identity → content → appearance → position → timing.

- **Image**: 74px thumbnail well, then dimensions/format/"copied into the
  project" in `text-6` (the file is copied, not linked — say so), size, opacity,
  3×3 placement grid, timing.
- **Text**: editable field with an accent caret, style segmented
  (Plate / Plain / Outline), size, four-swatch color, timing.
- **Blur / redact** (in 2a's inspector): method segmented
  (Blur / Pixelate / Fill), strength, timing with in/out fields and a
  "Follow the window" toggle, then x/y/w/h.

Timing pattern, shared: in/out fields two-up, a "Whole recording" toggle that
disables them, and a transition segmented (Cut / Fade / Slide).

### Redaction — the one hard rule

**Blur strength is three presets, never a slider: Strong / Heavy / Solid.**

The weakest available preset is already above the threshold where text is
recoverable. There is no way to dial redaction down to "looks about right",
because that is exactly the setting that leaks a password.

**The canvas renders export strength, not a stronger preview.** The redact layer
in 2a is drawn as the real blur plus a 45° accent hatch at 14% and a small
uppercase "redacted" label — the hatch marks it as an *editing object* without
making the obscuring look heavier than it is. Never apply an editor-only extra
blur, and never show the un-blurred content on hover, select, or scrub.

The inspector carries this as a standing note next to the strength control, in a
`shield`-marked panel:

> Three presets, no slider. The weakest one already exceeds what OCR can
> recover, and the canvas shows exactly what exports — never a preview that
> looks safer than the file.

"Follow the window" tracks the redaction to the window it was placed over, so a
window move mid-recording doesn't slide the secret out from under the box.

### 2c — Cuts

A cut is **a fold, not a hole.** Three states:

1. **Selecting** — drag on the ruler (or C from the playhead). The range gets an
   accent cap on the ruler carrying its duration, and the frames underneath dim
   to `rgba(10,11,13,0.72)` with 1.5px accent edges.
2. **Collapsed** — the span folds to a **16px seam spanning the full tray
   height**, all rows at once: `bg-deep` fill, 1px accent ring at 50%, and a
   vertical dashed accent stripe down the middle. A chip at the top of the seam
   shows `unfold_more` + the removed duration. One cut is one object; do not
   draw a gap per row.
3. **Expanded** — click the seam and the removed frames return **in place**,
   ghosted (66% scrim + 45° accent hatch at 10%), with 3px accent edge handles.
   A footer row offers `restore` "Restore 6.0s" and "Collapse", plus the hint
   "drag either edge to retime · ⌫ removes the cut".

State 3 is the design's whole argument: the frames are still there. Nothing in
the cut UI should use destructive language or red — a cut is a view, not a
deletion. Reflect it in the transport too: `0:14 / 2:41` with
`2:47 recorded · 6s cut` beside it in `text-6`, so the original length stays
visible.

Removing a cut is Backspace on a selected seam, or Restore. Both are undoable.

### 2d — Recordings (720px)

Header (count + total size, search, primary New) → filter chips
(All / Unedited / Exported) → rows.

Row: 78×46px thumbnail with duration chip → name + meta → `more_horiz` → Edit.
The newest unedited recording gets the accent wash, a `new` badge, and a filled
Edit button; everything else gets a subtle Edit. A row whose source is gone shows
`link_off` next to the name, "source moved" as its meta, and its action becomes
"Locate…".

### 2e — Proxy build

The editor **opens immediately** in this state; the shell is real, only playback
is not. Top bar renders normally with Preview and Export in `text-6` and no fill.
Canvas shows the first frame at 35% opacity behind a 320px progress block:
label + percent (accent) + estimate, 4px bar, then

> Building a 1080p working copy. The 4K original is untouched and is what
> exports.

Two actions: **Trim while it builds** (the one edit that works without seeking)
and **Cancel**. Timeline thumbnails fill in left to right as the proxy encodes;
un-encoded cells sit at `rgba(255,255,255,0.03)` with a caption saying so.

Progress is per-file and resumable. Reopening a recording whose proxy exists
skips this state entirely.

### 2f — Burned-in webcam

Reuses the 1h disabled pattern exactly: the panel stays, everything below the
header drops to `text-6`/`#4d4945`, sliders become empty 3px tracks, the toggle
is replaced in the header by a `burned in` badge, and an `info` panel explains
why:

> This take was recorded fullscreen with self-view on, so the camera is part of
> the frames. Nothing here can move or remove it.

One live control remains at the bottom — **Cover it with a redact layer** — which
is the only thing that actually helps.

### 2g — Errors and empties

350px cards, `#121316`, 13px radius. Every one: glyph + title, one or two lines
of `text-3` explaining state and consequence, then two actions where the
recoverable one is first.

| Card | Glyph | Tone |
|---|---|---|
| No camera found | `videocam_off` | `text-3` — not an error, recording still works |
| Disk almost full | `storage` | `live` glyph — names the time remaining and the automatic stop |
| Render stopped | `error` | `live` glyph — accent Retry, "Copy log" secondary |
| Source file missing | `link_off` | `text-3` — states that edits survive |
| Empty library | `video_library` | centered, spans both columns, primary Record button |

`live` red appears on error glyphs only — never on a button, never as a fill.
Copy rule: say what happened, then what survived, then what to press. No
apologies, no exclamation marks.

---

## 4. Decisions

**Export is a pane, not a modal.** 1f's card becomes the right inspector's
content in the same 268px column. Renders are the one thing you want to keep
working alongside, and a modal blocks the canvas for their duration. Once a
render starts the pane can be dismissed; progress collapses into a top-bar chip
(13px conic-gradient ring + "Rendering 64%" in accent on an 11% accent wash, as
shown in 2a). Clicking the chip reopens the pane.

**A finished recording auto-opens the editor.** Pressing stop is deliberate and
editing is the next thing you wanted, so stop leads straight into 2e. If the
recording ended any other way — hotkey from another workspace, disk pressure,
crash recovery — it lands in the list and posts a notification whose primary
action is Edit. The menubar keeps a permanent Recordings entry as the
always-available path.

---

## 5. Behavior notes

- Every hover/active transition is **120ms ease**. Focus and selection changes
  ~200ms.
- Nothing in the UI uses a border to mean "container" — containers are fills and
  shadows, borders/rings mean *selected*.
- Timeline rows scale horizontally with zoom level; gutter labels stay fixed.
- The canvas dim overlay (`0 0 0 9999px`) is how zoom regions read; keep it
  rather than drawing four separate scrims.
