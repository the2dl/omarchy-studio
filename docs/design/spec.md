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

### 1f — Export (480px)
Title bar with close → format segmented (MP4 selected: accent wash + ring) →
resolution and frame rate selects, two-up → quality slider with a live size
estimate → divider → render progress with percent in accent and time remaining.

The progress section only exists while rendering; before that the footer holds
the Export button.

---

## 3. Behavior notes

- Every hover/active transition is **120ms ease**. Focus and selection changes
  ~200ms.
- Nothing in the UI uses a border to mean "container" — containers are fills and
  shadows, borders/rings mean *selected*.
- Timeline rows scale horizontally with zoom level; gutter labels stay fixed.
- The canvas dim overlay (`0 0 0 9999px`) is how zoom regions read; keep it
  rather than drawing four separate scrims.
