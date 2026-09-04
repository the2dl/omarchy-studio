"""The caption layer: a transcript's timed strings burned into the export.

The failure this type will actually hit is drift -- a caption is right on a project with
no cuts and a second late on one with them -- so the cut-remapping tests are the centre
of this module, not an afterthought. The second is escaping: transcript text is speech,
and speech is full of apostrophes and percent signs that drawtext reads as syntax.
"""

from __future__ import annotations

import json

import pytest
from ffmpeg_harness import FONTFILE, framehashes, needs_ffmpeg, needs_font

from omarchy_studio import captions
from omarchy_studio.captions import (
    CAPTION_TYPE,
    DEFAULT_BOX_COLOR,
    DEFAULT_MAX_LINES,
    caption_cues,
    caption_layer,
    props_from_transcript,
    style_for,
    wrap_lines,
)
from omarchy_studio.geometry import Canvas
from omarchy_studio.layers import DEFAULT_FONTFILE, InputRegistry, compile_layer
from omarchy_studio.project import Layer
from omarchy_studio.timebase import CutMap, FrameRange, Timebase
from omarchy_studio.transcribe import TranscribeError

CANVAS = Canvas(320, 240)
TB = Timebase(30)
TOTAL = 60  # two seconds, so a segment can sit either side of a cut


def cutmap(cuts=()) -> CutMap:
    return CutMap([FrameRange(*c) for c in cuts], TOTAL)


def segs(*rows) -> list[dict]:
    return [{"start": s, "end": e, "text": t} for s, e, t in rows]


def caption(segments, **props) -> Layer:
    """A full-width caption band, 60 px tall on the test canvas."""
    return Layer(
        id="cap", type=CAPTION_TYPE, x=0.0, y=0.75, w=1.0, h=0.25,
        props={"segments": segments, **props},
    )


def compile_one(layer: Layer, *, cuts=(), label_in="[base]"):
    return compile_layer(
        layer, CANVAS, cutmap(cuts), TB, InputRegistry(), label_in=label_in
    )


def rect_of(layer: Layer):
    return layer.placement.resolve(CANVAS).to_even()


def cues_of(layer: Layer):
    style = style_for(layer.props, rect_of(layer))
    return caption_cues(layer.props, style, TB)


def drawtexts(chain: str) -> list[str]:
    """The drawtext fragments of a compiled caption, in graph order."""
    return [("drawtext=" + p).split("[")[0] for p in chain.split("drawtext=")[1:]]


# -- it is still the one primitive -------------------------------------------


def test_a_caption_ends_in_the_same_overlay_as_every_other_layer():
    f = compile_one(caption(segs((0.0, 1.0, "hello"))))
    assert "overlay=x=0:y=180:enable='gte(n,0)*lt(n,60)'" in f.filter_chain
    assert ":eof_action=repeat:shortest=0:format=auto" in f.filter_chain
    assert f.label_in == "[base]" and f.label_out.endswith("_o]")


def test_the_tile_is_full_length_never_a_single_frame():
    """Every other tile with no fade is generated ONCE and held by eof_action=repeat.
    A caption tile built that way is drawn at n=0 only, so whichever segment covers the
    first frame is burned across the whole video and no other one ever appears."""
    chain = compile_one(caption(segs((1.0, 2.0, "later")))).filter_chain
    assert ":r=1:d=1," not in chain
    assert "color=c=black@0.0:s=320x60:r=30/1:d=2.000000," in chain


def test_text_is_centred_in_the_tile_like_a_text_layer():
    chain = compile_one(caption(segs((0.0, 1.0, "hi")))).filter_chain
    assert "x=(w-text_w)/2:y=(h-text_h)/2" in chain
    # text_align centres each wrapped line inside the block; without it the second line
    # is left-aligned under the first and the cue reads ragged.
    assert "text_align=C" in chain


def test_the_shipped_font_file_is_the_default():
    chain = compile_one(caption(segs((0.0, 1.0, "hi")))).filter_chain
    assert f"fontfile={DEFAULT_FONTFILE}" in chain


# -- one tile, many timed strings --------------------------------------------


