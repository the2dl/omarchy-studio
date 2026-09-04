"""The editor's only route to the model, and the only place placement maths happens.

The QML editor is a pure view. It owns no model state and computes no placement: it
POSTs an intent in canvas pixels ("the webcam box is now at 1180,760 300x300") and gets
back the whole resolved state, in which every number came from geometry.py. That is the
entire reason the preview is trustworthy -- a second implementation of `resolve()` in
JavaScript would drift, and the drift is invisible until someone diffs rendered frames.

The transport is a loopback HTTP server rather than a file dump because the editor needs
the round trip live, on every drag frame. Measured from QML: 100 sequential
XMLHttpRequest round trips in 27ms (0.27ms each), which is two orders of magnitude under
a 60Hz frame, so there is no reason to cache or batch and no reason to let QML guess.

A token is required on every request. The socket is bound to 127.0.0.1, but any local
process can reach a loopback port, and this one rewrites the user's project.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import time
import re
import secrets
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from dataclasses import replace

from . import backgrounds
from . import cursor as _cursor_mod
from . import events as _events
from . import follow
from . import layers as _layers
from . import zoom as _zoom
from . import project as project_mod
from .geometry import (DEFAULT_REDACT, Canvas, Placement, Rect, Zoom,
                       pixelate_block_px, qml_blur)
from .project import (Bundle, CursorSettings, Edit, Layer, ProjectError,
                      WebcamSettings)
from .timebase import CutMap, FrameRange, Timebase, normalize

# The one font file both engines use. Naming a family instead lets fontconfig hand Qt
# and libavfilter different faces, and the metrics diverge before the glyphs visibly do.
FONT_FILE = "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf"

# The camera size control's range, as a fraction of the canvas width. The floor is a
# camera you can still see a face in; the ceiling is the point past which the camera is
# the recording and the screen is the decoration.
MIN_WEBCAM_WIDTH = 0.04
MAX_WEBCAM_WIDTH = 0.60

# A zoom track longer than this is a runaway click log, not a project; the preview would
# ship megabytes of JSON per state refresh for no visible gain.
_MAX_ZOOM_SAMPLES = 200_000


class BridgeError(RuntimeError):
    pass


# --- resolution: geometry.py in, JSON out -----------------------------------


def resolve_placement(place: Placement, canvas: Canvas) -> dict:
    """Explicit x/y/width/height for one QML Item.

    Named `rect` rather than spread into the reply so QML can never accidentally bind a
    stray `anchors.fill` alongside it: the values arrive as one object that is applied
    wholesale.
    """
    return {"rect": place.to_qml(canvas)}


def placement_from_rect(
    x: float, y: float, w: float, h: float, canvas: Canvas, anchor: str = "top-left"
) -> Placement:
    """The inverse of Placement.resolve: canvas pixels back to normalized 0..1.

    QML hands back pixels because that is what a drag produces, and this is the only
    conversion that exists -- dividing by the canvas in JavaScript would be a second
    implementation of the same mapping.
    """
    clamped = Rect(x, y, w, h).clamped_to(canvas)
    nx, ny = clamped.x / canvas.width, clamped.y / canvas.height
    nw, nh = clamped.w / canvas.width, clamped.h / canvas.height
    if anchor == "center":
        nx += nw / 2.0
        ny += nh / 2.0
    return Placement(nx, ny, nw, nh, anchor)


def resolve_zoom(zoom: Zoom, canvas: Canvas) -> dict:
    """Scale plus the translation that puts the viewport origin at the item's (0,0).

    transformOrigin is carried in the payload and applied in QML rather than assumed,
    because any other origin makes the translation depend on the item's size -- which is
    exactly the bug that produces a zoom that scales but never pans.
    """
    d = zoom.to_qml(canvas)
    vp = zoom.viewport(canvas)
    d["viewport"] = {"x": vp.x, "y": vp.y, "width": vp.w, "height": vp.h}
    return d


def resolve_layer(layer: Layer, canvas: Canvas, bundle: Bundle) -> dict:
    """One project layer as the properties of one QML Item.

    Unknown types survive the round trip (`project.Layer` keeps them) and are marked
    unsupported here rather than dropped, so a project written by a newer build shows a
    placeholder instead of silently losing an annotation.
    """
    d: dict[str, Any] = {
        "id": layer.id,
        "type": layer.type,
        "z": layer.z,
        "opacity": layer.opacity,
        "fade_frames": layer.fade_frames,
        # "" | "head" | "tail". Without this the inspector's "when" control could not
        # show which one a layer is in, and would sit on "In video" for a card that
        # actually plays before the recording.
        "pad": layer.pad,
        "enabled": layer.enabled,
        "t": layer.t.to_dict() if layer.t else None,
        "props": dict(layer.props),
        "supported": layer.type in ("image", "text", "shape", "blur", "pixelate"),
    }
    d.update(resolve_placement(layer.placement, canvas))
    if layer.type == "image":
        name = layer.props.get("asset", "")
        path = bundle.assets_dir / name if name else None
        d["source"] = path.as_uri() if path and path.exists() else ""
    elif layer.type == "text":
        # Centre-anchored: left-anchored text drifted ~7% of the string width between
        # Qt's shaper and libavfilter's drawtext even on the same font file.
        #
        # Every default below is layers.py's, reached through layers.py's own helpers.
        # They used to be this module's own, and the preview lied four ways at once: a
        # #101820 caption box at 0.85 where drawtext drew none (box_color carries the
        # alpha, and there is no separate box_opacity property), and a font sized off
        # the canvas height where drawtext sizes off the tile.
        r = layer.placement.resolve(canvas)
        box_rgb, box_alpha = _layers.split_color(layer.props.get("box_color", "black@0.0"))
        d["text"] = {
            "text": layer.props.get("text", ""),
            "cx": r.x + r.w / 2.0,
            "cy": r.y + r.h / 2.0,
            "pixelSize": _layers.font_px(layer.props.get("font_px"), r),
            "color": layer.props.get("color", "#ffffff"),
            "box_color": box_rgb,
            "box_opacity": box_alpha,
            "radius": _layers.radius_px(layer.props.get("radius", 0.0), r),
            "font_file": FONT_FILE,
        }
    elif layer.type == "shape":
        r = layer.placement.resolve(canvas)
        rgb, alpha = _layers.split_color(layer.props.get("color", "#ff3b30"))
        d["shape"] = {
            "color": rgb,
            "opacity": alpha,
            # Against the TILE's short side, which is what the export's mask uses. The
            # canvas width was a different basis entirely: 0.2 on a small shape rounded
            # to a lozenge here and stayed square in the render.
            "radius": _layers.radius_px(layer.props.get("radius", 0.0), r),
        }
    elif layer.type == "blur":
        d["blur"] = qml_blur(str(layer.props.get("preset", DEFAULT_REDACT)))
    elif layer.type == "pixelate":
        # Block size is normalized like every other dimension so a project authored on
        # the 1080p proxy pixelates identically on the master -- but through layers.py,
        # because reading the editor's normalized 0.012 as pixels floored the export to
        # a 2 px block: a mosaic that previewed unreadable and rendered legible.
        d["pixelate"] = {
            "block": _layers.block_px(layer.props["block"], canvas)
            if "block" in layer.props
            else pixelate_block_px(str(layer.props.get("preset", DEFAULT_REDACT)), canvas)
        }
    return d


def resolve_backdrop(bundle: Bundle) -> dict:
    """The inset the screen video sits in when a backdrop is on.

    Padding is normalized like every other dimension, and the inset is resolved through
    Placement so the preview's rounded corner lands where the export's mask does.
    """
    b = bundle.edit.backdrop
    canvas = bundle.canvas
    # Padding and corner radius are both measured against the SHORT side, and the inset
    # is centred -- render._backdrop does `pad = padding * min(W,H)` on both axes, so
    # resolving the padding per-axis would give a preview with the wrong margins on the
    # long one.
    short = min(canvas.width, canvas.height)
    pad = min(max(b.padding, 0.0), 0.45) * short
    place = Placement(
        pad / canvas.width,
        pad / canvas.height,
        (canvas.width - 2 * pad) / canvas.width,
        (canvas.height - 2 * pad) / canvas.height,
    )
    d = resolve_placement(place, canvas)
    r = place.resolve(canvas)
    # RESOLVED, not raw: `color`/`gradient` report the ground that will actually be
    # rendered, so a swatch selection reaches the preview through the same two keys a
    # custom colour always did. The raw model is still available under `edit.backdrop`
    # for anything that needs to know which of the two the user chose.
    ground = backgrounds.resolve(b, canvas)
    x0, y0, x1, y1 = backgrounds.gradient_line(ground.angle, canvas.width, canvas.height)
    d.update(
        {
            "enabled": b.enabled,
            "background": b.background,
            "color": ground.colors[0],
            "gradient": ground.colors[-1] if ground.is_gradient else None,
            # The whole definition, plus the gradient line in canvas pixels. QML paints
            # from THIS rather than from the angle: `gradient_line` is the construction
            # the export uses, and a second implementation in JavaScript is exactly the
            # kind of drift the preview exists to rule out.
            "ground": {
                **ground.to_dict(),
                "line": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            },
            "shadow": b.shadow,
            "padding": b.padding,
            "radius": b.corner_radius * short,
            # The export scales the whole zoomed canvas into the inset; the preview needs
            # the same two factors rather than deriving them from the rect itself.
            "content_scale": {
                "x": r.w / canvas.width,
                "y": r.h / canvas.height,
            },
        }
    )
    return d


def _materialize_implicit_camera(bundle: Bundle, layer_id: Any) -> None:
    """Store the implicit camera segment if `layer_id` names it.

    webcam_segments() reports a whole-take segment for a track nobody has split, and
    deliberately does NOT store it -- opening a recording must not dirty it. That id is
    real to the timeline, which hands it straight back to the bridge, so it has to
    become a real layer the moment anything acts on it. No-op for every other id.
    """
    if not layer_id or not _camera_is_editable(bundle):
        return
    wanted = str(layer_id)
    if any(l.id == wanted for l in bundle.edit.layers):
        return
    total = _safe_source_frames(bundle)
    if any(s.id == wanted
           for s in _layers.webcam_segments(bundle.edit, bundle.canvas, total)):
        _layers.materialize_webcam(bundle.edit, bundle.canvas, total)


def _camera_is_editable(bundle: Bundle) -> bool:
    """Whether the camera can be moved at all -- the one rule, stated once.

    A burned-in camera is part of the screen pixels and a recording without a camera
    stream has nothing to place, so neither can carry a camera track. resolve_webcam
    reports this to the UI; the ops enforce it, because the UI is not the only caller.
    """
    return not bundle.capture.camera_burned_in and bundle.capture.camera is not None


def resolve_webcam(bundle: Bundle) -> dict:
    """The camera overlay box, plus why the controls may be dead.

    A burned-in recording genuinely cannot have its webcam moved. The editor disables the
    controls and shows this reason; silently inert controls read as a bug.
    """
    cam = bundle.edit.webcam
    canvas = bundle.canvas
    place = cam.placement(canvas)
    d = resolve_placement(place, canvas)
    r = place.resolve(canvas)
    d.update(
        {
            "enabled": cam.enabled,
            "shape": cam.shape,
            "mirror": cam.mirror,
            # The size control's value: the width as a fraction of the canvas, which is
            # the one number that describes the camera box (the height is derived --
            # WebcamSettings.placement). The corner grip writes the same field through
            # `rect`; this is what lets a slider show and set it without a drag.
            "size": cam.w,
            # Always 0: `rounded` is drawn as a superellipse by SquircleShape, which
            # takes no radius. Kept in the payload so the overlay's binding stays valid.
            "radius": 0.0,
            "corner_radius": cam.corner_radius,
        }
    )
    if bundle.capture.camera_burned_in:
        d["editable"] = False
        d["disabled_reason"] = (
            "This recording was made with the camera burned into the screen pixels, so "
            "the webcam is part of the video and cannot be moved, resized or removed. "
            "Record with a separate camera stream to keep it editable."
        )
    elif bundle.capture.camera is None:
        d["editable"] = False
        d["disabled_reason"] = "This recording has no camera stream."
    else:
        d["editable"] = True
        d["disabled_reason"] = ""
    return d


def resolve_cursor(bundle: Bundle) -> dict:
    """The synthetic pointer's settings, resolved to the numbers the panel shows.

    `size` is normalized (a fraction of the canvas height) because that is what the model
    stores and what the slider writes back; `size_px` is the same value against THIS
    canvas, so the panel can print "32 px" without dividing anything in JavaScript. The
    smoothing slider is likewise 0..1 in the model and milliseconds here -- the seconds
    are cursor.py's, reached through cursor.py, so a change to SMOOTH_MAX_SECONDS cannot
    leave the panel reporting a number the render no longer uses.

    A recording made before the cursor track existed has nothing to draw. The controls
    are marked dead with a reason rather than silently doing nothing, the same way a
    burned-in webcam's are: inert controls read as a bug in the editor.
    """
    c = bundle.edit.cursor
    canvas = bundle.canvas
    tb = bundle.timebase
    ms_per_frame = 1000.0 * tb.fps_den / tb.fps_num
    d: dict[str, Any] = {
        "enabled": c.enabled,
        "size": c.size,
        "size_px": c.size * canvas.height,
        "min_size": CursorSettings.MIN_SIZE,
        "max_size": CursorSettings.MAX_SIZE,
        "smoothing": c.smoothing,
        "smoothing_ms": _cursor_mod.sigma_frames(c, tb) * ms_per_frame,
        "click_ripple": c.click_ripple,
        "ripple_frames": c.ripple_frames,
        "ripple_ms": c.ripple_frames * ms_per_frame,
    }
    path = bundle.events_dir / "cursor.bin"
    if not path.exists():
        d["editable"] = False
        d["disabled_reason"] = (
            "This recording has no cursor track, so there is no pointer to draw. "
            "Recordings made before the input recorder existed cannot have one added."
        )
        d["samples"] = 0
        return d
    try:
        track = _events.read_cursor_track(path)
    except _events.EventsError as e:
        # A cursor.bin this build cannot decode is worth naming: the file is right there
        # and the user is the only one who can tell us what wrote it.
        d["editable"] = False
        d["disabled_reason"] = f"The cursor track could not be read: {e}"
        d["samples"] = 0
        return d
    d["editable"] = True
    d["disabled_reason"] = ""
    d["samples"] = len(track)
    d["hz"] = track.hz
    # A track the recorder never finalized is still usable -- it is only the tail that is
    # unknown -- but the editor should be able to say so rather than leaving the user to
    # wonder why the pointer stops before the video does.
    d["truncated"] = not track.finalized
    return d


# --- events ------------------------------------------------------------------


def _geom(d: dict, key_w: str = "w", key_h: str = "h") -> tuple[float, float, float, float]:
    return (
        float(d.get("x", 0)),
        float(d.get("y", 0)),
        float(d.get(key_w, d.get("width", 0))),
        float(d.get(key_h, d.get("height", 0))),
    )


def click_events(bundle: Bundle) -> list[dict]:
    """Clicks as frame indices and normalized focal points, for the timeline ruler.

    Delegates to events.map_clicks rather than re-reading input.jsonl. The mapping
    applies the anchor, the calibration offset and the logical-to-physical scale
    together, and applying two of the three yields plausible-looking targets that
    nothing downstream flags -- so there must be exactly one implementation of it.
    """
    path = bundle.events_dir / "input.jsonl"
    if not path.exists() or bundle.capture.screen is None:
        return []
    mapped = _events.map_clicks(
        _events.read_clicks(path), bundle.capture, bundle.timebase
    )
    return [
        {"frame": m.frame, "button": m.button, "cx": m.cx, "cy": m.cy} for m in mapped
    ]


def zoom_track(bundle: Bundle) -> dict:
    """Per-frame resolved zoom, as parallel arrays, sampled from the EXPORT's envelope.

    This calls zoom.zoom_segments / zoom.zoom_at -- the same two functions the
    filtergraph is generated from -- rather than reproducing the easing here. A second
    implementation of the envelope is the exact failure the geometry seam exists to
    prevent: it stays plausible while diverging, so the preview lies about timing while
    looking correct, and nobody notices until frames are compared side by side.

    Sampled rather than described because the alternative is QML doing the easing,
    which moves the same problem one layer further out. Frames where the zoom is
    identity are omitted; the preview holds identity between samples.
    """
    canvas = bundle.canvas
    path = bundle.events_dir / "input.jsonl"
    clicks = (
        _events.map_clicks(
            _events.read_clicks(path), bundle.capture, bundle.timebase
        )
        if path.exists()
        else []
    )
    # Not bundle.cutmap(): that probes the media for an exact frame count, and the
    # preview has to keep working while the master is missing, still being written, or
    # on another machine. _safe_source_frames falls back to a duration estimate.
    # _safe_source_frames returns 0 when the media is unreadable, and a CutMap over 0
    # frames keeps nothing -- every click would map to None and the preview would show
    # no zoom at all rather than showing it without a duration. Widen the total to cover
    # everything we actually hold so an unreadable master degrades to "zoom renders,
    # timeline length unknown" instead of "zoom silently disabled".
    total = _safe_source_frames(bundle)
    if bundle.edit.cuts:
        total = max(total, max(c.end for c in bundle.edit.cuts))
    if clicks:
        total = max(total, max(c.frame for c in clicks) + 1)
    cutmap = CutMap(bundle.edit.cuts, total)
    segments = _zoom.zoom_segments(clicks, bundle.edit.zoom, bundle.timebase, cutmap)
    if not segments:
        return {"frames": [], "scale": [], "x": [], "y": []}

    frames: list[int] = []
    scales: list[float] = []
    xs: list[float] = []
    ys: list[float] = []
    # Segments are on the OUTPUT timeline and disjoint, so walking each one's own span
    # visits every non-identity frame exactly once without scanning the whole recording.
    # Each sample is then mapped BACK to source time, because the preview plays the
    # uncut proxy and steps over cuts: indexing the track by output frame would slide
    # every zoom earlier by the length of the cuts before it, and only on projects that
    # have cuts.
    for seg in segments:
        for f in range(seg.t.start, seg.t.end):
            z = _zoom.zoom_at(segments, f)
            if z.identity:
                continue
            src = cutmap.to_source(f)
            if frames and src <= frames[-1]:
                continue
            q = z.to_qml(canvas)
            frames.append(src)
            scales.append(q["scale"])
            xs.append(q["x"])
            ys.append(q["y"])
            if len(frames) >= _MAX_ZOOM_SAMPLES:
                return {"frames": frames, "scale": scales, "x": xs, "y": ys}
    return {"frames": frames, "scale": scales, "x": xs, "y": ys}


# --- the state dump ----------------------------------------------------------


def _safe_source_frames(bundle: Bundle) -> int:
    """Frame count, or 0 when the media is unreadable.

    The editor must still open on a bundle whose media is missing -- it is the only place
    that can tell the user why -- so this degrades instead of raising.
    """
    try:
        return bundle.source_frames()
    except Exception:
        return 0


def _media_entry(path: Path | None, proxy: Path | None) -> dict | None:
    """The proxy, or no URL at all.

    Never the master: seeking the 5K master took 517-651ms and half the seeks never
    delivered a frame, so a preview that silently falls back to it looks broken rather
    than slow. Until the proxy exists the entry carries an empty url and the editor
    shows the proxy build instead of loading anything.
    """
    if path is None and proxy is None:
        return None
    ready = bool(proxy and proxy.exists())
    if not ready and (path is None or not path.exists()):
        return None
    return {
        "url": proxy.as_uri() if ready and proxy else "",
        "master": str(path) if path else "",
        "ready": ready,
    }


def resolve_transcript(bundle: Bundle) -> dict:
    """Is there a transcript, and is a local engine available to make one?

    Both, because they lead to different sentences: "add the captions" when the
    transcript is already there, "transcribe it first" when it is not, and "install an
    engine" when the machine cannot. A menu item that fails on click would have to say
    all three after the fact instead.
    """
    from . import transcribe as _transcribe

    t = _transcribe.load(bundle)
    return {
        "available": t is not None and bool(t.segments),
        "cues": len(t.segments) if t is not None else 0,
        "language": (t.language or "") if t is not None else "",
        "engine": _transcribe.available_engine() or "",
    }


def project_state(
    bundle: Bundle,
    *,
    proxy: dict | None = None,
    export: dict | None = None,
    include_zoom_track: bool = True,
) -> dict:
    """Everything the editor draws, resolved. This is the only shape QML consumes.

    `include_zoom_track` exists because a drag POSTs an intent per frame and the track
    can run to tens of thousands of samples: re-serializing it 60 times a second would
    make the drag stutter for a value that only zoom settings and the click log change.
    Omitting the key means "unchanged", and the editor keeps the copy it has.
    """
    canvas = bundle.canvas
    tb = bundle.timebase
    total = _safe_source_frames(bundle)
    screen = bundle.capture.screen
    camera = bundle.capture.camera
    ms_per_frame = 1000.0 * tb.fps_den / tb.fps_num
    layers = sorted(bundle.edit.layers, key=lambda l: l.z)
    state = {
        "bundle": str(bundle.root),
        "name": bundle.root.name,
        "canvas": {"width": canvas.width, "height": canvas.height},
        # The export size lives in edit.json, so the pane reads it from state like every
        # other setting rather than holding a copy that could drift from the file the
        # renderer actually reads.
        "export_preset": bundle.edit.export_preset,
        # Following the recorded window. `available` is whether the window ever moved:
        # a toggle that would visibly do nothing is worse than no toggle, so the pane
        # only draws the control when there is something to follow.
        "follow": {
            "available": follow.has_track(bundle),
            "on": bundle.edit.follow_window,
        },
        # Output-only time at each end. `head` shifts every recorded frame later, so
        # the timeline draws the recording starting at head rather than at zero.
        "pads": {
            "head": bundle.edit.head_pad_frames,
            "tail": bundle.edit.tail_pad_frames,
            "head_ms": bundle.edit.head_pad_frames * ms_per_frame,
            "tail_ms": bundle.edit.tail_pad_frames * ms_per_frame,
        },
        # The output timeline, described once HERE and consumed by the editor, rather
        # than QML deriving it a second time from cuts and pads. A second
        # implementation of this mapping is the same hazard as a second copy of the
        # export's easing: the two agree until they do not, and the disagreement shows
        # up as a preview that is subtly not the video.
        #
        # `kept` is the recorded material in output order -- out is where a segment
        # starts on the output timeline, src where it starts in the recording.
        "timeline": _timeline_map(bundle),
        # The pad ground, so the preview paints the colour the export will. As a CSS
        # hex for QML, where render takes the same value as 0xRRGGBB.
        "pad_color": "#" + backgrounds.pad_colour(bundle.edit.backdrop)[2:],
        "timebase": {
            "fps_num": tb.fps_num,
            "fps_den": tb.fps_den,
            "fps": tb.fps,
            # QML converts frames to player milliseconds with this, and never the other
            # way: every frame index that enters the model is computed by Timebase, so a
            # rounding difference in JavaScript can only affect what is displayed.
            "ms_per_frame": ms_per_frame,
        },
        "source_frames": total,
        "duration_ms": total * ms_per_frame,
        "capture": {
            "camera_burned_in": bundle.capture.camera_burned_in,
            "has_camera": camera is not None,
            "monitor_scale": bundle.capture.monitor_scale,
            "monitor_name": bundle.capture.monitor_name,
            "created": bundle.capture.created,
        },
        "media": {
            "screen": _media_entry(
                bundle.media(Path(screen.path).name) if screen else None,
                proxy_path(bundle, "screen"),
            ),
            "camera": _media_entry(
                bundle.media(Path(camera.path).name) if camera else None,
                proxy_path(bundle, "camera"),
            ),
            # Signed, and read per recording: the offset between two files is launch
            # order plus per-pipeline warm-up, not a constant.
            "camera_offset_ms": bundle.camera_offset_frames() * ms_per_frame,
            # The camera's own auto-exposure ramp, in CAMERA milliseconds. The export
            # skips it (render._align_camera trims those frames and holds the first
            # settled one across the head); the preview has to skip the same ones or the
            # editor shows a black bubble the exported file does not have. Preview and
            # export disagreeing about the camera has already happened once here, over
            # the sign of the offset, and it is the worst kind of bug to ship: what you
            # check disagrees with what you send, and only the thing you check is wrong.
            "camera_warmup_ms": (
                camera.warmup_frames * 1000.0 * camera.fps_den / camera.fps_num
                if camera and camera.fps_num else 0.0
            ),
        },
        "edit": bundle.edit.to_dict(),
        "webcam": resolve_webcam(bundle),
        # The camera track, so the timeline can draw it. Always at least the implicit
        # whole-take segment, so the row is never mysteriously empty on a recording
        # whose camera is plainly on; `explicit` tells the UI whether acting on a
        # segment will first materialize the track.
        "webcam_track": {
            "explicit": any(l.type == "webcam" for l in bundle.edit.layers),
            "segments": [
                {
                    "id": seg.id,
                    "start": seg.t.start if seg.t else 0,
                    "end": seg.t.end if seg.t else total,
                    "start_ms": (seg.t.start if seg.t else 0) * ms_per_frame,
                    "end_ms": (seg.t.end if seg.t else total) * ms_per_frame,
                    "enabled": seg.enabled,
                    "shape": seg.props.get("shape", bundle.edit.webcam.shape),
                    "mirror": bool(seg.props.get("mirror", bundle.edit.webcam.mirror)),
                    "corner_radius": float(
                        seg.props.get("corner_radius", bundle.edit.webcam.corner_radius)),
                    # The size control's value, same meaning as resolve_webcam's:
                    # width as a fraction of the canvas, height derived.
                    "size": seg.w,
                    "fade_ms": seg.fade_frames * ms_per_frame,
                    # resolve_placement wraps its result in a "rect" key; unwrapped so
                    # this matches the flat shape st.webcam.rect has. Nested, the
                    # placement grid read r.x as undefined and posted NaN coordinates.
                    "rect": resolve_placement(seg.placement, canvas)["rect"],
                }
                for seg in _layers.webcam_segments(bundle.edit, canvas, total)
            ],
        },
        "backdrop": resolve_backdrop(bundle),
        "cursor": resolve_cursor(bundle),
        # Whether there is anything to caption WITH. Only the count and a flag, never
        # the cues: this dict is re-serialized on every drag frame, and a transcript of
        # a ten-minute recording is thousands of strings.
        "transcript": resolve_transcript(bundle),
        "layers": [resolve_layer(l, canvas, bundle) for l in layers],
        "clicks": click_events(bundle),
        "cuts": [c.to_dict() for c in normalize(bundle.edit.cuts)],
        "font_file": FONT_FILE,
        "proxy": proxy or {"state": "unknown", "progress": 0.0, "message": ""},
        "export": export or {"state": "idle", "progress": 0.0, "message": ""},
    }
    if include_zoom_track:
        state["zoom_track"] = zoom_track(bundle)
    return state


# --- intents: every model mutation lives here --------------------------------


def _new_id(edit_layers: list[Layer], kind: str) -> str:
    n = 1
    used = {l.id for l in edit_layers}
    while f"{kind}{n}" in used:
        n += 1
    return f"{kind}{n}"


def _range_from_ms(tb: Timebase, start_ms: Any, end_ms: Any) -> FrameRange | None:
    """Milliseconds from the UI onto the frame grid.

    Timebase.to_frame is the only sanctioned seconds -> frame path, so every boundary the
    editor creates is snapped by construction rather than by a later fixup.
    """
    if start_ms is None or end_ms is None:
        return None
    a = tb.to_frame(max(0.0, float(start_ms)) / 1000.0)
    b = tb.to_frame(max(0.0, float(end_ms)) / 1000.0)
    if b <= a:
        b = a + 1  # a zero-length drag is a click; give it one frame rather than raising
    return FrameRange(a, b)


def apply_op(bundle: Bundle, op: str, args: dict) -> None:
    """Mutate the in-memory Edit. Nothing here touches the disk; /save does that."""
    edit = bundle.edit
    canvas = bundle.canvas
    tb = bundle.timebase

    def rect_arg() -> Placement:
        x, y, w, h = _geom(args["rect"], "width", "height")
        return placement_from_rect(x, y, w, h, canvas, args.get("anchor", "top-left"))

    # An untouched camera track is ONE implicit segment that the UI can see and select
    # but that is not a stored layer, so every op resolving it by id raised
    # `no layer 'webcam'`. Fixed once here rather than per-op: it was patched in
    # set_webcam alone and update_layer and delete_layer kept the same hole, which is
    # what a user hit by clicking the full-width block -- a click carrying a pixel of
    # movement commits a drag through update_layer.
    _materialize_implicit_camera(bundle, args.get("id"))

    if op == "set_webcam" and args.get("id"):
        # A segment is selected, so the panel is editing THAT segment rather than the
        # whole-take default. Same field names either way, so the panel does not need a
        # second code path -- only an id to pass when it has one.
        if not _camera_is_editable(bundle):
            # Same guard the no-id branch has. Without it a burned-in recording, or one
            # with no camera at all, could still be given a camera track that the
            # renderer has no stream to fill.
            raise BridgeError("this recording's camera cannot be moved")
        layer = _find_layer(edit.layers, str(args["id"]))
        if layer.type != "webcam":
            raise BridgeError(f"layer {layer.id!r} is not a camera segment")
        if "enabled" in args:
            layer.enabled = bool(args["enabled"])
        if "rect" in args:
            pl = rect_arg()
            layer.x, layer.y, layer.w, layer.h, layer.anchor = (
                pl.x, pl.y, pl.w, pl.h, pl.anchor)
        if "size" in args:
            # Through WebcamSettings.placement so a circle stays square in PIXELS -- the
            # height is derived, exactly as it is for the whole-take setting. The probe
            # carries THIS SEGMENT'S shape, not the global one: a circular segment under
            # a rect default came out an ellipse, and vice versa.
            probe = replace(edit.webcam, w=float(args["size"]),
                            shape=str(layer.props.get("shape", edit.webcam.shape)))
            pl = probe.placement(canvas)
            layer.w, layer.h = pl.w, pl.h
        for key in ("shape", "corner_radius", "mirror"):
            if key in args:
                layer.props[key] = args[key]
        if "fade_ms" in args:
            layer.fade_frames = max(0, tb.to_frame(float(args["fade_ms"]) / 1000.0))

    elif op == "set_webcam":
        if bundle.capture.camera_burned_in:
            raise BridgeError("the camera is burned into this recording and cannot be edited")
        cam = edit.webcam
        if "rect" in args:
            p = rect_arg()
            cam.x, cam.y, cam.w, cam.h = p.x, p.y, p.w, p.h
        if "size" in args:
            # About the CENTRE, unlike the corner grip, which is anchored at its
            # top-left because that is the corner the hand is not holding. A slider has
            # no such corner, and growing from the top-left walks the bubble across the
            # frame while you drag -- so the centre is what stays put here.
            before = cam.placement(canvas).resolve(canvas)
            cam.w = max(MIN_WEBCAM_WIDTH, min(MAX_WEBCAM_WIDTH, float(args["size"])))
            after = cam.placement(canvas).resolve(canvas)
            p = placement_from_rect(
                before.x + (before.w - after.w) / 2.0,
                before.y + (before.h - after.h) / 2.0,
                after.w,
                after.h,
                canvas,
            )
            cam.x, cam.y, cam.w, cam.h = p.x, p.y, p.w, p.h
        for key in ("enabled", "mirror"):
            if key in args:
                setattr(cam, key, bool(args[key]))
        if "shape" in args:
            shape = WebcamSettings.LEGACY_SHAPES.get(args["shape"], args["shape"])
            if shape not in ("circle", "rounded", "rect"):
                raise BridgeError(f"unknown webcam shape {args['shape']!r}")
            cam.shape = shape
        if "corner_radius" in args:
            cam.corner_radius = float(args["corner_radius"])

    elif op == "set_zoom":
        z = edit.zoom
        if "enabled" in args:
            z.enabled = bool(args["enabled"])
        if "amount" in args:
            # Below 1.0 Zoom refuses to construct; clamp here so a slider cannot throw.
            z.amount = max(1.0, float(args["amount"]))
        for key, ms in (("hold_frames", "hold_ms"), ("ease_frames", "ease_ms"), ("merge_gap_frames", "merge_gap_ms")):
            if ms in args:
                setattr(z, key, max(1, tb.to_frame(float(args[ms]) / 1000.0)))

    elif op == "set_cursor":
        c = edit.cursor
        for key in ("enabled", "click_ripple"):
            if key in args:
                setattr(c, key, bool(args[key]))
        if "size" in args:
            c.size = float(args["size"])
        if "smoothing" in args:
            c.smoothing = float(args["smoothing"])
        if "ripple_ms" in args:
            # Frames, like every other duration in the project: the ripple's gates are
            # frame comparisons, and a duration stored in milliseconds would round
            # differently here and in the graph.
            c.ripple_frames = max(0, tb.to_frame(float(args["ripple_ms"]) / 1000.0))
        if "ripple_frames" in args:
            c.ripple_frames = max(0, int(args["ripple_frames"]))
        # Re-run the model's own clamps rather than repeating them here. A slider that
        # can reach 0.09 must not be able to write a pointer a tenth of the frame tall,
        # and CursorSettings is the one place that decides what the limits are.
        c.__post_init__()

    elif op == "set_backdrop":
        b = edit.backdrop
        for key in ("enabled", "shadow"):
            if key in args:
                setattr(b, key, bool(args[key]))
        for key in ("padding", "corner_radius"):
            if key in args:
                setattr(b, key, float(args[key]))
        # Naming a colour IS choosing the custom ground. Without this the swatch that
        # was selected keeps outranking `color`, and the colour picker reads as dead --
        # the same class of bug as the editor's three shape labels against four values.
        if "color" in args:
            b.color = _backdrop_color(args["color"])
            b.background = backgrounds.CUSTOM
        if "gradient" in args:
            second = args["gradient"]
            b.gradient = None if second in (None, "") else _backdrop_color(second)
            b.background = backgrounds.CUSTOM
        # Applied last so an op carrying both a swatch and a colour lands on the swatch:
        # the id is the more specific intent, and the colour is what the picker will be
        # seeded with if the user goes looking for it.
        if "background" in args:
            bg_id = str(args["background"])
            if bg_id != backgrounds.CUSTOM and backgrounds.find(bg_id) is None:
                raise BridgeError(f"unknown background {bg_id!r}")
            b.background = bg_id

    elif op == "add_image":
        src = Path(args["path"])
        if not src.exists():
            raise BridgeError(f"no such image {src}")
        name = bundle.add_asset(src)
        p = rect_arg() if "rect" in args else Placement(0.35, 0.35, 0.3, 0.3)
        edit.layers.append(
            Layer(
                id=_new_id(edit.layers, "image"),
                type="image",
                x=p.x, y=p.y, w=p.w, h=p.h, anchor=p.anchor,
                z=_next_z(edit.layers),
                props={"asset": name},
            )
        )

    elif op == "add_captions":
        # Built from the bundle's own transcript.json rather than from anything the UI
        # sends: the transcript is the artefact the model produced, and round-tripping
        # a few thousand cues through the bridge to get them back to where they started
        # would be the largest payload in the protocol by an order of magnitude.
        from . import captions as _captions
        from . import transcribe as _transcribe

        t = _transcribe.load(bundle)
        if t is None:
            raise BridgeError(
                "this recording has no transcript yet. Run "
                "`omarchy-studio-transcribe <recording>` first -- it runs locally and "
                "nothing leaves the machine."
            )
        if not t.segments:
            raise BridgeError(
                "the transcript has no cues, so there is nothing to caption. The "
                "recording's audio may be silent."
            )
        # REPLACES rather than appends. Two caption layers draw two plates over each
        # other and read as a rendering bug, and adding captions twice is the normal
        # thing to do after re-transcribing.
        edit.layers = [l for l in edit.layers if l.type != "caption"]
        edit.layers.append(_captions.caption_layer(t.to_dict()))

    elif op in ("add_blur", "add_pixelate", "add_text", "add_shape"):
        kind = op[4:]
        p = rect_arg()
        props: dict[str, Any] = {}
        if kind in ("blur", "pixelate"):
            # Both redaction methods ride the same preset ladder. This wrote `block` for
            # pixelate, and both readers prefer an explicit `block` over a preset, so
            # every pixelate layer made in the UI had an inert preset and a fixed 0.012
            # mosaic -- the preset ladder was bypassed for half the feature that the
            # spec's one non-negotiable rule is about. `block` is still READ for projects
            # that already carry one; nothing writes one now.
            props["preset"] = str(args.get("preset", DEFAULT_REDACT))
        elif kind == "text":
            props["text"] = str(args.get("text", "Text"))
        elif kind == "shape":
            # Fill redactions are shapes with a solid colour; taking it here means the
            # caller does not have to add-then-update and briefly show the wrong colour.
            if "color" in args:
                props["color"] = str(args["color"])
            if args.get("redact"):
                props["redact"] = True
        edit.layers.append(
            Layer(
                id=_new_id(edit.layers, kind),
                type=kind,
                x=p.x, y=p.y, w=p.w, h=p.h, anchor=p.anchor,
                z=_next_z(edit.layers),
                props=props,
            )
        )

    elif op == "update_layer":
        layer = _find_layer(edit.layers, args["id"])
        if "rect" in args:
            p = rect_arg()
            layer.x, layer.y, layer.w, layer.h, layer.anchor = p.x, p.y, p.w, p.h, p.anchor
        if "opacity" in args:
            layer.opacity = min(1.0, max(0.0, float(args["opacity"])))
        if "enabled" in args:
            layer.enabled = bool(args["enabled"])
        if "z" in args:
            layer.z = int(args["z"])
        if "props" in args:
            layer.props.update(args["props"])
        if "fade_ms" in args:
            # Stored as frames like every other time in the project, so a fade cannot
            # drift against the grid the gates are evaluated on.
            layer.fade_frames = max(0, tb.to_frame(float(args["fade_ms"]) / 1000.0))
        if "follow_window" in args:
            # Records the intent and the window it was placed over. The renderer does not
            # track windows yet, so this changes nothing about the output -- but a
            # redaction the user asked to follow a window is worth persisting rather than
            # silently dropping, and the UI shows it as pending rather than active.
            layer.props["follow_window"] = bool(args["follow_window"])
        if "pad" in args:
            want = str(args["pad"])
            if want not in ("", "head", "tail"):
                raise BridgeError(f"unknown pad {want!r}")
            if layer.type == "webcam":
                # The camera is recorded footage; there is none in a pad.
                raise BridgeError("the camera cannot be moved into a pad")
            if want:
                # The pad grows to hold the layer rather than the layer being clipped
                # to a pad that may not exist yet -- "make this the start" has to work
                # on the first try, with no separate step to create room for it.
                field = "head_pad_frames" if want == "head" else "tail_pad_frames"
                length = len(layer.t) if layer.t is not None else _DEFAULT_PAD_FRAMES(tb)
                setattr(edit, field, max(getattr(edit, field), length))
                layer.pad = want
                layer.t = FrameRange(0, length)
            else:
                # Back into the recording, over the whole of it: its old source range
                # was not kept, and guessing one would put it somewhere arbitrary.
                layer.pad = ""
                layer.t = None

        if "start_ms" in args or "end_ms" in args:
            want = _range_from_ms(tb, args.get("start_ms"), args.get("end_ms"))
            if want is not None and layer.type == "webcam":
                # Two cameras on screen at once is not something the pipeline draws, and
                # add_webcam_segment already refuses it -- so a trim must not be the one
                # way in. The timeline clamps as you drag; this makes it true anyway.
                want = _layers.clamp_webcam_range(
                    edit, layer, want, _safe_source_frames(bundle))
            layer.t = want

    elif op == "delete_layer":
        victim = _find_layer(edit.layers, args["id"])
        if victim.type == "webcam":
            # Through the model, because emptying the camera track has to turn the
            # camera OFF rather than fall back to the whole-take default.
            _layers.drop_webcam_segment(edit, victim.id)
        else:
            edit.layers.remove(victim)

    elif op == "add_cut":
        r = _range_from_ms(tb, args["start_ms"], args["end_ms"])
        assert r is not None
        total = _safe_source_frames(bundle)
        if total:
            r = FrameRange(min(r.start, total - 1), min(r.end, total))
        # Merging on insert is what keeps generated gates small: a layer spanning ~30
        # separate output intervals approaches ffmpeg's 100-term expression budget.
        edit.cuts = normalize(edit.cuts + [r])

    elif op == "delete_cut":
        i = int(args["index"])
        cuts = normalize(edit.cuts)
        if not 0 <= i < len(cuts):
            raise BridgeError(f"no cut at index {i}")
        del cuts[i]
        edit.cuts = cuts

    elif op == "split_webcam":
        if not _camera_is_editable(bundle):
            raise BridgeError("this recording's camera cannot be moved")
        at = tb.to_frame(float(args["at_ms"]) / 1000.0)
        if _layers.split_webcam(edit, canvas, _safe_source_frames(bundle), at) is None:
            # Not an error: the playhead sitting on a seam, or outside every segment, is
            # a normal place for it to be, and raising would make the button read as
            # broken exactly where a user is most likely to press it.
            pass

    elif op == "add_webcam_segment":
        if not _camera_is_editable(bundle):
            raise BridgeError("this recording's camera cannot be moved")
        t = _range_from_ms(tb, args.get("start_ms"), args.get("end_ms"))
        if t is None:
            raise BridgeError("add_webcam_segment needs a start and an end")
        if _layers.add_webcam_segment(
                edit, canvas, _safe_source_frames(bundle), t) is None:
            raise BridgeError("that overlaps a segment the camera is already on for")

    elif op == "set_pads":
        # Frames, via the timebase, like every other duration in the project -- a pad
        # measured in ms would drift against the grid the gates are evaluated on.
        for key, field in (("head_ms", "head_pad_frames"), ("tail_ms", "tail_pad_frames")):
            if key in args:
                setattr(edit, field, max(0, tb.to_frame(float(args[key]) / 1000.0)))
        # A pad that shrinks past a layer living in it would leave that layer with
        # nothing to sit on, so the layer is clamped rather than left dangling.
        for layer in edit.layers:
            if not layer.pad or layer.t is None:
                continue
            span = (edit.head_pad_frames if layer.pad == "head"
                    else edit.tail_pad_frames)
            if span <= 0:
                layer.pad, layer.t = "", None
            elif layer.t.end > span:
                layer.t = FrameRange(min(layer.t.start, span - 1), span)

    elif op == "set_follow":
        # The crop's size changes with this (a window that grew mid-take needs a bigger
        # frame), so the canvas changes, so every overlay's placement is recomputed from
        # it. Dropping the memoised plan is what makes the next read see the new size.
        edit.follow_window = bool(args["follow_window"])
        bundle._follow_cache = None

    elif op == "set_export":
        want = str(args["export_preset"])
        if want not in project_mod.EXPORT_PRESETS:
            raise BridgeError(f"unknown export preset {want!r}")
        edit.export_preset = want

    elif op == "set_audio":
        edit.normalize_audio = bool(args["normalize_audio"])

    elif op == "reset":
        bundle.reset_edit()

    else:
        raise BridgeError(f"unknown op {op!r}")


def _DEFAULT_PAD_FRAMES(tb: Timebase) -> int:
    """How long a card runs when nothing has said otherwise.

    Three seconds: long enough to read a title, short enough that nobody sits through
    it wondering whether the video is broken.
    """
    return max(1, tb.to_frame(3.0))


def _timeline_map(bundle: Bundle) -> dict:
    """The output timeline: pads at the ends, recorded segments between them."""
    from . import render as _render

    # The count this module already trusts everywhere else. effective_cutmap would
    # PROBE for it, and a bundle with no media then raised straight out of
    # project_state -- the editor failing to open rather than opening empty.
    cm = _render.effective_cutmap(bundle, _safe_source_frames(bundle))
    kept = []
    out = cm.head_pad
    for k in cm.kept:
        kept.append({"out": out, "src": k.start, "len": len(k)})
        out += len(k)
    return {
        "output_frames": cm.output_frames,
        "head": cm.head_pad,
        "tail": cm.tail_pad,
        "recorded_frames": cm.kept_frames,
        "kept": kept,
    }


def _next_z(layers: list[Layer]) -> int:
    """The z for a newly added layer -- above the other content, below the camera.

    Camera segments sit at 100 and are not in the layer list, so they cannot be
    reordered against. Counting them here made every new layer land at 101: add a
    full-frame image, which is exactly what a title card is, and it covered the
    speaker's own face. The reorder in LayerList already rewrites the stack as 1..n,
    so ignoring the camera here is also what makes the initial z agree with the one a
    single drag would produce.
    """
    content = [l.z for l in layers if l.type != "webcam"]
    return (max(content) + 1) if content else 1


def _find_layer(layers: list[Layer], layer_id: str) -> Layer:
    for l in layers:
        if l.id == layer_id:
            return l
    raise BridgeError(f"no layer {layer_id!r}")


# --- theme -------------------------------------------------------------------


def _backdrop_color(value: Any) -> str:
    """A custom backdrop colour, or a refusal the editor can show.

    Checked here rather than in the model because the model also loads files: an
    edit.json with a colour this build cannot parse degrades to a wrong-looking
    backdrop, but a colour arriving from a picker in this build's own UI is a bug worth
    surfacing while someone can still fix it.
    """
    if not backgrounds.is_color(value):
        raise BridgeError(f"bad backdrop colour {value!r}; want #rrggbb")
    return str(value)


def background_catalog() -> dict:
    """The backdrop library, for the swatch grid.

    Static for the life of the build, so it is served from its own route rather than
    riding in `project_state`, which is re-serialized on every drag frame. `custom` is
    named rather than hardcoded in QML so there is one spelling of the sentinel.
    """
    return {"custom": backgrounds.CUSTOM, "entries": backgrounds.catalog()}


def theme_tokens() -> dict:
    """The design tokens for the editor's chrome, resolved from the user's Omarchy theme.

    Served from here because theme.py owns every mix, alpha and contrast decision: QML
    applies the answer and derives nothing, which is the same rule the geometry seam
    follows and for the same reason. A missing or unreadable theme is not fatal -- the
    editor ships a default palette and simply keeps it.
    """
    try:
        from . import theme as theme_mod
    except ImportError:
        return {}
    try:
        return json.loads(theme_mod.dump())
    except Exception:
        return {}


# --- proxy -------------------------------------------------------------------

# Short GOP, no B-frames: this is what makes every seek land in 15-53ms instead of the
# master's 517-651ms with half the seeks delivering no frame at all. Used only by the
# fallback below; omarchy_studio.proxy owns the real one.
_PROXY_ARGS = [
    "-vf", "scale='min(1920,iw)':-2:flags=bicubic",
    "-c:v", "libx264", "-preset", "veryfast", "-b:v", "10M",
    "-g", "15", "-bf", "0", "-pix_fmt", "yuv420p",
]


def proxy_path(bundle: Bundle, stream: str) -> Path:
    """Where a stream's preview proxy lives.

    The name is omarchy_studio.proxy's, because that module is the one that generates it
    and the editor must look for the file that actually gets written.
    """
    return bundle.proxy_dir / f"{stream}-proxy.mp4"


class ProxyBuilder:
    """Builds the preview proxies in the background, one stream at a time.

    Delegates to omarchy_studio.proxy.ensure_proxy. The fallback transcode exists because
    a proxy failure must not take the editor with it: without a proxy there is nothing to
    play at all, and playing the master instead is not an option -- half its seeks never
    deliver a frame.
    """

    def __init__(self, bundle: Bundle) -> None:
        self.bundle = bundle
        self.status: dict = {"state": "idle", "progress": 0.0, "message": ""}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._procs: list[subprocess.Popen] = []
        self._cancelled = False
        self.on_change: Callable[[dict], None] | None = None

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self.status.update(kw)
            snapshot = dict(self.status)
        if self.on_change:
            self.on_change(snapshot)

    def start(self) -> None:
        self._cancelled = False
        self._thread = threading.Thread(target=self._run, name="proxy", daemon=True)
        self._thread.start()

    def restart(self) -> None:
        """Rebuild whatever is no longer valid, cancelling a build in flight.

        Called when an edit changes what the proxy IS rather than what is drawn over
        it -- today that is the window-follow toggle, which re-crops every frame. The
        preview has to be re-encoded or it goes on showing the framing the user just
        changed, which is the one failure this whole design is meant to prevent.
        """
        if self._thread is not None and self._thread.is_alive():
            self.stop()
            self._thread.join(timeout=2.0)
        self._procs.clear()
        self.start()

    def stop(self) -> None:
        self._cancelled = True
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        # Without this the UI keeps showing "building" against a build that has stopped,
        # which is worse than no progress at all: it is progress that is lying.
        self._set(state="cancelled", message="proxy build cancelled")

    def _jobs(self) -> list[str]:
        out = []
        for name in ("screen", "camera"):
            stream = getattr(self.bundle.capture, name, None)
            if stream is not None and self.bundle.media(Path(stream.path).name).exists():
                out.append(name)
        return out

    def _run(self) -> None:
        jobs = self._jobs()
        if not jobs:
            self._set(state="error", progress=0.0, message="this bundle has no media to preview")
            return
        problems = []
        for i, name in enumerate(jobs):
            self._set(
                state="building",
                progress=i / len(jobs),
                message=f"preview proxy: {name} ({i + 1}/{len(jobs)})",
            )
            if self._cancelled:
                return
            if self._fresh(name):
                continue
            try:
                if not self._delegate(name):
                    self._transcode(name, i, len(jobs))
            except Exception as e:
                problems.append(f"{name}: {e}")
                try:
                    self._transcode(name, i, len(jobs))
                except Exception as e2:
                    self._set(state="error", message=f"{name}: {e2}"[:400])
                    return
        ready = all(proxy_path(self.bundle, n).exists() for n in jobs)
        self._set(
            state="ready" if ready else "error",
            progress=1.0 if ready else 0.0,
            # A delegate failure that the fallback covered is still worth surfacing:
            # the shared module is what the rest of the system uses.
            message="; ".join(problems)[:400],
        )

    def _fresh(self, stream: str) -> bool:
        """Whether the proxy on disk is still the right proxy.

        Existence alone is not the question. A proxy built under a different crop --
        an older build, or the follow toggle since flipped -- is a file that plays
        perfectly and shows the wrong thing, so the fingerprint decides. If the
        module cannot answer, existence is the fallback: rebuilding every launch
        would be a worse failure than reusing a proxy that is probably fine.
        """
        if not proxy_path(self.bundle, stream).exists():
            return False
        try:
            from . import proxy as proxy_mod

            return not proxy_mod.is_stale(self.bundle, stream)
        except Exception:
            return True

    def _delegate(self, stream: str) -> bool:
        try:
            from . import proxy as proxy_mod
        except ImportError:
            return False
        fn = getattr(proxy_mod, "ensure_proxy", None)
        if fn is None:
            return False
        params = inspect.signature(fn).parameters
        kwargs: dict[str, Any] = {}
        if "stream" in params:
            kwargs["stream"] = stream
        elif stream != "screen":
            return False  # the module only knows how to proxy the screen
        if "progress" in params:
            kwargs["progress"] = lambda frac, msg="": self._set(
                state="building", progress=float(frac), message=str(msg)
            )
        dest = Path(fn(self.bundle, **kwargs))
        return dest.exists()

    def _transcode(self, stream: str, i: int, n: int) -> None:
        from .probe import frame_count

        src = self.bundle.media(Path(getattr(self.bundle.capture, stream).path).name)
        dst = proxy_path(self.bundle, stream)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            total = frame_count(src)
        except Exception:
            total = 0
        tmp = dst.with_name(dst.stem + ".part.mp4")
        cmd = ["ffmpeg", "-y", "-nostdin", "-i", str(src), *_PROXY_ARGS,
               "-c:a", "copy", "-progress", "pipe:1", "-loglevel", "error", str(tmp)]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._procs.append(p)
        assert p.stdout is not None
        for line in p.stdout:
            if line.startswith("frame=") and total:
                done = int(line.split("=", 1)[1] or 0)
                self._set(
                    state="building",
                    progress=(i + min(1.0, done / total)) / n,
                    message=f"preview proxy: {stream} ({i + 1}/{n})",
                )
        p.wait()
        if p.returncode != 0:
            err = (p.stderr.read() if p.stderr else "").strip()[-300:]
            tmp.unlink(missing_ok=True)
            raise BridgeError(f"proxy transcode failed: {err}")
        # Replace only on success: a half-written proxy that looks complete would be
        # reused and the editor would preview a truncated recording.
        tmp.replace(dst)


# --- export ------------------------------------------------------------------

_PROGRESS_RE = re.compile(r"^(frame|total|progress)=(.*)$")

# The renderer runs in a child process rather than a thread. render.render() blocks on
# an ffmpeg it does not expose, so a thread could not be cancelled and a wedged export
# would take the editor with it; a child in its own session can be killed as a group,
# which reaches the ffmpeg underneath.
_RUNNER = """
import importlib, sys
from pathlib import Path
from omarchy_studio.project import Bundle

