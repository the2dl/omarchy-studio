"""Run the QML interaction suites as part of `pytest tests/`.

The QML tests are written in QML and executed by Qt's own runner, so pytest cannot
collect their cases -- but a test suite that needs a second command to run is a suite
that stops being run. One pytest case per .qml file: the file is the unit, and its
failure output carries the individual case names.

THE RUNNER MUST BE Qt6'S, BY ABSOLUTE PATH. The `qmltestrunner` on PATH here belongs to
qt5-declarative and loads the Qt 5 QML engine, where every one of these files fails to
import -- and it reports that by exiting 1 having printed NOTHING AT ALL, which reads
exactly like a runner that ran no tests. That cost an evening once.

Offscreen because `when: windowShown` needs a window and CI has no display.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EDITOR = REPO / "editor"

# Qt6's runner sits outside PATH on Arch; the qt6-declarative package puts it here.
QMLTESTRUNNER = Path("/usr/lib/qt6/bin/qmltestrunner")

# `// qmltest: needs-bridge` opts a suite out: some of them drive a REAL bridge server
# rather than a stub, deliberately, and that server has to be started by hand. Marked in
# the .qml itself so the reason travels with the file rather than living in a name list
# here that nobody updates.
def _needs_bridge(path: Path) -> bool:
    return "qmltest: needs-bridge" in path.read_text(errors="replace")


SUITES = sorted(p for p in EDITOR.rglob("tst_*.qml") if not _needs_bridge(p))
MANUAL = sorted(p for p in EDITOR.rglob("tst_*.qml") if _needs_bridge(p))

needs_runner = pytest.mark.skipif(
    not QMLTESTRUNNER.exists(), reason=f"{QMLTESTRUNNER} not installed (qt6-declarative)"
)


@needs_runner
@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.relative_to(EDITOR).as_posix())
def test_qml_suite(suite: Path) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    r = subprocess.run(
        [str(QMLTESTRUNNER), "-input", str(suite)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    out = r.stdout + r.stderr
    # Both halves matter. A non-zero exit with no "Totals" line is the Qt5-runner
    # failure above; a zero exit with no cases is a file the runner could not collect,
    # which is just as silent.
    assert "Totals:" in out, f"runner produced no results for {suite.name}:\n{out}"
    assert r.returncode == 0, out
    assert ", 0 failed" in out, out


@needs_runner
def test_every_qml_suite_is_discovered() -> None:
    """A guard on the guard: if the glob stops matching, this file passes vacuously and
    every QML test silently stops running."""
    assert SUITES, "no tst_*.qml files found under editor/"
    # Every file is accounted for as either run or deliberately manual, so a suite
    # cannot go missing by being neither.
    assert len(SUITES) + len(MANUAL) == len(list(EDITOR.rglob("tst_*.qml")))


def test_the_qml_suites_are_not_run_by_the_qt5_runner_on_path() -> None:
    """Documents the trap rather than asserting a behaviour: `which qmltestrunner` finds
    Qt5's, which cannot load these files. Fails only if someone points the constant at a
    bare name and reintroduces the ambiguity."""
    assert QMLTESTRUNNER.is_absolute()