def test_each_segment_becomes_its_own_gated_drawtext():
    chain = compile_one(
        caption(segs((0.0, 0.5, "first"), (1.0, 1.5, "second")))
    ).filter_chain
    draws = drawtexts(chain)
    assert len(draws) == 2
    assert "enable='gte(n,0)*lt(n,15)'" in draws[0]
    assert "enable='gte(n,30)*lt(n,45)'" in draws[1]
    # One overlay for the whole transcript, not one per utterance.
    assert chain.count("overlay=") == 1


def test_the_gate_is_on_the_frame_index_never_on_time():
    chain = compile_one(caption(segs((0.5, 1.0, "hi")))).filter_chain
    assert "gte(n," in chain
    assert "between(" not in chain and "gte(t," not in chain


def test_a_sub_frame_segment_still_gets_one_frame():
    """whisper emits utterances shorter than a frame ("Mm."); rounding one away deletes
    a word from the export."""
    (cue,) = cues_of(caption(segs((1.00, 1.01, "Mm"))))
    assert cue.span == FrameRange(30, 31)


# -- cuts: the drift this feature will actually hit ---------------------------


def test_a_caption_after_a_cut_lands_at_the_remapped_time():
    """Source seconds are NOT output seconds. Ten frames removed before a segment slide
    it ten frames earlier in the export, and a caption track that is uniformly late is
    indistinguishable from a bad model until someone diffs frames."""
    chain = compile_one(
        caption(segs((1.0, 1.5, "after the cut"))), cuts=[(6, 16)]
    ).filter_chain
    # Source [30,45) with 10 frames removed before it becomes output [20,35).
    assert "enable='gte(n,20)*lt(n,35)'" in chain


def test_a_caption_inside_a_cut_is_not_drawn():
    chain = compile_one(
        caption(segs((0.4, 0.5, "removed"), (1.0, 1.2, "kept"))), cuts=[(10, 20)]
    ).filter_chain
    assert "removed" not in chain
    assert len(drawtexts(chain)) == 1


def test_a_caption_straddling_a_cut_gets_one_merged_gate():
    """`CutMap.remap` merges the pieces, which is what keeps the generated gate small
    enough to stay inside libavutil's 100-term expression budget."""
    chain = compile_one(
        caption(segs((0.0, 2.0, "spans it"))), cuts=[(10, 20)]
    ).filter_chain
    assert "enable='gte(n,0)*lt(n,50)'" in chain


def test_a_transcript_that_is_entirely_cut_leaves_an_empty_tile():
    f = compile_one(caption(segs((0.4, 0.6, "gone"))), cuts=[(10, 20)])
    assert "drawtext" not in f.filter_chain
    # The layer still exists and still composites; it simply has nothing to say.
    assert "overlay=" in f.filter_chain


# -- overlap ------------------------------------------------------------------


def test_overlapping_segments_the_later_one_wins():
    """Two drawtexts on one tile composite, so an overlap is glyphs crossing glyphs --
    unreadable in a way a missing caption is not."""
    cues = cues_of(caption(segs((0.0, 1.2, "first"), (1.0, 1.5, "second"))))
    assert [c.text for c in cues] == ["first", "second"]
    assert cues[0].span == FrameRange(0, 30)
    assert cues[1].span == FrameRange(30, 45)


def test_a_segment_swallowed_by_the_next_is_dropped_entirely():
    cues = cues_of(caption(segs((1.0, 1.4, "swallowed"), (1.0, 1.5, "winner"))))
    assert [c.text for c in cues] == ["winner"]


def test_an_out_of_order_transcript_is_sorted_before_it_is_resolved():
    """whisper.cpp's -oj output is not guaranteed monotonic across chunk boundaries."""
    cues = cues_of(caption(segs((1.0, 1.5, "second"), (0.0, 0.5, "first"))))
    assert [c.text for c in cues] == ["first", "second"]


def test_no_two_cues_overlap_after_resolution():
    cues = cues_of(
        caption(segs((0.0, 1.0, "a"), (0.5, 1.2, "b"), (0.6, 2.0, "c")))
    )
    for earlier, later in zip(cues, cues[1:]):
        assert earlier.span.end <= later.span.start


# -- escaping: the sharp edge -------------------------------------------------


NASTY = "It's 50% off: yes, really -- C:\\Users\\dan 100%"


def test_speech_is_escaped_the_way_a_text_layer_is():
    chain = compile_one(caption(segs((0.0, 1.0, NASTY)))).filter_chain
    # The apostrophe closes the quote, emits an escaped quote and reopens; the percent
    # sign would otherwise abort the graph, and the colon is eaten by the option
    # splitter even inside quotes.
    assert "It'\\\\\\''s" in chain
    assert "50\\\\%" in chain
    assert "off\\:" in chain


