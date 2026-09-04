"""Which processes belong to THIS recording, and how to stop exactly those.

A recording runs about five long-lived processes -- gpu-screen-recorder, the camera
ffmpeg, its fan-out, the HUD, the self-view mpv -- and the thing that stops them is a
SECOND INVOCATION of the same script, in a different process, with none of the shell
variables that knew their pids. So the stop path went looking for them by pattern:

    pkill -SIGINT -f "^gpu-screen-recorder"
    pkill -f "WebcamOverlay"
    pgrep -f "mkvtimestamp_v2 [^ ]*/media/cam\\.tsv"

That is asking the whole process table "does anything look like mine?", and it is wrong
in three separate ways. It kills a SECOND recording started by the same user. It kills
an unrelated process that merely has the string somewhere in its argv -- `pkill -f`
matches the full command line, so a text editor holding the file open is a candidate.
And the patterns interpolate paths (`pkill -x -f "cat $FIFO"`), so a bundle directory
containing regex metacharacters changes which processes match.

None of that is a privilege boundary -- pkill only reaches the caller's own uid -- but
"kills things it does not own, chosen by string matching" is not a property worth
keeping in the part of the app that runs while you record.

So: record the pids at start, verify identity at stop, signal exactly those.

IDENTITY IS (PID, START TIME), NOT PID. Pids are recycled, and a stale state file plus
a recycled pid means signalling something else entirely -- the one way this could get
WORSE than pattern matching. Field 22 of /proc/<pid>/stat is the process's start time in
clock ticks since boot; it is stable for the life of a process and a recycled pid gets a
different one. Both are recorded and both must match before anything is signalled.
"""
from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path


class SupervisorError(RuntimeError):
    pass


def _stat(pid: int) -> tuple[str, int] | None:
    """(state, start time) from /proc/<pid>/stat, or None if the process is gone.

    Parsed from the LAST ')' rather than by splitting: field 2 is the executable name in
    parentheses and may itself contain spaces and brackets, which is a classic way to
    mis-parse this file.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    try:
        after = raw[raw.rindex(")") + 2:].split()
        return after[0], int(after[19])   # state; field 22 overall
    except (ValueError, IndexError):
        return None


def start_time(pid: int) -> int | None:
    """The start time half of identity, or None if the process is gone."""
    st = _stat(pid)
    return None if st is None else st[1]


def is_running(pid: int, started: int) -> bool:
    """Whether THIS process is still doing something.

    A ZOMBIE is not. A signalled child stays in /proc, with the same start time, until
    its parent reaps it -- so a plain "does /proc/<pid> exist and match" answered yes to
    a process that had already exited, and wait_gone waited out its whole timeout on a
    corpse. State 'Z' is the difference between "still recording" and "dead but not yet
    collected".
    """
    st = _stat(pid)
    return st is not None and st[1] == started and st[0] != "Z"


@dataclass
class Child:
    name: str
    pid: int
    started: int          # /proc start time, the half of identity that defeats reuse

    def alive(self) -> bool:
        """Whether THIS process is still running -- not merely whether the pid is."""
        return is_running(self.pid, self.started)

    def to_dict(self) -> dict:
        return {"name": self.name, "pid": self.pid, "started": self.started}

    @classmethod
    def from_dict(cls, d: dict) -> "Child":
        return cls(str(d["name"]), int(d["pid"]), int(d["started"]))


@dataclass
class Recording:
    """The process set of one recording, persisted so a later invocation can stop it."""

    bundle: str = ""
    children: list[Child] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"version": 1, "bundle": self.bundle,
                "children": [c.to_dict() for c in self.children]}

    @classmethod
    def from_dict(cls, d: dict) -> "Recording":
        return cls(str(d.get("bundle", "")),
                   [Child.from_dict(c) for c in d.get("children", [])])


def load(path: str | Path) -> Recording:
    """The recorded process set, or an empty one. Never raises for a missing or corrupt
    file: a stop that cannot read the state still has to fall through to its legacy
    path rather than abort with the recording still running."""
    try:
        return Recording.from_dict(json.loads(Path(path).read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return Recording()


def save(path: str | Path, rec: Recording) -> None:
    """Atomically, so a stop reading it mid-write sees the old set rather than half of
    the new one."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".part")
    tmp.write_text(json.dumps(rec.to_dict(), indent=2) + "\n")
    os.replace(tmp, p)


def record(path: str | Path, name: str, pid: int, *, bundle: str = "") -> Child | None:
    """Add one child. Returns None if it is already gone, so a process that died during
    startup is never written as if it were running."""
    started = start_time(pid)
    if started is None:
        return None
    rec = load(path)
    if bundle:
        rec.bundle = bundle
    rec.children = [c for c in rec.children if c.pid != pid]
    child = Child(name, pid, started)
    rec.children.append(child)
    save(path, rec)
    return child


def living(path: str | Path) -> list[Child]:
    return [c for c in load(path).children if c.alive()]


def signal_all(path: str | Path, sig: int, *, names: tuple[str, ...] = ()) -> list[str]:
    """Signal the recorded children -- only the ones still genuinely alive.

    Returns the names signalled. `names` narrows it, because the stop sequence is not
    uniform: gpu-screen-recorder needs SIGINT to finalise its file, while the fan-out
    ffmpeg can be terminated outright.
    """
    sent = []
    for c in living(path):
        if names and c.name not in names:
            continue
        try:
            os.kill(c.pid, sig)
            sent.append(c.name)
        except (ProcessLookupError, PermissionError):
            continue
    return sent


def wait_gone(path: str | Path, timeout_s: float, *, names: tuple[str, ...] = ()) -> bool:
    """Whether everything named has exited within the timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        left = [c for c in living(path) if not names or c.name in names]
        if not left:
            return True
        time.sleep(0.1)
    return not [c for c in living(path) if not names or c.name in names]


def clear(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)
