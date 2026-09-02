"""The recordings library: scan the recordings directory, classify what is there, and
serve it to the QML shell (editor/library/) over the same loopback-plus-token transport
qmlbridge.py uses.

The QML side renders exactly what arrives and derives nothing -- every label, every
relative time and every "this row is broken" decision is made here, for the same reason
qmlbridge resolves all geometry: two implementations of "what does this directory mean"
drift, and the drift shows up as a row that says Edit over a bundle the editor refuses.

Three kinds of directory entry, decided by inspection rather than by any index file
(there is no index; the directory is the truth):

* a directory containing capture.json  -> a bundle. Editable, unless its screen media
  is gone ("source moved" -> Locate...) or its capture.json no longer parses.
* a flat video file                    -> a clip from the previous recorder. Listed and
  playable, never editable: there is no capture.json, so there are no events, no camera
  stream and no timebase for the editor to resolve against.
* anything else                        -> not a recording; skipped silently.

A partial bundle (the recorder hung mid-stop) must degrade, not crash: every probe on
an entry is wrapped, and a failed probe downgrades the row -- no duration chip, a
"partial recording" note -- instead of killing the scan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Flat files with these suffixes are clips from the previous recorder. It only ever
# wrote .mp4, but a hand-remuxed copy should not vanish from the list for its container.
CLIP_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm"}

# 2x the row thumbnail (78x46 in the mock) so a HiDPI screen gets real pixels.
THUMB_WIDTH = 312


def videos_dir() -> Path:
    """Where recordings land -- the recorder's own resolution order, reimplemented
    because that logic lives in bash (bin/omarchy-capture-screenrecording) where Python
    cannot import it: OMARCHY_SCREENRECORD_DIR, then XDG_VIDEOS_DIR, then ~/Videos."""
    override = os.environ.get("OMARCHY_SCREENRECORD_DIR")
    if override:
        return Path(override).expanduser()
    dirs = Path.home() / ".config" / "user-dirs.dirs"
    if dirs.exists():
        try:
            m = re.search(r'XDG_VIDEOS_DIR="([^"]+)"', dirs.read_text())
            if m:
                return Path(m.group(1).replace("$HOME", str(Path.home())))
        except OSError:
            pass
    return Path.home() / "Videos"


def thumb_cache_dir() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "omarchy-studio" / "thumbs"


# --- labels ------------------------------------------------------------------


def rel_time(ts: float, now: float | None = None) -> str:
    """The mock's register: "2 minutes ago", "Yesterday", "3 days ago"."""
    now = now if now is not None else datetime.now().timestamp()
    delta = max(0.0, now - ts)
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} minutes ago"
    then = datetime.fromtimestamp(ts)
    today = datetime.fromtimestamp(now).date()
    if then.date() == today:
        h = int(delta // 3600)
        return "1 hour ago" if h == 1 else f"{h} hours ago"
    days = (today - then.date()).days
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    return then.strftime("%b %-d")


def size_label(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n // 1024**2} MB"
    return f"{max(1, n // 1024)} KB"


def duration_label(secs: float) -> str:
    s = int(round(secs))
    return f"{s // 60}:{s % 60:02d}"


def _format_label(height: int) -> str:
    # Same threshold as main.qml's formatLabel(): 2160 minus a margin for the panel
    # strip a recorded monitor loses to bars.
    return "4K" if height >= 2100 else f"{height}p"


def _dir_size(root: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += (Path(base) / f).stat().st_size
            except OSError:
                pass
    return total


# --- probing -----------------------------------------------------------------


def probe_media(path: Path) -> dict | None:
    """Duration and geometry from ffprobe, or None. None is a real answer: the partial
    bundle's truncated stream must downgrade its row, not raise past the scan."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        return None
    try:
        duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    fps = 0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    m = re.match(r"(\d+)/(\d+)", rate)
    if m and int(m.group(2)):
        fps = round(int(m.group(1)) / int(m.group(2)))
    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
    }


def thumbnail(src: Path, duration: float) -> Path | None:
    """One real frame, cached by (path, mtime, size). No frame extracted means no
    thumbnail shown -- an invented image would claim content the file may not have."""
    try:
        st = src.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{src}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
    out = thumb_cache_dir() / f"{key}.jpg"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    # A second in avoids the black or half-drawn first frame most captures start with;
    # clips shorter than that get frame zero.
    seek = "1" if duration > 2 else "0"
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", seek, "-i", str(src),
             "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-2",
             "-y", str(out)],
            capture_output=True, timeout=20,
        )
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
    except (OSError, subprocess.TimeoutExpired):
        pass
    out.unlink(missing_ok=True)
    return None


# --- scanning ----------------------------------------------------------------


def _scan_bundle(root: Path, now: float) -> dict:
    entry: dict[str, Any] = {
        "path": str(root),
        "name": root.name,
        "kind": "bundle",
        "duration": "",
        "thumb": "",
        "missing": False,
        "broken": False,
        "editable": False,
        "unedited": not (root / "edit.json").exists(),
        "exported": any(root.glob("*.mp4")),   # the exporter's default output lands here
        "size_bytes": _dir_size(root),
    }
    try:
        created = root.stat().st_mtime
    except OSError:
        created = now
    capture = None
    try:
        capture = json.loads((root / "capture.json").read_text())
        created_s = capture.get("created")
        if created_s:
            created = datetime.fromisoformat(created_s).timestamp()
    except (OSError, ValueError, TypeError):
        # capture.json exists (that is what made this a bundle) but no longer parses.
        # The row survives so the media inside is still reachable; the editor cannot
        # open it, so it must not offer Edit.
        entry["broken"] = True
    entry["created"] = created
    entry["when"] = rel_time(created, now)

    screen_rel = "media/screen.mp4"
    if capture:
        screen_rel = (capture.get("screen") or {}).get("path") or screen_rel
    screen = root / screen_rel
    if not screen.exists():
        # The spec's "source moved" row: link_off, and the action becomes Locate...
        entry["missing"] = True
        entry["meta"] = f"{entry['when']} · source moved"
        entry["missing_rel"] = screen_rel
        return entry
    if entry["broken"]:
        entry["meta"] = f"{entry['when']} · recording data unreadable · plays only"
        entry["playable"] = str(screen)
        info = probe_media(screen)
        if info:
            entry["duration"] = duration_label(info["duration"])
            t = thumbnail(screen, info["duration"])
            entry["thumb"] = t.as_uri() if t else ""
        return entry

    entry["editable"] = True
    entry["playable"] = str(screen)
    parts = [entry["when"]]
    info = probe_media(screen)
    if info and info["height"]:
        entry["duration"] = duration_label(info["duration"])
        fps = info["fps"] or round(
            (capture.get("screen", {}).get("fps_num") or 0)
            / max(1, capture.get("screen", {}).get("fps_den") or 1))
        parts.append(f"{_format_label(info['height'])} {fps}".rstrip())
        t = thumbnail(screen, info["duration"])
        entry["thumb"] = t.as_uri() if t else ""
    else:
        # The hung-stop case: capture.json is fine, the stream is not. Say so instead
        # of pretending; the editor may still recover what the container holds.
        parts.append("partial recording")
    if entry["exported"]:
        parts.append("exported MP4")
    parts.append(size_label(entry["size_bytes"]))
    entry["meta"] = " · ".join(parts)
    return entry


def _scan_clip(path: Path, now: float) -> dict:
    """A flat file from the previous recorder. Playable, never editable -- without
    capture.json there is nothing for the editor to resolve, so the action reads Play
    and the meta line says why."""
    try:
        st = path.stat()
    except OSError:
        st = None
    created = st.st_mtime if st else now
    entry: dict[str, Any] = {
        "path": str(path),
        "name": path.stem,
        "kind": "clip",
        "created": created,
        "when": rel_time(created, now),
        "duration": "",
        "thumb": "",
        "missing": False,
        "broken": False,
        "editable": False,
        "unedited": False,   # nothing to edit, so it never surfaces under Unedited
        "exported": False,
        "playable": str(path),
        "size_bytes": st.st_size if st else 0,
    }
    parts = [entry["when"]]
    info = probe_media(path)
    if info and info["height"]:
        entry["duration"] = duration_label(info["duration"])
        parts.append(f"{_format_label(info['height'])} {info['fps']}".rstrip())
        t = thumbnail(path, info["duration"])
        entry["thumb"] = t.as_uri() if t else ""
    parts.append(size_label(entry["size_bytes"]))
    parts.append("older recording · plays only")
    entry["meta"] = " · ".join(parts)
    return entry


def scan(root: Path | None = None) -> dict:
    root = root or videos_dir()
    now = datetime.now().timestamp()
    entries: list[dict] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        children = []
    for child in children:
        try:
            if child.is_dir() and (child / "capture.json").exists():
                entries.append(_scan_bundle(child, now))
            elif child.is_file() and child.suffix.lower() in CLIP_SUFFIXES:
                entries.append(_scan_clip(child, now))
        except Exception:
            # One unreadable entry must not empty the library. Skipping it entirely is
            # the last resort after _scan_bundle's own downgrades.
            continue
    entries.sort(key=lambda e: e["created"], reverse=True)
    # The newest unedited, editable bundle gets the accent treatment (spec 2d): it is
    # the one the user just recorded and has not touched.
    for e in entries:
        e["fresh"] = False
    for e in entries:
        if e["kind"] == "bundle" and e["unedited"] and e["editable"]:
            e["fresh"] = True
            break
    total = sum(e["size_bytes"] for e in entries)
    return {
        "dir": str(root),
        "count": len(entries),
        "total_label": size_label(total) if entries else "0 MB",
        "recordings": entries,
    }


# --- desktop state for the menubar dropdown ----------------------------------

_MODMASK = [(64, "Super"), (8, "Alt"), (4, "Ctrl"), (1, "Shift")]


def _record_shortcut() -> str:
    """The live Hyprland binding for screen recording, read rather than assumed --
    showing a hint the compositor does not honour is worse than showing none."""
    try:
        out = subprocess.run(["hyprctl", "binds", "-j"],
                             capture_output=True, text=True, timeout=3)
        binds = json.loads(out.stdout)
    except Exception:
        return ""
    for b in binds:
        blob = (b.get("description", "") + b.get("arg", "")).lower()
        if "screenrecord" in blob:
            mods = [name for bit, name in _MODMASK if b.get("modmask", 0) & bit]
            key = str(b.get("key") or "").capitalize()
            return "+".join(mods + [key]) if key else ""
    return ""


def _focused_source() -> str:
    try:
        out = subprocess.run(["hyprctl", "monitors", "-j"],
                             capture_output=True, text=True, timeout=3)
        mons = json.loads(out.stdout)
    except Exception:
        return ""
    mon = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
    if not mon:
        return ""
    scale = mon.get("scale") or 1
    w = round(mon["width"] / scale)
    h = round(mon["height"] / scale)
    return f"{mon['name']} · {w}×{h}"


def _mics() -> tuple[str, list[dict]]:
    """Default source name plus the human list, monitors excluded -- a monitor source
    records the speakers, which is never what "which microphone" means."""
    try:
        default = subprocess.run(["pactl", "get-default-source"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"],
                             capture_output=True, text=True, timeout=3)
        sources = json.loads(out.stdout)
    except Exception:
        return "", []
    mics = [{"name": s["name"], "label": s.get("description", s["name"]),
             "current": s["name"] == default}
            for s in sources if not s["name"].endswith(".monitor")]
    current = next((m["label"] for m in mics if m["current"]), "")
    return current, mics


def _recording() -> bool:
    try:
        return subprocess.run(["pgrep", "-f", "^gpu-screen-recorder"],
                              capture_output=True, timeout=3).returncode == 0
    except Exception:
        return False


def menubar_state(count: int) -> dict:
    mic, mics = _mics()
    return {
        "state": "recording" if _recording() else "ready",
        "source": _focused_source(),
        "mic": mic,
        "mics": mics,
        "count": count,
        "shortcut": _record_shortcut(),
        "camera": bool(sorted(Path("/dev").glob("video*"))),
    }


# --- the session and its ops -------------------------------------------------


def _spawn(argv: list[str]) -> None:
    """Fire and forget, detached: the library outliving (or dying before) the editor it
    launched must affect neither."""
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


class LibraryError(RuntimeError):
    pass


class Session:
    """The scan cache plus every verb the library UI has, behind one lock -- the scan
    shells out to ffprobe and two rescans racing would double the work for a stale
    answer."""

    def __init__(self, repo_root: Path, root: Path | None = None) -> None:
        self.repo_root = repo_root
        self.root = root or videos_dir()
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.RLock()
        self.quit_requested = threading.Event()
        self._library: dict | None = None

    def library(self, rescan: bool = False) -> dict:
        with self.lock:
            if self._library is None or rescan:
                self._library = scan(self.root)
            return self._library

    def menubar(self) -> dict:
        return menubar_state(self.library()["count"])

    def _entry(self, path: str) -> dict:
        for e in self.library()["recordings"]:
            if e["path"] == path:
                return e
        raise LibraryError(f"not in the library: {path}")

    def op(self, name: str, args: dict) -> dict:
        with self.lock:
            recorder = str(self.repo_root / "bin" / "omarchy-capture-screenrecording")
            if name == "rescan":
                return self.library(rescan=True)
            if name == "edit":
                e = self._entry(args["path"])
                if not e["editable"]:
                    raise LibraryError("this entry has no capture data to edit")
                _spawn([str(self.repo_root / "bin" / "omarchy-studio"), e["path"]])
                return {"ok": True}
            if name == "play":
                e = self._entry(args["path"])
                if not e.get("playable"):
                    raise LibraryError("no playable media in this entry")
                _spawn(["xdg-open", e["playable"]])
                return {"ok": True}
            if name == "reveal":
                e = self._entry(args["path"])
                p = Path(e["path"])
                _spawn(["xdg-open", str(p if p.is_dir() else p.parent)])
                return {"ok": True}
            if name == "locate":
                return self._locate(args["path"], args["source"])
            if name == "record":
                # No args records a picked region (the recorder runs its own picker);
                # --fullscreen is the whole focused monitor.
                argv = [recorder]
                if args.get("mode", "screen") == "screen":
                    argv.append("--fullscreen")
                _spawn(argv)
                return {"ok": True}
            if name == "stop":
                _spawn([recorder, "--stop-recording"])
                return {"ok": True}
            if name == "set_mic":
                subprocess.run(["pactl", "set-default-source", args["name"]],
                               capture_output=True, timeout=3)
                return self.menubar()
            if name == "open_library":
                _spawn([str(self.repo_root / "bin" / "omarchy-recordings")])
                return {"ok": True}
            raise LibraryError(f"unknown op: {name}")

    def _locate(self, path: str, source: str) -> dict:
        """Reattach a moved screen file: a relative symlink at the path capture.json
        expects. A symlink rather than a rewrite because capture.json is immutable by
        design (project.py's first rule), and rather than a copy because the file the
        user just pointed at may be gigabytes."""
        e = self._entry(path)
        if not e.get("missing"):
            raise LibraryError("this entry is not missing its source")
        src = Path(source).expanduser()
        if not src.is_file():
            raise LibraryError(f"not a file: {src}")
        target = Path(e["path"]) / e.get("missing_rel", "media/screen.mp4")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        target.symlink_to(src.resolve())
        return self.library(rescan=True)


class _Handler(BaseHTTPRequestHandler):
    server_version = "omarchy-recordings/1"

    @property
    def session(self) -> Session:
        return self.server.session  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:
        pass

    def _authorized(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-Studio-Token", ""), self.session.token)

    def _send(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self) -> None:
        if not self._authorized():
            return self._send({"error": "bad token"}, 403)
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if route in ("/", "/library"):
                return self._send(self.session.library())
            if route == "/menubar":
                return self._send(self.session.menubar())
            if route == "/theme":
                # qmlbridge owns the theme endpoint's behaviour; importing it keeps
                # exactly one resolver, per the rule at the top of editor/Theme.qml.
                from .qmlbridge import theme_tokens
                return self._send(theme_tokens())
        except Exception as e:
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._authorized():
            return self._send({"error": "bad token"}, 403)
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as e:
            return self._send({"error": f"bad JSON: {e}"}, 400)
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if route == "/op":
                return self._send(self.session.op(body["op"], body.get("args", {})))
            if route == "/quit":
                self.session.quit_requested.set()
                return self._send({"ok": True})
        except (LibraryError, KeyError, ValueError, OSError) as e:
            return self._send({"error": str(e)}, 400)
        except Exception as e:
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send({"error": "not found"}, 404)


def serve(session: Session, port: int = 0) -> ThreadingHTTPServer:
    """Loopback only, same shape as qmlbridge.serve -- this endpoint spawns processes."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    srv.session = session  # type: ignore[attr-defined]
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="library", daemon=True).start()
    return srv
