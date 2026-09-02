"""Cut machinery: removed source ranges become a split/trim/concat chain.

Four measured facts are baked in and none of them is negotiable:

* `select`/`aselect` is the wrong mechanism. `aselect` drops audio in whole decoder
  frames (~21.3 ms for AAC) no matter how the video grid is snapped, so a cut boundary
  lands on a video frame and an audio packet edge that disagree; the residue accumulated
  to ~50 ms of A/V skew over six cuts. `split -> trim/atrim per kept segment -> concat`
  is the filter pair that exists specifically to keep A and V together.

* Boundaries are frame indices. `trim` takes `start_frame`/`end_frame` directly and
  `end_frame` is the first frame DROPPED, which is exactly FrameRange's half-open
  contract, so the video side cannot drift. `atrim` has no frame form, so it gets the
  exact leading-edge times of those same frames.

* EVERY time-varying input must be cut, not just the base. An uncut webcam drifts by
  precisely the total cut duration -- measured at exactly -90 and -150 frames for 3 s
  and 5 s of cuts at 30 fps. That is why this takes a list of labels.

* Cut FIRST, before the overlay stack, not last: 2.20 s versus 2.83 s on the same
  project, because every layer downstream then runs over fewer frames.
"""

from __future__ import annotations

import re

from .timebase import CutMap, Timebase


class CutError(ValueError):
    pass


def _stem(label: str) -> str:
    """A filtergraph-safe identifier derived from a stream label.

    Labels arrive as `[base]` or `[1:v]`; the brackets are delimiters rather than part
    of the name, and `:` cannot appear in a new label, so it is folded to an underscore.
    """
    s = re.sub(r"[^0-9A-Za-z_]", "_", label.strip().strip("[]"))
    if not s:
        raise CutError(f"unusable stream label {label!r}")
    return s


def cut_output_label(label: str) -> str:
    """The label a stream carries after `cut_chain`. Deterministic so the caller can
    wire the rest of the graph without parsing the chain it just generated."""
    return f"[{_stem(label)}_cut]"


def cut_labels(
    cutmap: CutMap,
    labels: list[str],
    with_audio: bool = False,
    *,
    audio_label: str = "[0:a]",
) -> dict[str, str]:
    """Map each input label to the label to use downstream.

    Identity when there are no cuts, because `cut_chain` then emits nothing. Callers
    should route through this rather than calling `cut_output_label` directly, so the
    no-cuts case does not dangle a reference to a label that was never created.
    """
    ins = list(labels) + ([audio_label] if with_audio else [])
    if not cutmap.cuts:
        return {l: l for l in ins}
    return {l: cut_output_label(l) for l in ins}


def cut_chain(
    cutmap: CutMap,
    tb: Timebase,
    labels: list[str],
    with_audio: bool = False,
    *,
    audio_label: str = "[0:a]",
) -> str:
    """Generate the ';'-joined chains that excise `cutmap.cuts` from every label.

    Returns "" when there is nothing to cut -- a no-op `null` per stream would cost a
    filter instance on every frame for no reason. Use `cut_labels` to pick the right
    downstream labels in both cases.

    `with_audio` pairs `audio_label` with the first video label through a single
    `concat=v=1:a=1`; that filter interleaves the two, which is the whole reason for
    preferring it over separate video and audio concats.
    """
    if not cutmap.cuts:
        return ""

    keep = cutmap.kept
    if not keep:
        raise CutError("every frame is cut; there is nothing to render")
    k = len(keep)

    vlabels = list(labels)
    if len(set(vlabels)) != len(vlabels):
        raise CutError(f"duplicate stream label in {vlabels!r}")
    if with_audio and audio_label in vlabels:
        raise CutError(f"{audio_label!r} is listed as both a video and the audio stream")
    if not vlabels and not with_audio:
        raise CutError("cut_chain called with no streams to cut")

    chains: list[str] = []
    paired = vlabels[0] if (with_audio and vlabels) else None

    for label in vlabels:
        stem = _stem(label)
        chains.append(
            f"{label}split={k}" + "".join(f"[{stem}_s{i}]" for i in range(k))
        )
        for i, seg in enumerate(keep):
            chains.append(
                f"[{stem}_s{i}]trim=start_frame={seg.start}:end_frame={seg.end},"
                f"setpts=PTS-STARTPTS[{stem}_k{i}]"
            )
        if label == paired:
            chains += _audio_segments(cutmap, tb, audio_label, k)
            astem = _stem(audio_label)
            pads = "".join(f"[{stem}_k{i}][{astem}_k{i}]" for i in range(k))
            chains.append(
                f"{pads}concat=n={k}:v=1:a=1"
                f"{cut_output_label(label)}{cut_output_label(audio_label)}"
            )
        else:
            pads = "".join(f"[{stem}_k{i}]" for i in range(k))
            chains.append(f"{pads}concat=n={k}:v=1:a=0{cut_output_label(label)}")

    if with_audio and paired is None:
        chains += _audio_segments(cutmap, tb, audio_label, k)
        astem = _stem(audio_label)
        pads = "".join(f"[{astem}_k{i}]" for i in range(k))
        chains.append(f"{pads}concat=n={k}:v=0:a=1{cut_output_label(audio_label)}")

    return ";".join(chains)


def _audio_segments(cutmap: CutMap, tb: Timebase, audio_label: str, k: int) -> list[str]:
    """atrim per kept segment, at the exact leading-edge time of each boundary frame.

    Seconds are unavoidable here -- atrim has no frame form -- but they are derived from
    the frame grid, so the audio edge sits on the same instant as the video edge instead
    of being quantized to a decoder frame the way aselect does it.
    """
    astem = _stem(audio_label)
    out = [f"{audio_label}asplit={k}" + "".join(f"[{astem}_s{i}]" for i in range(k))]
    for i, seg in enumerate(cutmap.kept):
        out.append(
            f"[{astem}_s{i}]atrim={tb.to_seconds(seg.start):.6f}:"
            f"{tb.to_seconds(seg.end):.6f},asetpts=PTS-STARTPTS[{astem}_k{i}]"
        )
    return out