def test_a_comma_in_speech_does_not_end_the_filter():
    """Filters in a chain are comma-separated, and the caption tile is one chain of
    many drawtexts. The graph parser honours the quotes at that level -- which is why
    `escape_drawtext` escapes ':' and not ',' -- and the ffmpeg round-trip below is what
    actually proves it."""
    chain = compile_one(
        caption(segs((0.0, 0.5, "one, two"), (0.6, 1.0, "three")))
    ).filter_chain
    assert len(drawtexts(chain)) == 2


def test_blank_and_whitespace_segments_are_dropped():
    """An empty drawtext still draws its plate, so a blank segment would leave a bare
    box hanging over the video. Clearing the text is how the editor says "not this
    one"."""
    cues = cues_of(caption(segs((0.0, 0.5, ""), (0.6, 1.0, "   \t "), (1.1, 1.5, "ok"))))
    assert [c.text for c in cues] == ["ok"]


def test_a_malformed_row_is_dropped_rather_than_taking_the_export_down():
    cues = cues_of(
        caption([{"start": "soon", "end": 1.0, "text": "bad"}, *segs((1.0, 1.5, "good"))])
    )
    assert [c.text for c in cues] == ["good"]


# -- wrapping and splitting ---------------------------------------------------


def test_a_long_line_wraps_inside_the_tile():
    layer = caption(segs((0.0, 2.0, "the quick brown fox jumps over the lazy dog")))
    style = style_for(layer.props, rect_of(layer))
    (cue,) = cues_of(layer)
    assert len(cue.lines) > 1
    assert all(len(line) <= style.wrap_chars for line in cue.lines)
    # Nothing is lost to the break.
    assert " ".join(cue.lines) == "the quick brown fox jumps over the lazy dog"


def test_a_word_longer_than_the_line_is_hard_broken():
    """A URL is a real thing to say out loud, and one of them overhanging the tile
    clips every line of that cue, not just its own."""
    assert wrap_lines("https://example.com/a/very/long/path", 10) == [
        "https://ex", "ample.com/", "a/very/lon", "g/path",
    ]


def test_a_segment_past_max_lines_splits_into_successive_cues():
    """Split, not truncate: truncation silently deletes words somebody said, and the
    export just reads as a shorter sentence."""
    words = " ".join(f"word{i}" for i in range(24))
    layer = caption(segs((0.0, 2.0, words)))
    cues = cues_of(layer)
    assert len(cues) > 1
    assert all(len(c.lines) <= DEFAULT_MAX_LINES for c in cues)
    assert " ".join(w for c in cues for w in c.text.split()) == words


def test_the_split_cues_tile_the_segment_they_came_from():
    layer = caption(segs((0.5, 1.5, " ".join(f"word{i}" for i in range(24)))))
    cues = cues_of(layer)
    assert cues[0].span.start == 15 and cues[-1].span.end == 45
    for earlier, later in zip(cues, cues[1:]):
        assert earlier.span.end == later.span.start


def test_max_lines_is_a_prop():
    words = " ".join(f"word{i}" for i in range(24))
    one = cues_of(caption(segs((0.0, 2.0, words)), max_lines=1))
    two = cues_of(caption(segs((0.0, 2.0, words)), max_lines=2))
    assert len(one) > len(two)
    assert all(len(c.lines) == 1 for c in one)


# -- style --------------------------------------------------------------------


def test_the_default_font_fits_max_lines_inside_the_tile():
    """`layers.font_px`'s 0.6*tile-height default would put two lines at 150% of the
    tile and crop the second one off the bottom. Heights are the measured ones: 0.91 em
    for the first line and 1.32 em for each after it, plus the plate padding."""
    layer = caption(segs((0.0, 1.0, "hi")))
    rect = rect_of(layer)
    style = style_for(layer.props, rect)
    block = style.font_px * (0.91 + 1.32 * (style.max_lines - 1)) + 2 * style.box_pad
    assert block <= rect.h
    assert block > rect.h * 0.8  # and it uses the tile it was given


