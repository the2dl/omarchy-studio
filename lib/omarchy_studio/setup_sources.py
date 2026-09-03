"""Source enumeration and the stdout contract for the pre-record setup window.

bin/omarchy-capture-setup prints exactly one line of JSON when the user hits Record,
and bin/omarchy-capture-screenrecording consumes it. Everything about that line --
which targets exist, how a region is spelled, which camera modes are legal -- is
defined here, in pure functions, so the contract is testable without a compositor.

The enumeration mirrors /usr/share/omarchy/bin/omarchy-capture-region rather than
re-deriving it: same workspace filter, same hidden-window drop, same
identical-geometry dedupe, and the same scale/transform arithmetic for monitors
(logical size = pixel size / scale, swapped for 90-degree transforms). Two
enumerations that disagree would offer the user a window the picker cannot capture.
"""

from __future__ import annotations

import re
from typing import Any

CAMERA_MODES = ("off", "circle", "rounded", "rect")

_REGION_RE = re.compile(r"^region:(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")
_SLURP_RE = re.compile(r"^(-?\d+),(-?\d+) (\d+)x(\d+)$")


# --- monitors ----------------------------------------------------------------


def monitor_logical(mon: dict) -> tuple[int, int, int, int]:
    """Logical (x, y, w, h) for one `hyprctl monitors -j` entry.

    Same arithmetic as capture-region's JQ_MONITOR_GEO: divide by scale, floor, and
    swap width/height for transforms 1 and 3 (90/270 degrees) -- window coordinates
    are logical, so monitor rects must be too or region targets land off-screen on
    any scaled display.
    """
    w = int(mon["width"] / mon.get("scale", 1.0))
    h = int(mon["height"] / mon.get("scale", 1.0))
    if mon.get("transform", 0) in (1, 3):
        w, h = h, w
    return int(mon["x"]), int(mon["y"]), w, h


def monitors(payload: list[dict]) -> list[dict]:
    """Screen-tab sources, one per monitor, in hyprctl order."""
    out = []
    for mon in payload:
        x, y, w, h = monitor_logical(mon)
        out.append({
            "kind": "monitor",
            "name": mon["name"],
            "label": mon.get("description") or mon["name"],
            "x": x, "y": y, "width": w, "height": h,
            "refresh": round(float(mon.get("refreshRate", 0))),
            "target": f"monitor:{mon['name']}",
        })
    return out


def active_workspace(payload: list[dict]) -> int | None:
    """The focused monitor's workspace id, as capture-region defines 'active'."""
    for mon in payload:
        if mon.get("focused"):
            return int(mon["activeWorkspace"]["id"])
    return None


# --- windows -----------------------------------------------------------------


def windows(clients: list[dict], workspace_id: int | None,
            skip_prefixes: tuple[str, ...] = ()) -> list[dict]:
    """Window-tab sources: active workspace only, hidden dropped, geometry deduped.

    The dedupe keeps the FIRST client at each geometry -- hidden group members and
    exactly-stacked windows collapse to one card, for the same reason capture-region
    collapses them: the capture rectangle is identical, so a second card would be a
    duplicate choice, not a second window.

    `skip_prefixes` drops windows by title prefix, and exists because enumeration is
    no longer a once-at-launch thing. The first pass runs before qml6 spawns and has
    nothing of ours to hide; every RE-enumeration (a camera was plugged in, so the
    lists rebuild) runs with our own sheets and the teleprompter mapped, and offering
    the user the monitor-sized sheet that is covering their screen as a capture
    target is not a choice, it is a bug with a card drawn around it.
    """
    out: list[dict] = []
    seen: set[tuple[int, int, int, int]] = set()
    for c in clients:
        if workspace_id is None or c.get("workspace", {}).get("id") != workspace_id:
            continue
        if c.get("hidden"):
            continue
        title = str(c.get("title") or "")
        if any(title.startswith(p) for p in skip_prefixes):
            continue
        x, y = int(c["at"][0]), int(c["at"][1])
        w, h = int(c["size"][0]), int(c["size"][1])
        if (x, y, w, h) in seen or w <= 0 or h <= 0:
            continue
        seen.add((x, y, w, h))
        out.append({
            "kind": "window",
            "title": c.get("title") or c.get("class") or "window",
            "cls": c.get("class", ""),
            "x": x, "y": y, "width": w, "height": h,
            "target": region_target_from_rect(x, y, w, h),
        })
    return out


# --- cameras -----------------------------------------------------------------


def cameras(listing: str) -> list[dict]:
    """Parse `omarchy-capture-webcam-list` output: '/dev/videoN  Name' per line."""
    out = []
    for line in listing.splitlines():
        m = re.match(r"^(/dev/video\d+)\s+(.*)$", line.strip())
        if m:
            out.append({"device": m.group(1), "name": m.group(2).strip() or m.group(1)})
    return out


# --- microphones -------------------------------------------------------------


def mics(payload: list[dict], default_name: str = "") -> list[dict]:
    """Real capture devices from `pactl --format=json list sources`.

    Monitor sources are dropped: every output has one, they outnumber the real
    inputs, and recording a monitor as "the mic" records the desktop back into the
    voice track. `default` marks the one PipeWire would pick, so the bar can open on
    it without a second query.
    """
    out = []
    for src in payload:
        name = str(src.get("name") or "")
        if not name or name.endswith(".monitor"):
            continue
        if str(src.get("monitor_source") or "") not in ("", "null"):
            continue
        out.append({
            "name": name,
            "label": str(src.get("description") or name),
            "default": name == default_name,
        })
    return out


# --- targets -----------------------------------------------------------------


