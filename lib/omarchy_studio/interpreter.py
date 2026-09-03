"""Re-exec a bin/ entry point into the repo's virtualenv.

WHY THIS EXISTS. The scripts in bin/ are launched by their shebang, which is the system
python, but the project's third-party dependencies live in the repo's .venv -- the
speech-to-text engine most visibly. Without this, a checkout that HAS faster-whisper
installed still reports "no local speech-to-text engine found" and sends the user off to
install it a second time.

COMPARED BY sys.prefix, NOT BY RESOLVING THE INTERPRETER PATH. A venv's bin/python is a
symlink to the system one, so realpath() says the two are the same file and the re-exec
never fires -- while running it through the symlink is precisely what puts the venv's
site-packages on the path. That check cost a debugging round the first time.

The bash entry points do the same thing with STUDIO_PYTHON (see
bin/omarchy-capture-screenrecording); OMARCHY_STUDIO_PYTHON overrides both.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["reexec_into_venv"]

_GUARD = "OMARCHY_STUDIO_REEXEC"


def reexec_into_venv(script: str, repo: Path) -> None:
    """Replace this process with the same script under .venv/bin/python, if that helps.

    Returns normally -- and does nothing -- when there is no venv, when we are already
    inside it, or when the guard says we have been here before. Never raises: a failure
    to upgrade the interpreter must not stop the tool from running on the one it has.
    """
    if os.environ.get(_GUARD):
        return
    venv_dir = repo / ".venv"
    python = os.environ.get("OMARCHY_STUDIO_PYTHON") or str(venv_dir / "bin" / "python")
    if not os.path.exists(python):
        return
    try:
        if Path(sys.prefix).resolve() == venv_dir.resolve():
            return
    except OSError:
        return
    os.environ[_GUARD] = "1"
    try:
        os.execv(python, [python, os.path.abspath(script), *sys.argv[1:]])
    except OSError:
        # An unexecutable venv is a broken checkout, not a reason to refuse to run.
        os.environ.pop(_GUARD, None)
