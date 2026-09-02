"""The render driver: a Bundle in, one ffmpeg process out.

STAGE ORDER IS LOAD-BEARING. cut -> zoom -> backdrop -> layers, in that order:

* Cut FIRST, not last. Same project, 2.20 s against 2.83 s, because every stage
  downstream then runs over fewer frames. Layer gates are remapped to output time by
  `CutMap.remap`, and rendering cut-first with remapped ranges against cut-last with
  verbatim ranges gave bit-identical frames, so the reordering is provably free.
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

from . import cuts, layers as layers_mod, probe
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


def effective_cutmap(bundle: Bundle) -> CutMap:
    """The edit's cuts, plus `trim_head_frames` as a leading cut.

    A head trim is an excision like any other, so it goes through the same
    split/trim/concat machinery rather than an `-ss` that would renumber frame 0 and
    slide every cut and layer index authored against the untrimmed recording.
    """
    total = bundle.source_frames()
    ranges = list(bundle.edit.cuts)
    head = int(bundle.edit.trim_head_frames)
    if head > 0:
        ranges.append(FrameRange(0, min(head, total)))
    return CutMap(ranges, total)


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
        cam_aligned = _align_camera(g, "[cam_tb]", bundle.camera_offset_frames())

    has_audio = bool(capture.screen.has_audio)

    # `fps` re-establishes the frame grid before anything indexes into it. gsr is passed
    # -fm cfr, but a dropped frame was still observed within seven seconds of capture,
    # and every gate downstream is a frame index.
    g.add(layers_mod.timebase_chain("[0:v]", tb, "[base]"))

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
        registry.bind("camera", labels[cam_aligned])

    zf = zoom_filter(_segments(bundle, cutmap), canvas, tb)
    if zf:
        g.add(f"{cur}{zf}[zoomed]")
        cur = "[zoomed]"

    layer_list = _layer_list(bundle, registry)
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

    tail = "format=yuv420p"
    if for_proxy and canvas.width > 1920:
        tail = "scale=1920:-2:flags=bicubic," + tail
    g.add(f"{cur}{tail}[vout]")

    maps = ["-map", "[vout]"]
    if has_audio:
        aout = labels[audio_label]
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


def _align_camera(g: _Graph, label: str, offset: int) -> str:
    """Put camera frame 0 on screen frame 0, in whole frames.

    Positive offset means the camera started later, so the head is padded by cloning its
    first frame -- which is what a viewer expects to see in the moment before the camera
    woke up. Negative means it started earlier, so the early frames are dropped.

    Both are exact on the project grid. `-itsoffset` is not: verified on this build, a
    +0.2 s offset through `fps=30` delays by exactly 6 frames but -0.2 s advances by 4.
    """
    if offset == 0:
        return label
    out = "[cam_aligned]"
    if offset > 0:
        g.add(f"{label}tpad=start={offset}:start_mode=clone{out}")
    else:
        g.add(f"{label}trim=start_frame={-offset},setpts=PTS-STARTPTS{out}")
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


def _layer_list(bundle: Bundle, registry: layers_mod.InputRegistry) -> list[Layer]:
    """Enabled layers in z order, with the webcam adapted from its settings.

    The webcam is stored as settings rather than a layer only because the editor gives
    it a dedicated panel; it composites like any other overlay. It is added only when
    the camera actually reached the graph, so a burned-in or disabled camera cannot
    leave a webcam layer asking the registry for a stream nobody bound.
    """
    layer_list = [_resolve_asset(l, bundle) for l in bundle.edit.layers if l.enabled]
    has_webcam = any(l.type == "webcam" for l in layer_list)
    if registry.has("camera") and not has_webcam:
        layer_list.append(layers_mod.webcam_layer(bundle.edit.webcam))
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
    if layer.type != "image" or not name or layer.props.get("path"):
        return layer
    return replace(layer, props={**layer.props, "path": str(bundle.assets_dir / name)})


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

    if bd.gradient:
        g.add(
            f"gradients=s={W}x{H}:c0={_hex(bd.color)}:c1={_hex(bd.gradient)}"
            f":x0=0:y0=0:x1={W}:y1={H}:r={rate},format=rgba[bgi]"
        )
    else:
        g.add(f"color=c={_hex(bd.color)}:s={W}x{H}:r={rate},format=rgba[bgi]")

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
    g.add("[bg][content]overlay=x=0:y=0:shortest=1:format=auto[composited]")
    return "[composited]"


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