def test_the_wrap_width_follows_the_font_size():
    """Measured on the shipped font: 0.60 em per character, exactly, because it is
    monospaced. Smaller glyphs mean more of them fit on a line of the same tile."""
    rect = rect_of(caption([]))
    default = style_for({"segments": []}, rect)
    smaller = style_for({"segments": [], "font_px": 8}, rect)
    assert smaller.wrap_chars > default.wrap_chars
    assert default.wrap_chars == int(
        (rect.w - 2 * default.box_pad) / (default.font_px * 0.6)
    )


def test_style_prop_names_match_a_text_layer():
    # A REAL font path. `fontfile` is validated now -- it must resolve to an existing
    # file -- because a fontfile is interpolated into the graph and a ',' in it ended
    # the drawtext and appended another with `textfile=`, reading a file into the
    # exported video. "/tmp/x.ttf" does not exist, so it correctly falls back and the
    # assertion below would be testing the fallback rather than the prop.
    real_font = DEFAULT_FONTFILE
    layer = caption(
        segs((0.0, 1.0, "hi")),
        font_px=20, color="#ff3b30", fontfile=real_font, box_color="#101820@0.9",
    )
    chain = compile_one(layer).filter_chain
    assert "fontsize=20" in chain
    assert "fontcolor=0xff3b30" in chain
    assert f"fontfile={real_font}" in chain
    assert "boxcolor=0x101820@0.9" in chain


def test_the_default_plate_is_semi_opaque():
    """Captions are burned in over arbitrary video. White glyphs on the 0.55 black plate
    measure 4.76:1 against a white frame -- a maximised light-theme editor, which is a
    normal thing to record -- where 0.50 gives 3.98:1 and bare glyphs give 1:1."""
    assert DEFAULT_BOX_COLOR == "#000000@0.55"
    chain = compile_one(caption(segs((0.0, 1.0, "hi")))).filter_chain
    assert "box=1:boxcolor=0x000000@0.55" in chain


def test_a_fully_transparent_box_colour_drops_the_box_entirely():
    chain = compile_one(
        caption(segs((0.0, 1.0, "hi")), box_color="black@0.0")
    ).filter_chain
    assert "drawtext=" in chain and "box=1" not in chain


def test_the_plate_alpha_is_not_folded_into_a_mask():
    """`_tile_text` has to fold its box alpha into the rounded-rect mask because
    alphamerge REPLACES the alpha plane. drawtext composites its own box, so the alpha
    goes straight through -- and the box appears and disappears WITH the words instead
    of hanging over every silence in the recording."""
    chain = compile_one(caption(segs((0.0, 1.0, "hi")))).filter_chain
    assert "alphamerge" not in chain and "geq=" not in chain


# -- props from a transcript --------------------------------------------------


TRANSCRIPT = {
    "version": 1,
    "engine": "faster-whisper",
    "model": "small",
    "language": "en",
    "source_stream": "screen",
    "offset_s": 0.0,
    "segments": [
        {"start": 0.0, "end": 2.34, "text": "Hello there."},
        {"start": 2.5, "end": 4.0, "text": "It's 50% off."},
    ],
}


def test_props_are_built_from_a_transcript_json_file(tmp_path):
    p = tmp_path / "transcript.json"
    p.write_text(json.dumps(TRANSCRIPT))
    props = props_from_transcript(p)
    assert props["segments"] == TRANSCRIPT["segments"]


def test_props_are_built_from_a_transcript_object():
    from omarchy_studio.transcribe import Transcript

    props = props_from_transcript(Transcript.from_dict(TRANSCRIPT))
    assert [s["text"] for s in props["segments"]] == ["Hello there.", "It's 50% off."]


def test_a_newer_transcript_raises_rather_than_rendering_as_silence(tmp_path):
    p = tmp_path / "transcript.json"
    p.write_text(json.dumps({**TRANSCRIPT, "version": 99}))
    with pytest.raises(TranscribeError, match="version 99"):
        props_from_transcript(p)


def test_style_overrides_ride_along_with_the_segments():
    props = props_from_transcript(TRANSCRIPT, font_px=48, max_lines=1)
    assert props["font_px"] == 48 and props["max_lines"] == 1


def test_the_layer_carries_its_own_copy_of_the_words():
    """A re-transcribe must not silently discard an edit to the words, and the project
    has to render on a machine where the bundle's transcript.json was deleted."""
    props = props_from_transcript(TRANSCRIPT)
    props["segments"][0]["text"] = "Hello, world."
    assert TRANSCRIPT["segments"][0]["text"] == "Hello there."


