#!/usr/bin/env python3
"""
input_recorder.py -- SPIKE prototype for Omarchy non-destructive screen recording.

Writes a JSONL sidecar of timestamped cursor positions + mouse clicks, suitable for
driving auto-zoom and a synthetic smooth cursor at render time.

  cursor: sampled at --hz over Hyprland's request socket (.socket.sock), one
          connect/send/recv/close per sample (the socket is one-shot per connection).
  clicks: Hyprland non-consuming Lua binds (opts.non_consuming = true) on
          mouse:272/273/274. The Lua handler calls hl.get_cursor_pos() and appends a
          line to a spool file; this process tails the spool and stamps arrival time.

All binds are removed on exit, and a Hyprland-side dead-man timer removes them even if
this process is SIGKILLed.

usage: input_recorder.py OUT.jsonl [--hz 120] [--duration SEC]
"""
import argparse, atexit, json, os, signal, socket, subprocess, sys, threading, time

SIG  = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
RT   = os.environ["XDG_RUNTIME_DIR"]
SOCK = f"{RT}/hypr/{SIG}/.socket.sock"
TAG  = "OMARCHY-REC-CLICK"          # bind description, used for cleanup
BTNS = {"272": "left", "273": "right", "274": "middle"}

def mono():     return time.clock_gettime(time.CLOCK_MONOTONIC)
def realtime(): return time.clock_gettime(time.CLOCK_REALTIME)

def hypr_eval(lua):
    r = subprocess.run(["hyprctl", "eval", lua], capture_output=True, text=True)
    if "error" in (r.stdout or "").lower():
        print("hyprctl eval error:", r.stdout.strip()[:300], file=sys.stderr)
        return False
    return True

def sample_cursor():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK); s.sendall(b"cursorpos")
    d = s.recv(64); s.close()
    x, _, y = d.partition(b",")
    return int(x), int(y)

# ---------------------------------------------------------------- click binds
LUA_ON = r'''
_OMARCHY_REC = _OMARCHY_REC or {}
_OMARCHY_REC.spool = "%(spool)s"
local function handler(btn)
  return function()
    local p = hl.get_cursor_pos()
    local f = io.open(_OMARCHY_REC.spool, "a")
    if f then
      f:write(btn .. " " .. tostring(p and p.x or -1) .. " " .. tostring(p and p.y or -1) .. "\n")
      f:close()
    end
  end
end
for _, kb in ipairs(_OMARCHY_REC.binds or {}) do pcall(function() kb:unbind() end) end
_OMARCHY_REC.binds = {}
for _, b in ipairs({ "272", "273", "274" }) do
  table.insert(_OMARCHY_REC.binds,
    hl.bind("mouse:" .. b, handler(b),
            { mouse = true, non_consuming = true, description = "%(tag)s-" .. b }))
end
-- dead-man: unbind even if the recorder process dies without cleaning up
if _OMARCHY_REC.dead then pcall(function() _OMARCHY_REC.dead:set_enabled(false) end) end
_OMARCHY_REC.dead = hl.timer(function()
  for _, kb in ipairs(_OMARCHY_REC.binds or {}) do pcall(function() kb:unbind() end) end
  _OMARCHY_REC.binds = {}
end, { timeout = %(deadman)d, type = "oneshot" })
'''

LUA_OFF = r'''
if _OMARCHY_REC then
  for _, kb in ipairs(_OMARCHY_REC.binds or {}) do pcall(function() kb:unbind() end) end
  _OMARCHY_REC.binds = {}
  if _OMARCHY_REC.dead then pcall(function() _OMARCHY_REC.dead:set_enabled(false) end) end
end
'''

class ClickWatcher(threading.Thread):
    """Tails the Lua spool file, stamping each click with arrival time."""
    def __init__(self, spool, sink, poll_s=0.001):
        super().__init__(daemon=True)
        self.spool, self.sink, self.poll_s = spool, sink, poll_s
        self.stop = threading.Event(); self.pos = 0; self.n = 0
    def run(self):
        while not self.stop.is_set():
            try:
                if os.path.getsize(self.spool) > self.pos:
                    t = mono()
                    with open(self.spool) as f:
                        f.seek(self.pos); chunk = f.read(); self.pos = f.tell()
                    for line in chunk.splitlines():
                        p = line.split()
                        if len(p) == 3:
                            self.n += 1
                            self.sink({"t": round(t, 6), "type": "click",
                                       "button": BTNS.get(p[0], p[0]),
                                       "x": int(float(p[1])), "y": int(float(p[2]))})
            except FileNotFoundError:
                pass
            self.stop.wait(self.poll_s)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--hz", type=float, default=120.0)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--no-clicks", action="store_true")
    a = ap.parse_args()

    spool = os.path.abspath(a.out) + ".clickspool"
    open(spool, "w").close()
    out = open(a.out, "w", buffering=1)
    lock = threading.Lock()
    def emit(rec):
        with lock: out.write(json.dumps(rec) + "\n")

    cleaned = threading.Event()
    def cleanup(*_):
        if cleaned.is_set(): return
        cleaned.set()
        hypr_eval(LUA_OFF)
        try: out.close()
        except Exception: pass
        try: os.unlink(spool)
        except Exception: pass
    atexit.register(cleanup)
    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, lambda *_: (cleanup(), sys.exit(0)))

    # anchor: lets the renderer line this log up with gsr's -write-first-frame-ts file,
    # which contains "monotonic_microsec realtime_microsec" for video frame 0.
    emit({"t": round(mono(), 6), "type": "meta", "schema": 1, "hz": a.hz,
          "clock_monotonic": round(mono(), 6), "clock_realtime": round(realtime(), 6),
          "note": "t is CLOCK_MONOTONIC seconds; align with gsr <out>.ts"})

    watcher = None
    if not a.no_clicks:
        deadman = int(((a.duration or 3600) + 60) * 1000)
        if not hypr_eval(LUA_ON % {"spool": spool, "tag": TAG, "deadman": deadman}):
            print("WARNING: click binds failed to register", file=sys.stderr)
        watcher = ClickWatcher(spool, emit); watcher.start()

    period = 1.0 / a.hz
    start = mono(); i = 0; last = None; nsamp = 0
    try:
        while True:
            if a.duration and mono() - start >= a.duration: break
            target = start + i * period; i += 1
            d = target - mono()
            if d > 0: time.sleep(d)
            t = mono()
            try: x, y = sample_cursor()
            except Exception: continue
            nsamp += 1
            if (x, y) != last:                 # delta-compress: only emit on change
                emit({"t": round(t, 6), "type": "cursor", "x": x, "y": y})
                last = (x, y)
    except KeyboardInterrupt:
        pass
    finally:
        dur = mono() - start
        if watcher: watcher.stop.set(); watcher.join(timeout=1.0)
        nclick = watcher.n if watcher else 0
        emit({"t": round(mono(), 6), "type": "end", "duration": round(dur, 3),
              "samples": nsamp, "clicks": nclick})
        cleanup()
        print(f"samples={nsamp} ({nsamp/dur:.1f} Hz effective)  clicks={nclick}  "
              f"duration={dur:.2f}s  -> {a.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
