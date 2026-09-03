"""The mic level meter, shared by every surface that has to answer "is my mic on?".

Extracted from bin/omarchy-capture-setup when the recording HUD needed the same meter:
the setup bar asks the question before a take and the HUD asks it during one, and two
implementations of an RMS window would drift into two different ideas of what "loud"
looks like -- which is exactly the kind of thing nobody notices until the two are on
screen at the same time.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import threading

__all__ = ["MicMeter"]


class MicMeter:
    """RMS off the default source via parec, published as a 0..1 level plus dBFS.

    This feeds the bar's "is my mic actually working" meter, not an analyser: 8kHz
    mono s16, 100ms windows, fast attack / slow decay. If parec is missing or dies,
    level stays 0 and the meter rests -- setup must never block on audio plumbing.
    """

    def __init__(self) -> None:
        self.level = 0.0
        self.db = -120.0
        self.alive = False
        self.device = ""            # "" == whatever pactl calls the default
        self._proc: subprocess.Popen | None = None
        self._generation = 0

    def start(self, device: str = "") -> None:
        if shutil.which("parec") is None:
            return
        self.device = device
        self._generation += 1
        threading.Thread(target=self._pump, args=(self._generation,),
                         daemon=True).start()

    def retarget(self, device: str) -> None:
        """Point the meter at another source. The picker is the only caller: a
        meter still reading the old device would answer "is THIS mic working?"
        with another mic's level, which is worse than reading nothing."""
        if device == self.device and self.alive:
            return
        self.stop()
        self.level = 0.0
        self.db = -120.0
        self.start(device)

    def _pump(self, generation: int) -> None:
        argv = ["parec", "--raw", "--format=s16le", "--rate=8000", "--channels=1",
                "--latency-msec=60"]
        if self.device:
            argv += ["-d", self.device]
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return
        # A retarget that raced this start owns the meter; this pump exits rather
        # than fighting it for self.level.
        if generation != self._generation:
            proc.terminate()
            return
        self._proc = proc
        self.alive = True
        assert proc.stdout is not None
        window = 800 * 2  # 100ms of s16 mono at 8kHz
        while generation == self._generation:
            buf = proc.stdout.read(window)
            if not buf or len(buf) < window:
                break
            samples = struct.unpack(f"<{len(buf) // 2}h", buf)
            rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
            # Perceptual-ish mapping: -50dB..0dB onto 0..1, so room tone sits in
            # the tail and speech reaches the accent segments.
            db = 20.0 * math.log10(rms) if rms > 0 else -120.0
            target = max(0.0, min(1.0, (db + 50.0) / 50.0))
            self.level = target if target > self.level else self.level * 0.72
            self.db = db
        self.alive = False

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
