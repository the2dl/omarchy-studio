"""An export renders what the user is looking at.

The renderer runs in a SEPARATE PROCESS. It is handed the bundle's path and nothing
else, so it opens edit.json off disk -- while every edit the editor makes lives in
memory until /save. Exporting without saving therefore rendered the last SAVED edit,
which for a recording nobody had explicitly saved is the one `begin` wrote: camera at
the setup placement, zoom off, no layers, default backdrop.

That is exactly what it looked like from the user's chair -- "where I positioned my
camera did not export, held bottom right; zoom did not export; the background didn't
export either" -- and it is why the symptom hit every feature at once rather than any
one of them. Nothing was broken in the renderer; it was reading a different edit.
"""
from __future__ import annotations

import json

import pytest
import synthetic

from omarchy_studio import qmlbridge
from omarchy_studio.project import Bundle, Edit

REPO = qmlbridge.Path(__file__).resolve().parents[1]


@pytest.fixture
def session(tmp_path):
    synthetic.make_bundle(tmp_path / "rec", seconds=1.0, width=640, height=360,
                          camera=True, media=False)
    return qmlbridge.Session(Bundle(tmp_path / "rec"), REPO)


def _on_disk(session) -> Edit:
    return Edit.from_dict(json.loads(session.bundle.edit_path.read_text()))


def test_an_edit_is_in_memory_until_something_saves_it(session):
    """The premise. Not a bug on its own -- it is why /save exists."""
    session.op("set_webcam", {"rect": {"x": 64, "y": 36, "width": 96, "height": 96}})
    assert session.bundle.edit.webcam.x == pytest.approx(0.1)
    assert session.dirty is True


def test_exporting_saves_first(session):
    """Without this the child process renders the placement the user replaced."""
    session.op("set_webcam", {"rect": {"x": 64, "y": 36, "width": 96, "height": 96}})
    session.export(None)
    assert _on_disk(session).webcam.x == pytest.approx(0.1)
    assert session.dirty is False, "and the UI stops claiming unsaved work"


def test_every_edit_reaches_the_export_not_just_the_camera(session):
    """The report named the camera, the zoom and the backdrop. One cause, so one fix
    has to cover all of them -- and the layers nobody had checked yet."""
    session.op("set_webcam", {"rect": {"x": 128, "y": 108, "width": 96, "height": 96}})
    session.op("set_zoom", {"enabled": True})
    session.op("set_backdrop", {"color": "#123456"})
    session.op("add_text", {"rect": {"x": 10, "y": 10, "width": 200, "height": 60}})
    session.export(None)
    disk = _on_disk(session)
    assert disk.webcam.x == pytest.approx(0.2)
    assert disk.zoom.enabled is True
    assert disk.backdrop.color == "#123456"
    assert [l.type for l in disk.layers] == ["text"], "layers too -- same file"


def test_the_export_still_starts_when_the_edit_is_already_saved(session):
    session.op("set_webcam", {"rect": {"x": 256, "y": 36, "width": 96, "height": 96}})
    session.save()
    assert session.dirty is False
    session.export(None)
    assert _on_disk(session).webcam.x == pytest.approx(0.4)


def test_a_failed_save_does_not_silently_export_the_old_edit(session, monkeypatch):
    """Rendering the wrong thing is worse than not rendering: a stale export looks
    finished and is wrong, and the user has no reason to look twice."""
    def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(session.bundle, "save_edit", boom)
    session.op("set_webcam", {"rect": {"x": 320, "y": 36, "width": 96, "height": 96}})
    with pytest.raises(OSError):
        session.export(None)
    assert session.exporter.snapshot()["state"] != "running"


# --- and the same defect on the way out --------------------------------------


def test_closing_the_editor_keeps_the_work(session, tmp_path):
    """Same cause as the export bug: the edit is in memory, and /quit only set an
    event. Move the camera, close the window, and the afternoon was gone -- with the
    master untouched the whole time, so there was nothing destructive to protect
    against by discarding it."""
    import urllib.request

    srv = qmlbridge.serve(session, 0)
    try:
        session.op("set_webcam", {"rect": {"x": 64, "y": 36, "width": 96, "height": 96}})
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_port}/quit", data=b"{}",
            headers={"X-Studio-Token": session.token,
                     "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req).read()
        assert session.quit_requested.is_set()
        assert _on_disk(session).webcam.x == pytest.approx(0.1)
    finally:
        srv.shutdown()


def test_a_failed_save_still_lets_the_window_close(session, monkeypatch, capsys):
    """Trapping the user in a window they asked to close is worse than the failure."""
    import urllib.request

    def boom() -> None:
        raise OSError("disk full")

    srv = qmlbridge.serve(session, 0)
    try:
        session.op("set_webcam", {"rect": {"x": 64, "y": 36, "width": 96, "height": 96}})
        monkeypatch.setattr(session.bundle, "save_edit", boom)
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_port}/quit", data=b"{}",
            headers={"X-Studio-Token": session.token,
                     "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req).read()
        assert session.quit_requested.is_set()
    finally:
        srv.shutdown()


# --- and it shows you the file when it lands ---------------------------------


def _finished(session, returncode: int, monkeypatch, hook=None) -> list:
    """Drive Exporter._pump against a stub renderer that exits `returncode`."""
    revealed: list = []
    monkeypatch.setattr(qmlbridge.reveal_mod, "reveal",
                        hook or (lambda p: revealed.append(str(p))))

    class FakeProc:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = iter(["frame=1\n", "progress=end\n"])
            self.stderr = _Err()

        def wait(self) -> None:
            pass

    class _Err:
        def read(self) -> str:
            return "boom" if returncode else ""

    session.exporter._proc = FakeProc()
    session.exporter._set(state="running", output="/tmp/out.mp4")
    session.exporter._pump(1)
    return revealed


def test_a_finished_export_opens_the_folder(session, monkeypatch):
    """The convention everywhere else, and the reason it is on by default."""
    assert _finished(session, 0, monkeypatch) == ["/tmp/out.mp4"]
    assert session.exporter.snapshot()["state"] == "done"


def test_a_failed_export_opens_nothing(session, monkeypatch):
    assert _finished(session, 1, monkeypatch) == []
    assert session.exporter.snapshot()["state"] == "error"


def test_reveal_can_be_turned_off(session, monkeypatch):
    """A batch export opening a file manager per file would be an assault."""
    session.exporter.reveal_on_done = False
    assert _finished(session, 0, monkeypatch) == []
    assert session.exporter.snapshot()["state"] == "done"


def test_the_export_is_done_before_anything_is_opened(session, monkeypatch):
    """A file manager that failed to open must not make a finished export look
    unfinished, so `done` is set first and reveal never touches the state."""
    seen: list = []
    _finished(session, 0, monkeypatch,
              hook=lambda p: seen.append(session.exporter.snapshot()["state"]))
    assert seen == ["done"]
