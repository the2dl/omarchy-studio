"""The single geometry seam.

Every placement in the system -- the zoom viewport, the webcam box, each overlay layer
-- is defined once here and emitted twice: as QML scene-graph values for the live
preview, and as ffmpeg filter arguments for the export. Two implementations of the same
placement will drift, and the drift is invisible until someone compares rendered frames,
so both consumers derive from one function and a CI check compares silhouettes at 2px.

Conventions, all measured rather than assumed:

* Canvas coordinates are output pixels, origin top-left, y down. Both engines agree.
* QML transforms use `transformOrigin: Item.TopLeft`. Any other origin makes the
  translation depend on the item's size, which is exactly the bug that produces a zoom
  that scales but never pans.
* Text is anchored by its CENTRE. Centre-anchored text matched to 0.0px; left-anchored
  drifted ~7% of string width, because glyph advances differ between Qt's shaper and
  libavfilter's drawtext even with the same font file.
* Blur is calibrated, not shared: Qt's MultiEffect blur and ffmpeg's gblur are different
  kernels. The preview is biased *stronger* than the export -- a redaction that looks
  sufficient in the preview and renders weaker is a data leak.
"""

from __future__ import annotations

from dataclasses import dataclass

# Qt MultiEffect blur=1.0 at blurMax=48 lines up with roughly this gblur sigma.
# Measured by matching perceived radius on a redaction box at 1080p.
_QT_BLURMAX_TO_GBLUR_SIGMA = 18.0 / 48.0

# The preview leans this much heavier than the export. Never below 1.0.
_PREVIEW_BLUR_BIAS = 1.15


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _fmt(v: float) -> str:
    """Fixed-point for filter strings. ffmpeg parses these as doubles; six places is
    well under a pixel at any resolution we render and keeps graphs diffable."""
    return f"{v:.6f}".rstrip("0").rstrip(".") or "0"


@dataclass(frozen=True)
class Canvas:
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"degenerate canvas {self.width}x{self.height}")


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in canvas pixels."""

    x: float
    y: float
    w: float
    h: float

    def clamped_to(self, canvas: Canvas) -> "Rect":
        w = min(self.w, canvas.width)
        h = min(self.h, canvas.height)
        return Rect(
            _clamp(self.x, 0, canvas.width - w),
            _clamp(self.y, 0, canvas.height - h),
            w,
            h,
        )

    def to_even(self) -> "Rect":
        """Snap to even integers. yuv420 chroma subsampling requires even dimensions,
        and ffmpeg's crop silently floors odd values, which desyncs preview from export."""
        x, y = int(round(self.x)), int(round(self.y))
        w, h = int(round(self.w)), int(round(self.h))
        return Rect(x - (x & 1), y - (y & 1), w - (w & 1), h - (h & 1))


@dataclass(frozen=True)
class Zoom:
    """A zoom viewport: magnify by `scale` about a normalized focal point.

    `cx`/`cy` are 0..1 across the canvas -- click coordinates arrive normalized so the
    same project renders correctly at any export resolution. scale 1.0 is no zoom.
    """

    scale: float = 1.0
    cx: float = 0.5
    cy: float = 0.5

    def __post_init__(self) -> None:
        if self.scale < 1.0:
            raise ValueError(f"zoom scale {self.scale} would shrink the frame")

    @property
    def identity(self) -> bool:
        return self.scale <= 1.0 + 1e-9

    def viewport(self, canvas: Canvas) -> Rect:
        """The source rectangle that fills the output, clamped inside the canvas.

        Clamping is why a click near an edge does not pan past it -- a click 220px from
        the top of a 1440px frame lands at 0.39 of frame height rather than centred at
        0.5, which is correct behaviour and a common source of "the zoom is wrong" bugs.
        """
        w = canvas.width / self.scale
        h = canvas.height / self.scale
        return Rect(
            self.cx * canvas.width - w / 2.0,
            self.cy * canvas.height - h / 2.0,
            w,
            h,
        ).clamped_to(canvas)

    # -- the two emissions ----------------------------------------------------

    def to_qml(self, canvas: Canvas) -> dict:
        """Scene-graph values for an Item with transformOrigin: Item.TopLeft.

        With the origin at top-left a source point p maps to p*scale, so putting the
        viewport's origin at the item's (0,0) is a translation by -origin*scale.
        """
        vp = self.viewport(canvas)
        return {
            "scale": self.scale,
            "x": -vp.x * self.scale,
            "y": -vp.y * self.scale,
            "transformOrigin": "TopLeft",
        }

    def to_ffmpeg_crop(self, canvas: Canvas) -> str:
        """A `crop` filter isolating the viewport, ready to be scaled back up.

        crop+scale rather than zoompan: zoompan truncates its crop origin to integer
        input pixels, which puts 1.30px RMS / 2.75px peak jitter on slow pans, and it
        accepts no runtime parameters at all.
        """
        vp = self.viewport(canvas).to_even()
        return f"crop=w={int(vp.w)}:h={int(vp.h)}:x={int(vp.x)}:y={int(vp.y)}"