def region_target_from_rect(x: int, y: int, w: int, h: int) -> str:
    return f"region:{w}x{h}+{x}+{y}"


def region_target(picked: str) -> str:
    """Canonical target for an `omarchy-capture-region ... --match-monitor` result.

    The picker prints either 'monitor:NAME' (kept as-is) or slurp's 'X,Y WxH'
    (rewritten to region:WxH+X+Y). Anything else -- an empty pick, picker noise --
    raises, because a malformed target printed to stdout poisons the recorder.
    """
    picked = picked.strip()
    if picked.startswith("monitor:") and len(picked) > len("monitor:"):
        return picked
    m = _SLURP_RE.match(picked)
    if not m:
        raise ValueError(f"unrecognized picker output: {picked!r}")
    x, y, w, h = (int(g) for g in m.groups())
    return region_target_from_rect(x, y, w, h)


def parse_target(target: str) -> dict:
    """Split a contract target back into its parts; raises on any malformed form.

    Exists so the consumer side has one authoritative parse to import, and so the
    contract tests can round-trip every form.
    """
    if target.startswith("monitor:") and len(target) > len("monitor:"):
        return {"kind": "monitor", "name": target[len("monitor:"):]}
    if target.startswith("camera:"):
        dev = target[len("camera:"):]
        if not re.match(r"^/dev/video\d+$", dev):
            raise ValueError(f"bad camera target: {target!r}")
        return {"kind": "camera", "device": dev}
    m = _REGION_RE.match(target)
    if m:
        w, h, x, y = (int(g) for g in m.groups())
        if w <= 0 or h <= 0:
            raise ValueError(f"empty region: {target!r}")
        return {"kind": "region", "x": x, "y": y, "width": w, "height": h}
    raise ValueError(f"unrecognized target: {target!r}")


# --- the stdout contract ------------------------------------------------------


def parse_camera_rect(raw: Any) -> dict[str, int] | None:
    """The self-view's placement, in absolute LOGICAL desktop pixels, or None.

    Absolute rather than normalized because the bar does not know what it is
    normalizing against: the self-view is dragged on a monitor-sized sheet, while the
    capture may be a region somewhere inside that monitor. Only capture.begin knows the
    capture rectangle, so it does the division and this stays a plain rect.

    None is a legitimate answer -- no camera, or a build of the bar that does not send
    one -- and means "use WebcamSettings' defaults", which is what every run did before
    the placement was carried at all.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"camera_rect must be an object, not {type(raw).__name__}")
    try:
        rect = {k: int(round(float(raw[k]))) for k in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"camera_rect needs numeric x/y/width/height: {raw!r}") from e
    if rect["width"] <= 0 or rect["height"] <= 0:
        raise ValueError(f"degenerate camera_rect {raw!r}")
    return rect


def config(target: str, mic: bool, desktop_audio: bool,
           camera: str, camera_device: str | None,
           countdown: int = 3, mic_device: str | None = None,
           camera_rect: Any = None) -> dict[str, Any]:
    """The one line bin/omarchy-capture-setup prints, validated.

    Keys are always all present (the consumer must not need .get defaults):
      target         monitor:NAME | region:WxH+X+Y | camera:/dev/videoN
      mic            bool -- record the microphone
      mic_device     the PulseAudio source to record, or null for the default one.
                     Null is not "no mic" -- `mic` decides that; it means the
                     recorder resolves the default at record time, which is the
                     right behaviour when the user never touched the picker.
      desktop_audio  bool -- record system audio
      camera         off | circle | rounded | rect (overlay shape; forced off for a
                     camera: target, where the camera IS the recording)
      camera_device  /dev/videoN or null when no camera exists
      camera_rect    where the self-view was left, as {x, y, width, height} in
                     absolute logical desktop pixels, or null. Null means "use the
                     WebcamSettings defaults" -- the placement is a preference, and a
                     recording must never fail for want of one.
      countdown      whole seconds of on-screen countdown AFTER the line printed.
                     When > 0 the line is emitted the moment Start is pressed so
                     capture init can overlap the countdown; every setup surface
                     is destroyed before `countdown` seconds elapse, and frames
                     captured earlier than that must not be kept. 0 means the
                     screen was already clean when the line arrived.
    """
    parsed = parse_target(target)
    if not isinstance(countdown, int) or not (0 <= countdown <= 10):
        raise ValueError(f"countdown must be an int in 0..10, not {countdown!r}")
    if camera not in CAMERA_MODES:
        raise ValueError(f"camera must be one of {CAMERA_MODES}, not {camera!r}")
    if camera_device is not None and not re.match(r"^/dev/video\d+$", camera_device):
        raise ValueError(f"bad camera_device: {camera_device!r}")
    # A source name reaches a shell as one gsr -a argument; anything with
    # whitespace in it is either not a real name or an injection attempt.
    if mic_device is not None and not re.match(r"^[A-Za-z0-9_.:+-]+$", mic_device):
        raise ValueError(f"bad mic_device: {mic_device!r}")
    if parsed["kind"] == "camera":
        # Recording the camera full-frame and overlaying it on itself is not a
        # thing; the setup UI disables the overlay row, and this guard makes the
        # contract hold even if the UI regresses.
        camera = "off"
        camera_device = parsed["device"]
    if camera != "off" and camera_device is None:
        raise ValueError("camera overlay requested but no camera_device")
    rect = parse_camera_rect(camera_rect) if camera != "off" else None
    return {
        "target": target,
        "mic": bool(mic),
        "mic_device": mic_device if mic else None,
        "desktop_audio": bool(desktop_audio),
        "camera": camera,
        "camera_device": camera_device,
        "camera_rect": rect,
        "countdown": countdown,
    }