def test_caption_layer_sits_in_the_lower_third_above_the_webcam():
    layer = caption_layer(TRANSCRIPT)
    assert layer.type == CAPTION_TYPE
    assert layer.y > 0.6 and layer.x > 0.0 and layer.x + layer.w < 1.0
    assert layer.z > 100  # the webcam's default; a caption behind a face is never wanted
    assert compile_one(layer) is not None


# -- it has to survive contact with ffmpeg ------------------------------------


def _tile_graph(layer: Layer, cuts=()) -> str:
    """The caption tile alone, mapped straight out -- no base video, so a difference in
    the hashes is the caption and nothing else."""
    chains, label = captions.caption_tile(
        layer, "cap", rect_of(layer), cutmap(cuts), TB
    )
    return ";\n".join([*chains, f"{label}null[vout]"])


@needs_ffmpeg
@needs_font
def test_a_caption_appears_only_during_its_own_segment(tmp_path):
    layer = caption(segs((0.1, 0.2, "one, two: it's 50%"), (0.3, 0.4, "three")))
    hashes = framehashes(_tile_graph(layer), tmp_path, frames=15)
    blank, first, second = hashes[0], hashes[3], hashes[9]
    assert blank != first and first != second and blank != second
    # frames 0-2 empty, 3-5 the first cue, 6-8 empty again, 9-11 the second.
    assert hashes[:12] == [blank] * 3 + [first] * 3 + [blank] * 3 + [second] * 3


@needs_ffmpeg
@needs_font
def test_a_cut_moves_the_caption_and_does_not_smear_it(tmp_path):
    """The same caption, with and without ten frames removed ahead of it: it has to land
    ten frames earlier and be exactly as long."""
    layer = caption(segs((0.5, 0.6, "after")))
    plain = framehashes(_tile_graph(layer), tmp_path, frames=20)
    cut = framehashes(_tile_graph(layer, cuts=[(0, 10)]), tmp_path, frames=20)
    assert plain[15:18] == cut[5:8]
    assert plain[15] != plain[0] and cut[5] != cut[0]
    assert cut[:5] == [cut[0]] * 5


@needs_ffmpeg
@needs_font
def test_escaped_speech_renders_the_same_pixels_as_an_unescaped_reference(tmp_path):
    """The methodology `exprs._DRAWTEXT_ESCAPES` was derived with: `textfile=` with
    `expansion=none` is the only escaping-free path into drawtext, so a frame hash
    against it is proof the escaped form says the same thing -- where merely parsing
    would also pass for a caption that silently lost half its characters."""
    for text in (
        "It's fine",
        "50% off: today",
        "one, two, three",
        "C:\\Users\\dan",
        "100%% and 'quoted'",
    ):
        layer = caption(segs((0.0, 1.0, text)))
        style = style_for(layer.props, rect_of(layer))
        rect = rect_of(layer)
        ref_file = tmp_path / "ref.txt"
        # Wrapped here too, or the reference is a different string on a narrow tile.
        ref_file.write_text("\n".join(wrap_lines(text, style.wrap_chars)))
        reference = (
            f"color=c=black@0.0:s={int(rect.w)}x{int(rect.h)}:r=30/1:d=1.0,format=rgba,"
            f"drawtext=fontfile={FONTFILE}:textfile={ref_file}:expansion=none"
            f":fontsize={style.font_px}:fontcolor={style.color}"
            f":box=1:boxcolor=0x000000@0.55:boxborderw={style.box_pad}"
            f":text_align=C:x=(w-text_w)/2:y=(h-text_h)/2[vout]"
        )
        got = framehashes(_tile_graph(layer), tmp_path, frames=1)
        want = framehashes(reference, tmp_path, frames=1)
        assert got == want, text


@needs_ffmpeg
@needs_font
def test_a_whole_transcripts_worth_of_segments_still_parses(tmp_path):
    """One overlay, 300 gated drawtexts -- the shape a ten-minute demo produces. The
    gates are what has to stay inside libavutil's 100-term expression budget."""
    rows = segs(*((i / 300.0, (i + 0.5) / 300.0, f"line {i}") for i in range(300)))
    hashes = framehashes(_tile_graph(caption(rows)), tmp_path, frames=3)
    assert len(hashes) == 3
