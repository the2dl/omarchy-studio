"""Attacks that worked, pinned so they cannot come back.

Every case here was CONFIRMED against this code: a crafted `edit.json` read a file the
victim could read and painted it into the exported video, which the victim would then
share. The bundle is the attack surface -- `Bundle(root)` loads edit.json verbatim and
`build_graph` never validates it -- so "someone sent me a recording" is enough.

Threat model: a single-user Linux desktop. What matters is code execution, reading or
writing outside the project, and one local account affecting another. "The user can edit
their own project" is not a finding and nothing here tests for it.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest
import synthetic
from ffmpeg_harness import needs_ffmpeg

from omarchy_studio import layers as layers_mod, render
from omarchy_studio.exprs import escape_filter_value
from omarchy_studio.project import Bundle, Edit, Layer

FONT = "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf"


def _bundle(tmp_path, name="rec"):
    root = tmp_path / name
    synthetic.make_bundle(root, seconds=1.0, width=320, height=180, camera=False)
    return Bundle(root)


def _graph(bundle) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return render.build_graph(bundle).graph


# --- filtergraph injection ---------------------------------------------------


@needs_ffmpeg
def test_a_fontfile_cannot_append_filters(tmp_path):
    """THE headline vulnerability, confirmed end to end before the fix: a ',' in the
    fontfile ends the drawtext and starts another, and drawtext's `textfile=` renders a
    file's CONTENTS into the frame. A canary file came back out of the exported MP4,
    readable by OCR.
    """
    b = _bundle(tmp_path)
    b.edit.layers = [Layer(id="t", type="text", x=0.0, y=0.0, w=1.0, h=1.0, props={
        "text": "decoy",
        "fontfile": f"{FONT}:text=X,drawtext=textfile=/etc/passwd:x=0:y=0"})]
    g = _graph(b)
    assert "textfile=" not in g
    assert "/etc/passwd" not in g


@needs_ffmpeg
@pytest.mark.parametrize("prop", ["color", "box_color"])
def test_a_colour_cannot_append_filters(tmp_path, prop):
    """A colour is a closed vocabulary, so it is VALIDATED rather than escaped --
    escaping would pass nonsense through for ffmpeg to interpret later."""
    b = _bundle(tmp_path, f"c{prop}")
    b.edit.layers = [Layer(id="t", type="text", x=0.0, y=0.0, w=1.0, h=1.0, props={
        "text": "decoy", prop: "white,drawtext=textfile=/etc/passwd:x=5:y=5"})]
    g = _graph(b)
    assert "textfile=" not in g and "/etc/passwd" not in g


@needs_ffmpeg
def test_a_caption_style_cannot_append_filters(tmp_path):
    """Captions reuse the text layer's prop spelling, so they inherited the identical
    hole -- and a caption track comes from transcription, not from typing."""
    b = _bundle(tmp_path, "cap")
    b.edit.layers = [Layer(id="c", type="caption", x=0.0, y=0.0, w=1.0, h=1.0, props={
        "cues": [{"start_ms": 0, "end_ms": 500, "text": "hello"}],
        "fontfile": f"{FONT}:text=X,drawtext=textfile=/etc/passwd",
        "color": "white,drawtext=textfile=/etc/passwd"})]
    g = _graph(b)
    assert "textfile=" not in g and "/etc/passwd" not in g


def test_shape_colours_are_validated():
    assert layers_mod.safe_color("#1b1d24") == "#1b1d24"
    assert layers_mod.safe_color("red@0.5") == "red@0.5"
    for hostile in ("white,drawtext=textfile=/etc/passwd", "a:b", "x'y", "c[d]",
                    "e;f", "g=h", "back\\slash"):
        got = layers_mod.safe_color(hostile)
        assert not any(ch in got for ch in ",:;='[]\\"), got


def test_a_font_must_be_a_real_file_and_is_escaped():
    """Both halves matter: escaping alone still lets a project point at any file on
    disk, and requiring a file alone still lets ':' and ',' append filters."""
    assert layers_mod.safe_fontfile(FONT).endswith("Regular.ttf")
    assert "textfile" not in layers_mod.safe_fontfile(f"{FONT}:x,drawtext=textfile=/etc/passwd")
    assert layers_mod.safe_fontfile("/no/such/font.ttf").endswith(
        Path(layers_mod.DEFAULT_FONTFILE).name)


def test_the_filter_escaper_neutralises_every_separator():
    got = escape_filter_value("a,b:c;d=e'f[g]")
    for ch in ",:;='[]":
        assert f"\\{ch}" in got


# --- arbitrary ffmpeg inputs -------------------------------------------------


@needs_ffmpeg
def test_an_image_path_cannot_name_an_arbitrary_protocol(tmp_path):
    """No -protocol_whitelist is set, so an attacker-chosen `path` was not merely an
    arbitrary local read: an http:// input makes the export fetch a URL from the
    victim's machine."""
    b = _bundle(tmp_path, "proto")
    b.edit.layers = [Layer(id="i", type="image", x=0.1, y=0.1, w=0.2, h=0.2,
                           props={"path": "http://169.254.169.254/latest/meta-data/"})]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        inputs = render.build_graph(b).inputs
    assert not any(a.startswith("http") for a in inputs)


@needs_ffmpeg
def test_an_asset_name_cannot_escape_the_bundle(tmp_path):
    b = _bundle(tmp_path, "trav")
    b.edit.layers = [Layer(id="i", type="image", x=0.1, y=0.1, w=0.2, h=0.2,
                           props={"asset": "../../../../../../etc/passwd"})]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        inputs = render.build_graph(b).inputs
    assert not any("/etc/passwd" in a for a in inputs)


# --- resource bounds ---------------------------------------------------------


def test_a_pad_cannot_ask_for_ten_billion_frames():
    """The length went straight into -frames:v, unbounded, so one line of edit.json
    encoded until the disk filled."""
    assert Edit.from_dict({"tail_pad_frames": 10_000_000_000}).tail_pad_frames <= 60 * 60 * 60
    assert Edit.from_dict({"head_pad_frames": "nonsense"}).head_pad_frames == 0


# --- the token file's own hygiene --------------------------------------------


def test_the_token_file_is_private_and_swept(tmp_path, monkeypatch):
    """A file per launch, forever, is a defect even when none of them leaks.

    Moving the token off argv introduced exactly that: 68 files piled up in the runtime
    directory in a day of testing, each holding a token for a session that had ended.
    Launchers unlink their own; this sweep is the backstop for a crash or a kill -9.
    """
    import os
    import stat
    import time as _time

    from omarchy_studio import qmlbridge

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = qmlbridge.write_token_file("a-token")
    assert path.parent == tmp_path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, "the token file is not private"
    assert path.read_text() == "a-token"

    # A file older than any plausible session is collected on the next write.
    stale = tmp_path / "omarchy-studio-token-ancient"
    stale.write_text("old")
    old = _time.time() - 48 * 60 * 60
    os.utime(stale, (old, old))
    qmlbridge.write_token_file("another")
    assert not stale.exists(), "a stale token file survived the sweep"
    assert path.exists(), "the sweep removed a token file that is still in use"
