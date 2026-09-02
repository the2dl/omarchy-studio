# Handoff — screen recorder UI

Three things in here:

- **`spec.md`** — the design spec. Tokens, control anatomy, and a per-screen
  breakdown. Start here.
- **`screens.png`** — the first eight screens rendered at 1:1.
- **`screens-layers-cuts.png`** — layers, cuts, recordings, proxy build, disabled
  camera, and error/empty states.
- **`mockup.dc.html`** — the working mockup. Open it in a browser to
  inspect real markup and pull exact values for anything the spec doesn't name.
  `support.js` and `uploads/` are its dependencies; keep them next to it.

## Reading the mockup

Every screen is inline-styled, no stylesheet, so computed values are visible
directly on the element. Screens are wrapped in divs with the id shown as a
badge (`1a`, `2c`, …) matching the spec sections. The `2x` set is the newer
turn and sits above the `1x` set in the file.

The mockup is a static mock: nothing is wired, no state, no interaction. Sliders,
toggles and segmented controls are drawn in one representative position. Treat
positions as *examples of the visual state*, not defaults.

## What is not specified

Deliberately left open — decide these in implementation:

- Permission prompts (Hyprland screencopy / portal consent).
- Keyboard shortcuts beyond the handful named in the spec.
- Arrow/callout layers — deferred on purpose.
- Multi-monitor and multi-track audio.
- Screen-to-screen transitions and window sizing.
- Actual capture, encoding, zoom detection — this is paint only.

## Non-negotiable

The redaction rule in spec section "Redaction — the one hard rule". Presets not a
slider, and the canvas must render export strength. A redaction that looks
sufficient while editing and renders weaker is a leaked secret, so this is a
correctness requirement, not a style preference.
