"""ffprobe wrappers.

Frame counts are read from the container rather than computed as duration*fps. gsr
writes VFR by default and Omarchy passes -fm cfr, but a dropped frame was still
observed within seven seconds of capture, so the two disagree in practice and only the
container is authoritative.
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path


class ProbeError(RuntimeError):
    pass


def _run(args: list[str]) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, check=True)
    except FileNotFoundError as e:
        raise ProbeError("ffprobe not found; install ffmpeg") from e
    except subprocess.CalledProcessError as e:
        raise ProbeError(f"ffprobe failed: {e.stderr.strip()[:400]}") from e
    return r.stdout


@lru_cache(maxsize=64)
def _streams(path: str) -> list[dict]:
    out = _run(
        [
            "ffprobe", "-v", "error", "-of", "json",
            "-show_streams", "-show_format", str(path),
        ]
    )
    return json.loads(out).get("streams", [])


def video_stream(path: Path) -> dict:
    for s in _streams(str(path)):
        if s.get("codec_type") == "video":
            return s
    raise ProbeError(f"{path} has no video stream")


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in _streams(str(path)))


def dimensions(path: Path) -> tuple[int, int]:
    s = video_stream(path)
    return int(s["width"]), int(s["height"])


def fps(path: Path) -> tuple[int, int]:
    s = video_stream(path)
    rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/0"
    num, _, den = rate.partition("/")
    num, den = int(num), int(den or 1)
    if num <= 0 or den <= 0:
        raise ProbeError(f"{path} reports unusable frame rate {rate!r}")
    return num, den


@lru_cache(maxsize=64)
def frame_count(path: str | Path) -> int:
    """Exact frame count.

    nb_frames is absent on plenty of containers, so fall back to counting packets --
    slower but exact, and correctness matters more here than a few hundred ms.
    """
    s = video_stream(Path(path))
    n = s.get("nb_frames")
    if n and int(n) > 0:
        return int(n)
    out = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_packets", "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0", str(path),
        ]
    ).strip()
    if not out.isdigit():
        raise ProbeError(f"could not count frames in {path}")
    return int(out)


def has_discardable_warmup(path: Path) -> bool:
    """True when the first GOP carries discardable warm-up packets.

    This is the guard the unconditional `-ss 0.1` trim should have been. Recordings
    without warm-up packets -- which is most of them -- must not be trimmed, because the
    trim shifts every timestamp by exactly -100ms and silently invalidates the anchor.
    """
    out = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-read_intervals", "%+0.2", "-show_entries", "packet=flags",
            "-of", "csv=p=0", str(path),
        ]
    )
    return "D" in out
