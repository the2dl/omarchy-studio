"""Showing a finished export in the file manager the user actually chose.

Two mechanisms exist and they disagree on the machine this was built for.
`xdg-open` on the folder honours the user's real default for inode/directory;
org.freedesktop.FileManager1.ShowItems SELECTS the file, which is what "reveal"
means everywhere else -- but it is a D-Bus name owned by whichever file manager
claimed it at install time.

Here `xdg-mime query default inode/directory` answers flea and the FileManager1
service file says nautilus. Reaching for ShowItems because it is the "proper" API
would open a file manager the user does not use, which they had already reported
once against the image picker. So selecting is conditional and opening the folder is
the floor.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from omarchy_studio import reveal as R

FLEA = "com.thisisgm.flea.desktop"


@pytest.fixture
def spawned(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(R, "_spawn", lambda argv: calls.append(argv))
    monkeypatch.setattr(R.shutil, "which", lambda n: f"/usr/bin/{n}")
    return calls


def _fake_env(monkeypatch, *, default: str, owner: str) -> None:
    monkeypatch.setattr(R, "default_file_manager", lambda: default)
    monkeypatch.setattr(R, "_filemanager1_binary", lambda: owner)


# --- the machine this was built for ------------------------------------------


def test_a_foreign_filemanager1_owner_does_not_get_the_reveal(tmp_path, spawned, monkeypatch):
    """The reported bug, as a test: flea is the default, nautilus owns the D-Bus
    name, and reaching for ShowItems would open nautilus."""
    _fake_env(monkeypatch, default=FLEA, owner="nautilus")
    f = tmp_path / "export.mp4"
    f.write_bytes(b"")
    assert R.reveal(f) == "folder"
    assert spawned == [["xdg-open", str(tmp_path)]]


def test_the_matching_owner_gets_to_select_the_file(tmp_path, spawned, monkeypatch):
    """When the two agree, the user gets the better behaviour -- the file selected
    rather than a folder they still have to scan."""
    _fake_env(monkeypatch, default="org.gnome.Nautilus.desktop", owner="nautilus")
    f = tmp_path / "export.mp4"
    f.write_bytes(b"")
    assert R.reveal(f) == "selected"
    argv = spawned[0]
    assert argv[0] == "dbus-send"
    assert "org.freedesktop.FileManager1.ShowItems" in argv
    assert f"array:string:{f.resolve().as_uri()}" in argv


def test_an_unknown_owner_falls_back_rather_than_guessing(tmp_path, spawned, monkeypatch):
    _fake_env(monkeypatch, default=FLEA, owner="")
    f = tmp_path / "export.mp4"
    f.write_bytes(b"")
    assert R.reveal(f) == "folder"


# --- the floor ---------------------------------------------------------------


def test_a_directory_opens_rather_than_selecting_itself(tmp_path, spawned, monkeypatch):
    _fake_env(monkeypatch, default="org.gnome.Nautilus.desktop", owner="nautilus")
    assert R.reveal(tmp_path) == "folder"
    assert spawned == [["xdg-open", str(tmp_path)]]


def test_a_path_that_is_not_there_opens_nothing(tmp_path, spawned, monkeypatch):
    _fake_env(monkeypatch, default=FLEA, owner="nautilus")
    assert R.reveal(tmp_path / "gone" / "x.mp4") == "missing"
    assert spawned == []


def test_no_xdg_open_is_not_an_exception(tmp_path, monkeypatch):
    """This runs after a render has already succeeded. Failing to open a window must
    not turn a finished export into an error."""
    _fake_env(monkeypatch, default=FLEA, owner="nautilus")
    monkeypatch.setattr(R.shutil, "which", lambda n: None)
    f = tmp_path / "export.mp4"
    f.write_bytes(b"")
    assert R.reveal(f) == "unavailable"


def test_a_spawn_that_throws_is_swallowed(tmp_path, monkeypatch):
    _fake_env(monkeypatch, default=FLEA, owner="nautilus")
    monkeypatch.setattr(R.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(R, "_spawn", lambda argv: (_ for _ in ()).throw(OSError("nope")))
    f = tmp_path / "export.mp4"
    f.write_bytes(b"")
    assert R.reveal(f) == "failed"


# --- reading the service file ------------------------------------------------


def test_the_owner_is_read_from_disk_not_asked_over_the_bus(tmp_path, monkeypatch):
    """Asking D-Bus who owns the name ACTIVATES it, which would launch a file manager
    just to find out whether we wanted that one."""
    svc = tmp_path / "org.freedesktop.FileManager1.service"
    svc.write_text("[D-BUS Service]\n"
                   "Name=org.freedesktop.FileManager1\n"
                   "Exec=/usr/bin/nautilus --gapplication-service\n")
    monkeypatch.setattr(R, "_SERVICE_FILES", (str(svc),))
    assert R._filemanager1_binary() == "nautilus"


def test_a_missing_service_file_is_not_an_error(monkeypatch):
    monkeypatch.setattr(R, "_SERVICE_FILES", ("/nonexistent/x.service",))
    assert R._filemanager1_binary() == ""
