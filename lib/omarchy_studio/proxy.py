"""Short-GOP preview proxies.

The editor's preview must never read the master. Seeking the 5K master took 517-651 ms
and half the seeks never delivered a frame at all; the proxy seeks in 15-53 ms with every
seek landing. That difference is the GOP, not the resolution -- `-g 15 -bf 0` is what
makes a seek land on a nearby keyframe, and the downscale is only there to keep the
decode cheap enough to composite live.

The proxy is derived, disposable state: it lives in the bundle's proxy/ directory, which
can be deleted at any time and regenerates on demand. It is deliberately NOT part of the
capture, so a stale one is a performance bug rather than a correctness one -- but it is
still fingerprinted against its source, because a proxy of the wrong recording would be
a correctness bug of the worst kind.
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .probe import frame_count
from .project import Bundle, ProjectError

# Measured: this is the setting that makes seeks land, so it is also the setting that
# must invalidate a proxy when it changes.
GOP = 15
PROXY_WIDTH = 1920
# CRF, not a bitrate target. A fixed 10M made the proxy TEN TIMES LARGER than the
# master it stands in for -- 174MB against 18MB for a 2:13 desktop capture -- because a
# constant bitrate spends its whole budget on a mostly-static screen that gsr had already
# compressed to about 1 Mbit/s. At ~5GB per hour of recording that is a worse problem
# than the slow seeking it was solving.
#
# CRF 30 is deliberately low quality: nobody grades colour on a scrub proxy, and the
# export always reads the master. The short GOP is what makes seeks land, and it is kept
# -- it costs compression, but against a quality target rather than a bitrate floor it
# costs a fraction of what it did.
_VIDEO_ARGS = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "30",
    "-maxrate", "6M",
    "-bufsize", "12M",
    "-g", str(GOP),
    "-bf", "0",
    "-pix_fmt", "yuv420p",
]


class ProxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProxySpec:
    """Which stream is being proxied and where it lands."""

    source: Path
    dest: Path
    stamp: Path


def _spec(bundle: Bundle, stream: str) -> ProxySpec:
    s = getattr(bundle.capture, stream, None)
    if s is None:
        raise ProjectError(f"capture has no {stream} stream to proxy")
    src = bundle.media(Path(s.path).name)
    if not src.exists():
        raise ProxyError(f"missing media {src}")
    return ProxySpec(
        source=src,
        dest=bundle.proxy_dir / f"{stream}-proxy.mp4",
        stamp=bundle.proxy_dir / f"{stream}-proxy.json",
    )


def _fingerprint(spec: ProxySpec) -> dict:
    st = spec.source.stat()
    return {
        "source": spec.source.name,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "gop": GOP,
        "width": PROXY_WIDTH,
    }


def is_stale(bundle: Bundle, stream: str = "screen") -> bool:
    spec = _spec(bundle, stream)
    if not spec.dest.exists() or not spec.stamp.exists():
        return True
    try:
        return json.loads(spec.stamp.read_text()) != _fingerprint(spec)
    except (OSError, ValueError):
        return True


def ensure_proxy(
    bundle: Bundle,
    stream: str = "screen",
    progress: Callable[[float, str], None] | None = None,
) -> Path:
    """Return a short-GOP preview proxy, generating it only if it is stale.

    Reuse is checked against a fingerprint of the source rather than a timestamp
    comparison: media/ is immutable, so a mismatch means this proxy belongs to a
    different recording or was made with different settings, and either way the answer
    is to regenerate.

    `progress` is called with (fraction 0..1, message) as ffmpeg encodes. Without it the
    editor opens on a recording it cannot yet scrub and can only show an indeterminate
    bar -- which on a long capture is several minutes of a UI that looks stuck. The
    fraction is frames encoded over `probe.frame_count`, not ffmpeg's own out_time,
    because the frame count is what the timeline is already indexed by.
    """
    spec = _spec(bundle, stream)
    if not is_stale(bundle, stream):
        return spec.dest

    bundle.proxy_dir.mkdir(parents=True, exist_ok=True)
    # The temp name keeps the .mp4 last: ffmpeg picks the muxer from the extension, and
    # a trailing ".tmp" leaves it with nothing to infer from.
    tmp = spec.dest.with_name(spec.dest.stem + ".part.mp4")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(spec.source),
        # min() rather than a bare width: a camera stream is already below the proxy
        # width, and scaling it UP would cost decode time to gain nothing. The GOP is
        # the point; the scale is the concession.
        "-vf", f"scale='min({PROXY_WIDTH},iw)':-2:flags=bicubic",
        *_VIDEO_ARGS,
        # Audio is copied because the preview scrubs against it and re-encoding would
        # shift it relative to the video it is supposed to be checking.
        "-c:a", "copy",
        str(tmp),
    ]
    if progress is None:
        r = subprocess.run(cmd, capture_output=True, text=True)
        rc, err = r.returncode, r.stderr
    else:
        rc, err = _encode_with_progress(cmd, spec, progress)
    if rc != 0:
        tmp.unlink(missing_ok=True)
        raise ProxyError(f"proxy generation failed:\n{err.strip()[-2000:]}")

    # Replace only once ffmpeg has succeeded: a half-written proxy that looks complete
    # would be reused, and the editor would preview a truncated recording.
    tmp.replace(spec.dest)
    spec.stamp.write_text(json.dumps(_fingerprint(spec), indent=2) + "\n")
    return spec.dest


def _encode_with_progress(
    cmd: list[str], spec: ProxySpec, progress: Callable[[float, str], None]
) -> tuple[int, str]:
    """Run the encode, reporting frames done over the source's total.

    `-progress pipe:1` emits key=value blocks on stdout; `frame=` is the one that maps
    onto the timeline. stderr is drained on a thread rather than left to fill: ffmpeg
    blocks when a pipe buffer fills, and an encode that stalls at 60% with no error is
    far harder to diagnose than one that fails outright.
    """
    try:
        total = float(frame_count(spec.source))
    except Exception:
        total = 0.0

    proc = subprocess.Popen(
        [*cmd[:1], "-progress", "pipe:1", "-nostats", *cmd[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    err_chunks: list[str] = []
    drain = threading.Thread(
        target=lambda: err_chunks.append(proc.stderr.read() or ""), daemon=True
    )
    drain.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if key != "frame" or not total:
            continue
        try:
            done = int(value)
        except ValueError:
            continue
        # Clamped: ffmpeg can report one more frame than the probe counted on a stream
        # whose last packet carries no picture, and a bar that reaches 101% reads as a bug.
        progress(min(done / total, 1.0), f"{done}/{int(total)} frames")

    rc = proc.wait()
    drain.join(timeout=2)
    if rc == 0:
        progress(1.0, "done")
    return rc, "".join(err_chunks)


def clear(bundle: Bundle) -> None:
    """Drop every proxy. Safe at any time -- proxies are derived state."""
    for p in sorted(bundle.proxy_dir.glob("*-proxy.*")):
        p.unlink(missing_ok=True)
