from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import needs_ffmpeg

from omarchy_studio import probe, proxy
from omarchy_studio.project import Bundle, ProjectError

W, H, SECONDS = 320, 240, 2.0


@pytest.fixture
def bundle(tmp_path) -> Bundle:
    root = tmp_path / "rec"
    synthetic.make_bundle(root, seconds=SECONDS, width=W, height=H)
    return Bundle(root)


def keyframe_positions(path: Path) -> list[int]:
    # `default=nw=1:nk=1` rather than `csv=p=0`: csv appends a stray comma to the first
    # row, which silently drops frame 0 -- always a keyframe -- from the result.
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "frame=key_frame", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return [i for i, v in enumerate(out) if v == "1"]


@needs_ffmpeg
def test_a_proxy_is_generated_with_a_short_gop(bundle):
    """Short GOP is what makes the preview seekable: 517-651 ms per seek on the master
    with half the seeks never delivering a frame, against 15-53 ms with every seek
    landing. The downscale is incidental."""
    p = proxy.ensure_proxy(bundle)
    assert p.exists()
    keys = keyframe_positions(p)
    total = probe.frame_count(p)
    assert len(keys) >= total // proxy.GOP
    assert max(b - a for a, b in zip(keys, keys[1:])) <= proxy.GOP


@needs_ffmpeg
def test_the_proxy_keeps_every_frame_of_the_master(bundle):
    p = proxy.ensure_proxy(bundle)
    master = bundle.media("screen.mp4")
    assert probe.frame_count(p) == probe.frame_count(master)


@needs_ffmpeg
def test_a_fresh_proxy_is_reused_rather_than_rebuilt(bundle):
    first = proxy.ensure_proxy(bundle)
    stamp = first.stat().st_mtime_ns
    assert proxy.is_stale(bundle) is False
    second = proxy.ensure_proxy(bundle)
    assert second == first
    assert second.stat().st_mtime_ns == stamp


@needs_ffmpeg
def test_a_proxy_of_different_media_is_stale(bundle):
    proxy.ensure_proxy(bundle)
    assert proxy.is_stale(bundle) is False
    # media/ is immutable, so a fingerprint mismatch means this proxy belongs to a
    # different recording -- which is a correctness problem, not a staleness one.
    master = bundle.media("screen.mp4")
    master.write_bytes(master.read_bytes() + b"\0")
    assert proxy.is_stale(bundle) is True


@needs_ffmpeg
def test_a_missing_proxy_is_stale_and_a_corrupt_stamp_is_too(bundle):
    assert proxy.is_stale(bundle) is True
    proxy.ensure_proxy(bundle)
    (bundle.proxy_dir / "screen-proxy.json").write_text("{not json")
    assert proxy.is_stale(bundle) is True


@needs_ffmpeg
def test_a_camera_proxy_is_not_upscaled(bundle):
    """The camera is already below the proxy width; scaling it up would cost decode
    time to gain nothing. The GOP is the point."""
    p = proxy.ensure_proxy(bundle, "camera")
    assert probe.dimensions(p) == probe.dimensions(bundle.media("camera.mp4"))


@needs_ffmpeg
def test_a_wide_master_is_brought_down_to_the_proxy_width(tmp_path):
    root = tmp_path / "wide"
    synthetic.make_bundle(root, seconds=1.0, width=2560, height=1440, camera=False)
    p = proxy.ensure_proxy(Bundle(root))
    assert probe.dimensions(p) == (proxy.PROXY_WIDTH, 1080)


@needs_ffmpeg
def test_clear_drops_every_proxy(bundle):
    proxy.ensure_proxy(bundle)
    proxy.ensure_proxy(bundle, "camera")
    proxy.clear(bundle)
    assert list(bundle.proxy_dir.glob("*-proxy.*")) == []
    assert proxy.is_stale(bundle) is True


def test_proxying_a_stream_the_capture_does_not_have_is_an_error(tmp_path):
    root = tmp_path / "nocam"
    synthetic.make_bundle(root, seconds=1.0, width=W, height=H, camera=False, media=False)
    with pytest.raises(ProjectError):
        proxy.ensure_proxy(Bundle(root), "camera")


def test_a_missing_media_file_is_reported_rather_than_encoded(tmp_path):
    root = tmp_path / "nomedia"
    synthetic.make_bundle(root, seconds=1.0, width=W, height=H, media=False)
    with pytest.raises(proxy.ProxyError):
        proxy.ensure_proxy(Bundle(root))