@dataclass(frozen=True)
class Placement:
    """Where a layer sits on the canvas, in normalized 0..1 coordinates.

    Normalized so a project authored against a 1080p proxy renders identically against
    a 1440p master -- the preview and the export use different source resolutions by
    design, and pixel coordinates would silently scale wrong between them.
    """

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    anchor: str = "top-left"  # or "center"

    def resolve(self, canvas: Canvas) -> Rect:
        w = self.w * canvas.width
        h = self.h * canvas.height
        x = self.x * canvas.width
        y = self.y * canvas.height
        if self.anchor == "center":
            x -= w / 2.0
            y -= h / 2.0
        elif self.anchor != "top-left":
            raise ValueError(f"unknown anchor {self.anchor!r}")
        return Rect(x, y, w, h)

    def to_qml(self, canvas: Canvas) -> dict:
        """Explicit x/y/width/height.

        Never `anchors.fill` on a transformed item: anchors silently override explicit
        x/y bindings with no warning, producing an overlay that ignores its placement.
        """
        r = self.resolve(canvas)
        return {"x": r.x, "y": r.y, "width": r.w, "height": r.h}

    def to_ffmpeg_overlay(self, canvas: Canvas) -> tuple[str, str]:
        """(scale_args, overlay_xy) for `scale=<args>` feeding `overlay=<xy>`."""
        r = self.resolve(canvas).to_even()
        return f"w={int(r.w)}:h={int(r.h)}", f"x={int(r.x)}:y={int(r.y)}"


def blur_sigma(strength: float, *, for_preview: bool) -> float:
    """Map a 0..1 redaction strength onto each engine's blur parameter.

    The preview is deliberately biased stronger. A user who drags a blur box until the
    password is unreadable in the preview must not get a weaker blur in the export.
    """
    if not 0.0 <= strength <= 1.0:
        raise ValueError(f"blur strength {strength} outside 0..1")
    sigma = strength * 48.0 * _QT_BLURMAX_TO_GBLUR_SIGMA
    return sigma * _PREVIEW_BLUR_BIAS if for_preview else sigma


def qml_blur(strength: float) -> dict:
    """MultiEffect properties. blurMax is fixed so `blur` alone carries the strength."""
    return {"blurEnabled": True, "blurMax": 48, "blur": _clamp(strength * _PREVIEW_BLUR_BIAS, 0.0, 1.0)}


def ffmpeg_blur(strength: float) -> str:
    """gblur, not boxblur -- a Gaussian is the same kernel family as Qt's, so the two
    stay comparable as strength varies. boxblur diverges badly at high radius."""
    return f"gblur=sigma={_fmt(blur_sigma(strength, for_preview=False))}:steps=3"


def text_placement(text: str, place: Placement, canvas: Canvas, font_px: int) -> dict:
    """Both engines' arguments for one centre-anchored text run.

    `fontfile` is passed explicitly and the same file is named in QML: relying on
    fontconfig to resolve a family name gives the two engines different faces, and the
    metrics diverge before anyone notices the glyphs did.
    """
    r = place.resolve(canvas)
    cx, cy = r.x + r.w / 2.0, r.y + r.h / 2.0
    return {
        "qml": {
            "x": cx,
            "y": cy,
            "horizontalAlignment": "AlignHCenter",
            "verticalAlignment": "AlignVCenter",
            "pixelSize": font_px,
            "anchorMode": "center",
        },
        # drawtext positions by the text box's top-left, so centre it explicitly with
        # the text_w/text_h it computes at render time.
        "ffmpeg_xy": f"x={_fmt(cx)}-text_w/2:y={_fmt(cy)}-text_h/2",
        "font_px": font_px,
        "text": text,
    }
