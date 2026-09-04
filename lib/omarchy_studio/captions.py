"""Timed transcript segments -> one caption tile.

A `caption` layer is the one primitive again -- an RGBA tile overlaid at the layer's
placement -- but its content changes with time. Every segment is one `drawtext` on the
same tile, gated by its own `enable=`, so the whole transcript costs one overlay rather
than one layer per utterance. Forty separate text layers would each pay the ~0.7-1.0 ms
per 1080p frame `layers` measures; the transcript of a ten-minute demo is ~300 segments,
and that arrangement is unshippable before the text is even correct.

Three things about this module exist because of a specific failure:

* THE TILE MUST NOT BE STATIC. Every other tile producer is handed `static=True` when
  the layer has no fade, generating one frame that `overlay`'s eof_action=repeat holds
  for the whole render (0.129 s against 1.294 s, bit-identical). A caption tile built
  that way is drawn once at n=0, so whichever segment covers frame 0 is burned across
  the entire video and every later one never appears. `caption_tile` therefore always
  asks for a full-length source, and that is the price of the type.

* TIMES ARE REMAPPED, NOT COPIED. transcript.json is in SOURCE-timeline seconds
  (`transcribe` says so at length); `enable=` counts OUTPUT frames, because cuts are
  applied upstream by `cuts.cut_chain`. Every second of removed material before a
  segment slides it that much earlier in the export, and a caption track that is
  uniformly late is indistinguishable from a bad model until you diff frames.

* SEGMENTS ARE THE LAYER'S OWN COPY. `props["segments"]` is a verbatim copy of the rows
  from transcript.json, not a reference to the file. Editing the words in the timeline
  is the second thing anybody does with captions -- whisper mishears the product name --
  and a re-transcribe (which `transcribe_bundle --force` invites) would otherwise
  silently discard those edits. It also keeps edit.json self-describing: the project
  renders identically on a machine where the bundle's transcript.json was deleted.

Style props deliberately reuse `_tile_text`'s spelling (`font_px`, `color`, `fontfile`,
`box_color`) so one editor panel can drive both. `radius` is the one that does NOT carry
over, and the reason is in `caption_tile`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .exprs import escape_drawtext, frame_gate
from .geometry import Rect
# The tile helpers are borrowed rather than reimplemented: a second spelling of the
# colour source or the hex-colour form drifts from the one every other tile uses, and
# the drift is invisible until someone diffs rendered frames. `layers` imports this
# module lazily, at its dispatch site, so this direction of the cycle is the safe one.
from .layers import (DEFAULT_FONTFILE, _color_source, _hexcol, safe_color,
                     safe_fontfile, split_color)
from .project import Layer
from .timebase import CutMap, FrameRange, Timebase

CAPTION_TYPE = "caption"

# Measured on the shipped font at fontsize 100 by rendering a box-only drawtext and
# reading the white pixels back: exactly 60.00 px per character for 1, 2, 4 and 8
# characters -- JetBrains Mono is monospaced at 0.60 em. A proportional font is narrower
# than this on average, so wrapping against it breaks a line or two early rather than
# running off the tile, which is the safe direction to be wrong in. `wrap_chars`
# overrides for anyone who ships a different face.
CHAR_ADVANCE_EM = 0.60

# The same measurement for height: drawtext's multi-line box is 91 px for one line and
# 132 px per line after it at fontsize 100 (the font's hhea ascender+descender is
# 1.32 em). Both numbers are needed to pick a default font size that cannot overflow the
# plate -- `layers.font_px`'s 0.6*tile-height default would put two lines at 150% of the
# tile and the second one would be cropped off the bottom.
FIRST_LINE_EM = 0.91
EXTRA_LINE_EM = 1.32

DEFAULT_COLOR = "white"

# Measured, not taste. White glyphs on a 0.55-black plate composited over a WHITE frame
# -- the worst case a screen recording actually produces, a maximised editor in a light
# theme -- give 4.76:1 contrast, just clear of WCAG AA's 4.5:1. The same measurement at
# 0.50 is 3.98:1, and bare glyphs with no plate are 1:1 over white and simply vanish.
# Captions are burned in over arbitrary video, so the default has to survive the worst
# frame in the recording rather than look tasteful over the first one.
DEFAULT_BOX_COLOR = "#000000@0.55"

# Padding between the glyphs and the plate edge, in ems of the font size -- the "<= 1
# means normalized" convention `layers.radius_px` and `layers.block_px` already use, so
# the plate keeps its proportions when the same project is rendered against a 1440p
# master instead of a 1080p proxy.
DEFAULT_BOX_PAD = 0.3

# Two lines is where the BBC and Netflix subtitle guides both cap a cue, and it is what
# the default font size is solved against. A segment that wraps to more than this is
# SPLIT into successive cues rather than truncated: truncation silently deletes words
# somebody actually said, and the failure is invisible in the export -- the caption just
# reads as a shorter sentence. Splitting costs a caption that changes twice, which the
# viewer can at least see happening.
DEFAULT_MAX_LINES = 2

# Where a caption goes when nothing says otherwise: the lower third, inset from both
# edges. Full width would let a long line run under the webcam overlay, which sits at
# 0.83 by default.
DEFAULT_PLACEMENT = (0.1, 0.78, 0.8, 0.14)


@dataclass(frozen=True)
class CaptionStyle:
    """Resolved style for one caption tile. Every field is in output pixels by the time
    it gets here -- normalization against the tile happens once, in `style_for`."""

    font_px: int
    color: str
    fontfile: str
    box_color: str
    box_pad: int
    max_lines: int
    wrap_chars: int


@dataclass(frozen=True)
class Cue:
    """One drawn caption: the wrapped lines, and the SOURCE frame range it holds for.

    Source frames, like `Layer.t` and for the same reason -- adding or deleting a cut
    must not slide a caption off the sentence it transcribes. `caption_tile` is the only
    place the range is put on the output timeline.
    """

    lines: tuple[str, ...]
    span: FrameRange

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# -- props ------------------------------------------------------------------


def props_from_transcript(transcript, **style) -> dict:
    """Build a caption layer's props from a transcript.

    Accepts a `transcribe.Transcript`, the parsed dict, or a path to transcript.json --
    the editor has the object in hand, a CLI has the path, and neither should have to
    know which the other uses. A path goes through `Transcript.from_dict`, so a file
    written by a newer build raises there rather than rendering as an empty caption
    track.

    Any keyword is passed through as a style prop, so `props_from_transcript(t,
    font_px=48)` is the whole call the UI needs.
    """
    from .transcribe import Transcript  # optional-engine module; import is cheap

    if isinstance(transcript, (str, Path)):
        transcript = Transcript.from_dict(json.loads(Path(transcript).read_text()))
    elif isinstance(transcript, dict):
        transcript = Transcript.from_dict(transcript)

    props = {
        # Verbatim rows, in transcript.json's own shape, so the two are diffable and a
        # UI can splice one list into the other without a conversion step.
        "segments": [
            {"start": float(s.start), "end": float(s.end), "text": str(s.text)}
            for s in transcript.segments
        ],
    }
    props.update(style)
    return props


def caption_layer(transcript, *, id: str = "captions", z: int = 200, **style) -> Layer:
    """A ready-to-render caption layer, the way `layers.webcam_layer` adapts the webcam.

    z=200 puts it above the webcam's default 100: a caption hidden behind the talking
    head is the one arrangement of the two that is never wanted.
    """
    x, y, w, h = DEFAULT_PLACEMENT
    return Layer(
        id=id,
        type=CAPTION_TYPE,
        x=x,
        y=y,
        w=w,
        h=h,
        z=z,
        props=props_from_transcript(transcript, **style),
    )


# -- style ------------------------------------------------------------------


def style_for(props: dict, rect: Rect) -> CaptionStyle:
    """Resolve the style props against the tile.

    The font size is solved so that `max_lines` lines PLUS their padding fit the tile
    height exactly, because the tile is the only thing that bounds a caption: drawtext
    will happily render past the edge of its frame and the words are simply gone.
    """
    max_lines = max(1, int(props.get("max_lines") or DEFAULT_MAX_LINES))
    block_em = FIRST_LINE_EM + EXTRA_LINE_EM * (max_lines - 1)
    pad = float(props.get("box_pad", DEFAULT_BOX_PAD))

    size = int(props.get("font_px") or 0)
    if size > 0:
        box_pad = int(round(pad * size if pad <= 1.0 else pad))
    elif pad <= 1.0:
        # Padding scales with the size we are still solving for, so it goes into the
        # denominator rather than being subtracted from the height first.
        size = max(8, int(rect.h / (block_em + 2 * pad)))
        box_pad = int(round(pad * size))
    else:
        box_pad = int(round(pad))
        size = max(8, int((rect.h - 2 * box_pad) / block_em))

    wrap = int(props.get("wrap_chars") or 0)
    if wrap <= 0:
        wrap = max(1, int((rect.w - 2 * box_pad) / (size * CHAR_ADVANCE_EM)))

    return CaptionStyle(
        font_px=size,
        color=_hexcol(str(props.get("color", DEFAULT_COLOR))),
        # Same treatment as a text layer: captions reuse the identical prop spelling,
        # so they inherited the identical filter-injection hole.
        fontfile=safe_fontfile(props.get("fontfile")),
        box_color=str(props.get("box_color", DEFAULT_BOX_COLOR)),
        box_pad=max(0, box_pad),
        max_lines=max_lines,
        wrap_chars=wrap,
    )


# -- segments -> cues -------------------------------------------------------


def wrap_lines(text: str, chars: int) -> list[str]:
    """Greedy word wrap at `chars` columns.

    drawtext has no wrapping of its own -- it draws exactly the string it is given, off
    the end of the frame if that is where the string goes -- so the break has to be
    decided here, before the graph is written.

    A single word longer than the line is hard-broken rather than allowed to overhang: a
    URL or a stack-trace-shaped noun is a real thing to say out loud, and one of them
    pushing the plate past the tile edge would clip every line of that cue.
    """
    chars = max(1, int(chars))
    lines: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > chars:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:chars])
            word = word[chars:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= chars:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _rows(props: dict) -> list[tuple[float, float, str]]:
    """(start, end, text) triples from props, with the unrenderable ones dropped.

    Blank and whitespace-only text goes first: `transcribe.normalize_segments` already
    strips those out of a machine-written transcript, but props are hand-editable JSON
    and clearing a caption's text in the editor is how a user says "not this one". An
    empty drawtext still draws its plate, so a blank segment would leave a bare box
    hanging on screen.

    A row with unparseable times is dropped rather than raised on, for the same reason
    `transcribe.load` swallows a corrupt file: the other three hundred captions are
    still correct, and taking the whole export down over one row helps nobody.
    """
    out: list[tuple[float, float, str]] = []
    for row in props.get("segments") or []:
        try:
            start = float(row["start"])
            end = float(row["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = " ".join(str(row.get("text", "")).split())
        if not text:
            continue
        out.append((max(0.0, start), max(0.0, end), text))
    return out


def _spans(props: dict, tb: Timebase) -> list[tuple[FrameRange, str]]:
    """Segments as source frame ranges, sorted, non-overlapping, later wins.

    Two segments that overlap after snapping are drawn as two drawtexts on the same
    tile, and drawtext composites -- the second sentence lands ON TOP of the first,
    glyphs crossing glyphs, which is unreadable in a way a missing caption is not. The
    later one wins because that is what a viewer expects: speech moves forward, and the
    words on screen should be the words being said now.
    """
    rows = sorted(
        (
            (tb.to_frame(start), tb.to_frame(end), text)
            for start, end, text in _rows(props)
        ),
        key=lambda r: (r[0], r[1]),
    )

    out: list[tuple[FrameRange, str]] = []
    for start, end, text in rows:
        # A segment shorter than one frame still gets one: whisper emits sub-frame
        # utterances ("Mm.") and rounding one away deletes a word from the export.
        end = max(end, start + 1)
        while out and out[-1][0].end > start:
            prev_span, prev_text = out.pop()
            if start - prev_span.start >= 1:
                out.append((FrameRange(prev_span.start, start), prev_text))
                break
            # Nothing left of the earlier segment once the later one takes its ground;
            # it is dropped, and the loop checks the one before it.
        out.append((FrameRange(start, end), text))
    return out


def caption_cues(props: dict, style: CaptionStyle, tb: Timebase) -> list[Cue]:
    """Every cue this caption layer draws, in source frames.

    A segment that wraps past `max_lines` becomes several cues sharing its interval,
    split in proportion to their character counts so the reading rate stays roughly
    constant across the split rather than flashing the tail of the sentence.
    """
    out: list[Cue] = []
    for span, text in _spans(props, tb):
        out.extend(_chunk(span, wrap_lines(text, style.wrap_chars), style.max_lines))
    return out


def _chunk(span: FrameRange, lines: list[str], max_lines: int) -> list[Cue]:
    groups = [lines[i : i + max_lines] for i in range(0, len(lines), max_lines)]
    if not groups:
        return []
    if len(groups) == 1:
        return [Cue(tuple(groups[0]), span)]

    # One frame per cue is the floor; a segment too short to give every chunk a frame
    # loses its tail, which is already what happened to the speech.
    groups = groups[: len(span)]
    weights = [sum(len(line) for line in g) or 1 for g in groups]
    total = sum(weights)

    out: list[Cue] = []
    cursor = span.start
    for i, group in enumerate(groups):
        if i == len(groups) - 1:
            end = span.end
        else:
            share = sum(weights[: i + 1]) / total
            end = span.start + round(len(span) * share)
            # Leave a frame for each cue still to come, or FrameRange rejects it.
            end = max(cursor + 1, min(end, span.end - (len(groups) - i - 1)))
        out.append(Cue(tuple(group), FrameRange(cursor, end)))
        cursor = end
    return out


# -- the tile ---------------------------------------------------------------


def caption_tile(
    layer: Layer, name: str, rect: Rect, cutmap: CutMap, tb: Timebase
) -> tuple[list[str], str]:
    """The chains for a caption tile, and the label they end on.

    Same contract as `layers._tile_text`, minus the `static` flag: see the module
    docstring for why a caption tile can never be a one-frame source.

    The plate is drawtext's OWN box, not the tile-wide rounded rectangle `_tile_text`
    alphamerges in. That is why `radius` is absent from the schema, and it is a trade
    rather than an oversight: drawtext's box is drawn per cue, so it appears and
    disappears with the words, where a tile-wide plate is baked into the tile's alpha
    plane and would hang on screen through every silence in the recording -- a black bar
    over the video for the 40% of a demo where nobody is talking. Making that plate
    time-varying means a `geq` with a frame term, which is a per-pixel interpreter
    running once per frame; every mask in `layers` is built from a 1-frame source
    specifically to avoid that. drawtext's box has square corners. That is the cost.

    It also means the box alpha needs no folding into a mask the way `_tile_text` has
    to: `boxcolor` is composited by drawtext, not merged over the alpha plane, so
    "#000000@0.55" arrives as 55% black and stays there.
    """
    w, h = int(rect.w), int(rect.h)
    style = style_for(layer.props, rect)
    _rgb, box_alpha = split_color(style.box_color)

    # Transparent, full length. `static=False` is the whole point -- the per-cue
    # `enable=` counts frames off this source, so it has to produce them.
    chains = [_color_source("black@0.0", w, h, cutmap, tb, f"[{name}_r]", False)]

    draws: list[str] = []
    for cue in caption_cues(layer.props, style, tb):
        ranges = cutmap.remap(cue.span)
        if not ranges:
            continue  # the whole cue sits inside a cut
        box = (
            f":box=1:boxcolor={_hexcol(style.box_color)}:boxborderw={style.box_pad}"
            if box_alpha > 0.0
            else ""
        )
        draws.append(
            f"drawtext=fontfile={style.fontfile}"
            f":text='{escape_drawtext(cue.text)}'"
            f":fontsize={style.font_px}:fontcolor={safe_color(style.color)}{box}"
            # text_align centres each wrapped line within the block; x/y then centre the
            # block in the tile, which is `_tile_text`'s expression unchanged, so a
            # one-line caption lands on the same pixel a text layer of the same string
            # would. Without text_align the second line is left-aligned under the first
            # and the cue reads as ragged.
            f":text_align=C:x=(w-text_w)/2:y=(h-text_h)/2"
            f":enable='{frame_gate(ranges)}'"
        )

    if not draws:
        # A caption layer over a silent stretch, or one whose every cue was cut away.
        # The transparent tile still composites to a no-op, which is the honest result:
        # the layer exists, it simply has nothing to say here.
        return chains, f"[{name}_r]"

    # One chain, comma-joined. Commas inside `text='...'` are safe -- the graph parser
    # honours the quotes at the level that splits filters, which is why `escape_drawtext`
    # escapes ':' and not ',' -- verified by round-tripping the escaped form against
    # `textfile=` with `expansion=none` for strings containing commas, colons,
    # apostrophes, percent signs and backslashes.
    chains.append(f"[{name}_r]{','.join(draws)}[{name}_s]")
    return chains, f"[{name}_s]"
