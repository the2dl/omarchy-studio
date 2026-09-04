"""qmllint as a gate, on the one check that catches the bug this project keeps making.

`[missing-property]` is a member read off a type that does not have it. In QML that is
SILENT -- the expression is `undefined`, which is falsy, so a visibility binding just
stops showing something and a function call throws deep in a handler nobody is
watching. Nothing about the file looks wrong and `qml6` loads it happily.

It cost three separate bugs in two days:

  * `root.camNow` where camNow was declared inside `content`, so the preview showed
    the global webcam through every pad;
  * `root.cameraSync` where cameraSync is an ID, which threw a TypeError at the exact
    moment an intro handed over to the recording;
  * `parent.parent.picked` / `parent.parent.sel`, level counts that any wrapper breaks.

qmllint found all three in seconds. It is now a test.

THE RUNNER MUST BE QT6'S, BY ABSOLUTE PATH. `/usr/bin/qmllint` is qt5-declarative's and
reports none of this -- the same trap as qmltestrunner, and the reason this check was
believed to be useless here for so long.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EDITOR = REPO / "editor"
QMLLINT = Path("/usr/lib/qt6/bin/qmllint")

# tst_*.qml are harnesses: they instantiate components with stub data and reach into
# internals on purpose, which is exactly the shape qmllint flags. The production QML is
# what has to be clean.
SOURCES = sorted(p for p in EDITOR.rglob("*.qml") if not p.name.startswith("tst_"))

needs_lint = pytest.mark.skipif(
    not QMLLINT.exists(), reason=f"{QMLLINT} not installed (qt6-declarative)"
)


def _lint() -> str:
    r = subprocess.run(
        [str(QMLLINT), "-I", str(EDITOR), *map(str, SOURCES)],
        cwd=REPO, capture_output=True, text=True, timeout=300,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
    )
    return r.stdout + r.stderr


@needs_lint
def test_no_member_is_read_off_a_type_that_lacks_it() -> None:
    out = _lint()
    bad = [l for l in out.splitlines() if "[missing-property]" in l]
    assert not bad, (
        "qmllint found members read off types that do not have them. In QML these are "
        "silent -- undefined is falsy -- so this is the class that loads clean and "
        "misbehaves:\n  " + "\n  ".join(bad)
    )


@needs_lint
def test_the_gate_is_actually_looking_at_the_editor() -> None:
    """A guard on the guard: a glob that stops matching makes the test above pass
    vacuously and the check silently stops running."""
    assert len(SOURCES) > 40, f"only {len(SOURCES)} qml files found; the glob broke"
    assert any(p.name == "Preview.qml" for p in SOURCES)


@needs_lint
def test_qmllint_still_reports_the_bug_it_is_here_to_catch(tmp_path) -> None:
    """The check itself has to keep working. If a Qt upgrade renames or drops
    missing-property, the gate above would go quiet rather than fail, and that is the
    failure mode this whole file exists to prevent.
    """
    bug = tmp_path / "Bug.qml"
    bug.write_text(
        "import QtQuick\n"
        "Item {\n"
        "    id: root\n"
        "    Item { readonly property int inner: 1 }\n"
        "    Text { text: root.inner }\n"      # declared on the child, read off root
        "}\n"
    )
    r = subprocess.run([str(QMLLINT), str(bug)], capture_output=True, text=True, timeout=60)
    assert "[missing-property]" in (r.stdout + r.stderr), (
        "qmllint no longer reports a member read off a type that lacks it; the gate in "
        "this file is no longer checking anything"
    )
