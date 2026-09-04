"""Stopping exactly the processes this recording started, and nothing else.

The recorder used to find its own processes by pattern -- `pkill -f "^gpu-screen-
recorder"`, `pkill -f "WebcamOverlay"` -- because the stop path is a second invocation
of the script with none of the shell variables that knew their pids. Pattern matching
kills a second recording by the same user, kills anything that merely has the string in
its argv, and interpolates bundle paths into the pattern.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omarchy_studio import supervisor


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _reap(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.kill()
    p.wait(timeout=10)


def test_a_recorded_child_is_recognised_as_alive(tmp_path):
    p = _sleeper()
    try:
        state = tmp_path / "rec.json"
        child = supervisor.record(state, "gsr", p.pid, bundle="/tmp/b")
        assert child is not None and child.alive()
        assert [c.name for c in supervisor.living(state)] == ["gsr"]
        assert supervisor.load(state).bundle == "/tmp/b"
    finally:
        _reap(p)


def test_a_dead_child_is_not_recorded_as_running(tmp_path):
    """A process that dies during startup must never be written as if it were up, or a
    later stop waits for something that was never there."""
    p = _sleeper()
    p.kill()
    p.wait(timeout=10)
    assert supervisor.record(tmp_path / "rec.json", "gsr", p.pid) is None


def test_a_recycled_pid_is_not_mistaken_for_the_original(tmp_path):
    """The one way this could be WORSE than pattern matching: a stale state file plus a
    reused pid means signalling something else entirely. Identity is (pid, start time),
    and the start time of a recycled pid differs.
    """
    p = _sleeper()
    state = tmp_path / "rec.json"
    supervisor.record(state, "gsr", p.pid)
    _reap(p)

    # Same pid, different process: forge the start time the way reuse would.
    rec = supervisor.load(state)
    rec.children[0].started += 1
    supervisor.save(state, rec)

    assert supervisor.living(state) == []
    assert supervisor.signal_all(state, signal.SIGTERM) == []


def test_only_the_named_children_are_signalled(tmp_path):
    """The stop sequence is not uniform -- gsr needs SIGINT to finalise its file while
    the fan-out can just be terminated -- so it has to be able to signal a subset."""
    keep, go = _sleeper(), _sleeper()
    try:
        state = tmp_path / "rec.json"
        supervisor.record(state, "gsr", keep.pid)
        supervisor.record(state, "fanout", go.pid)

        assert supervisor.signal_all(state, signal.SIGTERM, names=("fanout",)) == ["fanout"]
        assert supervisor.wait_gone(state, 10, names=("fanout",))
        assert keep.poll() is None, "the wrong process was signalled"
    finally:
        _reap(keep)
        _reap(go)


def test_an_unrelated_process_with_a_matching_name_is_untouched(tmp_path):
    """The actual bug: `pkill -f gpu-screen-recorder` reaches anything whose command
    line contains that string, including a second recording and an editor holding the
    file open. Identity is a recorded pid, so a look-alike is not a candidate at all.
    """
    lookalike = subprocess.Popen(
        [sys.executable, "-c",
         "import time; time.sleep(120)  # gpu-screen-recorder WebcamOverlay cam.tsv"])
    mine = _sleeper()
    try:
        state = tmp_path / "rec.json"
        supervisor.record(state, "gsr", mine.pid)
        supervisor.signal_all(state, signal.SIGKILL)
        supervisor.wait_gone(state, 10)
        assert lookalike.poll() is None, "a process we do not own was killed"
    finally:
        _reap(lookalike)
        _reap(mine)


def test_a_missing_or_corrupt_state_file_is_empty_not_fatal(tmp_path):
    """A stop that cannot read the state still has to reach its fallback rather than
    abort with the recording left running."""
    assert supervisor.load(tmp_path / "nope.json").children == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert supervisor.load(bad).children == []


def test_the_state_is_written_atomically(tmp_path):
    """A stop reading mid-write must see the previous set, never half of the new one."""
    state = tmp_path / "rec.json"
    p = _sleeper()
    try:
        supervisor.record(state, "gsr", p.pid)
        for _ in range(20):
            supervisor.record(state, "hud", os.getpid())
            supervisor.load(state)          # would raise on a torn file
        assert {c.name for c in supervisor.living(state)} == {"gsr", "hud"}
    finally:
        _reap(p)


def test_a_zombie_is_not_running(tmp_path):
    """A signalled child stays in /proc with the same start time until its parent reaps
    it. Counting that as alive made wait_gone sit out its whole timeout on a corpse --
    which in the recorder is the difference between "stopped" and "appears to hang"."""
    p = _sleeper()
    state = tmp_path / "rec.json"
    supervisor.record(state, "gsr", p.pid)
    p.kill()
    # Deliberately NOT reaped yet: this is the zombie window.
    for _ in range(100):
        if supervisor._stat(p.pid) and supervisor._stat(p.pid)[0] == "Z":
            break
        time.sleep(0.02)
    assert supervisor._stat(p.pid)[0] == "Z", "could not produce a zombie to test"
    assert supervisor.living(state) == []
    assert supervisor.wait_gone(state, 1.0)
    p.wait(timeout=10)
