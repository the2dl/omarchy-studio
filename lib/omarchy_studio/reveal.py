"""Showing a finished file to the user, in the file manager they actually chose.

There are two ways to do this on a freedesktop system and they disagree on this
machine, which is the whole reason this module exists rather than one `xdg-open`
call at each site:

  `xdg-open <dir>`  opens the handler registered for inode/directory -- the user's
                    real default. It cannot select a file, only open its folder.

  org.freedesktop.FileManager1.ShowItems  SELECTS the file, which is what "reveal"
                    means everywhere else. But it is a D-Bus activated name owned by
                    whichever file manager claimed it at install time, which is not
                    necessarily the user's default at all.

On this machine `xdg-mime query default inode/directory` answers flea while
/usr/share/dbus-1/services/org.freedesktop.FileManager1.service says nautilus. Going
straight to ShowItems would open a file manager the user does not use -- a bug they
had already reported once, against the image picker.

So: select the file when the D-Bus owner IS the user's default, and otherwise fall
back to opening the folder in the default. Better framing in the right app beats
perfect framing in the wrong one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


def _spawn(argv: list[str]) -> None:
    """Fire and forget, detached: the file manager must outlive us, and its exit
    status is not ours to care about."""
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _run(argv: list[str], timeout: float = 2.0) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip()


def default_file_manager() -> str:
    """The .desktop id handling directories, e.g. "com.thisisgm.flea.desktop"."""
    return _run(["xdg-mime", "query", "default", "inode/directory"])


_EXEC_RE = re.compile(r"^Exec=(\S+)", re.MULTILINE)

_SERVICE_FILES = (
    "/usr/share/dbus-1/services/org.freedesktop.FileManager1.service",
    "/usr/local/share/dbus-1/services/org.freedesktop.FileManager1.service",
)


def _filemanager1_binary() -> str:
    """The program D-Bus would start for org.freedesktop.FileManager1, or "".

    Read from the service file rather than asked over the bus on purpose: asking
    ACTIVATES it, which would launch a file manager just to find out whether we
    wanted that one.
    """
    for path in _SERVICE_FILES:
        try:
            m = _EXEC_RE.search(Path(path).read_text())
        except OSError:
            continue
        if m:
            return Path(m.group(1)).name
    return ""


def _selecting_is_safe() -> bool:
    """Whether ShowItems would land in the user's own file manager.

    Matched on the binary name against the desktop id, which is loose -- but it errs
    toward the fallback, and the fallback is correct rather than merely different.
    """
    owner = _filemanager1_binary()
    if not owner:
        return False
    desktop = default_file_manager().removesuffix(".desktop").lower()
    return bool(desktop) and owner.lower() in desktop.replace("-", ".").split(".")


def reveal(path: str | Path) -> str:
    """Show `path` to the user. Returns how it was shown, for logs and tests.

    Never raises: this runs after a render has already succeeded, and failing to
    open a window must not turn a finished export into an error.
    """
    # An escape hatch that the test suite sets, because this function's whole job is a
    # side effect on someone's desktop. Running the suite opened a real file-manager
    # window -- Flea, on workspace 2 -- and left it there along with an orphaned
    # xdg-open. A test must not reach out of the process and put a window in front of
    # the person running it.
    if os.environ.get("OMARCHY_STUDIO_NO_REVEAL"):
        return "suppressed"

    p = Path(path)
    folder = p if p.is_dir() else p.parent
    try:
        if not folder.exists():
            return "missing"
        if not p.is_dir() and _selecting_is_safe() and shutil.which("dbus-send"):
            _spawn([
                "dbus-send", "--session", "--print-reply=literal",
                "--dest=org.freedesktop.FileManager1",
                "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1.ShowItems",
                f"array:string:{p.resolve().as_uri()}", "string:",
            ])
            return "selected"
        if shutil.which("xdg-open"):
            _spawn(["xdg-open", str(folder)])
            return "folder"
        return "unavailable"
    except Exception:
        return "failed"
