"""A synthetic recording bundle, for tests and for smoke-running the editor.

Real capture needs a display, a camera and gsr; everything downstream of capture only
needs media with the right shape, so this builds one with lavfi. Kept out of the editor
and the library on purpose: nothing that ships should be able to fabricate a capture.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from omarchy_studio import project
from omarchy_studio.project import Capture, Stream

FPS = 30
ANCHOR_US = 1_000_000_000


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _lavfi(dst: Path, src: str, seconds: float, size: str, audio: bool) -> None:
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
           "-f", "lavfi", "-i", f"{src}=size={size}:rate={FPS}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-g", "15", "-bf", "0", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True)


def make_bundle(
    root: Path,
    *,
    seconds: float = 3.0,
    width: int = 1280,
    height: int = 720,
    camera: bool = True,
    burned_in: bool = False,
    media: bool = True,
    clicks: tuple[float, ...] = (0.6, 1.4, 1.5, 2.2),
) -> Path:
    """Lay out a bundle at `root` and return it. `media=False` skips the ffmpeg work."""
    root = Path(root)
    cap = Capture(
        created="2026-09-02T14:32:17",
        screen=Stream("media/screen.mp4", width, height, FPS, 1, ANCHOR_US, has_audio=True),
        camera=(
            Stream("media/camera.mp4", 640, 480, FPS, 1, ANCHOR_US + 120_000)
            if camera and not burned_in
            else None
        ),
        logical_geometry={"x": 200, "y": 100, "w": width // 2, "h": height // 2},
        physical_geometry={"x": 400, "y": 200, "w": width, "h": height},
        monitor_scale=2.0,
        monitor_name="DP-1",
        camera_burned_in=burned_in,
    )
    project.create(root, cap)
    if media:
        _lavfi(root / "media" / "screen.mp4", "testsrc2", seconds, f"{width}x{height}", True)
        if cap.camera is not None:
            _lavfi(root / "media" / "camera.mp4", "smptebars", seconds, "640x480", False)
    lines = [
        json.dumps({"t": ANCHOR_US / 1e6, "type": "meta", "schema": 1, "hz": 120.0}),
    ]
    for i, t in enumerate(clicks):
        lines.append(
            json.dumps(
                {
                    "t": ANCHOR_US / 1e6 + t,
                    "type": "click",
                    "button": "left",
                    # Logical pixels inside the captured region, which starts at 200,100.
                    "x": 200 + 100 + i * 40,
                    "y": 100 + 80 + i * 20,
                }
            )
        )
    lines.append(json.dumps({"t": ANCHOR_US / 1e6 + seconds, "type": "end", "clicks": len(clicks)}))
    (root / "events").mkdir(parents=True, exist_ok=True)
    (root / "events" / "input.jsonl").write_text("\n".join(lines) + "\n")
    return root


def make_proxies(root: Path) -> None:
    """Pretend the proxy build already ran, by copying the media across.

    Named the way omarchy_studio.proxy names them, since that is what the editor looks
    for; a fixture with its own convention would test nothing.
    """
    proxy = Path(root) / "proxy"
    proxy.mkdir(parents=True, exist_ok=True)
    for stream in ("screen", "camera"):
        src = Path(root) / "media" / f"{stream}.mp4"
        if src.exists():
            shutil.copy2(src, proxy / f"{stream}-proxy.mp4")


if __name__ == "__main__":  # used by the editor smoke test
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--burned-in", action="store_true")
    a = ap.parse_args()
    print(make_bundle(Path(a.root), seconds=a.seconds, burned_in=a.burned_in))
