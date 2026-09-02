"""ffmpeg harness for the filtergraph generator tests.

The compilers are pure string generators, but a generated string ffmpeg rejects is
worthless, and every constraint they encode came from ffmpeg's own behaviour. So the
interesting assertions run the real binary rather than matching on the text.

Graphs always go through `-/filter_complex <file>` -- argv dies at ~288 KB with E2BIG,
and ffmpeg 9.0.1 removed `-filter_complex_script`, so the leading-slash "read this
argument from a file" form is the only remaining way to pass a large graph.

This lives outside conftest.py on purpose: conftest is shared with the rest of the
suite, and these helpers are only wanted by the three generator test modules.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")

FONTFILE = "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf"
needs_font = pytest.mark.skipif(
    not Path(FONTFILE).exists(), reason=f"{FONTFILE} not installed"
)

BLACK_64 = ["-f", "lavfi", "-i", "color=c=black:s=64x48:r=30"]
NULL_64 = ["-f", "lavfi", "-i", "nullsrc=s=64x48:r=30"]


def run_graph(
    graph: str,
    tmp_path: Path,
    *,
    inputs: list[list[str]] | None = None,
    maps: tuple[str, ...] | list[str] = ("[vout]",),
    frames: int | None = None,
    framehash: bool = False,
) -> subprocess.CompletedProcess:
    """Run one filtergraph to /dev/null. Returns the CompletedProcess."""
    gp = tmp_path / "graph.txt"
    gp.write_text(graph + "\n")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    for group in inputs or []:
        cmd += group
    cmd += ["-/filter_complex", str(gp)]
    for m in maps:
        cmd += ["-map", m]
    if frames is not None:
        cmd += ["-frames:v", str(frames)]
    cmd += ["-f", "framehash" if framehash else "null", "-"]
    return subprocess.run(cmd, capture_output=True, text=True)


def framehashes(
    graph: str,
    tmp_path: Path,
    *,
    inputs: list[list[str]] | None = None,
    maps: tuple[str, ...] | list[str] = ("[vout]",),
    frames: int | None = None,
) -> list[str]:
    """Per-frame content hashes of the mapped stream.

    Comparing hash *sequences* is how an off-by-one at a boundary becomes visible: a
    leaked or dropped frame shifts every hash after it, whereas the frame COUNT can stay
    correct -- which is exactly how the 318-in-899 boundary leak hid.
    """
    r = run_graph(
        graph, tmp_path, inputs=inputs, maps=maps, frames=frames, framehash=True
    )
    assert r.returncode == 0, r.stderr[-3000:]
    return [
        line.split(",")[-1].strip()
        for line in r.stdout.splitlines()
        if line and not line.startswith("#")
    ]


def make_counter_clip(path: Path, n_frames: int = 60, fps: int = 30) -> Path:
    """A lossless clip whose every frame is a different solid colour, plus a tone.

    Unique frames turn "did exactly the right frames survive the cut" into a hash
    comparison rather than a frame count, and the audio exercises concat's v=1:a=1.
    """
    dur = n_frames / fps
    graph = (
        f"color=c=black:s=64x64:r={fps}:d={dur:.6f},"
        f"geq=r='mod(N*4,256)':g='mod(N*7,256)':b='mod(N*11,256)',format=yuv444p[vout]"
    )
    gp = path.parent / "counter.txt"
    gp.write_text(graph + "\n")
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur:.6f}",
            "-/filter_complex", str(gp),
            "-map", "[vout]", "-map", "0:a",
            "-c:v", "ffv1", "-c:a", "pcm_s16le",
            "-frames:v", str(n_frames), str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return path
