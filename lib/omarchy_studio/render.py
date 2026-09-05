"""The render driver: a Bundle in, one ffmpeg process out.

STAGE ORDER IS LOAD-BEARING. cut -> cursor -> zoom -> backdrop -> layers, in that order:

* Cut FIRST, not last. Same project, 2.20 s against 2.83 s, because every stage
  downstream then runs over fewer frames. Layer gates are remapped to output time by
  `CutMap.remap`, and rendering cut-first with remapped ranges against cut-last with
  verbatim ranges gave bit-identical frames, so the reordering is provably free.
* Cursor BEFORE the zoom, so the zoom magnifies the pointer along with the pixels under
  it. Drawn after the zoom it would have to have the zoom's own per-frame viewport
  inverted into all four of its overlay expressions, and any disagreement between the
  two copies shows up as the pointer sliding across the thing it is pointing at.
  It costs the stream its chroma subsampling -- see `_cursor`.
* Zoom SECOND, while the base is still planar YUV. `perspective` costs 8.4 ms/frame at
  1440p on yuv420p and 14.9 ms/frame once the backdrop has converted the stream to
  rgba/gbrap, and it is the same pixels either way.
* Backdrop THIRD, and it is the stage that fixes the output length. A `color` or
  `gradients` source used as the overlay MAIN input drags its 1/1 timebase into the
  output -- a 6.9 s clip once came out 208 seconds long -- so the backdrop is infinite
  and the real video reaches it through `shortest=1`.
* Layers LAST. Their cost is linear in the number ever added, not in how long each is
  visible (2.75 s against 2.70 s for a layer visible 1 s of 20 versus throughout), so
  there is nothing to gain by ordering them by visibility.

THE GRAPH GOES TO A FILE. argv dies at ~288 KB with E2BIG, and ffmpeg 9.0.1 -- which is
what is installed -- has removed `-filter_complex_script`. The surviving syntax is
`-/filter_complex <path>`, ffmpeg's general "read this argument from a file" prefix.
Verified on this build, including a graph containing newlines.

ENCODE ON THE CPU. VAAPI measured slower at both sizes (18.11 s against 19.82 s at
1080p, 49.43 against 52.80 at 4K) because every filter in the graph is CPU-only, so
hwupload only adds a copy; VAAPI offloads the encode, which was never the bottleneck.

THE CAMERA IS ALIGNED IN THE GRAPH, NOT WITH `-itsoffset`. `-itsoffset` is seconds, the
cut is frame indices, and the two do not agree in both directions: measured on this
build, `-itsoffset 0.2` into `fps=30` delays the content by exactly the 6 frames asked
for, but `-itsoffset -0.2` advances it by 4 rather than 6, because frames with negative
timestamps are dropped by a rule of their own. `tpad`/`trim` on the project grid are
exact in both directions and compose with the frame-index cut that follows.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import backgrounds, cuts, follow, layers as layers_mod, probe
from .geometry import Canvas
from .project import Bundle, BackdropSettings, Layer, ProjectError
from .timebase import CutMap, FrameRange, Timebase
from .zoom import zoom_filter, zoom_segments

# The capture pop: gsr's first audio packets carry the stream opening. The mute has to
# outlast the pop and stop well short of any plausible first word.
POP_MUTE_SECONDS = 0.4
POP_FADE_SECONDS = 0.05

# Applied only when probe.has_discardable_warmup() finds warm-up packets in the first
# GOP. Doing it unconditionally shifted every timestamp by exactly -100 ms and
# invalidated the anchor; doing it only when those packets are present removes material
# the decoder was never going to output, so frame 0 still means frame 0.
HEAD_TRIM_SECONDS = 0.1

ProgressCallback = Callable[[int, int], None]


class RenderError(RuntimeError):
    pass


@dataclass
class RenderPlan:
    """Everything needed to invoke ffmpeg, with the graph still a string."""

    graph: str
    inputs: list[str]
    maps: list[str]
    output_args: list[str]
    total_frames: int
    canvas: Canvas
    head_trim_seconds: float = 0.0
    # One argv group per ffmpeg input, in index order; `inputs` is this flattened.
    input_specs: list[list[str]] = field(default_factory=list)

    def argv(self, graph_path: Path, out_path: Path) -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-progress", "pipe:1", "-nostats",
            *self.inputs,
            "-/filter_complex", str(graph_path),
            *self.maps,
            *self.output_args,
            str(out_path),
        ]


class _Graph:
    """Chain accumulator.

    Chains are joined with ';' followed by a newline: `exprs.escape_drawtext` can emit a
    real newline inside a caption, so joining on newlines alone would make a multi-line
    caption indistinguishable from a chain separator. ffmpeg accepts the whitespace.
    """

    def __init__(self) -> None:
        self.chains: list[str] = []

    def add(self, chain: str) -> None:
        if chain:
            self.chains.append(chain)

    def text(self) -> str:
        return ";\n".join(self.chains)


def effective_cutmap(bundle: Bundle, total: int | None = None) -> CutMap:
    """The edit's cuts, plus `trim_head_frames` as a leading cut.

    A head trim is an excision like any other, so it goes through the same
    split/trim/concat machinery rather than an `-ss` that would renumber frame 0 and
    slide every cut and layer index authored against the untrimmed recording.

    `total` lets a caller supply the frame count it already has. The editor's bridge
    describes this same map to the UI and must not probe to do it -- a bundle whose
    media is missing has to answer "no timeline", not raise, or the editor cannot open
    it at all.
    """
    total = bundle.source_frames() if total is None else total
    ranges = list(bundle.edit.cuts)
    head = int(bundle.edit.trim_head_frames)
    if head > 0 and total > 0:
        # `total > 0` because FrameRange refuses an empty range: with no media the
        # count is 0, min(head, 0) is 0, and FrameRange(0, 0) raised straight out of
        # project_state -- the editor failing to OPEN a bundle whose media is missing.
        # The previous fix for this only covered trim 0, and every real recording has a
        # trim of 80-166 frames.
        ranges.append(FrameRange(0, min(head, total)))
    return CutMap(ranges, total,
                  head_pad=bundle.edit.head_pad_frames,
                  tail_pad=bundle.edit.tail_pad_frames)


def build_graph(bundle: Bundle, *, for_proxy: bool = False) -> RenderPlan:
    """Assemble the whole filtergraph and the argv around it.

    `for_proxy` builds the same composition as a fast 1080p draft -- short GOP, no B
    frames, veryfast. That is the edited result made scrubbable, and is a different
    thing from `proxy.ensure_proxy`, which proxies the untouched master for the preview.
    """
    capture = bundle.capture
    if capture.screen is None:
        raise ProjectError("bundle has no screen stream; nothing to render")
    canvas = bundle.canvas
    tb = bundle.timebase
    edit = bundle.edit
    cutmap = effective_cutmap(bundle)
    if cutmap.output_frames <= 0:
        raise RenderError("every frame is cut; there is nothing to render")

    g = _Graph()
    registry = layers_mod.InputRegistry()

    screen_path = bundle.media(Path(capture.screen.path).name)
    head_trim = 0.0
    if probe.has_discardable_warmup(screen_path):
        head_trim = tb.to_seconds(tb.to_frame(HEAD_TRIM_SECONDS))
        registry.add(["-ss", f"{head_trim:.6f}", "-i", str(screen_path)])
    else:
        registry.add(["-i", str(screen_path)])

    camera = capture.camera
    want_cam = camera is not None and not capture.camera_burned_in and edit.webcam.enabled
    cam_aligned: str | None = None
    if want_cam:
        assert camera is not None
        idx = registry.add(["-i", str(bundle.media(Path(camera.path).name))])
        # Resample to the project grid FIRST: `trim` counts frames on its input's own
        # grid, so a 30 fps camera cut against indices computed for a 60 fps screen
        # would remove the wrong material.
        g.add(layers_mod.timebase_chain(f"[{idx}:v]", tb, "[cam_tb]"))
        cam_aligned = _align_camera(
            g,
            "[cam_tb]",
            bundle.camera_offset_frames(),
            bundle.camera_warmup_frames(),
        )

    has_audio = bool(capture.screen.has_audio)

    # `fps` re-establishes the frame grid before anything indexes into it. gsr is passed
    # -fm cfr, but a dropped frame was still observed within seven seconds of capture,
    # and every gate downstream is a frame index.
    # The crop comes FIRST, before the frame grid and everything after it. A region
    # capture that needed a live self-view recorded the whole monitor through the portal
    # -- the only backend that can hide the bubble -- so the frame the user chose is a
    # rectangle inside that stream. Cropping here means every later stage (cuts, zoom,
    # cursor, layers, the canvas itself) sees exactly the frame they asked for, with no
    # second coordinate system anywhere.
    # When the edit asks to follow the recorded window, the crop's OFFSET becomes an
    # expression over t instead of a number. Same filter, same position in the graph,
    # same fixed output size -- so nothing downstream learns that the frame is moving,
    # which is the only reason panning does not have to be replumbed through zoom,
    # the cursor and every layer.
    fplan = follow.for_bundle(bundle)
    crop = capture.crop_rect()
    base_in = "[0:v]"
    if fplan is not None and not fplan.static:
        g.add(
            f"[0:v]crop={fplan.w}:{fplan.h}:"
            f"x='{fplan.x_expr()}':y='{fplan.y_expr()}'[cropped]"
        )
        base_in = "[cropped]"
    elif fplan is not None:
        x, y, w, h = fplan.rect()
        g.add(f"[0:v]crop={w}:{h}:{x}:{y}[cropped]")
        base_in = "[cropped]"
    elif crop is not None:
        x, y, w, h = crop
        g.add(f"[0:v]crop={w}:{h}:{x}:{y}[cropped]")
        base_in = "[cropped]"
    g.add(layers_mod.timebase_chain(base_in, tb, "[base]"))

    audio_label = "[0:a]"
    if has_audio:
        # Before the cut, deliberately: the pop belongs to the capture's start, not to
        # the edited timeline's. `afade` in is silent before `st`, so one filter both
        # mutes and ramps. If the user cuts the head away the fade goes with it, instead
        # of silencing the first 450 ms of whatever now begins the video.
        g.add(f"[0:a]afade=t=in:st={POP_MUTE_SECONDS}:d={POP_FADE_SECONDS}[apop]")
        audio_label = "[apop]"

    # Every time-varying input is cut, the camera above all: an uncut camera drifts by
    # exactly the total cut duration (-90 and -150 frames for 3 s and 5 s of cuts).
    cut_targets = ["[base]"] + ([cam_aligned] if cam_aligned else [])
    labels = cuts.cut_labels(cutmap, cut_targets, has_audio, audio_label=audio_label)
    g.add(cuts.cut_chain(cutmap, tb, cut_targets, has_audio, audio_label=audio_label))
    cur = labels["[base]"]
    if cam_aligned:
        # The camera is padded with the base, not just aligned to it. _pad_ends shifts
        # the BASE by head_pad, and the webcam overlay composites by timestamp -- so a
        # camera left unpadded showed frame head+k at output frame head+k, running the
        # bubble head_pad frames (plus every earlier cut) ahead of the picture, and
        # freezing on its last frame for the length of the pad. Measured at head=30:
        # the bubble was 29-30 frames early throughout.
        #
        # `add` rather than `clone`: these frames are never seen -- the webcam layer's
        # gate is remapped through the same cut map and so is dark for the whole pad --
        # and cloning a frame to hide it wastes the decode.
        cam_label = labels[cam_aligned]
        if cutmap.head_pad > 0 or cutmap.tail_pad > 0:
            parts = []
            if cutmap.head_pad > 0:
                parts.append(f"start={cutmap.head_pad}:start_mode=add")
            if cutmap.tail_pad > 0:
                parts.append(f"stop={cutmap.tail_pad}:stop_mode=add")
            g.add(f"{cam_label}tpad={':'.join(parts)}[cam_padded]")
            cam_label = "[cam_padded]"
        registry.bind("camera", cam_label)

    cur = _pad_ends(g, cur, cutmap, edit, tb)
    cur = _cursor(g, cur, bundle, cutmap, registry)

    zf = zoom_filter(_segments(bundle, cutmap), canvas, tb)
    if zf:
        g.add(f"{cur}{zf}[zoomed]")
        cur = "[zoomed]"

    layer_list = _layer_list(bundle, registry)

    # One camera, several segments: ffmpeg consumes a labelled pad exactly once, so N
    # webcam layers need N copies of it. Without the split the second segment silently
    # rendered the SCREEN inside its mask -- a bubble full of desktop, no error anywhere.
    cam_consumers = sum(1 for l in layer_list if l.type == "webcam")
    if cam_aligned and cam_consumers > 1:
        outs = [f"[cam_seg{i}]" for i in range(cam_consumers)]
        g.add(f"{labels[cam_aligned]}split={cam_consumers}{''.join(outs)}")
        registry.bind_fanout("camera", outs)

    if edit.backdrop.enabled:
        cur = _backdrop(g, cur, canvas, tb, edit.backdrop)
    elif layer_list:
        g.add(f"{cur}format=rgba[canvas]")
        cur = "[canvas]"

    for layer in layer_list:
        frag = layers_mod.compile_layer(
            layer, canvas, cutmap, tb, registry, label_in=cur
        )
        if frag is None:
            continue
        g.add(frag.filter_chain)
        cur = frag.label_out

    # The count above is a guess made before compiling, and compile_layer skips a layer
    # -- returning before it ever asks for a source -- when a cut removes its range
    # entirely or its rect rounds to nothing. Every label left over has to go somewhere:
    # ffmpeg rejects the ENTIRE graph over one unconnected pad, so a camera segment
    # sitting inside the recorded countdown took the whole export down with it. This
    # covers the single-segment case too, where there is no split and the cut camera
    # label itself would dangle.
    for orphan in registry.unconsumed("camera"):
        g.add(f"{orphan}nullsink")

    tail = "format=yuv420p"
    if for_proxy and canvas.width > 1920:
        tail = "scale=1920:-2:flags=bicubic," + tail
    elif not for_proxy:
        # The export size, last thing before the encoder, so every stage above it -- the
        # zoom especially -- composes at the master's full resolution and only the
        # finished frame is resampled. lanczos rather than bicubic: this is the one
        # downscale a viewer actually sees, and it is where text either stays crisp or
        # does not.
        out_h = export_height(bundle.edit.export_preset, canvas)
        if out_h is not None:
            tail = f"scale=-2:{out_h}:flags=lanczos," + tail
    g.add(f"{cur}{tail}[vout]")

    maps = ["-map", "[vout]"]
    if has_audio:
        aout = labels[audio_label]
        if cutmap.head_pad > 0 or cutmap.tail_pad > 0:
            # Silence for the pads, and the HEAD delay is what keeps the recording's
            # audio under its own picture: pad the video without delaying the audio and
            # every word lands `head_pad` frames early for the whole export.
            #
            # Nothing was recorded in a pad, so there is nothing else it could be. A
            # card you talk over is a different thing entirely -- an image covering the
            # first seconds of the real recording, where the audio is real.
            secs = tb.fps_den / tb.fps_num
            steps = []
            if cutmap.head_pad > 0:
                steps.append(f"adelay=all=1:delays={cutmap.head_pad * secs * 1000.0:.3f}")
            if cutmap.tail_pad > 0:
                steps.append(f"apad=pad_dur={cutmap.tail_pad * secs:.3f}")
            g.add(f"{aout}{','.join(steps)}[apadded]")
            aout = "[apadded]"
        if edit.declick_audio:
            # BEFORE loudnorm, deliberately: a key clack is the loudest thing in a
            # narration track, so normalising first would set the gain from the noise
            # and leave the voice quiet. Removing the impulses changes the right
            # answer, so it has to happen first.
            #
            # adeclick and not afftdn or arnndn, measured on speech plus broadband
            # clacks: samples above -9dBFS went 564 -> 1 with adeclick, while the
            # clean speech moved by -37dB (inaudible). afftdn removed no transients at
            # all, and RNNoise -- the obvious answer, and the one that needs a model
            # file -- left 9 and altered the voice MORE than it removed (+2dB).
            g.add(f"{aout}adeclick[adeclicked]")
            aout = "[adeclicked]"
        if edit.normalize_audio:
            # loudnorm AFTER the cut: it measures the material it is normalizing, and a
            # cut that removes a loud passage changes the right answer.
            g.add(f"{aout}loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
        else:
            g.add(f"{aout}anull[aout]")
        maps += ["-map", "[aout]"]

    return RenderPlan(
        graph=g.text(),
        inputs=registry.argv(),
        maps=maps,
        output_args=_output_args(cutmap.output_frames, has_audio, for_proxy=for_proxy),
        total_frames=cutmap.output_frames,
        canvas=canvas,
        head_trim_seconds=head_trim,
        input_specs=registry.inputs,
    )


def _pad_ends(g: _Graph, cur: str, cutmap: CutMap, edit, tb: Timebase) -> str:
    """Output-only frames at the head and tail, so a card has somewhere to live.

    Applied to the base straight after the cut chain and BEFORE the cursor, the zoom
    and the layers -- all of which are gated in output frames that already include the
    head offset, so padding first is what makes those gates land where they say.

    The ground is the backdrop's own colour, so a title card that does not fill the
    frame sits on the same colour as the recording does rather than on a black band
    nothing else in the project uses.
    """
    if cutmap.head_pad <= 0 and cutmap.tail_pad <= 0:
        return cur
    colour = backgrounds.pad_colour(edit.backdrop)
    parts = []
    if cutmap.head_pad > 0:
        parts.append(f"start={cutmap.head_pad}:start_mode=add")
    if cutmap.tail_pad > 0:
        parts.append(f"stop={cutmap.tail_pad}:stop_mode=add")
    g.add(f"{cur}tpad={':'.join(parts)}:color={colour}[padded]")
    return "[padded]"


def _align_camera(g: _Graph, label: str, offset: int, warmup: int = 0) -> str:
    """Put camera frame 0 on screen frame 0, in whole frames.

    Positive offset means the camera started later, so the head is padded by cloning its
    first frame -- which is what a viewer expects to see in the moment before the camera
    woke up. Negative means it started earlier, so the early frames are dropped.

    Both are exact on the project grid. `-itsoffset` is not: verified on this build, a
    +0.2 s offset through `fps=30` delays by exactly 6 frames but -0.2 s advances by 4.

    `warmup` is the sensor's auto-exposure ramp (Stream.warmup_frames, converted to
    screen frames by Bundle.camera_warmup_frames), and the frame it clones is the first
    frame PAST it. What the head holds is the only thing that changes: trim `warmup`,
    then pad by `offset + warmup` instead of by `offset`. After the trim the stream's
    frame 0 is the old frame W, and padding by offset+W puts it back at screen frame
    W+offset -- exactly where it started. Camera frame k lands at screen frame k+offset
    either way, so the lip sync this function exists to protect is untouched, and the
    head shows a settled face instead of the bubble fading up from black.

    Negative `offset + warmup` needs no warm-up branch of its own: the whole point of
    that case is that the camera's first frames belong before screen frame 0 and are
    dropped, and `offset + warmup <= 0` means -offset >= warmup, so the ramp is inside
    the frames going away regardless.
    """
    # Where the first SETTLED camera frame belongs, in screen frames.
    head = offset + warmup
    out = "[cam_aligned]"
    if head <= 0:
        if offset == 0:
            return label  # warmup is 0 here too; nothing to do, and no filter emitted
        g.add(f"{label}trim=start_frame={-offset},setpts=PTS-STARTPTS{out}")
        return out
    pre = f"trim=start_frame={warmup},setpts=PTS-STARTPTS," if warmup > 0 else ""
    g.add(f"{label}{pre}tpad=start={head}:start_mode=clone{out}")
    return out


def _segments(bundle: Bundle, cutmap: CutMap):
    """Zoom segments from the recorded click track, or none if it is unusable.

    A missing or malformed input.jsonl degrades to "no auto-zoom" rather than a failed
    export: the recording itself is still perfectly good video, and refusing to render
    it would be the worse failure.
    """
    settings = bundle.edit.zoom
    if not settings.enabled:
        return []
    from . import events

    path = bundle.events_dir / "input.jsonl"
    if not path.exists():
        return []
    try:
        clicks = events.map_clicks(
            events.read_clicks(path), bundle.capture, bundle.timebase
        )
    except events.EventsError:
        return []
    return zoom_segments(clicks, settings, bundle.timebase, cutmap)


# --- cursor ------------------------------------------------------------------

# The overlay's working format. NOT the yuv420 default: `overlay` in yuv420 snaps its
# x/y to EVEN pixels, because the chroma planes are half resolution and a chroma sample
# cannot land between two of them. Measured on this build with `x='n'` -- yuv420 walked
# 0,2,2,4,4,6 across six frames while yuv444 walked 0,1,2,3,4,5. Two-pixel steps on a
# slowly-moving pointer are exactly the staircase the smoothing above exists to remove,
# so the format is worth what it costs: `perspective` measured 8.28 ms/frame on yuv420p,
# 11.65 on yuv444p and 14.21 on rgba at 2560x1440, so the cursor buys whole-pixel
# positioning for 3.4 ms/frame and still undercuts doing the same overlay in rgba.
_CURSOR_OVERLAY_FORMAT = "yuv444"


def _cursor(
    g: _Graph,
    cur: str,
    bundle: Bundle,
    cutmap: CutMap,
    registry: layers_mod.InputRegistry,
) -> str:
    """Composite the synthetic pointer, or return the frame untouched.

    Every failure here degrades to "no cursor", never to a failed export, and the list of
    things that can fail is long: the bundle may predate the cursor track, cursor.bin may
    be truncated by a killed recorder, the capture may have no anchor, the cache
    directory may be unwritable. The recording is still perfectly good video in all of
    them, and refusing to render it would be the worse failure -- the same rule
    `_segments` follows for the auto-zoom.
    """
    settings = bundle.edit.cursor
    if not settings.enabled:
        return cur
    from . import cursor as cursor_mod, events

    try:
        path = bundle.events_dir / "cursor.bin"
        if not path.exists():
            return cur
        track = events.read_cursor_track(path)
        clicks_path = bundle.events_dir / "input.jsonl"
        clicks = events.read_clicks(clicks_path) if clicks_path.exists() else []
        plan = cursor_mod.build_plan(
            track, clicks, bundle.capture, bundle.timebase, cutmap, settings, bundle.canvas
        )
        if plan is None:
            return cur
        arrow, sheet = cursor_mod.write_sprites(plan, bundle.proxy_dir / "cursor")
    except (events.EventsError, cursor_mod.CursorError, OSError, ValueError):
        return cur

    tb = bundle.timebase
    # The ripple goes UNDER the pointer: the ring marks the point the tip is already on,
    # and drawing it over the arrow would put a translucent band across the glyph.
    if sheet is not None:
        # `-loop 1` at the PROJECT rate, because the tile selector below is a function of
        # the filmstrip stream's own frame counter. A still image would deliver one frame
        # and `crop` would evaluate its x exactly once, freezing the ripple on tile 0 --
        # the same once-at-init trap that made `crop` unusable for the zoom.
        idx = registry.add(
            ["-loop", "1", "-framerate", f"{tb.fps_num}/{tb.fps_den}", "-i", str(sheet)]
        )
        s = plan.ripple_px
        g.add(
            f"[{idx}:v]crop=w={s}:h={s}:"
            f"x='{cursor_mod.ripple_stage_expr(plan)}*{s}':y=0[cur_ripple]"
        )
        rx, ry = cursor_mod.ripple_position_exprs(plan)
        # shortest=1 because that input is infinite: without it the graph would run until
        # every input ends and the looped filmstrip never does.
        g.add(
            f"{cur}[cur_ripple]overlay=x='{rx}':y='{ry}'"
            f":shortest=1:format={_CURSOR_OVERLAY_FORMAT}[cur_rippled]"
        )
        cur = "[cur_rippled]"

    ax, ay = cursor_mod.overlay_position_exprs(plan)
    idx = registry.add(["-i", str(arrow)])
    # A single still frame, held by overlay's eof_action=repeat -- the same idiom
    # layers.py uses for a static image tile. Only the POSITION changes per frame, and
    # that is an expression, so there is nothing to loop.
    g.add(
        f"{cur}[{idx}:v]overlay=x='{ax}':y='{ay}'"
        f":format={_CURSOR_OVERLAY_FORMAT}[cursored]"
    )
    return "[cursored]"


def _layer_list(bundle: Bundle, registry: layers_mod.InputRegistry) -> list[Layer]:
    """Enabled layers in z order, with the webcam adapted from its settings.

    The webcam is stored as settings rather than a layer only because the editor gives
    it a dedicated panel; it composites like any other overlay. It is added only when
    the camera actually reached the graph, so a burned-in or disabled camera cannot
    leave a webcam layer asking the registry for a stream nobody bound.
    """
    layer_list = [_resolve_asset(l, bundle) for l in bundle.edit.layers if l.enabled]
    # Against ALL layers, not the enabled ones: a track whose only segment is switched
    # off is a camera the user turned off for that stretch, and testing the filtered
    # list read it as "no track at all" and appended the whole-take camera instead --
    # so disabling the one segment made the head play for the entire recording.
    has_webcam = any(l.type == "webcam" for l in bundle.edit.layers)
    if registry.has("camera") and not has_webcam:
        layer_list.append(layers_mod.webcam_layer(bundle.edit.webcam, bundle.canvas))
    elif has_webcam and not registry.has("camera"):
        layer_list = [l for l in layer_list if l.type != "webcam"]
    return sorted(layer_list, key=lambda l: l.z)


def _resolve_asset(layer: Layer, bundle: Bundle) -> Layer:
    """Turn an image layer's asset NAME into a path ffmpeg can open.

    The editor copies a dropped image into `assets/` and stores the bare filename, so
    the bundle stays portable across machines. layers.py is pure -- it has never heard
    of a Bundle -- and handed that filename straight to `-i`, which ffmpeg resolved
    against its own working directory: every image layer added through the editor died
    with "No such file or directory" at export. Resolved here, on a copy, because the
    Edit on disk must keep the portable name.
    """
    name = layer.props.get("asset")
    if layer.type != "image":
        return layer

    # The asset name is CONFINED to the bundle, and `path` is always recomputed from it
    # rather than trusted.
    #
    # Both halves were exploitable. `props["path"]` was honoured verbatim when present,
    # so a crafted edit.json could hand ffmpeg any path -- or any PROTOCOL, since no
    # -protocol_whitelist is set: "http://..." makes the export fetch a URL from the
    # victim's host, "/etc/shadow" reads whatever the user can read. And the asset name
    # was joined to assets_dir without normalising, so "../../../../etc/passwd"
    # resolved straight out of the bundle -- pathlib does not collapse "..".
    #
    # A layer whose asset escapes the bundle is dropped rather than repaired: there is
    # no correct file to substitute, and rendering something else under an attacker's
    # chosen name would be worse than rendering nothing.
    if not name:
        return replace(layer, props={**layer.props, "path": ""})
    assets = bundle.assets_dir.resolve()
    try:
        resolved = (assets / str(name)).resolve()
        resolved.relative_to(assets)
    except (OSError, ValueError):
        return replace(layer, props={**layer.props, "path": ""})
    return replace(layer, props={**layer.props, "path": str(resolved)})


# --- backdrop ---------------------------------------------------------------


def _hex(colour: str) -> str:
    return "0x" + colour.lstrip("#")


def _rounded_rect_mask(w: int, h: int, r: int) -> str:
    """Antialiased rounded-rect coverage, ~1px of edge softening.

    Built from a ONE-FRAME lavfi source so `geq` -- a per-pixel expression interpreter --
    runs once for the whole render rather than once per frame.
    """
    if r <= 0:
        return "255"
    dx = f"max(max({r}-X,X-({w - 1 - r})),0)"
    dy = f"max(max({r}-Y,Y-({h - 1 - r})),0)"
    return f"clip(255*({r}-hypot({dx},{dy})+0.5),0,255)"


def _ground(bg: backgrounds.Background, W: int, H: int, rate: str) -> str:
    """The infinite source that fills the whole frame behind the video.

    `speed=0` is not cosmetic. `gradients` turns its own line by `speed` radians every
    frame and defaults to 0.01, so the ground shipped as a slowly rotating gradient --
    at 30 fps a black-to-white diagonal's top-left pixel walked 0x00 -> 0x0d over 45
    frames. Motion in a still plate reads as an encoder artefact.

    The oversized frame and the `crop` are the price of an arbitrary angle, and of a
    gradient that is the same on every render: see `backgrounds.GradientPlan`.
    """
    if not bg.is_gradient:
        return f"color=c={_hex(bg.colors[0])}:s={W}x{H}:r={rate}"
    if not 2 <= len(bg.colors) <= backgrounds.MAX_STOPS:
        raise RenderError(
            f"background {bg.id!r} has {len(bg.colors)} stops; "
            f"gradients takes 2 to {backgrounds.MAX_STOPS}"
        )
    p = backgrounds.gradient_plan(bg.angle, W, H)
    stops = ":".join(f"c{i}={_hex(c)}" for i, c in enumerate(bg.colors))
    src = (
        f"gradients=s={p.width}x{p.height}:{stops}:nb_colors={len(bg.colors)}"
        f":x0={p.x0}:y0={p.y0}:x1={p.x1}:y1={p.y1}:speed=0:r={rate}"
    )
    if p.crop is not None:
        src += ",crop=" + ":".join(str(v) for v in p.crop)
    return src


def _backdrop(
    g: _Graph, cur: str, canvas: Canvas, tb: Timebase, bd: BackdropSettings
) -> str:
    """Inset the video on a coloured ground, with rounded corners and a drop shadow.

    The ground carries `r=` at the PROJECT rate, not lavfi's default 25. It is the main
    input of the final overlay, so its rate governs the output's: leaving it at 25
    silently resampled a 30 fps timeline and lost 8 frames of a 52-frame render, with no
    warning and a perfectly playable file at the end of it.
    """
    W, H = canvas.width, canvas.height
    rate = f"{tb.fps_num}/{tb.fps_den}"
    pad = int(round(bd.padding * min(W, H)))
    dw = ((W - 2 * pad) // 2) * 2
    dh = ((H - 2 * pad) // 2) * 2
    if dw < 2 or dh < 2:
        raise RenderError(f"backdrop padding {bd.padding} leaves no room for the video")
    px, py = (W - dw) // 2, (H - dh) // 2
    radius = int(round(bd.corner_radius * min(W, H)))

    g.add(_ground(backgrounds.resolve(bd, canvas), W, H, rate) + ",format=rgba[bgi]")

    g.add(f"{cur}scale={dw}:{dh}:flags=bicubic,format=rgba[inset]")
    g.add(
        f"color=c=black:s={dw}x{dh}:r=1:d=1,format=gray,"
        f"geq=lum='{_rounded_rect_mask(dw, dh, radius)}'[maskraw]"
    )
    g.add("[maskraw]split=2[mask_a][mask_b]")
    g.add("[inset][mask_a]alphamerge=repeatlast=1:shortest=0[rounded]")
    g.add(f"[rounded]pad={W}:{H}:{px}:{py}:color=black@0.0,format=rgba[content]")

    if bd.shadow:
        m = max(radius * 2, 48)
        g.add(
            f"[mask_b]pad={dw + 2 * m}:{dh + 2 * m}:{m}:{m}:color=black,"
            f"gblur=sigma={m / 3.0:.3f}:steps=3[shadow_a]"
        )
        g.add(f"color=c=black:s={dw + 2 * m}x{dh + 2 * m}:r=1:d=1,format=rgba[shadow_c]")
        g.add("[shadow_c][shadow_a]alphamerge,colorchannelmixer=aa=0.6[shadow]")
        g.add(
            f"[bgi][shadow]overlay=x={px - m}:y={py - m + m // 4}"
            ":eof_action=repeat:shortest=0:format=auto[bg]"
        )
    else:
        # The mask was split in two; the unused half needs a sink or the graph is
        # unconfigurable.
        g.add("[mask_b]nullsink")
        g.add("[bgi]null[bg]")

    # shortest=1 against the infinite backdrop: the real video defines both the length
    # and the timebase. The other way round produced a 208-second file from a 6.9 s clip.
    #
    # The content's PTS are normalised to a clean 0..N-1 sequence first. Concat (from a
    # cut) and tpad (from a pad) each leave the last frame's timing slightly off, and
    # with BOTH the overlay's shortest=1 decided the content had ended one frame early
    # -- a 65-frame render produced 64. Only that combination: backdrop+cut and
    # backdrop+pad were each fine, which is why it survived the pads going in.
    g.add("[content]setpts=N/FRAME_RATE/TB[content_cfr]")
    g.add("[bg][content_cfr]overlay=x=0:y=0:shortest=1:format=auto[composited]")
    return "[composited]"


# Heights for the names in project.EXPORT_PRESETS. `native` has no entry: it is the
# absence of a downscale.
EXPORT_HEIGHTS = {"1080p": 1080, "1440p": 1440, "4k": 2160}


def export_height(preset: str, canvas: Canvas) -> int | None:
    """The height to scale the finished frame to, or None to leave it at the canvas.

    Never UPSCALES. A preset is a ceiling, not a target: asking for 1440p from a 1080p
    capture has to give 1080p, because inventing pixels costs render time and file size
    to make the picture no better. An unknown name is native for the same reason
    Edit.from_dict falls back -- a bad string must not stop an export.
    """
    want = EXPORT_HEIGHTS.get(preset)
    if want is None or want >= canvas.height:
        return None
    return want


def _output_args(total_frames: int, has_audio: bool, *, for_proxy: bool) -> list[str]:
    args = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if for_proxy:
        # Short GOP is what makes the result seekable; the downscale is incidental.
        args += ["-preset", "veryfast", "-crf", "23", "-g", "15", "-bf", "0"]
    else:
        args += ["-preset", "medium", "-crf", "20", "-movflags", "+faststart"]
    # The output length is known exactly from the cut map, so state it rather than
    # trusting every stage of the graph to agree about where the stream ends.
    args += ["-frames:v", str(total_frames)]
    args += ["-c:a", "aac", "-b:a", "160k"] if has_audio else ["-an"]
    return args


# --- running ----------------------------------------------------------------


def render(
    bundle: Bundle,
    out_path: str | Path,
    *,
    progress: ProgressCallback | None = None,
    for_proxy: bool = False,
) -> Path:
    """Render the bundle to `out_path` and return it."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return run_plan(
        build_graph(bundle, for_proxy=for_proxy), out_path, progress=progress
    )


def run_plan(
    plan: RenderPlan, out_path: str | Path, *, progress: ProgressCallback | None = None
) -> Path:
    """Run a prepared plan, reporting progress from ffmpeg's own counter.

    `-progress pipe:1` emits key=value lines on stdout; stderr goes to a file rather
    than a second pipe, because draining only one of two pipes deadlocks as soon as the
    other fills, and ffmpeg is perfectly capable of filling it.
    """
    out_path = Path(out_path)
    workdir = Path(tempfile.mkdtemp(prefix="omarchy-studio-render-"))
    try:
        graph_path = workdir / "filtergraph.txt"
        graph_path.write_text(plan.graph + "\n")
        errlog = workdir / "ffmpeg.log"
        with errlog.open("wb") as err:
            proc = subprocess.Popen(
                plan.argv(graph_path, out_path),
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "frame" and progress is not None and value.strip().isdigit():
                    progress(min(int(value), plan.total_frames), plan.total_frames)
            rc = proc.wait()
        if rc != 0:
            raise RenderError(f"ffmpeg exited {rc}\n{errlog.read_text()[-4000:]}")
        if progress is not None:
            progress(plan.total_frames, plan.total_frames)
        return out_path
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
