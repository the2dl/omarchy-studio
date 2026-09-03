#!/usr/bin/env python3
"""Report every setup-bar control that something else is drawn over.

    ./bin/omarchy-capture-setup --selftest 20000 --probe-input > /tmp/audit.log
    .venv/bin/python tests/input_audit.py /tmp/audit.log

    # and with a device list open, which is the interesting case:
    ./bin/omarchy-capture-setup --selftest 20000 --probe-input --open-picker mic > ...

Not a pytest test: it needs a live Hyprland and a mapped window, so it is run by hand.

WHAT IT ANSWERS. Every control carries an objectName beginning "ctl:". With
--probe-input the sheet hit-tests each one at its own centre and four corners and
reports which control it actually finds there. Anything other than the control
itself means something is drawn over it, and a user aiming there will miss.

WHY THIS AND NOT SYNTHETIC CLICKS. The bug this exists for made every control on the
bar except Start recording dead. The bar was a separate window, the monitor-sized
sheet mapped after it and covered it, and clicks aimed at the bar were delivered to
the sheet -- while the bar stayed visible through it. A qmltestrunner click, being
synthesised inside the process, would have reported every control working.

That failure needed the compositor to see, and it was chased with cursor warps and
hover reports. Two findings retired that approach:

  * A MouseArea accepts hover and stops it reaching a HoverHandler on an ancestor, so
    the readout went silent over exactly the controls it was meant to measure.
  * The bar is now an item inside the sheet (editor/setup/SetupBar.qml), so there is
    one window and the compositor has nothing left to route wrongly.

What remains is intra-window occlusion, which is geometry, so geometry is what this
measures. If the bar is ever given its own window again, this stops being sufficient.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

POINTS = ("centre", "TL", "TR", "BL", "BR")


def reaches(log: Path) -> list[tuple[str, list[str]]]:
    out = []
    for line in log.read_text(errors="replace").splitlines():
        m = re.search(r"PROBE REACH (\S+) (.+)$", line)
        if m:
            out.append((m.group(1), m.group(2).split()))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: input_audit.py LOGFILE")
        return 2
    log = Path(argv[1])
    if not log.exists():
        print(f"{log}: no such file")
        return 2
    controls = reaches(log)
    if not controls:
        print("no PROBE REACH lines -- was it run with --probe-input?")
        return 2

    bad = []
    print(f"{'control':<24} {'centre':<24} corners")
    print("-" * 78)
    for name, hits in controls:
        centre, corners = hits[0], hits[1:]
        ok = all(h == name for h in hits)
        if not ok:
            bad.append((name, dict(zip(POINTS, hits))))
        others = sorted(set(corners))
        print(f"{name:<24} {centre:<24} "
              f"{'ok' if ok else 'COVERED BY ' + ','.join(o for o in others if o != name)}")
    print("-" * 78)

    if bad:
        print(f"\n{len(bad)} control(s) something is drawn over:")
        for name, hits in bad:
            covered = {p: h for p, h in hits.items() if h != name}
            print(f"  {name}: {covered}")
        return 1
    print(f"\nall {len(controls)} controls answer for their own centre and four corners")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
