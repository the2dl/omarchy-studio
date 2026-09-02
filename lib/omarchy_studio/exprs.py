"""Filtergraph expression helpers.

Everything a generated graph needs to say "when" or "how much" goes through here, so
that the two expression traps we measured are unrepresentable rather than merely
avoided.

1. GATE ON THE FRAME INDEX, NEVER ON TIME. `between(t,A,B)` is inclusive at both ends
   and lights one extra frame per range; `gte(t,A)*lt(t,B)` with '%.6f' seconds is
   float-fragile *exactly at* frame times, which is where every snapped boundary lands
   -- walking 899 boundaries leaked an extra frame on 318 of them while keeping the
   frame count correct, so counting frames does not detect it. `gte(n,A)*lt(n,B)` on
   integers has no such edge.

2. COMBINE WITH BALANCED TREES, NEVER LINEAR CHAINS. libavutil/eval.c has a recursion
   budget of 100 that a left-linear `a+b+c+...` consumes one term at a time. Verified on
   the installed ffmpeg n9.0.1: a 150-term linear OR dies with "Error when evaluating
   the expression" (which surfaces as a misleading allocation failure in longer graphs),
   while the same 3000 terms nested as a balanced tree -- depth 12 -- parse and run.
"""

from __future__ import annotations

from .timebase import FrameRange

# drawtext sees a filter argument twice: once through the filtergraph tokenizer, then
# again through its own text expansion. Each layer eats one backslash, so a character
# special to the *expansion* needs two and a literal backslash needs four. Every entry
# below was found by rendering the string and comparing the frame hash against the same
# string fed through `textfile=` with `expansion=none`, which is the only escaping-free
# path into drawtext; 250 random strings over this alphabet now round-trip exactly.
_DRAWTEXT_ESCAPES = {
    "\\": "\\\\\\\\",  # -> \\ -> \
    "%": "\\\\%",  # bare % aborts the graph; %{...} would expand as a metadata ref
    "'": "'\\\\\\''",  # close the quote, emit an escaped quote, reopen
    ":": "\\:",  # consumed by the option splitter even inside quotes
    " ": "\\ ",  # leading whitespace is trimmed off the token otherwise
    "\t": "\\\t",
    "\n": "\\\n",
}


class ExprError(ValueError):
    pass


def _tree(terms: list[str], join) -> str:
    if len(terms) == 1:
        return terms[0]
    mid = len(terms) // 2
    return join(_tree(terms[:mid], join), _tree(terms[mid:], join))


def balanced_or(terms: list[str]) -> str:
    """Logical OR of 0/1 terms as a balanced `max()` tree.

    max() rather than a sum: the result stays in {0,1}, so gates can be multiplied
    together (a layer gate times a zoom gate) without two overlapping terms summing to 2
    and breaking a downstream comparison.
    """
    if not terms:
        return "0"
    return _tree(list(terms), lambda a, b: f"max({a},{b})")


def balanced_sum(terms: list[str]) -> str:
    """Arithmetic sum as a balanced tree. Used for weighted blends -- the auto-zoom
    focal point is sum(envelope_i * cx_i) / sum(envelope_i)."""
    if not terms:
        return "0"
    return _tree(list(terms), lambda a, b: f"({a}+{b})")


def frame_gate(ranges: list[FrameRange]) -> str:
    """An `enable=` expression true exactly on the given half-open frame ranges.

    Ranges are OUTPUT frame indices: the caller has already put source ranges through
    `CutMap.remap`, which merges adjacent output intervals and so keeps this expression
    small even for a layer straddling many cuts.

    An empty list yields "0" -- permanently off -- rather than an empty string, which
    ffmpeg would reject.
    """
    if not ranges:
        return "0"
    return balanced_or([f"gte(n,{r.start})*lt(n,{r.end})" for r in ranges])


def _clamped_fade(r: FrameRange, fade_frames: int) -> int:
    """A fade longer than half the range would have the in- and out-ramps cross before
    either reached full opacity, so a short annotation would silently never be solid."""
    if fade_frames <= 0:
        return 0
    return min(int(fade_frames), len(r) // 2)


def fade_alpha(r: FrameRange, fade_frames: int) -> str:
    """A 0..1 alpha ramp over `r`, as an expression in the frame index `n`.

    For consumers that take a per-frame alpha *expression* -- drawtext's `alpha=` is the
    only cheap one; everything else in libavfilter wants either a constant or the `geq`
    per-pixel interpreter. Static tiles use `fade_filters` instead.

    This describes the ramp only. Visibility outside `r` is the `enable=` gate's job, so
    with no fade the expression is the constant 1.
    """
    f = _clamped_fade(r, fade_frames)
    if f == 0:
        return "1"
    return f"clip(min((n-{r.start})/{f},({r.end}-n)/{f}),0,1)"


def fade_filters(r: FrameRange, fade_frames: int) -> str:
    """The same ramp as a `fade` filter fragment, for an RGBA tile.

    `fade` takes frame options (`s`/`n`), so this stays on the frame grid like every
    other boundary in the system -- the seconds-valued `st`/`d` form would reintroduce
    the float-at-a-frame-boundary problem the timebase exists to prevent.

    Returns "" when there is no fade, so callers can concatenate unconditionally.
    """
    f = _clamped_fade(r, fade_frames)
    if f == 0:
        return ""
    return (
        f"fade=t=in:alpha=1:s={r.start}:n={f},"
        f"fade=t=out:alpha=1:s={r.end - f}:n={f}"
    )


def escape_drawtext(s: str) -> str:
    """Escape a caption for use inside a single-quoted `text='...'` argument.

    See `_DRAWTEXT_ESCAPES` for how the mapping was derived. Note the newline and tab
    escapes emit a literal backslash followed by the real whitespace character, so a
    graph containing a multi-line caption spans several lines in the graph file; write
    the graph out verbatim rather than joining chains on newlines.
    """
    return "".join(_DRAWTEXT_ESCAPES.get(ch, ch) for ch in s)
