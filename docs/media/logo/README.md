# Studio logo — handoff (direction 1c, "Push-in")

## The mark

Three nested rounded frames receding to a filled core square. It reads as a zoom push-in — the app's signature editor move — and as a screen within a screen.

Chosen from six directions explored in `Studio Logo.dc.html` in the parent project (`1a` cursor in frame · `1b` capture brackets · **`1c` push-in** · `1d` caret wordmark · `1e` slate · `1f` keyframe).

## Fidelity

**High fidelity geometry, one designer pass short of final.** Every coordinate, stroke weight and opacity below is intentional and the SVGs in this package are exact. What has *not* had a professional pass: optical corner-radius harmony between the three frames, and the wordmark's letter-spacing against a real licensed cut of JetBrains Mono. Treat the geometry as approved and those two as open.

These are hand-constructed vectors, not generated imagery.

## Construction

All geometry on a **64 × 64 grid**. The mark's own bounding box is 54 × 46, centred horizontally, optically centred vertically (`y=9` to `y=55`).

| element | x | y | w | h | radius | stroke | opacity |
| --- | --- | --- | --- | --- | --- | --- | --- |
| outer frame | 5 | 9 | 54 | 46 | 10 | 4 | 0.32 |
| middle frame | 16 | 18 | 32 | 28 | 7 | 4.5 | 0.62 |
| core square | 26 | 26 | 12 | 12 | 3.5 | — (filled) | 1 |

Notes on why these values:
- Frames step in by **11 units horizontally and 9 vertically** — a proportional inset, so the recession reads as perspective rather than a concentric target.
- Stroke *increases* as it approaches the viewer (4 → 4.5 → solid). Combined with the opacity ramp this is what makes it push in rather than sit flat.
- Each radius is ~0.19 of the shape's short side, so the corners feel like the same corner at three depths.
- The 4:3.4 outer frame is deliberately not 16:9 — at icon scale a true widescreen frame reads as a letterbox bar.

## Two variants — this matters

**Full mark** (`mark-full-*.svg`) — 24px and above. Uses the opacity ramp.

**Small mark** (`mark-small-*.svg`) — **below 24px, and any monochrome context.** Drops the middle frame, thickens the outer frame to 6, and grows the core to 14 × 14 at (25, 25) with radius 4. No opacity at all.

| element | x | y | w | h | radius | stroke |
| --- | --- | --- | --- | --- | --- | --- |
| outer frame | 7 | 11 | 50 | 42 | 10 | 6 |
| core square | 25 | 25 | 14 | 14 | 4 | — (filled) |

The full mark's receding strokes were the known weakness of this direction — they thin out and the opacity ramp muddies at tray size. The small mark is not an optimisation, it is the required form below 24px. Ship both; pick by rendered size, not by context.

## Colour

| use | value |
| --- | --- |
| accent (the mark) | `#f2a25a` |
| on-accent (mark knocked out of an accent tile) | `#1a1006` |
| dark icon tile | `#121316` |
| wordmark text on dark | `#f6f4f2` |
| monochrome | `currentColor` — inherit, never hardcode |

The mark is single-colour by design. The three frames differ in opacity, not hue. **Never** introduce a second colour, a gradient, or a glow.

## Files

```
svg/
  mark-full-accent.svg          64×64, #f2a25a — default
  mark-full-currentcolor.svg    64×64, inherits colour — for inline use
  mark-small-accent.svg         64×64, small-size form, #f2a25a
  mark-small-currentcolor.svg   64×64, small-size form, inherits — tray/menu
  icon-dark.svg                 112×112 app icon, mark on #121316, r26 tile
  icon-accent.svg               112×112 app icon, knocked out of #f2a25a, r26 tile
  lockup-horizontal.svg         176×40, mark + wordmark
png/
  mark-16 / 24 / 32 / 64 / 128 / 256 / 512.png   accent on transparent
  icon-dark-256 / 512.png                        app icon, dark tile
  icon-accent-256 / 512.png                      app icon, accent tile
preview.html                    all variants at real size, on dark and light
```

The PNGs are rasterised from these SVGs at the sizes named. 16 and 24 use the small mark; 32 and up use the full mark.

## Implementation

**Inline SVG, not an `<img>`,** anywhere the mark should follow theme colour — that is what `mark-*-currentcolor.svg` is for. Set `color` on the parent.

**Icon tile radius** is `26/112` = 0.232 of the tile. If the platform supplies its own mask (macOS squircle, Android adaptive), export the mark on a transparent tile and let the OS mask it — do not stack two radii.

**Tray/status icon**: `mark-small-currentcolor.svg`, rendered at the bar's icon size. Follow the bar's foreground colour. If Studio adopts `1b`'s idea later, a recording state would tint the *core square only* to `#e0523f` — the frames stay foreground-coloured.

**Favicon**: 32px full mark. Provide 16px from the small mark as a separate entry rather than letting the browser downscale.

**Still to produce** (needs a build step or design tool, not in this package): `.ico` multi-resolution bundle, `.icns` for macOS, and a `.desktop`-referenced hicolor icon set if Studio ships as a Linux desktop app. All derive from the PNGs here at standard sizes.

## Lockup

Mark at 28px, then `Studio` in **JetBrains Mono Medium (500)**, 21px, `letter-spacing: -0.01em`, gap 11px, baselines optically aligned (the mark's vertical centre sits on the cap-height midpoint, not the baseline).

`lockup-horizontal.svg` uses a live `<text>` element so you can restyle it. **Outline the text before shipping** to anywhere the font may not be installed.

Stacked lockup (mark above wordmark, centred) is fine at gap 14px. There is no other approved arrangement.

## Clear space and minimum size

**Clear space**: the width of the core square — 12 grid units, or 0.19 × the mark's width — on all four sides. Nothing enters it, including the wordmark in a lockup (the 11px gap is measured from the mark's bounding box, and exceeds clear space at 28px).

**Minimum sizes**: full mark 24px. Small mark 16px. Lockup 120px wide. Below 16px use the core square alone.

## Don't

- Don't use the full mark below 24px — use the small mark.
- Don't add a fourth frame, or reorder the recession.
- Don't apply the opacity ramp in monochrome contexts; it becomes a grey mush.
- Don't rotate, skew, or apply perspective — the recession is already the perspective.
- Don't place the accent mark on a mid-tone background; it needs `#121316`-dark or lighter than `#f5f2ee`.
- Don't outline, emboss, or drop-shadow the mark. The icon *tile* may carry the app's standard `0 30px 80px rgba(0,0,0,0.6)` in marketing shots; the mark itself never does.
- Don't set the wordmark in anything but JetBrains Mono Medium.
