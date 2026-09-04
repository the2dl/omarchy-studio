"""Frame-index time model.

Every editable time position in a project is an integer frame index on the SOURCE
timeline. Frame 0 is the screen stream's first frame; the grid is the capture fps,
fixed when the recording is made.

Two measured results forced this, and both are easy to reintroduce by accident:

1. Cut boundaries must land on the frame grid. `trim` keeps whole video frames while
   `atrim` is sample-exact, so an unsnapped boundary leaves a sub-frame audio residue
   that accumulates: ~50 ms of A/V skew over six cuts, and every layer downstream of a
   cut arrives one frame late per cut.

2. Snapping alone is not enough. A boundary expressed as `%.6f` seconds and gated with
   `lt(t, B)` is float-fragile *exactly at* frame times -- which is where snapping puts
   every boundary. Walking 899 boundaries leaked an extra frame on 318 of them (35%),
   and because the start edge shifts late while the frame *count* stays right, counting
   frames does not detect it.

Storing frame indices makes both failures unrepresentable rather than merely fixed.
Seconds appear only at the edges: parsing user input, and emitting ffmpeg expressions.
"""

from __future__ import annotations

from dataclasses import dataclass


class TimebaseError(ValueError):
    pass


@dataclass(frozen=True)
class Timebase:
    """A frame grid. `fps` is exact; capture is always CFR (gsr is passed -fm cfr)."""

    fps_num: int
    fps_den: int = 1

    def __post_init__(self) -> None:
        if self.fps_num <= 0 or self.fps_den <= 0:
            raise TimebaseError(f"invalid fps {self.fps_num}/{self.fps_den}")

    @classmethod
    def from_fps(cls, fps: float | int) -> "Timebase":
        """Build from a float fps, recovering the exact NTSC ratios."""
        if isinstance(fps, int) or float(fps).is_integer():
            return cls(int(fps), 1)
        # Tolerance has to admit a human-written "29.97" as well as ffprobe's exact
        # 30000/1001 (= 29.97003), so it is loose enough to cover the rounding people
        # actually type but far tighter than the gap to the neighbouring rates.
        for num, den in ((24000, 1001), (30000, 1001), (60000, 1001), (120000, 1001)):
            if abs(num / den - fps) < 1e-3:
                return cls(num, den)
        raise TimebaseError(f"non-integer fps {fps!r} is not a known NTSC rate")

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den

    # -- conversion -----------------------------------------------------------

    def to_frame(self, seconds: float) -> int:
        """Nearest frame index. This is the ONLY sanctioned seconds -> frame path,
        so every boundary in the system is snapped by construction."""
        if seconds < 0:
            raise TimebaseError(f"negative time {seconds}")
        return round(seconds * self.fps_num / self.fps_den)

    def to_seconds(self, frame: int) -> float:
        """Exact presentation time of a frame's leading edge."""
        return frame * self.fps_den / self.fps_num

    def frame_midpoint(self, frame: int) -> float:
        """Half a frame past the leading edge.

        Used wherever a time-domain ffmpeg expression is unavoidable: evaluating a
        gate at the midpoint keeps it away from the boundary where float comparison
        is ambiguous. Frame-index gating is preferred; this is the fallback.
        """
        return (frame + 0.5) * self.fps_den / self.fps_num

    def duration_frames(self, seconds: float) -> int:
        return self.to_frame(seconds)


