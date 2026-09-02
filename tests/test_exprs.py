from __future__ import annotations

import random

import pytest
from ffmpeg_harness import (
    BLACK_64,
    FONTFILE,
    NULL_64,
    framehashes,
    needs_ffmpeg,
    needs_font,
    run_graph,
)

from omarchy_studio.exprs import (
    balanced_or,
    balanced_sum,
    escape_drawtext,
    fade_alpha,
    fade_filters,
    frame_gate,
)
from omarchy_studio.timebase import FrameRange


def _max_depth(s: str) -> int:
    depth = best = 0
    for ch in s:
        if ch == "(":
            depth += 1
            best = max(best, depth)
        elif ch == ")":
            depth -= 1
    return best


def _max_call_depth(s: str) -> int:
    """Nesting depth counting max(...) calls, which have no bare parentheses."""
    return _max_depth(s.replace("max(", "("))


# -- balanced trees ----------------------------------------------------------


def test_single_and_empty_terms():
    assert balanced_or(["a"]) == "a"
    assert balanced_sum(["a"]) == "a"
    assert balanced_or([]) == "0"
    assert balanced_sum([]) == "0"


def test_or_is_a_max_tree_not_a_chain():
    assert balanced_or(["a", "b"]) == "max(a,b)"
    assert "+" not in balanced_or([f"t{i}" for i in range(64)])


def test_200_terms_nest_no_deeper_than_eight():
    terms = [f"t{i}" for i in range(200)]
    assert _max_call_depth(balanced_or(terms)) <= 8
    assert _max_depth(balanced_sum(terms)) <= 8


def test_tree_keeps_every_term_once():
    terms = [f"t{i}" for i in range(37)]
    joined = balanced_sum(terms)
    for t in terms:
        assert joined.count(t + ")") + joined.count(t + "+") == 1


# -- frame gates -------------------------------------------------------------


def test_frame_gate_is_half_open_on_the_frame_index():
    assert frame_gate([FrameRange(10, 20)]) == "gte(n,10)*lt(n,20)"


def test_frame_gate_never_uses_time_or_between():
    g = frame_gate([FrameRange(0, 5), FrameRange(9, 12)])
    assert "between" not in g
    assert "gte(t," not in g and "lt(t," not in g
    assert g == "max(gte(n,0)*lt(n,5),gte(n,9)*lt(n,12))"


def test_empty_gate_is_permanently_off():
    assert frame_gate([]) == "0"


# -- fades -------------------------------------------------------------------


def test_no_fade_is_a_constant():
    assert fade_alpha(FrameRange(0, 100), 0) == "1"
    assert fade_filters(FrameRange(0, 100), 0) == ""


def test_fade_is_clamped_to_half_the_range():
    # A 40-frame fade on a 30-frame layer would never reach full opacity.
    f = fade_filters(FrameRange(100, 130), 40)
    assert "n=15" in f and "s=100" in f and "s=115" in f


def test_fade_filters_are_frame_indexed_not_seconds():
    f = fade_filters(FrameRange(60, 120), 10)
    assert f == "fade=t=in:alpha=1:s=60:n=10,fade=t=out:alpha=1:s=110:n=10"
    assert "st=" not in f and "d=" not in f


def test_fade_alpha_expression_endpoints():
    e = fade_alpha(FrameRange(10, 50), 8)
    assert e == "clip(min((n-10)/8,(50-n)/8),0,1)"


# -- ffmpeg has to accept all of it ------------------------------------------


def _gate_graph(expr: str) -> str:
    return f"[0:v]drawbox=x=0:y=0:w=16:h=16:color=red:t=fill:enable='{expr}'[vout]"


@needs_ffmpeg
def test_ffmpeg_accepts_a_3000_click_gate(tmp_path):
    """The measured ceiling: ffmpeg's expression parser has a 100-level recursion
    budget, and a linear chain spends one level per term."""
    ranges = [FrameRange(2 * i, 2 * i + 1) for i in range(3000)]
    r = run_graph(_gate_graph(frame_gate(ranges)), tmp_path, inputs=[NULL_64], frames=2)
    assert r.returncode == 0, r.stderr[-2000:]


@needs_ffmpeg
def test_a_linear_chain_of_the_same_terms_is_rejected(tmp_path):
    """Negative control. Without this the test above proves nothing -- it would pass
    just as happily if the balanced tree were quietly a linear chain."""
    terms = [f"gte(n,{2 * i})*lt(n,{2 * i + 1})" for i in range(200)]
    r = run_graph(_gate_graph("+".join(terms)), tmp_path, inputs=[NULL_64], frames=2)
    assert r.returncode != 0
    assert "expression" in r.stderr.lower()


