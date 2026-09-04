"""Layer -> filtergraph fragment.

ONE PRIMITIVE. Every non-redact layer becomes an RGBA tile of the layer's size, an
alpha ramp, then an `overlay` at the layer's position gated on the frame index. image,
text, shape and webcam differ only in how the tile is produced. That is what keeps the
QML preview and the ffmpeg export in agreement: in QML the same layer is an Item with
explicit x/y/width/height, an opacity ramp and a visible gate, and the two composites
measured geometrically identical to within 2 px at 1920x1080.

`blur` and `pixelate` are the one exception -- they read the pixels beneath them, so
they compile to split -> crop -> gblur/pixelize -> overlay-back.

Things this module deliberately does not do:

* It never computes a placement. Every number comes from `geometry.Placement.resolve`
  and every blur sigma from `geometry.ffmpeg_blur`; a second implementation of the same
  maths drifts, and the drift is invisible until someone diffs rendered frames.
* It never cuts anything. Cuts are applied FIRST, upstream, by `cuts.cut_chain` (2.20 s
  versus 2.83 s on the same project), so the labels reaching this module are already on
  the output timeline and layer gates are `CutMap.remap`ped to match.
* It never resamples the camera. `trim` cuts on frame indices, so a camera stream has to
  be put on the project frame grid (`timebase_chain`) BEFORE it is cut, not after.

Cost, measured: ~0.7-1.0 ms per layer per 1080p frame, linear in the number of layers
EVER ADDED. `enable` saves nothing -- a layer visible 1 s out of 20 costs the same as
one visible throughout (2.75 s vs 2.70 s at n=5, 18.30 s vs 17.49 s at n=40) -- so the
only mitigation for a heavy project is baking co-temporal static layers into a cached
plate, which is the caller's decision, not this module's.

What this module can do about it, and does: a tile with no fade is generated as a
SINGLE frame and held by overlay's eof_action=repeat. Measured at 1080p with 40 shape
layers over 60 frames -- 0.129 s against 1.294 s for full-length tile sources, with
bit-identical output.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field, replace

from .exprs import escape_drawtext, fade_filters, frame_gate
from .geometry import DEFAULT_REDACT, Canvas, Rect, ffmpeg_blur, ffmpeg_pixelate
from .project import Layer, WebcamSettings
from .timebase import CutMap, FrameRange, Timebase

# One font file, named identically here and in QML. Resolving a family name through
# fontconfig gives libavfilter and Qt different faces, and the metrics diverge long
# before anybody notices the glyphs did.
DEFAULT_FONTFILE = "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf"

_OVERLAY_TYPES = frozenset({"image", "text", "caption", "shape", "webcam"})
_REDACT_TYPES = frozenset({"blur", "pixelate"})
# `zoom` is a crop on the base video, not an overlay; the zoom compiler owns it and
# silently skipping it here keeps a normal project from emitting a warning per zoom.
_NOT_AN_OVERLAY = frozenset({"zoom"})
# Deferred from v1: the arrow head geometry diverged 93% between drawtext-era masks and
# the QML preview, and no calibration was found.
_DEFERRED_TYPES = frozenset({"arrow"})


class LayerError(ValueError):
    pass


class UnsupportedLayer(UserWarning):
    """A layer was skipped. Forward compatibility is a stated property of the format:
    a project written by a newer build must degrade to 'some overlays missing'."""


class InputRegistry:
    """Allocates ffmpeg input indices and resolves named source streams.

    Two kinds of entry, because layers need both:

    * inputs the graph opens (`add`) -- an image asset gets a real `-i`;
    * labels the caller has already produced (`bind`) -- the camera reaches a webcam
      layer as an in-graph label, because it must be resampled and cut before use.
    """

    def __init__(self) -> None:
        self._args: list[list[str]] = []
        self._labels: dict[str, str] = {}
        self._queues: dict[str, list[str]] = {}
        self._unconsumed: dict[str, list[str]] = {}

    def add(self, args: list[str], key: str | None = None) -> int:
        """Register an ffmpeg input argument group; returns its input index."""
        idx = len(self._args)
        self._args.append(list(args))
        if key is not None:
            self._labels[key] = f"[{idx}:v]"
        return idx

    def bind(self, key: str, label: str) -> None:
        """Point a named source at an existing stream label, e.g. the cut camera."""
        self._labels[key] = label
        self._queues.pop(key, None)
        self._unconsumed[key] = [label]

    def bind_fanout(self, key: str, labels: list[str]) -> None:
        """Hand out one label per consumer, in order.

        ffmpeg consumes a labelled pad EXACTLY ONCE. Two webcam segments both reading
        the camera's label produced no error and no camera: the second reference fell
        through to the screen, so the second bubble showed the desktop inside its mask.
        The caller emits a `split` and passes its outputs here.
        """
        if not labels:
            raise LayerError(f"bind_fanout({key!r}) needs at least one label")
        self._labels[key] = labels[0]
        self._queues[key] = list(labels)
        self._unconsumed[key] = list(labels)

    def has(self, key: str) -> bool:
        return key in self._labels

    def label(self, key: str) -> str:
        if key not in self._labels:
            raise LayerError(f"no source registered under {key!r}")
        queue = self._queues.get(key)
        if queue is None:
            label = self._labels[key]
        elif not queue:
            # More consumers than the split was sized for. Raising beats handing out a
            # label twice, which is exactly the silent wrong-source bug fanout exists
            # to prevent.
            raise LayerError(f"more consumers of {key!r} than the split provides")
        else:
            label = queue.pop(0)
        remaining = self._unconsumed.get(key)
        if remaining and label in remaining:
            remaining.remove(label)
        return label

    def unconsumed(self, key: str) -> list[str]:
        """Labels bound for `key` that no layer actually read.

        Counting consumers ahead of compiling is a guess, and compile_layer has several
        early returns -- a range that a cut removes entirely, a rect that rounds to
        nothing -- that happen BEFORE it asks for a source. ffmpeg rejects the whole
        graph over one unconnected pad ("Filter 'split' has output 0 unconnected"), so
        the caller drains whatever is left into a sink. Draining is deliberately not
        conditioned on WHY a layer skipped: a future skip nobody has thought of yet must
        not be able to break every export.
        """
        return list(self._unconsumed.get(key, []))

    @property
    def inputs(self) -> list[list[str]]:
        return self._args

    def argv(self) -> list[str]:
        """The flattened `-i ...` arguments, in index order. This is the authoritative
        list -- `LayerFragment.extra_inputs` is a view of the groups one layer added."""
        return [a for group in self._args for a in group]


@dataclass
class LayerFragment:
    """One layer's contribution to the graph.

    `filter_chain` is ';'-joined and has no trailing separator, so fragments join with
    ';'. `label_out` is what the next layer takes as its `label_in`.
    """

    filter_chain: str
    label_in: str
    label_out: str
    extra_inputs: list[list[str]] = field(default_factory=list)


def timebase_chain(label_in: str, tb: Timebase, label_out: str) -> str:
    """Put a source on the project frame grid. Must run BEFORE the cut.

    `trim` counts frames on its input's own grid, so cutting a 30 fps camera against
    frame indices computed for a 60 fps screen removes the wrong material. Resampling
    first makes the two grids the same object.
    """
    return f"{label_in}fps={tb.fps_num}/{tb.fps_den},setsar=1{label_out}"


# --- the webcam track -------------------------------------------------------
#
# The camera is a TRACK OF SEGMENTS, not a keyframed curve, and that is a decision
# rather than a shortcut. What people ask for is "head on for the intro, gone for the
# middle, back at the end" -- a clip operation. A curve would need a new interpolated
# representation plus a second copy of the export's easing, and the editor and the
# export disagreeing about easing is the worst class of bug this project has had. Fades
# (Layer.fade_frames, already generic) supply the polish that motion would otherwise be
# reached for. A segment can grow a start and end position later and animate between
# them without any of this changing.
#
# Segments live in edit.layers as ordinary `webcam` layers, which render._layer_list
# already prefers over Edit.webcam. So a take nobody has touched stays ONE static
# setting and one toggle, and only becomes segments the first time it is split -- the
# simple case never pays for the complicated one.


def webcam_segments(
    edit, canvas: Canvas, total_frames: int
) -> list[Layer]:
    """The camera's segments in time order.

    Explicit layers when they exist, otherwise the single implicit one that Edit.webcam
    describes -- so callers see one shape whether or not the track has been touched.
    An empty list means the camera is off for the whole recording.
    """
    # The global toggle first, and it outranks the track. render gates the camera INPUT
    # on this flag, so a track that kept drawing segments while it was off had the
    # timeline promising a camera the export could not contain. Segments are KEPT, not
    # discarded -- toggling off and on again gets the same track back.
    if not edit.webcam.enabled:
        return []
    explicit = [l for l in edit.layers if l.type == "webcam"]
    if explicit:
        return sorted(explicit, key=lambda l: l.t.start if l.t else 0)
    return [webcam_layer(edit.webcam, canvas, FrameRange(0, max(1, total_frames)))]


def materialize_webcam(edit, canvas: Canvas, total_frames: int) -> list[Layer]:
    """Promote the implicit whole-take camera into a real segment, in place.

    Called before any edit that needs something to act ON. Idempotent: a track that is
    already explicit is returned untouched.
    """
    explicit = [l for l in edit.layers if l.type == "webcam"]
    if explicit:
        return sorted(explicit, key=lambda l: l.t.start if l.t else 0)
    # The id is NOT rewritten. The timeline hands the implicit segment's id straight to
    # the bridge, so renaming it here made every control on a freshly-clicked camera
    # throw `no layer 'webcam'` -- the id the UI was holding stopped existing at the
    # moment it became real.
    segments = webcam_segments(edit, canvas, total_frames)
    for seg in segments:
        edit.layers.append(seg)
    return segments


def split_webcam(
    edit, canvas: Canvas, total_frames: int, at_frame: int
) -> Layer | None:
    """Cut the segment under `at_frame` in two. Returns the new right-hand half.

    A split exactly on a boundary is a no-op rather than an error: the playhead sitting
    on a seam is a normal place for it to be, and refusing would make the button feel
    broken at the one position a user is most likely to try it from.
    """
    segments = materialize_webcam(edit, canvas, total_frames)
    for seg in segments:
        if seg.t is None or not (seg.t.start < at_frame < seg.t.end):
            continue
        right = replace(seg, id=_next_webcam_id(edit.layers),
                        t=FrameRange(at_frame, seg.t.end),
                        props=dict(seg.props))
        seg.t = FrameRange(seg.t.start, at_frame)
        edit.layers.append(right)
        return right
    return None


def add_webcam_segment(
    edit, canvas: Canvas, total_frames: int, t: FrameRange
) -> Layer | None:
    """A segment in a gap, styled like its nearest neighbour.

    Inheriting the neighbour's look rather than the global default is what makes
    "bring my head back at the end" one action: the head that comes back is the head
    that left, in the same shape and size, and only its position is worth changing.
    Overlapping an existing segment is refused -- two cameras on screen at once is not
    a thing this pipeline draws, and silently clipping one would be worse than saying no.
    """
    # Re-arms the camera: emptying the track turns it off (see drop_webcam_segment), so
    # adding the first segment back has to undo that or the new segment would be stored
    # and never rendered.
    #
    # An emptied track must NOT be materialized on the way, though. Doing so recreated
    # the implicit whole-take segment, which then overlapped the range being asked for
    # and refused it -- so the camera, once removed entirely, could never be brought
    # back. `enabled` being false is exactly the signal that the track was emptied on
    # purpose rather than never touched.
    was_off = not edit.webcam.enabled
    edit.webcam.enabled = True
    if was_off:
        segments = [l for l in edit.layers if l.type == "webcam"]
    else:
        segments = materialize_webcam(edit, canvas, total_frames)
    for seg in segments:
        if seg.t is not None and seg.t.start < t.end and t.start < seg.t.end:
            return None
    template = min(
        segments,
        key=lambda s: abs((s.t.start if s.t else 0) - t.start),
        default=None,
    )
    if template is not None:
        fresh = replace(template, id=_next_webcam_id(edit.layers), t=t,
                        props=dict(template.props))
    else:
        fresh = webcam_layer(edit.webcam, canvas, t)
        fresh.id = _next_webcam_id(edit.layers)
    edit.layers.append(fresh)
    return fresh


def _next_webcam_id(existing: list[Layer]) -> str:
    used = {l.id for l in existing}
    n = 1
    while f"webcam{n}" in used:
        n += 1
    return f"webcam{n}"


def webcam_layer(
    settings: WebcamSettings,
    canvas: Canvas,
    t: FrameRange | None = None,
    z: int = 100,
) -> Layer:
    """Adapt `Edit.webcam` into the Layer the one primitive understands.

    The webcam is a layer like any other -- it is stored as settings only because the
    editor gives it a dedicated panel.
    """
    place = settings.placement(canvas)
    return Layer(
        id="webcam",
        type="webcam",
        t=t,
        # Through settings.placement so the export's box is the preview's box; a
        # circle is square in pixels and h is derived, not the stored value.
        x=place.x,
        y=place.y,
        w=place.w,
        h=place.h,
        z=z,
        props={
            "shape": settings.shape,
            "corner_radius": settings.corner_radius,
            "mirror": settings.mirror,
        },
    )


def compile_layer(
    layer: Layer,
    canvas: Canvas,
    cutmap: CutMap,
    tb: Timebase,
    inputs: InputRegistry,
    *,
    label_in: str | None = None,
) -> LayerFragment | None:
    """Compile one layer into a filtergraph fragment.

    Returns None when the layer contributes nothing -- disabled, an unknown or deferred
    type, or a time range that a cut removed entirely. The caller then carries its
    current label forward unchanged.

    `label_in` defaults to a label derived from the layer id; pass the current head of
    the video chain instead, which is what the assembler does.
    """
    name = _safe_name(layer.id)
    lin = label_in if label_in is not None else f"[{name}_in]"

    if not layer.enabled:
        return None
    if layer.type in _NOT_AN_OVERLAY:
        return None
    if layer.type in _DEFERRED_TYPES:
        warnings.warn(
            f"layer {layer.id!r}: {layer.type!r} is deferred from v1 and was skipped",
            UnsupportedLayer,
            stacklevel=2,
        )
        return None
    if layer.type not in _OVERLAY_TYPES and layer.type not in _REDACT_TYPES:
        warnings.warn(
            f"layer {layer.id!r}: unknown type {layer.type!r} was skipped",
            UnsupportedLayer,
            stacklevel=2,
        )
        return None

    span = layer.t if layer.t is not None else FrameRange(0, cutmap.total_frames)
    ranges = cutmap.remap(span)
    if not ranges:
        return None  # the whole layer sits inside a cut
    gate = frame_gate(ranges)
    # The ramp runs over the layer's OUTPUT extent; remap already merged the pieces.
    ramp_span = FrameRange(ranges[0].start, ranges[-1].end)

    if layer.type in _REDACT_TYPES:
        return _compile_redaction(layer, name, canvas, gate, lin)
    return _compile_overlay(layer, name, canvas, cutmap, tb, inputs, gate, ramp_span, lin)


# -- the one primitive -------------------------------------------------------


def _compile_overlay(
    layer: Layer,
    name: str,
    canvas: Canvas,
    cutmap: CutMap,
    tb: Timebase,
    inputs: InputRegistry,
    gate: str,
    ramp_span: FrameRange,
    lin: str,
) -> LayerFragment | None:
    # No clamping: an overlay may legitimately hang off the canvas edge, and `overlay`
    # accepts negative coordinates. (Redactions ARE clamped -- `crop` cannot.)
    rect = layer.placement.resolve(canvas).to_even()
    if rect.w < 2 or rect.h < 2:
        warnings.warn(
            f"layer {layer.id!r}: degenerate size {rect.w}x{rect.h} after even-snapping",
            UnsupportedLayer,
            stacklevel=3,
        )
        return None

    # A tile that never changes is generated ONCE and held by overlay's
    # eof_action=repeat. Measured at 1080p with 40 shape layers over 60 frames: 0.129 s
    # against 1.294 s for full-length tile sources, bit-identical output. Only a fade
    # forces real frames, because `fade` counts them.
    ramp = fade_filters(ramp_span, layer.fade_frames)

    before = len(inputs.inputs)
    if layer.type == "image":
        chains, tile = _tile_image(layer, name, rect, cutmap, tb, inputs, not ramp)
    elif layer.type == "text":
        chains, tile = _tile_text(layer, name, rect, cutmap, tb, not ramp)
    elif layer.type == "caption":
        chains, tile = _tile_caption(layer, name, rect, cutmap, tb, not ramp)
    elif layer.type == "shape":
        chains, tile = _tile_shape(layer, name, rect, cutmap, tb, not ramp)
    else:
        chains, tile = _tile_webcam(layer, name, rect, inputs)
    extra_inputs = inputs.inputs[before:]

    tail = ",".join(b for b in (ramp, _opacity(layer)) if b)
    if tail:
        chains.append(f"{tile}{tail}[{name}_f]")
        tile = f"[{name}_f]"

    out = f"[{name}_o]"
    # eof_action=repeat + shortest=0 because the streams end at different instants:
    # a 227 ms tail difference was measured against a 179 ms start offset, purely from
    # camera frame granularity. The base must define the length, never the tile.
    chains.append(
        f"{lin}{tile}overlay=x={int(rect.x)}:y={int(rect.y)}:enable='{gate}'"
        f":eof_action=repeat:shortest=0:format=auto{out}"
    )
    return LayerFragment(";".join(chains), lin, out, extra_inputs)


def _opacity(layer: Layer) -> str:
    if not 0.0 <= layer.opacity <= 1.0:
        raise LayerError(f"layer {layer.id!r} opacity {layer.opacity} outside 0..1")
    if layer.opacity >= 1.0:
        return ""
    return f"colorchannelmixer=aa={layer.opacity:.6f}"


# -- tile producers ----------------------------------------------------------


def _tile_image(
    layer: Layer,
    name: str,
    rect: Rect,
    cutmap: CutMap,
    tb: Timebase,
    inputs: InputRegistry,
    static: bool,
) -> tuple[list[str], str]:
    path = layer.props.get("path") or layer.props.get("asset")
    if not path:
        raise LayerError(f"image layer {layer.id!r} has no props['path']")
    args = ["-i", str(path)] if static else [
        "-loop", "1",
        "-framerate", f"{tb.fps_num}/{tb.fps_den}",
        "-t", f"{_output_seconds(cutmap, tb):.6f}",
        "-i", str(path),
    ]
    idx = inputs.add(args)
    w, h = int(rect.w), int(rect.h)
    if layer.props.get("fit") == "contain":
        scale = (
            f"scale={w}:{h}:flags=bicubic:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black@0.0"
        )
    else:
        scale = f"scale={w}:{h}:flags=bicubic"
    return ([f"[{idx}:v]format=rgba,{scale},setsar=1[{name}_s]"], f"[{name}_s]")


def _tile_text(
    layer: Layer, name: str, rect: Rect, cutmap: CutMap, tb: Timebase, static: bool
) -> tuple[list[str], str]:
    text = layer.props.get("text", "")
    w, h = int(rect.w), int(rect.h)
    size = font_px(layer.props.get("font_px"), rect)
    color = _hexcol(layer.props.get("color", "white"))
    fontfile = layer.props.get("fontfile", DEFAULT_FONTFILE)
    box_rgb, box_alpha = split_color(layer.props.get("box_color", "black@0.0"))
    box_rgb = _hexcol(box_rgb)
    radius = radius_px(layer.props.get("radius", 0.0), rect)

    chains: list[str] = []
    src = f"[{name}_r]"
    if box_alpha <= 0.0:
        chains.append(_color_source("black@0.0", w, h, cutmap, tb, src, static))
    else:
        chains.append(_color_source(box_rgb, w, h, cutmap, tb, src, static))
        # alphamerge REPLACES the alpha plane, so a translucent box colour would come
        # out fully opaque. Fold the box alpha into the mask instead, and draw the text
        # AFTER the merge so the glyphs stay at alpha 1 -- which is what the QML sibling
        # Text item does, and the mismatch was found by the preview cross-check.
        chains.append(
            f"color=c=black:s={w}x{h}:r=1:d=1,format=gray,"
            f"geq=lum='({_rounded_rect_mask(w, h, radius)})*{box_alpha:.6f}'[{name}_m]"
        )
        chains.append(f"{src}[{name}_m]alphamerge=repeatlast=1:shortest=0[{name}_b]")
        src = f"[{name}_b]"

    # Tile-relative centring. The tile's origin IS the placement rect's origin, so
    # (w-text_w)/2 is algebraically the same point as geometry.text_placement's
    # canvas-absolute cx-text_w/2 -- and centre-anchoring is what measured 0.0 px
    # against the preview, where left-anchoring drifted ~7% of the string width.
    chains.append(
        f"{src}drawtext=fontfile={fontfile}:text='{escape_drawtext(text)}'"
        f":fontsize={size}:fontcolor={color}"
        f":x=(w-text_w)/2:y=(h-text_h)/2[{name}_s]"
    )
    return chains, f"[{name}_s]"


def _tile_caption(
    layer: Layer, name: str, rect: Rect, cutmap: CutMap, tb: Timebase, static: bool
) -> tuple[list[str], str]:
    """A transcript's worth of timed strings on one tile. See `captions`.

    `static` is accepted to match every other tile producer and then ignored, which is
    the one thing worth saying here: a caption tile gates each segment on the frame
    index, so a one-frame source would draw whichever segment covers frame 0 and let
    overlay's eof_action=repeat hold it across the entire video.
    """
    # Deferred, not at module scope: `captions` borrows this module's tile helpers, and
    # importing it back from the top of the file would be a cycle.
    from .captions import caption_tile

    return caption_tile(layer, name, rect, cutmap, tb)


def _tile_shape(
    layer: Layer, name: str, rect: Rect, cutmap: CutMap, tb: Timebase, static: bool
) -> tuple[list[str], str]:
    w, h = int(rect.w), int(rect.h)
    rgb, alpha = split_color(layer.props.get("color", "#ff3b30"))
    rgb = _hexcol(rgb)
    radius = radius_px(layer.props.get("radius", 0.0), rect)
    chains = [_color_source(rgb, w, h, cutmap, tb, f"[{name}_r]", static)]
    if radius <= 0 and alpha >= 1.0:
        return chains, f"[{name}_r]"
    chains.append(
        f"color=c=black:s={w}x{h}:r=1:d=1,format=gray,"
        f"geq=lum='({_rounded_rect_mask(w, h, radius)})*{alpha:.6f}'[{name}_m]"
    )
    chains.append(f"[{name}_r][{name}_m]alphamerge=repeatlast=1:shortest=0[{name}_s]")
    return chains, f"[{name}_s]"


def _tile_webcam(
    layer: Layer, name: str, rect: Rect, inputs: InputRegistry
) -> tuple[list[str], str]:
    cam = inputs.label("camera")
    w, h = int(rect.w), int(rect.h)
    shape = layer.props.get("shape", "circle")
    # A square centre crop expressed in `crop`'s own expressions, so the compiler stays
    # pure -- probing the camera file here would make every layer test need media.
    # `shape=rect` skips it and stretches the native frame into the box instead.
    side = "min(iw,ih)"
    pre = (
        f"crop=w='{side}':h='{side}':x='(iw-{side})/2':y='(ih-{side})/2',"
        if shape in ("circle", "rounded")
        else ""
    )
    mirror = "hflip," if layer.props.get("mirror", True) else ""
    chains = [
        f"{cam}{pre}{mirror}scale={w}:{h}:flags=bicubic,setsar=1,format=rgba[{name}_c]"
    ]
    if shape == "rect":
        return chains, f"[{name}_c]"
    # `rounded` is the superellipse, not a rectangle with a big radius -- see
    # _squircle_mask for why those are not the same shape. The shallow rounded rectangle
    # the webcam used to offer under this name is gone; `corner_radius` survives on the
    # model for the backdrop, which still wants a real radius.
    mask = _circle_mask(w, h) if shape == "circle" else _squircle_mask(w, h)
    chains.append(
        f"color=c=black:s={w}x{h}:r=1:d=1,format=gray,geq=lum='{mask}'[{name}_m]"
    )
    chains.append(f"[{name}_c][{name}_m]alphamerge=repeatlast=1:shortest=0[{name}_s]")
    return chains, f"[{name}_s]"


# -- redaction ---------------------------------------------------------------


def _compile_redaction(
    layer: Layer, name: str, canvas: Canvas, gate: str, lin: str
) -> LayerFragment | None:
    # Clamped, unlike an overlay: `crop` outside the frame is a hard error.
    rect = layer.placement.resolve(canvas).clamped_to(canvas).to_even()
    if rect.w < 2 or rect.h < 2:
        warnings.warn(
            f"layer {layer.id!r}: degenerate redaction {rect.w}x{rect.h}",
            UnsupportedLayer,
            stacklevel=3,
        )
        return None

    preset = str(layer.props.get("preset", DEFAULT_REDACT))
    if layer.type == "blur":
        # gblur, not boxblur: the same Gaussian kernel family as Qt's MultiEffect, so
        # preview and export track each other across presets.
        op = ffmpeg_blur(preset)
    else:
        # Pixelate rides the SAME preset ladder as blur. It used to take a continuous
        # `block` prop, which is a slider by another name -- and it is the path that
        # already shipped one leak (normalized 0.012 read as pixels, floored to a 2px
        # mosaic: previewed unreadable, exported legible). A caller that still passes an
        # explicit `block` is honoured, but nothing writes one any more.
        if "block" in layer.props:
            block = int(round(block_px(layer.props["block"], canvas)))
            op = f"pixelize=w={block}:h={block}"
        else:
            op = ffmpeg_pixelate(preset, canvas)

    # No fade and no opacity on a redaction, deliberately: a partially transparent
    # blur box leaks the pixels it exists to hide.
    x, y = int(rect.x), int(rect.y)
    chains = [
        f"{lin}split=2[{name}_a][{name}_b]",
        f"[{name}_a]crop=w={int(rect.w)}:h={int(rect.h)}:x={x}:y={y},{op},"
        f"format=rgba[{name}_t]",
        f"[{name}_b][{name}_t]overlay=x={x}:y={y}:enable='{gate}'"
        f":eof_action=repeat:shortest=0:format=auto[{name}_o]",
    ]
    return LayerFragment(";".join(chains), lin, f"[{name}_o]", [])


# -- small helpers -----------------------------------------------------------


def _safe_name(layer_id: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_]", "_", str(layer_id))
    if not s or s[0].isdigit():
        s = "L" + s
    return s


def _output_seconds(cutmap: CutMap, tb: Timebase) -> float:
    return cutmap.output_frames * tb.fps_den / tb.fps_num


def _color_source(
    color: str, w: int, h: int, cutmap: CutMap, tb: Timebase, label: str, static: bool
) -> str:
    rate = "r=1:d=1" if static else (
        f"r={tb.fps_num}/{tb.fps_den}:d={_output_seconds(cutmap, tb):.6f}"
    )
    return f"color=c={color}:s={w}x{h}:{rate},format=rgba{label}"


def _hexcol(c: str) -> str:
    return "0x" + c.lstrip("#") if c.startswith("#") else c


def split_color(spec: str) -> tuple[str, float]:
    """Split 'colour@alpha' into the bare colour token and a 0..1 alpha.

    Public because the preview needs the identical split: `box_color` is ONE property,
    and a preview that reads a separate `box_opacity` draws a box the export does not.
    The colour is returned in the spelling it was written in -- `_hexcol` converts to
    ffmpeg's 0x form at the point of use, and QML wants the '#' form.
    """
    rgb, sep, a = str(spec).partition("@")
    return rgb, (float(a) if sep else 1.0)


def radius_px(radius: float, rect: Rect) -> float:
    """Corner radius is stored normalized to the TILE's short side so the same project
    renders identically against a 1080p proxy and a 1440p master.

    Public for the same reason as `split_color`: the preview normalized against the
    canvas width instead, so a 0.2 radius on a small tile rounded a shape into a
    lozenge in the preview and left it square in the export.
    """
    r = float(radius)
    return r * min(rect.w, rect.h) if r <= 1.0 else r


def block_px(block: float, canvas: Canvas) -> float:
    """Pixelate block size, in output pixels.

    Values at or below 1 are normalized to the canvas WIDTH -- the same "<=1 means
    normalized" convention `radius_px` uses -- because a mosaic authored on a 1080p
    proxy has to hide the same words on a 1440p master. The editor writes the
    normalized form (0.012), and reading it as pixels floored it to a 2 px block: a
    redaction that previews as an unreadable mosaic and exports as legible text.
    """
    b = float(block)
    return max(2.0, b * canvas.width if b <= 1.0 else b)


def font_px(font: float | int | None, rect: Rect) -> int:
    """Caption size in pixels, defaulting to 0.6 of the tile height.

    Public so the preview sizes from the TILE, as drawtext does, rather than from the
    canvas height: the two defaults disagreed by more than 2x on a short caption box.
    """
    return int(font) if font else max(8, round(rect.h * 0.6))


def _rounded_rect_mask(w: int, h: int, r: float) -> str:
    """Antialiased rounded-rect coverage, ~1 px of edge softening.

    Built from a 1-frame lavfi source so `geq` -- a per-pixel interpreter -- runs once
    for the whole render rather than once per frame.
    """
    if r <= 0:
        return "255"
    r = min(r, min(w, h) / 2.0)
    dx = f"max(max({r:.4f}-X,X-({w}-1-{r:.4f})),0)"
    dy = f"max(max({r:.4f}-Y,Y-({h}-1-{r:.4f})),0)"
    return f"clip(255*({r:.4f}-hypot({dx},{dy})+0.5),0,255)"


def _circle_mask(w: int, h: int) -> str:
    """An ellipse inscribed in the tile; for a square tile that is a circle."""
    rx, ry = w / 2.0, h / 2.0
    r = min(rx, ry)
    return (
        f"clip(255*({r:.4f}-hypot((X-{rx:.4f}+0.5)*{r / rx:.6f},"
        f"(Y-{ry:.4f}+0.5)*{r / ry:.6f})+0.5),0,255)"
    )


# The Lamé exponent. 2 is exactly an ellipse and 4 is the classic squircle; past about
# 6 the flats grow long enough that it reads as a rounded rectangle again, which is a
# shape the product already has. 4 is the one people mean by the word.
SQUIRCLE_N = 4.0


def _squircle_mask(w: int, h: int, n: float = SQUIRCLE_N) -> str:
    """A superellipse inscribed in the tile: |x/a|^n + |y/b|^n = 1.

    Not a rounded rectangle with a big radius. A rounded rect is three primitives --
    straight, arc, straight -- and the curvature jumps at both joins; a superellipse has
    one continuously varying curvature, which is the whole visual point of the shape.

    Antialiased the same way as the other masks, from the n-norm distance: `t` is 1 on
    the outline and scales linearly across it near the edge, so `r*(1-t)` is a signed
    distance in pixels accurate enough for the ~1px of softening the tile needs. At
    n=2 this reduces algebraically to _circle_mask, which is the cheapest proof it is
    right.
    """
    a, b = w / 2.0, h / 2.0
    r = min(a, b)
    ax = f"(abs(X-{a:.4f}+0.5)/{a:.4f})"
    ay = f"(abs(Y-{b:.4f}+0.5)/{b:.4f})"
    t = f"pow(pow({ax},{n:.1f})+pow({ay},{n:.1f}),{1.0 / n:.6f})"
    return f"clip(255*({r:.4f}*(1-{t})+0.5),0,255)"


def drop_webcam_segment(edit, layer_id: str) -> bool:
    """Remove one segment; emptying the track turns the camera OFF.

    Without the second half, deleting every segment fell back to the implicit
    whole-take camera and the head reappeared for the entire recording -- the track
    could add and move the camera but never remove it, which is the one thing the
    feature exists for. `enabled` is the tombstone rather than a new schema field, and
    add_webcam_segment lifts it again.
    """
    before = len(edit.layers)
    edit.layers = [l for l in edit.layers if l.id != layer_id]
    if len(edit.layers) == before:
        return False
    if not any(l.type == "webcam" for l in edit.layers):
        edit.webcam.enabled = False
    return True