@dataclass(frozen=True)
class FrameRange:
    """A half-open frame interval [start, end). Half-open is what makes adjacent
    ranges tile without overlapping, and it matches `gte(n,A)*lt(n,B)` exactly."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise TimebaseError(f"negative start frame {self.start}")
        if self.end <= self.start:
            raise TimebaseError(f"empty or inverted range [{self.start}, {self.end})")

    def __len__(self) -> int:
        return self.end - self.start

    def __contains__(self, frame: int) -> bool:
        return self.start <= frame < self.end

    def overlaps(self, other: "FrameRange") -> bool:
        return self.start < other.end and other.start < self.end

    def shifted(self, delta: int) -> "FrameRange":
        return FrameRange(self.start + delta, self.end + delta)

    def intersect(self, other: "FrameRange") -> "FrameRange | None":
        lo, hi = max(self.start, other.start), min(self.end, other.end)
        return FrameRange(lo, hi) if hi > lo else None

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, d: dict) -> "FrameRange":
        return cls(int(d["start"]), int(d["end"]))


def normalize(ranges: list[FrameRange]) -> list[FrameRange]:
    """Sort and merge touching or overlapping ranges.

    Cuts must be normalized before rendering: a layer spanning ~30 separate output
    intervals approaches ffmpeg's 100-term expression budget, and merging adjacent
    intervals is what keeps generated gates small.
    """
    if not ranges:
        return []
    out: list[FrameRange] = []
    for r in sorted(ranges, key=lambda x: x.start):
        if out and r.start <= out[-1].end:
            if r.end > out[-1].end:
                out[-1] = FrameRange(out[-1].start, r.end)
        else:
            out.append(r)
    return out


class CutMap:
    """Maps source frames to output frames across a set of removed ranges.

    Cuts are stored in source time. Output time is derived and never persisted:
    rendering cut-first with remapped layer ranges and cut-last with verbatim ranges
    produced bit-identical frames (PSNR infinite, 450/450), so source time is the free
    choice -- and it means adding or deleting a cut never slides an annotation off the
    thing it annotates.
    """

    def __init__(self, cuts: list[FrameRange], total_frames: int,
                 head_pad: int = 0, tail_pad: int = 0) -> None:
        self.cuts = normalize(cuts)
        self.total_frames = total_frames
        # Output-only time, before and after everything that was recorded. Cuts can
        # only ever REMOVE time, so without these the output can never be longer than
        # the capture and a title card has nowhere to live. Nothing was recorded in a
        # pad, which is the whole point and also the whole constraint: there is no
        # camera and no audio there, because none exists.
        self.head_pad = max(0, int(head_pad))
        self.tail_pad = max(0, int(tail_pad))
        for c in self.cuts:
            if c.end > total_frames:
                raise TimebaseError(
                    f"cut [{c.start}, {c.end}) runs past the source ({total_frames} frames)"
                )
        # Kept source segments, in order.
        self.kept: list[FrameRange] = []
        cursor = 0
        for c in self.cuts:
            if c.start > cursor:
                self.kept.append(FrameRange(cursor, c.start))
            cursor = c.end
        if cursor < total_frames:
            self.kept.append(FrameRange(cursor, total_frames))

    @property
    def kept_frames(self) -> int:
        """Output frames that came from the recording."""
        return sum(len(k) for k in self.kept)

    @property
    def output_frames(self) -> int:
        return self.head_pad + self.kept_frames + self.tail_pad

    def remap_pad(self, pad: str, r: FrameRange) -> list[FrameRange]:
        """Project a range given in PAD frames onto the output timeline.

        A second coordinate space, and deliberately not folded into the first. Layer
        ranges are source frames so that adding or deleting a cut never slides an
        annotation off what it annotates -- and a pad has no source frames at all. The
        alternatives both slide something: negative source indices for the head pad
        break FrameRange's invariant, and re-basing every layer on output time means
        growing the intro drags every annotation in the recording along with it.
        """
        if pad == "head":
            span, base = self.head_pad, 0
        elif pad == "tail":
            span, base = self.tail_pad, self.head_pad + self.kept_frames
        else:
            raise TimebaseError(f"unknown pad {pad!r}")
        start = max(0, min(r.start, span))
        end = max(start, min(r.end, span))
        if end <= start:
            return []
        return [FrameRange(base + start, base + end)]

    def is_cut(self, source_frame: int) -> bool:
        return any(source_frame in c for c in self.cuts)

    def to_output(self, source_frame: int) -> int | None:
        """Output frame index, or None if this source frame was cut away."""
        offset = self.head_pad
        for k in self.kept:
            if source_frame in k:
                return offset + (source_frame - k.start)
            offset += len(k)
        return None

    def to_source(self, output_frame: int) -> int:
        """The recorded frame shown at this output frame.

        Raises for an output frame inside a PAD, because none is: a pad is time that
        was never recorded. Callers that walk the whole output timeline must ask
        `is_pad` first -- the cursor compositor did not, and since render catches
        ValueError and degrades to "no cursor", a single pad silently removed the
        pointer from every export.
        """
        if self.is_pad(output_frame):
            raise TimebaseError(
                f"output frame {output_frame} is in a pad; nothing was recorded there"
            )
        offset = self.head_pad
        for k in self.kept:
            if output_frame < offset + len(k):
                return k.start + (output_frame - offset)
            offset += len(k)
        raise TimebaseError(
            f"output frame {output_frame} past the end ({self.output_frames} frames)"
        )

    def is_pad(self, output_frame: int) -> bool:
        """Whether this output frame is in a pad rather than in the recording."""
        return (output_frame < self.head_pad
                or output_frame >= self.head_pad + self.kept_frames)

    def remap(self, r: FrameRange) -> list[FrameRange]:
        """Project a source range onto the output timeline.

        A range straddling a cut becomes several output ranges; one entirely inside a
        cut vanishes. Results are merged, so a layer spanning many small cuts still
        emits a compact gate.
        """
        pieces: list[FrameRange] = []
        offset = self.head_pad
        for k in self.kept:
            hit = r.intersect(k)
            if hit is not None:
                lo = offset + (hit.start - k.start)
                pieces.append(FrameRange(lo, lo + len(hit)))
            offset += len(k)
        return normalize(pieces)