@needs_ffmpeg
def test_balanced_sum_of_3000_terms_parses(tmp_path):
    terms = [f"gte(n,{i})" for i in range(3000)]
    expr = f"gt({balanced_sum(terms)},0)"
    r = run_graph(_gate_graph(expr), tmp_path, inputs=[NULL_64], frames=2)
    assert r.returncode == 0, r.stderr[-2000:]


@needs_ffmpeg
def test_gate_lights_exactly_the_half_open_range(tmp_path):
    """A leaked boundary frame keeps the frame COUNT right, so this compares content."""
    graph = (
        "[0:v]format=rgb24,drawbox=x=0:y=0:w=64:h=48:color=white:t=fill"
        f":enable='{frame_gate([FrameRange(3, 6)])}'[vout]"
    )
    hashes = framehashes(
        graph, tmp_path, inputs=[BLACK_64], frames=9
    )
    black, white = hashes[0], hashes[3]
    assert black != white
    assert hashes == [black] * 3 + [white] * 3 + [black] * 3


@needs_ffmpeg
def test_fade_filters_ramp_monotonically(tmp_path):
    """The alpha ramp has to actually ramp, not just parse."""
    r = FrameRange(0, 20)
    graph = (
        "color=c=black:s=64x48:r=30:d=1,format=rgba[bg];"
        f"color=c=white:s=64x48:r=30:d=1,format=rgba,{fade_filters(r, 5)}[fg];"
        "[bg][fg]overlay=x=0:y=0:shortest=1:format=auto,format=gray[vout]"
    )
    hashes = framehashes(graph, tmp_path, frames=20)
    # Distinct hashes while ramping in, a plateau in the middle, distinct again on the
    # way out; the plateau proves the ramp reaches and holds full opacity.
    assert len(set(hashes[0:5])) == 5
    assert len(set(hashes[6:14])) == 1
    assert len(set(hashes[15:20])) == 5


@needs_ffmpeg
@needs_font
def test_fade_alpha_expression_is_accepted_by_drawtext(tmp_path):
    expr = fade_alpha(FrameRange(0, 30), 6)
    graph = (
        f"[0:v]format=rgba,drawtext=fontfile={FONTFILE}:text='x':fontsize=20"
        f":fontcolor=white:x=1:y=1:alpha='{expr}'[vout]"
    )
    r = run_graph(graph, tmp_path, inputs=[NULL_64], frames=10)
    assert r.returncode == 0, r.stderr[-2000:]


# -- drawtext escaping -------------------------------------------------------


def test_escape_table():
    assert escape_drawtext("plain") == "plain"
    assert escape_drawtext("a:b") == "a\\:b"
    assert escape_drawtext("50%") == "50\\\\%"
    assert escape_drawtext("a\\b") == "a\\\\\\\\b"
    assert escape_drawtext("it's") == "it'\\\\\\''s"
    assert escape_drawtext("a b") == "a\\ b"


def _oracle(s: str, tmp_path) -> list[str]:
    """Render via `textfile=` with `expansion=none` -- the only path into drawtext that
    does no escaping at all, so it defines what the escaped form must equal."""
    tf = tmp_path / "oracle.txt"
    tf.write_text(s)
    graph = (
        f"[0:v]drawtext=fontfile={FONTFILE}:textfile={tf}:expansion=none"
        ":fontsize=28:fontcolor=white:x=8:y=8[vout]"
    )
    return framehashes(
        graph, tmp_path, inputs=[["-f", "lavfi", "-i", "color=c=black:s=640x160:r=1"]], frames=1
    )


def _escaped(s: str, tmp_path) -> list[str]:
    graph = (
        f"[0:v]drawtext=fontfile={FONTFILE}:text='{escape_drawtext(s)}'"
        ":fontsize=28:fontcolor=white:x=8:y=8[vout]"
    )
    return framehashes(
        graph, tmp_path, inputs=[["-f", "lavfi", "-i", "color=c=black:s=640x160:r=1"]], frames=1
    )


@needs_ffmpeg
@needs_font
@pytest.mark.parametrize(
    "s",
    [
        "plain text",
        "ratio 1:2",
        "100% done",
        "a%{pts}b",
        "back\\slash",
        "it's fine",
        "comma, semi; equals=",
        "  leading and trailing  ",
        "[brackets] {braces}",
        "unicode: é → ✓",
        "two\nlines",
    ],
)
def test_escaped_text_renders_identically_to_the_oracle(s, tmp_path):
    assert _escaped(s, tmp_path) == _oracle(s, tmp_path)


@needs_ffmpeg
@needs_font
def test_escaping_fuzz(tmp_path):
    alphabet = list("abZ09  ") + [
        ":", ",", "'", "\\", "%", "[", "]", ";", "=", '"', "{", "}", "@", "$", "é",
    ]
    rng = random.Random(20260902)
    for _ in range(25):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
        assert _escaped(s, tmp_path) == _oracle(s, tmp_path), repr(s)
