#!/usr/bin/env python3
"""Samples the pointer and records clicks for the duration of a capture.

Two independent sources feed one events directory:

  cursor.bin   polled. Hyprland has no "the pointer moved" event, so position is
               sampled at a fixed rate over the request socket. One
               connect/send/recv/close per sample, directly on
               $XDG_RUNTIME_DIR/hypr/$SIG/.socket.sock -- NOT `hyprctl cursorpos`.
               Forking hyprctl reaches the same sample rate and costs 21.8% of a core
               instead of 0.2%, because the work is process creation, not IPC.

  input.jsonl  pushed. Three non-consuming Hyprland keybinds on mouse:272/273/274 run
               a pure-Lua handler that appends to a spool file (0.08ms; going through
               hl.dsp.exec_cmd instead costs 3.1ms and puts a process spawn on the
               click path). This process tails the spool and stamps arrival time.

Bind lifecycle is the safety-critical part of this file. The binds are non-consuming, so
while they exist the user's clicks still work normally -- but they are global state
inside the compositor and they outlive this process. Four independent mechanisms remove
them, because any one of them can be the one that fails:

  1. release-on-exit    atexit, plus SIGINT/SIGTERM/SIGHUP handlers.
  2. dead-man timer     an hl.timer inside the compositor, re-armed by a heartbeat from
                        here. A SIGKILLed recorder cannot run any handler at all, and
                        this is what covers that case.
  3. sweep on start     the next run clears anything the previous one left.
  4. Lua __gc           a dropped HL.Keybind unbinds itself, which covers the compositor
                        reloading its config out from under us.

The sweep clears binds through the handles the previous run parked in a compositor-side
global, and verifies the result by description against `hyprctl binds`. It deliberately
does NOT fall back to hl.unbind(key): that function takes a bare keysym with no modmask,
so hl.unbind("mouse:272") also deletes the user's SUPER+mouse:272 "Move window". Leaving
a bind of ours behind is bad; silently deleting one of the user's is worse.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from omarchy_studio.events import (  # noqa: E402
    CursorSample,
    CursorWriter,
    InputWriter,
    WindowWriter,
)

TAG = "OMARCHY-STUDIO-INPUT"  # bind description prefix; the sweep matches on this
BUTTONS = {"272": "left", "273": "right", "274": "middle"}
DEADMAN_S = 15.0  # binds die this long after the last heartbeat
HEARTBEAT_S = 5.0


class RecorderError(RuntimeError):
    pass


def mono_us() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000


def realtime_us() -> int:
    return time.clock_gettime_ns(time.CLOCK_REALTIME) // 1000


def hypr_socket_path() -> str:
    try:
        sig = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
        rt = os.environ["XDG_RUNTIME_DIR"]
    except KeyError as e:
        raise RecorderError(f"not running under Hyprland ({e} unset)") from e
    return f"{rt}/hypr/{sig}/.socket.sock"


# --- compositor-side Lua ----------------------------------------------------

# `%(...)s` interpolation, not str.format: Lua is all braces.

LUA_ARM = r'''
_OMARCHY_STUDIO = _OMARCHY_STUDIO or {}
local R = _OMARCHY_STUDIO
R.tag = "%(tag)s"
R.spool = "%(spool)s"

-- Removing a bind needs its handle. Only handles whose description carries our tag are
-- touched, so a stale table from an older build can never take out a user bind.
R.release = function()
  for _, e in ipairs(R.binds or {}) do
    if type(e.desc) == "string" and e.desc:sub(1, #R.tag) == R.tag then
      pcall(function() e.kb:unbind() end)
    end
  end
  R.binds = {}
  if R.dead then pcall(function() R.dead:set_enabled(false) end) end
  R.dead = nil
end
R.release()

-- Every handler body is inside pcall. This runs on the compositor's input path, and an
-- uncaught Lua error there is a wedged pointer for the whole session.
local function handler(kind, code)
  return function()
    pcall(function()
      local p = hl.get_cursor_pos()
      local f = io.open(R.spool, "a")
      if f then
        f:write(kind .. " " .. code .. " " ..
                tostring(p and p.x or -1) .. " " ..
                tostring(p and p.y or -1) .. "\n")
        f:close()
      end
    end)
  end
end

R.binds = {}
local function add(key, kind, code)
  local desc = R.tag .. "-" .. code
  local ok, kb = pcall(function()
    return hl.bind(key, handler(kind, code),
                   { mouse = true, non_consuming = true, description = desc })
  end)
  -- The handle must be kept: HL.Keybind has a __gc that unbinds, so dropping it here
  -- would remove the bind again at the next collection.
  if ok and kb then table.insert(R.binds, { kb = kb, desc = desc }) end
end
%(adds)s

R.dead = hl.timer(function() pcall(R.release) end,
                  { timeout = %(deadman_ms)d, type = "oneshot" })
'''

# set_timeout restarts the countdown from now (verified: a 2s oneshot re-armed at 1.2s
# fired at 3.2s, not 2.0s), so the heartbeat is one call and allocates nothing.
LUA_HEARTBEAT = r'''
local R = _OMARCHY_STUDIO
if R and R.dead then pcall(function() R.dead:set_timeout(%(deadman_ms)d) end) end
'''

LUA_RELEASE = r'''
local R = _OMARCHY_STUDIO
if R and R.release then pcall(R.release) end
'''


def lua_str(s: str) -> str:
    """Escape a value for a Lua double-quoted literal.

    The spool path is a bundle directory the user named, and a recording called
    `demo "final"` would otherwise close the Lua string early and register nothing --
    which fails as a missing click track hours later, not as an error at record time.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def hypr_eval(lua: str) -> bool:
    """Run Lua in the compositor. Returns False on a reported error.

    `hyprctl eval` has no value channel -- it prints "ok" for anything that ran -- so
    errors are detected by exit status and by the error text it echoes.
    """
    try:
        r = subprocess.run(
            ["hyprctl", "eval", lua], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    blob = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0 and "error" not in blob.lower()


_DESC_RE = re.compile(r"^\tdescription:\s*(.*)$", re.MULTILINE)


def bind_descriptions() -> list[str]:
    """Every registered bind's description, from `hyprctl binds`."""
    try:
        r = subprocess.run(
            ["hyprctl", "binds"], capture_output=True, text=True, timeout=5, check=True
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        raise RecorderError(f"could not read hyprctl binds: {e}") from e
    return _DESC_RE.findall(r.stdout)


def stale_binds() -> list[str]:
    return [d for d in bind_descriptions() if d.startswith(TAG)]


# --- clicks -----------------------------------------------------------------


class ClickBinds:
    """Owns the compositor-side binds for one recording."""

    def __init__(self, spool: Path, *, with_scroll: bool = False) -> None:
        self.spool = spool
        self.with_scroll = with_scroll
        self.armed = False

    def sweep(self) -> list[str]:
        """Clear binds a previous run left behind. Returns what it found.

        Called before arming. The common cause is a SIGKILLed recorder whose dead-man
        timer has not yet expired.
        """
        found = stale_binds()
        if not found:
            return []
        hypr_eval(LUA_RELEASE)
        remaining = stale_binds()
        if remaining:
            raise RecorderError(
                f"{len(remaining)} stale bind(s) left by an earlier run could not be "
                f"removed: {remaining}. Refusing to add more. They will expire on their "
                f"own within {DEADMAN_S:.0f}s of that run's last heartbeat, or a "
                "`hyprctl reload` clears them immediately."
            )
        return found

    def arm(self) -> None:
        adds = [f'add("mouse:{code}", "click", "{code}")' for code in BUTTONS]
        if self.with_scroll:
            # Off by default: a wheel notch fires the Lua handler and a file append
            # like a click does, at ten times the rate, and auto-zoom does not use it.
            adds += [
                'add("mouse_up", "scroll", "up")',
                'add("mouse_down", "scroll", "down")',
            ]
        lua = LUA_ARM % {
            "tag": lua_str(TAG),
            "spool": lua_str(str(self.spool)),
            "adds": "\n".join(adds),
            "deadman_ms": int(DEADMAN_S * 1000),
        }
        if not hypr_eval(lua):
            raise RecorderError("hyprctl eval failed to register the click binds")
        registered = stale_binds()
        if len(registered) != len(adds):
            self.release()
            raise RecorderError(
                f"expected {len(adds)} binds, the compositor reports {len(registered)}"
            )
        self.armed = True

    def heartbeat(self) -> None:
        hypr_eval(LUA_HEARTBEAT % {"deadman_ms": int(DEADMAN_S * 1000)})

    def release(self) -> None:
        self.armed = False
        hypr_eval(LUA_RELEASE)


class SpoolTail(threading.Thread):
    """Tails the spool, stamping each line with the time it was noticed.

    The stamp is this process's read time, not the compositor's -- Lua's os.time() has
    one-second granularity, which is 60 frames of error. The polling interval is
    therefore the timing error on a click, so it is kept at 1ms: well inside one frame
    at 60fps and an order of magnitude below the 36-45ms calibration offset it sits
    alongside.

    Two writers append here: the compositor's Lua handler for clicks and scrolls, and
    the `mark` subcommand of the wrapper for chapter marks. Both write single lines
    under 4KB with O_APPEND, which the kernel serializes, so no lock is needed and no
    signal has to reach this process to record a mark.

    Line format is `kind rest`. For click and scroll `rest` is `code x y`; for chapter
    it is the label, which may contain spaces.
    """

    def __init__(self, spool: Path, sink, poll_s: float = 0.001) -> None:
        super().__init__(daemon=True)
        self.spool = spool
        self.sink = sink
        self.poll_s = poll_s
        self.stop = threading.Event()
        self.pos = 0
        self.count = 0

    def drain(self) -> None:
        try:
            if self.spool.stat().st_size <= self.pos:
                return
        except FileNotFoundError:
            return
        t_us = mono_us()
        with self.spool.open() as f:
            f.seek(self.pos)
            chunk = f.read()
            self.pos = f.tell()
        if not chunk.endswith("\n"):
            # A torn append. Rewind to the last complete line; the next drain sees the
            # rest, at a stamp a millisecond later than ideal but never mangled.
            keep, _, tail = chunk.rpartition("\n")
            self.pos -= len(tail.encode())
            chunk = keep + "\n" if keep else ""
        for line in chunk.splitlines():
            kind, _, rest = line.partition(" ")
            if kind == "chapter":
                self.count += 1
                self.sink(kind, rest, t_us, 0, 0)
                continue
            parts = rest.split()
            if len(parts) != 3:
                continue
            try:
                xi, yi = int(float(parts[1])), int(float(parts[2]))
            except ValueError:
                continue
            self.count += 1
            self.sink(kind, parts[0], t_us, xi, yi)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.drain()
            except OSError:
                pass
            self.stop.wait(self.poll_s)


# --- cursor -----------------------------------------------------------------


@dataclass
class SampleStats:
    """Achieved rate and pacing error, reported so a regression in either is visible."""

    requested_hz: float
    samples: int
    duration_s: float
    dropped: int
    jitter_mean_us: float
    jitter_p95_us: float
    jitter_max_us: float

    @property
    def achieved_hz(self) -> float:
        return self.samples / self.duration_s if self.duration_s > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "requested_hz": self.requested_hz,
            "achieved_hz": round(self.achieved_hz, 2),
            "samples": self.samples,
            "duration_s": round(self.duration_s, 3),
            "dropped": self.dropped,
            "jitter_mean_us": round(self.jitter_mean_us, 1),
            "jitter_p95_us": round(self.jitter_p95_us, 1),
            "jitter_max_us": round(self.jitter_max_us, 1),
        }


class CursorSampler:
    """Polls the pointer at a fixed rate and streams it to cursor.bin."""

    def __init__(self, sock_path: str) -> None:
        self.sock_path = sock_path

    def sample(self) -> tuple[int, int]:
        """One connect/send/recv/close. The request socket is one-shot per connection,
        so the connection cannot be held open across samples."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(0.5)
            s.connect(self.sock_path)
            s.sendall(b"cursorpos")
            data = s.recv(64)
        finally:
            s.close()
        x, _, y = data.partition(b",")
        return int(x.strip()), int(y.strip())

    def run(
        self, writer: CursorWriter, *, hz: float, stop: threading.Event, duration: float | None
    ) -> SampleStats:
        period = 1.0 / hz
        period_us = 1_000_000.0 / hz
        start = time.monotonic()
        i = 0
        n = 0
        dropped = 0
        prev_us: int | None = None
        jitter: list[float] = []

        while not stop.is_set():
            now = time.monotonic()
            if duration is not None and now - start >= duration:
                break
            target = start + i * period
            i += 1
            if target > now:
                stop.wait(target - now)
                if stop.is_set():
                    break
            elif now - target > period:
                # Fell behind (a suspend, or the socket blocked). Re-aim at the present
                # instead of firing off the whole backlog in a burst.
                i = int((now - start) / period) + 1
            t_us = mono_us()
            try:
                x, y = self.sample()
            except OSError:
                dropped += 1
                continue
            writer.append(CursorSample(t_us, x, y))
            n += 1
            if prev_us is not None:
                jitter.append(abs((t_us - prev_us) - period_us))
            prev_us = t_us

        elapsed = time.monotonic() - start
        jitter.sort()
        return SampleStats(
            requested_hz=hz,
            samples=n,
            duration_s=elapsed,
            dropped=dropped,
            jitter_mean_us=statistics.fmean(jitter) if jitter else 0.0,
            jitter_p95_us=jitter[int(len(jitter) * 0.95)] if jitter else 0.0,
            jitter_max_us=max(jitter) if jitter else 0.0,
        )



class WindowSampler:
    """Polls one window's rectangle and streams the changes to window.jsonl.

    A window target records the whole monitor and crops back to the window's rect.
    That rect is only true for the instant it was picked -- the user is free to move
    or resize the window mid-take, and today that silently records the desktop the
    window left behind. This track is what makes the crop a decision the editor
    makes afterwards rather than one the picker made in advance.

    Polling, not the event socket: `openwindow`/`movewindow` events say a window
    moved but not to where, so every event would need this same query anyway, and a
    poll cannot miss a move it never got told about (a tiling reflow caused by an
    unrelated window has no event addressed to ours).
    """

    # A window move is a discrete jump a human made, not a continuous signal. 10Hz
    # puts the jump within a couple of frames of where it happened, which is finer
    # than the ease the editor will draw over it anyway, and costs one small query
    # per tick against the cursor track's 120.
    DEFAULT_HZ = 10.0

    def __init__(self, sock_path: str, address: str) -> None:
        self.sock_path = sock_path
        self.address = address

    def _query(self) -> list[dict]:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(1.0)
            s.connect(self.sock_path)
            s.sendall(b"j/clients")
            chunks: list[bytes] = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
        finally:
            s.close()
        return json.loads(b"".join(chunks) or b"[]")

    def sample(self) -> tuple[int, int, int, int] | None:
        """The window's rect, or None if it no longer exists.

        Hyprland reports a hidden window (another workspace, or minimised) with its
        last geometry, so `hidden` is treated as gone: continuing to follow a window
        the user cannot see would pan the frame to a rectangle showing something
        else entirely.
        """
        for c in self._query():
            if str(c.get("address") or "") != self.address:
                continue
            if c.get("hidden"):
                return None
            at, size = c.get("at") or [], c.get("size") or []
            if len(at) < 2 or len(size) < 2:
                return None
            w, h = int(size[0]), int(size[1])
            if w <= 0 or h <= 0:
                return None
            return int(at[0]), int(at[1]), w, h
        return None

    def run(self, writer: WindowWriter, *, hz: float, stop: threading.Event) -> None:
        """Sample until `stop`. Never raises: this rides alongside a recording that
        is already running, and losing the ability to re-frame afterwards must not
        be able to take the take itself down."""
        period = 1.0 / hz if hz > 0 else 0.1
        gone_marked = False
        while True:
            try:
                rect = self.sample()
            except (OSError, ValueError):
                # A query that failed says nothing about the window -- the socket
                # was busy. Skipping the tick is right; writing `gone` here would
                # invent a disappearance the editor would then have to honour.
                if stop.wait(period):
                    return
                continue
            if rect is None:
                if not gone_marked:
                    writer.gone(mono_us())
                    gone_marked = True
            else:
                # A window that comes back (workspace switched away and back) resumes
                # the track; the `gone` marker already in the file stands as the record
                # of the gap, and the reader holds the last rect across it.
                gone_marked = False
                writer.sample(mono_us(), *rect)
            if stop.wait(period):
                return


# --- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record the input-event track.")
    ap.add_argument("events_dir", help="bundle events/ directory; created if absent")
    ap.add_argument("--hz", type=float, default=120.0)
    ap.add_argument("--scale", type=float, default=1.0, help="monitor scale, recorded "
                    "in cursor.bin so a mid-recording rescale is detectable")
    ap.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    ap.add_argument("--no-clicks", action="store_true")
    ap.add_argument("--with-scroll", action="store_true")
    ap.add_argument("--window", default=None, metavar="ADDRESS",
                    help="also track this Hyprland window's rectangle into "
                         "window.jsonl, so the crop can follow it in the editor")
    ap.add_argument("--window-title", default="", help="label for the tracked window")
    ap.add_argument("--window-hz", type=float, default=WindowSampler.DEFAULT_HZ)
    ap.add_argument("--stats-json", default=None, help="write the rate/jitter report here")
    a = ap.parse_args(argv)
    if a.window is not None and not re.match(r"^0x[0-9a-f]{1,16}$", a.window):
        # This is interpolated into nothing and compared as a string, but a bad
        # address means the caller's plumbing is wrong, and a window track that
        # silently matches no window is worse than a refusal.
        ap.error(f"bad --window address: {a.window!r}")

    events = Path(a.events_dir)
    events.mkdir(parents=True, exist_ok=True)
    spool = events / "spool"
    spool.write_text("")

    sock_path = hypr_socket_path()
    cursor = CursorWriter(events / "cursor.bin", hz=a.hz, scale=a.scale)
    inputs = InputWriter(events / "input.jsonl")
    binds = ClickBinds(spool, with_scroll=a.with_scroll)
    stop = threading.Event()
    released = threading.Event()

    def cleanup(*_: object) -> None:
        """Idempotent, and safe to call from a signal handler: it only sets an Event
        and forks hyprctl."""
        if released.is_set():
            return
        released.set()
        binds.release()

    atexit.register(cleanup)

    def on_signal(*_: object) -> None:
        # Unbind first, then ask the sampler to wind down. sys.exit() is deliberately
        # not called: raising out of the handler would skip the report and leave
        # cursor.bin unfinalized, and the binds -- the only thing that must not
        # outlive this process -- are already gone by the time this returns.
        cleanup()
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    def on_input(kind: str, code: str, t_us: int, x: int, y: int) -> None:
        if kind == "click":
            inputs.click(t_us, BUTTONS.get(code, code), x, y)
        elif kind == "scroll":
            inputs.scroll(t_us, code, x, y)
        elif kind == "chapter":
            inputs.chapter(t_us, code)

    tail: SpoolTail | None = None
    windows: WindowWriter | None = None
    stats: SampleStats | None = None
    swept: list[str] = []
    try:
        if not a.no_clicks:
            swept = binds.sweep()
            if swept:
                print(f"swept {len(swept)} stale bind(s): {swept}", file=sys.stderr)
            binds.arm()
            threading.Thread(
                target=_heartbeat_loop, args=(binds, stop), daemon=True
            ).start()
        tail = SpoolTail(spool, on_input)
        tail.start()

        if a.window:
            # Daemon thread: the cursor sampler owns the lifetime, and `stop` is
            # already set in the finally below before window.close() runs.
            windows = WindowWriter(events / "window.jsonl")
            windows.meta(address=a.window, title=a.window_title, hz=a.window_hz)
            threading.Thread(
                target=WindowSampler(sock_path, a.window).run,
                kwargs={"writer": windows, "hz": a.window_hz, "stop": stop},
                daemon=True,
            ).start()

        # Anchoring metadata. Both clocks are stamped together so these events can be
        # lined up with gsr's -write-first-frame-ts sidecar, which carries the same pair.
        inputs.meta(
            t_us=mono_us(),
            clock_monotonic_us=mono_us(),
            clock_realtime_us=realtime_us(),
            hz=a.hz,
            monitor_scale=a.scale,
            clicks=not a.no_clicks,
            scroll=a.with_scroll,
        )

        stats = CursorSampler(sock_path).run(
            cursor, hz=a.hz, stop=stop, duration=a.duration
        )
    finally:
        stop.set()
        if tail is not None:
            tail.stop.set()
            tail.join(timeout=1.0)
            tail.drain()  # a click in the last millisecond is still a click
        cursor.close()
        if windows is not None:
            windows.close()
        cleanup()
        spool.unlink(missing_ok=True)

    report = {
        **(stats.to_dict() if stats else {}),
        "events": tail.count if tail else 0,
        "cursor_bytes": (events / "cursor.bin").stat().st_size,
        "swept_stale_binds": len(swept),
        "window_track": a.window or None,
    }
    inputs.meta(t_us=mono_us(), event="end", **report)
    inputs.close()

    if a.stats_json:
        Path(a.stats_json).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), file=sys.stderr)

    leftover = stale_binds()
    if leftover:
        print(f"WARNING: binds still registered after cleanup: {leftover}", file=sys.stderr)
        return 1
    return 0


def _heartbeat_loop(binds: ClickBinds, stop: threading.Event) -> None:
    """Re-arms the compositor-side dead-man timer.

    Silence here is what removes the binds when this process is SIGKILLed, so the
    interval must stay comfortably under DEADMAN_S -- a missed beat during a stall must
    not expire the binds mid-recording.
    """
    while not stop.wait(HEARTBEAT_S):
        binds.heartbeat()


if __name__ == "__main__":
    raise SystemExit(main())