mod = importlib.import_module(sys.argv[1])
bundle = Bundle(Path(sys.argv[2]))
out = Path(sys.argv[3])

def progress(done, total):
    print("total=%d" % total, flush=True)
    print("frame=%d" % done, flush=True)

mod.render(bundle, out, progress=progress)
print("progress=end", flush=True)
"""


class Exporter:
    """Runs the renderer and turns its progress callback into a fraction for the UI.

    Anything the child prints that is not a progress line is kept as the status message,
    so a renderer failure shows up in the window instead of only in a terminal nobody is
    watching.
    """

    MODULE = "omarchy_studio.render"

    def __init__(self, bundle: Bundle, repo_root: Path) -> None:
        self.bundle = bundle
        self.repo_root = repo_root
        self.status: dict = {"state": "idle", "progress": 0.0, "message": "", "output": ""}
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self.status.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.status)

    def _output_path(self, output: str | None) -> Path:
        """Where the render is written.

        Deliberately NOT confined to the bundle: the export pane offers a native Save
        dialog and choosing a destination is the feature. Confining it would trade a
        real capability for very little, because after the token moved off the argv
        (see `Session`) the only callers that can reach this endpoint are processes
        already running as the user -- and such a process can write files directly
        without asking an exporter to do it.

        The cross-user reachability that made an arbitrary output path genuinely
        dangerous is fixed where it was caused, not papered over here.
        """
        if not output:
            return self.bundle.root / f"{self.bundle.root.name}.mp4"
        return Path(str(output))

    def start(self, output: str | None = None) -> dict:
        if self._proc and self._proc.poll() is None:
            raise BridgeError("an export is already running")
        out = self._output_path(output)
        self._set(state="running", progress=0.0, message="starting renderer", output=str(out))
        argv = [sys.executable, "-c", _RUNNER, self.MODULE, str(self.bundle.root), str(out)]
        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_child_env(self.repo_root),
                cwd=str(self.repo_root),
                start_new_session=True,
            )
        except OSError as e:
            self._set(state="error", message=str(e))
            return self.snapshot()
        threading.Thread(target=self._pump, args=(self._output_frames(),), daemon=True).start()
        return self.snapshot()

    def _output_frames(self) -> int:
        """Expected output length, or 0 when the media cannot be probed. A fabricated
        denominator would show a progress bar that finishes at the wrong moment, which
        is worse than showing the frame number alone."""
        try:
            return max(1, self.bundle.cutmap().output_frames)
        except Exception:
            return 0

    def cancel(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            self._set(state="cancelled", message="export cancelled")
            # The whole session, so the ffmpeg the renderer spawned dies with its parent
            # instead of running to completion on a file nobody wants.
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                p.terminate()

    def _pump(self, total: int) -> None:
        p = self._proc
        assert p is not None and p.stdout is not None
        for line in p.stdout:
            line = line.strip()
            m = _PROGRESS_RE.match(line)
            if not m:
                if line:
                    self._set(message=line[:200])
                continue
            key, val = m.group(1), m.group(2)
            if key == "total" and val.isdigit():
                # The renderer knows the exact output length from the cut map; prefer it
                # over the estimate made before the graph was built.
                total = max(1, int(val))
            elif key == "frame" and val.isdigit():
                n = int(val)
                if total:
                    self._set(progress=min(1.0, n / total), message=f"frame {n}/{total}")
                else:
                    self._set(message=f"frame {n}")
            elif key == "progress" and val == "end":
                self._set(progress=1.0)
        p.wait()
        err = (p.stderr.read() if p.stderr else "").strip()
        if self.snapshot()["state"] == "cancelled":
            return
        if p.returncode == 0:
            self._set(state="done", progress=1.0, message=f"wrote {self.snapshot()['output']}")
        else:
            tail = [l for l in err.splitlines() if l.strip()][-3:]
            self._set(state="error", message=" / ".join(tail) or f"renderer exited {p.returncode}")


_TOKEN_PREFIX = "omarchy-studio-token-"
# Older than any plausible session. A launcher removes its own file when its child
# exits; this is only for the ones a crash or a kill -9 left behind.
_TOKEN_STALE_SECONDS = 24 * 60 * 60


def _sweep_stale_token_files(directory: Path) -> None:
    """Remove token files nothing is using any more.

    Every launch writes one and, until this existed, nothing ever removed it: 68 had
    piled up in the runtime directory during one day of testing. Each is 0600 in a 0700
    directory so none of them leaked, but a file per launch forever is a defect, and
    they hold tokens for sessions that ended.
    """
    try:
        entries = list(directory.glob(_TOKEN_PREFIX + "*"))
    except OSError:
        return
    now = time.time()
    for entry in entries:
        try:
            if now - entry.stat().st_mtime > _TOKEN_STALE_SECONDS:
                entry.unlink()
        except OSError:
            continue          # someone else's, or gone already: not ours to insist on


def write_token_file(token: str) -> Path:
    """Put the session token in a 0600 file and return its path.

    NOT on the child's command line. /proc/<pid>/cmdline is world-readable on Linux --
    no hidepid on a default install -- so a token passed as `--token <value>` is legible
    to every local user on the machine, including ones with no business near this
    session. The bridges it guards are not read-only: the recording HUD's `discard`
    finalises a take and rmtree's the bundle, the teleprompter's /state rewrites the
    script mid-recording, and /op edits the project.

    XDG_RUNTIME_DIR is 0700 and per-user, so a 0600 file inside it is reachable only by
    this user (and root, which is already game over). The PATH may be public; only the
    contents matter. Falls back to a private temp dir when the runtime dir is unset
    rather than dropping the file somewhere shared.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    directory = Path(runtime) if runtime else Path(tempfile.mkdtemp(prefix="omarchy-studio-"))
    _sweep_stale_token_files(directory)
    fd, name = tempfile.mkstemp(prefix="omarchy-studio-token-", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    return Path(name)


def _child_env(repo_root: Path) -> dict:
    env = dict(os.environ)
    lib = str(repo_root / "lib")
    env["PYTHONPATH"] = lib + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


# --- the server --------------------------------------------------------------


class Session:
    """One open project: the bundle, the proxy build and the export, behind one lock.

    Serialized because QML fires state-changing POSTs from drag handlers and a threaded
    HTTP server would otherwise interleave two mutations of the same Edit.
    """

    def __init__(self, bundle: Bundle, repo_root: Path) -> None:
        self.bundle = bundle
        self.repo_root = repo_root
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.RLock()
        self.proxy = ProxyBuilder(bundle)
        self.exporter = Exporter(bundle, repo_root)
        self.dirty = False
        self.quit_requested = threading.Event()
        # Undo is a stack of whole-Edit snapshots rather than per-op inverses. Edit is a
        # few kB of JSON even with hundreds of layers, and a snapshot cannot get an
        # inverse subtly wrong -- which matters here because ops like the redact method
        # switch are already add-then-delete chains whose hand-written inverse would be
        # the easiest thing in the file to get wrong.
        self._undo: list[dict] = []
        self._redo: list[dict] = []

    # A drag fires an op per frame; without coalescing, one drag would be 60 undo steps
    # and Ctrl+Z would appear broken. Consecutive ops of the same kind on the same target
    # collapse into the first one's snapshot.
    _COALESCE = frozenset(
        {"set_webcam", "update_layer", "set_zoom", "set_backdrop", "set_cursor"}
    )
    UNDO_LIMIT = 100

    # Only these change the zoom track; every other op leaves it out of the reply so a
    # drag does not re-serialize thousands of samples per second.
    ZOOM_OPS = frozenset({"set_zoom", "reset"})

    def state(self, *, include_zoom_track: bool = True) -> dict:
        with self.lock:
            s = project_state(
                self.bundle,
                proxy=dict(self.proxy.status),
                export=self.exporter.snapshot(),
                include_zoom_track=include_zoom_track,
            )
            s["dirty"] = self.dirty
            # Exposed so the top bar can disable rather than hide the buttons: a control
            # that vanishes when unavailable moves everything beside it.
            s["canUndo"] = bool(self._undo)
            s["canRedo"] = bool(self._redo)
            return s

    def op(self, name: str, args: dict) -> dict:
        with self.lock:
            key = (name, str(args.get("id", "")))
            if not (
                self._undo
                and name in self._COALESCE
                and self._undo[-1].get("_key") == key
            ):
                snap = self.bundle.edit.to_dict()
                snap["_key"] = key
                self._undo.append(snap)
                del self._undo[:-self.UNDO_LIMIT]
            # Any new edit invalidates the redo branch, as in every editor.
            self._redo.clear()
            apply_op(self.bundle, name, args)
            self.dirty = True
            if name in self.REFRAME_OPS:
                self._reframe()
            return self.state(include_zoom_track=name in self.ZOOM_OPS)

    # Ops that change the FRAMING, so the proxy has to be re-encoded rather than
    # merely redrawn. Undo and redo reach the same setting by another road, which is
    # why _restore rebuilds too rather than this list being consulted there.
    REFRAME_OPS = frozenset({"set_follow", "reset"})

    def _reframe(self) -> None:
        self.bundle._follow_cache = None
        try:
            self.proxy.restart()
        except Exception:
            # A proxy that will not rebuild is a preview problem, not an edit
            # problem: the edit is already saved and the export reads the master.
            pass

    def _restore(self, snap: dict) -> None:
        snap = {k: v for k, v in snap.items() if k != "_key"}
        was = self.bundle.edit.follow_window
        self.bundle.edit = Edit.from_dict(snap)
        self.dirty = True
        if self.bundle.edit.follow_window != was:
            self._reframe()

    def undo(self) -> dict:
        with self.lock:
            if not self._undo:
                return self.state()
            cur = self.bundle.edit.to_dict()
            cur["_key"] = ("", "")
            self._redo.append(cur)
            self._restore(self._undo.pop())
            return self.state()

    def redo(self) -> dict:
        with self.lock:
            if not self._redo:
                return self.state()
            cur = self.bundle.edit.to_dict()
            cur["_key"] = ("", "")
            self._undo.append(cur)
            self._restore(self._redo.pop())
            return self.state()

    def save(self) -> dict:
        with self.lock:
            self.bundle.save_edit()
            self.dirty = False
            return self.state()


class _Handler(BaseHTTPRequestHandler):
    server_version = "omarchy-studio-bridge/1"

    @property
    def session(self) -> Session:
        return self.server.session  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:
        pass  # the default logs every drag frame to stderr

    def _authorized(self) -> bool:
        tok = self.headers.get("X-Studio-Token", "")
        if not tok and "?" in self.path:
            tok = self.path.split("?", 1)[1].partition("token=")[2].partition("&")[0]
        # On BYTES, not str. compare_digest raises TypeError for a non-ASCII str, so
        # an unauthenticated client could crash the request thread -- and print a
        # traceback -- just by sending a header with an accent in it.
        return secrets.compare_digest(tok.encode("utf-8", "surrogateescape"),
                                      self.session.token.encode("utf-8"))

    def _send(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # QML abandoned an in-flight request; the next one carries the state

    def _route(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def do_GET(self) -> None:
        if not self._authorized():
            return self._send({"error": "bad token"}, 403)
        r = self._route()
        try:
            if r in ("/", "/state"):
                return self._send(self.session.state())
            if r == "/export":
                return self._send(self.session.exporter.snapshot())
            if r == "/proxy":
                return self._send(dict(self.session.proxy.status))
            if r == "/theme":
                return self._send(theme_tokens())
            if r == "/backgrounds":
                return self._send(background_catalog())
        except Exception as e:
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._authorized():
            return self._send({"error": "bad token"}, 403)
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as e:
            return self._send({"error": f"bad JSON: {e}"}, 400)
        r = self._route()
        try:
            if r == "/op":
                return self._send(self.session.op(body["op"], body.get("args", {})))
            if r == "/save":
                return self._send(self.session.save())
            if r == "/export":
                self.session.exporter.start(body.get("output"))
                return self._send(self.session.exporter.snapshot())
            if r == "/export/cancel":
                self.session.exporter.cancel()
                return self._send(self.session.exporter.snapshot())
            if r == "/proxy/cancel":
                # Cancelling leaves the recording perfectly usable -- the master is
                # untouched and is what exports; only scrubbing is unavailable until a
                # proxy exists. ensure_proxy writes through a .part file and replaces
                # only on success, so a terminated encode leaves nothing half-written
                # for the next open to mistake for a finished proxy.
                self.session.proxy.stop()
                return self._send(dict(self.session.proxy.status))
            if r == "/undo":
                return self._send(self.session.undo())
            if r == "/redo":
                return self._send(self.session.redo())
            if r == "/quit":
                self.session.quit_requested.set()
                return self._send({"ok": True})
        except (BridgeError, ProjectError, KeyError, ValueError) as e:
            # A rejected intent is normal (a cut past the end, a burned-in webcam); the
            # editor shows the message and keeps the state it already has.
            return self._send({"error": str(e), "state": self.session.state()}, 400)
        except Exception as e:
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send({"error": "not found"}, 404)


def serve(session: Session, port: int = 0) -> ThreadingHTTPServer:
    """Bind and start. Loopback only -- this endpoint rewrites the user's project."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    srv.session = session  # type: ignore[attr-defined]
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="bridge", daemon=True).start()
    return srv


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Resolve a bundle for the QML editor.")
    ap.add_argument("bundle")
    ap.add_argument("--serve", action="store_true", help="run the loopback bridge")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)
    bundle = Bundle(Path(a.bundle))
    repo_root = Path(__file__).resolve().parents[2]
    if not a.serve:
        print(json.dumps(project_state(bundle), indent=2))
        return 0
    session = Session(bundle, repo_root)
    srv = serve(session, a.port)
    print(json.dumps({"port": srv.server_port, "token": session.token}), flush=True)
    session.proxy.start()
    try:
        while not session.quit_requested.wait(0.25):
            pass
    except KeyboardInterrupt:
        pass
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
