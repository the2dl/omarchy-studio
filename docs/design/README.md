# Handoff — screen recorder UI

Three things in here:

- **`spec.md`** — the design spec. Tokens, control anatomy, and a per-screen
  breakdown. Start here.
- **`screens.png`** — all eight screens rendered at 1:1. The visual target.
- **`mockup.dc.html`** — the working mockup. Open it in a browser to
  inspect real markup and pull exact values for anything the spec doesn't name.
  `support.js` and `uploads/` are its dependencies; keep them next to it.

## Reading the mockup

Every screen is inline-styled, no stylesheet, so computed values are visible
directly on the element. Screens are wrapped in divs with the id shown as a
badge (`1a`, `1b`, …) matching the spec sections.

The mockup is a static mock: nothing is wired, no state, no interaction. Sliders,
toggles and segmented controls are drawn in one representative position. Treat
positions as *examples of the visual state*, not defaults.

## What is not specified

Deliberately left open — decide these in implementation:

- Empty states, error states, permission prompts.
- Keyboard shortcuts beyond the two shown in `1a`.
- What happens between screens (transitions, whether export is a modal or a pane).
- The library/recordings list (not designed).
- Actual capture, encoding, zoom detection — this is paint only.
