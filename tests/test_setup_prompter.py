"""The setup picker must not hand its stdout to anything that outlives it.

THE BUG THIS EXISTS FOR. The picker's stdout IS the contract channel:
bin/omarchy-capture-screenrecording reads the configuration with
`setup_json=$(omarchy-capture-setup)`, and a command substitution ends at EOF -- which
needs EVERY writer to close, not just the one that was supposed to write. The Script
toggle spawns the teleprompter, which is deliberately built to outlive the picker, and
it inherited stdout. So: the user pressed Record, the countdown ran, the picker printed
its JSON and exited, and the recorder sat in anon_pipe_read forever having started
nothing. No bundle, no encoder, no error -- observed with the recorder's fd 3 and the
prompter's fd 1 on the same pipe inode.

The first test pins the fix. The second pins the PROPERTY, by actually leaking a
long-lived child across a pipe and showing the read never finishes -- because a test
that only checks a keyword argument stops meaning anything the moment somebody adds a
second spawn.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "omarchy-capture-setup"


@pytest.fixture(scope="module")
def setup_cli():
    spec = importlib.util.spec_from_loader(
        "omarchy_setup_cli",
        importlib.machinery.SourceFileLoader("omarchy_setup_cli", str(SCRIPT)),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["omarchy_setup_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_prompter_is_spawned_without_the_contract_stdout(setup_cli, monkeypatch):
    seen = {}

    class FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(setup_cli.subprocess, "Popen", fake_popen)
    session = setup_cli.SetupSession()
    assert session.set_prompter(True) is True

    assert "omarchy-teleprompter" in seen["argv"][0]
    # All three, not just stdout: stderr on the picker's stderr would still be a live
    # write end if anything ever reads that too, and an inherited stdin is a background
    # process fighting the terminal for input.
    assert seen["kwargs"]["stdout"] is subprocess.DEVNULL
    assert seen["kwargs"]["stderr"] is subprocess.DEVNULL
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    # It has to outlive the picker, which is the whole reason the leak was possible.
    assert seen["kwargs"]["start_new_session"] is True


def test_a_long_lived_child_holding_stdout_never_reaches_eof():
    """The property itself, demonstrated rather than asserted about.

    `$(...)` is a read to EOF. This is what the recorder was doing, and why a keyword
    argument on one Popen call is load-bearing.
    """
    leaky = (
        "import subprocess, sys, time\n"
        # The child outlives the parent and keeps the inherited stdout open.
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('CONFIG', flush=True)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", leaky], stdout=subprocess.PIPE)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            # communicate() reads to EOF exactly as a command substitution does. The
            # parent exits almost immediately; the pipe stays open regardless.
            proc.communicate(timeout=3)
    finally:
        proc.kill()
        proc.wait(timeout=5)

    tidy = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "                 stdin=subprocess.DEVNULL, start_new_session=True)\n"
        "print('CONFIG', flush=True)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", tidy], stdout=subprocess.PIPE)
    out, _ = proc.communicate(timeout=10)
    assert out.strip() == b"CONFIG"
