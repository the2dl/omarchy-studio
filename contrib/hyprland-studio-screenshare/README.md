# omarchy-studio-screenshare (Hyprland plugin)

> **STATUS: working in the nested stock rig** (Hyprland 0.56.2): background shows
> through the excluded window, on consecutive captures, before and after moving it,
> with an opaque *and* a translucent excluded window. Not yet verified on a real DRM
> session. See "Where this stands".

Leaves `no_screen_share` windows **out** of a screenshare — showing what is behind
them — instead of covering them with black rectangles.

This replaces `contrib/hyprland-screenshare-exclude/`, which did the same thing as an
out-of-tree patch to Hyprland itself.

## Why not the patch

The patch worked, and was verified end to end. It also meant carrying a compositor
fork: a custom PKGBUILD, `pkgrel 1.1` so `-Syu` reports "local is newer" and skips it,
a full Hyprland rebuild for every release, and — the bad one — when an update *does*
overwrite it, the option disappears and the self-view silently falls back with nothing
on screen saying why.

Upstreaming it is not a near-term answer either. The equivalent upstream PR
([#11572](https://github.com/hyprwm/Hyprland/pull/11572)) has been open since
September 2025, and Hyprland auto-closes PRs from unvouched contributors.

A plugin gets the same behaviour with `hyprpm update` after a Hyprland release, and
when it cannot load, the compositor is still the stock package.

## What it does

Hyprland already renders to two colour attachments when
`CMonitor::needsUnmodifiedCopy()` is true — the `SH_FEAT_MIRROR` path that landed
upstream for HDR/SDR screensharing. Attachment 0 is the frame you see; attachment 1 is
the mirror texture that `saveBufferForMirror()` blits into `mirrorFB()`, which is what
`CScreenshareFrame::renderMonitor()` reads.

So the plugin:

1. **Forces that path on** for a monitor carrying a visible excluded window, by hooking
   `CMonitor::needsUnmodifiedCopy()`.
2. **Masks attachment 1** around the excluded window's pass elements — a custom pass
   element issuing `glDrawBuffers({COLOR_ATTACHMENT0, GL_NONE})` before the window and
   putting `COLOR_ATTACHMENT1` back after it.
3. **Suppresses the black rect**, so the screenshare shows the content the mirror
   actually holds.
4. **Makes sure what is behind the window is drawn into the mirror at all.** Two
   things in a stock frame prevent that, and neither is visible until you look for it:
   - `CRenderPass::simplify()` subtracts opaque regions, so nothing is drawn under an
     opaque window — the mirror keeps a *frozen* copy of whatever was there when the
     window arrived (the self-view is mpv: opaque). An excluded window now reports no
     opaque region while it is excluded.
   - `misc:background_color` is a `CClearPassElement`, drawn with
     `glClearBufferfv(GL_COLOR, 0, …)` — draw buffer 0 only, never the mirror. Stock
     with `render:keep_unmodified_copy = 1` and no wallpaper screenshots **black**,
     no plugin involved. The clear is repeated on draw buffer 1 for excluding monitors.

Cost is MRT bandwidth while such a window is visible, plus repainting the pixels
under it — the same cost the patch paid, plus one bubble's worth. Nothing about what
the user sees changes.

## API vs. hooks

Almost all of it is public API: the event bus (`render.pre`, `render.stage`,
`screenshare.state`, `monitor.preRemoved`), `EK_CUSTOM` pass elements,
`IFramebuffer::getMirrorTexture()`, `CGLFramebuffer::getFBID()`, and a config keyword.

Six function hooks:

- `CScreenshareFrame::renderMonitor` — brackets the original call with a flag. `this`
  stays an opaque `void*`; the class is not in the shipped headers and is never touched.
- `IHyprRenderer::shouldRenderWindow(PHLWINDOW, PHLMONITOR)` — returns false for an
  excluded window *while that flag is set*, which is exactly what the black-rect loop
  skips on. Everywhere else it is a straight passthrough.
- `CMonitor::needsUnmodifiedCopy()` — returns true for a monitor we are excluding on,
  which is what turns the two-attachment path on.
- `IHyprRenderer::renderWindow(...)` — wraps one window's pass elements with the mirror
  mask, on every exit path.
- `CSurfacePassElement::opaqueRegion()` — empty for an excluded window on an excluding
  monitor, so the content under it is actually drawn (and so reaches the mirror).
- `CGLElementRenderer::draw(WP<CClearPassElement>, ...)` — repeats the background clear
  on the mirror attachment for excluding monitors.

Hooks are the part that breaks on a Hyprland release, so that surface is kept as small
as possible. If a hook refuses to install the plugin throws at init and does not load,
and the recorder's probe falls back — see "Failure modes".

## Why the window keeps its `no_screen_share` rule

The plugin could have used its own marker instead, which would have removed the need
for both hooks (stock would draw no black rect for a window it does not consider
excluded). It deliberately does not, because of how each choice fails:

- Rule kept, plugin missing → **black box**. Obviously wrong, impossible to miss.
- Own marker, plugin missing → **the bubble is recorded normally**. Looks fine, ruins
  the take, and you find out afterwards.

## Building and installing

    ./install.sh        # stages into ~/.local/share/omarchy-studio/hyprpm/, hyprpm add + enable
    hyprpm reload       # loads it into the running compositor -- see below before doing this

`hyprpm` only accepts a git repo with the manifest at its root, and this directory is a
subdirectory of the project, so `install.sh` stages it into a small git repo of its own
and points hyprpm at that. Re-run it after editing the source (it commits there and runs
`hyprpm update`). `hyprpm` builds against its own **stock** Hyprland headers, which is
what this needs — the plugin must match the upstream ABI. After a Hyprland update:
`hyprpm update`.

**First load while the patched package is still installed:** the patched build reports
the same commit hash as stock, so the version gate cannot refuse it. The plugin touches
none of the layouts the patch shifts (`CMonitor` fields, `IFramebuffer`/`IFadeout`
vtables, `CRendererHintsPassElement::SData`), and both mechanisms masking the same
attachment is harmless, but do the first `hyprpm reload` from a session you can afford
to lose, or after `pacman -U` back to the stock package.

To build by hand against those same headers:

    PKG_CONFIG_PATH=/var/cache/hyprpm/$USER/headersRoot/share/pkgconfig:$PKG_CONFIG_PATH \
      cmake -B build -S . -DCMAKE_BUILD_TYPE=Release && cmake --build build

**Do not build against `/usr/include/hyprland` on a machine still running the patched
package** — those headers carry the patch, and its extra virtuals in `IFramebuffer` and
`IFadeout` shift vtable layouts away from stock.

## Failure modes

- **Plugin not loaded** — `no_screen_share` windows get black boxes, i.e. stock
  behaviour. The recorder's probe (`lib/omarchy_studio/compositor.py`) sees no plugin
  and drops the `exclude` self-view mode, falling back to parking the bubble outside
  the capture, or to no live preview.
- **Hyprland updated** — the plugin refuses to load on a version hash mismatch (that
  check is the first thing `pluginInit` does). `hyprpm update` rebuilds it.
- **A hook refuses to install** — same as not loaded; nothing is left half-applied.
- **Killed mid-session** — `PLUGIN_EXIT` restores `render:keep_unmodified_copy`. If the
  compositor dies instead, the option is whatever the config says at next start.

## Verifying

Same test as the patch, and it must be done on a real DRM session:

1. Put a `no_screen_share` window over a colour-cycling background.
2. Capture with `grim` and with `gpu-screen-recorder -w portal`.
3. Both should show the background *behind* the window, matching a control point
   outside it on every sampled frame — no black box, no edge halo, no stale region.
   Moving the window mid-recording must leave nothing behind.
4. The same window MUST still appear in a `gpu-screen-recorder -w DP-1` KMS capture —
   that is what proves it was really on screen and not merely hidden.


## Where this stands

Verified in a nested **stock** Hyprland 0.56.2 (see `test/`), which is how this can be
tested without risking the real session. Measured, two captures each:

| rig                                              | centre | corners |
|--------------------------------------------------|--------|---------|
| stock, `keep_unmodified_copy=1`, **no plugin**   | black  | black   |
| stock, no plugin (black-box path)                | black  | green   |
| plugin, solid background, opaque window          | green  | green   |
| plugin, green window behind, translucent window  | green  | green   |
| plugin, green window behind, opaque window       | green  | green   |
| plugin, window moved mid-session (old + new pos) | green  | green   |

The first row is the finding that unblocked this: **the "whole capture goes black"
symptom was stock behaviour of the MRT path with a bare background, not a plugin
bug**. The original four-hook plugin already worked as soon as a real surface was
behind the window; the fifth and sixth hooks make it hold up on a bare background
and under an opaque window (see "What it does", point 4). The `currentFB` vs
`mainFB` lead in `globalFeatures()` was a red herring: in a normal frame they are the
same framebuffer.

Not done:

- **Real DRM verification** (the "Verifying" list above), including
  `gpu-screen-recorder -w portal` and the KMS control.
- **Close-fade snapshots.** The patch masked a closing window's fadeout; this plugin
  does not (stock's black-rect loop does not cover it either). It could not be
  measured: in the rig a killed client never produces a visible fade even with a 10 s
  `fadeOut`, with or without the plugin. The cheap, code-backed mitigation is
  `no_anim = true` on the excluded window's rule — `CWindow::onUnmap` only makes the
  snapshot when `!noAnim()` (`Window.cpp:2586`), so there is no fadeout at all. Note
  `animation = "none"` (what the HUD and teleprompter rules use) is an animation
  *style*, not `no_anim`.
- Colour: the mirror clear writes the work-buffer-converted colour, which is only
  identical to the mirror's sRGB for an SDR monitor. HDR/fp16 work buffers would need
  the unconverted colour.

### Bugs already found and fixed here, worth not re-introducing

1. The version check compared `__hyprland_api_get_hash()` (the server's composed
   `<hash>_aq_<ver>_hu_<ver>...`) against `getHyprlandVersion().hash` (the bare commit).
   Those are never equal, so the plugin refused to load every time. Compare against
   `__hyprland_api_get_client_hash()`.
2. Forcing the MRT path by setting `render:keep_unmodified_copy = 1` does not work:
   **`hyprctl keyword` is a no-op under the Lua config manager**, which is what Omarchy
   uses. Hence the `needsUnmodifiedCopy` hook.
3. Bracketing the mask on the `RENDER_PRE_WINDOW` / `RENDER_POST_WINDOW` bus events is
   wrong. `renderWindow()` has early returns between the two emits, so POST is not
   guaranteed to follow PRE — measured: PRE fired every frame, POST fired **zero**
   times, leaving the mask off for the rest of the frame. Wrapping `renderWindow`
   itself is what makes the pair symmetric, which is why the original patch used a
   `CScopeGuard`.

### Test rig gotchas that cost real time

- `hyprctl plugin load` **reloads the config**, which drops window rules installed
  dynamically with `hyprctl eval hl.window_rule(...)`. The test window silently lost
  its `no_screen_share` on every plugin load, so the rig measured nothing. Put the
  rules in the config file.
- `hyprctl plugin load` caches by path: a rebuilt `.so` at the same path keeps
  reporting the *previous* load's error. Copy to a fresh filename between attempts.
- Test against a **stock** Hyprland binary. The locally patched package is still
  installed here, and its own exclusion code is a confound even with
  `render:screenshare_exclude_windows = false`.
- ...and then do NOT put `render.screenshare_exclude_windows` in the stock rig's
  config: stock rejects the unknown key, which trips Lua **emergency mode** and draws
  an error banner into the frame (and the capture).
- `foot -T excl` is not enough: the shell's prompt resets the title, the
  `^excl$` rule stops matching, and the window is captured normally — which reads as
  "no exclusion". Use `-o main.locked-title=yes` and run `sleep` instead of a shell.
- A solid `misc:background_color` never reaches the mirror in stock (see "What it
  does", point 4). Without the clear hook, the rig measures that stock bug, not the
  plugin. Either keep the hook or put a real surface behind the window.
- An opaque excluded window over a *window* is nondeterministic without the
  opaque-region hook: black on the first capture, correct on a later one, depending
  on whether the region under it was damaged before the mirror texture was reallocated.
