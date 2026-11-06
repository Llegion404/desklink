"""KDE display-geometry introspection for live resolution changes.

The portal reports stream sizes once, at session start. KWin keeps an active
screencast stream alive across monitor mode changes (it scales into the
negotiated buffer), so the video survives — but the *logical* desktop size
changes, and that is what input mapping scales normalized coordinates into.
There is no D-Bus signal for this, so we poll `kscreen-doctor -o` (libkscreen)
while clients are subscribed and refresh StreamInfo logical sizes in place.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

log = logging.getLogger("desklink.displays")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_OUT = re.compile(r"Output:\s+\d+\s+(\S+)")
_GEOM = re.compile(r"Geometry:\s+(-?\d+),(-?\d+)\s+(\d+)x(\d+)")


def kscreen_available() -> bool:
    return shutil.which("kscreen-doctor") is not None


def query_output_geometries() -> dict[tuple[int, int], tuple[int, int]]:
    """Return {logical (x, y): logical (w, h)} for enabled outputs.

    Keys are geometry origins so callers can match portal streams (which
    carry position + size) to outputs even when sizes have changed.
    """
    try:
        proc = subprocess.run(["kscreen-doctor", "-o"], capture_output=True,
                              text=True, timeout=5)
    except Exception as exc:  # missing binary, timeouts, ...
        log.debug("kscreen-doctor failed: %s", exc)
        return {}
    if proc.returncode != 0:
        return {}

    cur_enabled = False
    geoms: dict[tuple[int, int], tuple[int, int]] = {}
    for raw in proc.stdout.splitlines():
        line = _ANSI.sub("", raw).strip()
        if _OUT.search(line):
            cur_enabled = False  # new output block until we see "enabled"
            continue
        if line == "enabled":
            cur_enabled = True
            continue
        if line == "disabled":
            cur_enabled = False
            continue
        m = _GEOM.search(line)
        if m and cur_enabled:
            geoms[(int(m.group(1)), int(m.group(2)))] = (
                int(m.group(3)), int(m.group(4)))
    return geoms


def match_stream(stream, geoms: dict[tuple[int, int], tuple[int, int]]):
    """Find the output a portal stream maps to (None if unmatched).

    Exact origin match first; otherwise the output whose center is closest
    to the stream's center (origins shift when a neighbour resizes).
    """
    if (stream.x, stream.y) in geoms:
        return geoms[(stream.x, stream.y)]
    best, best_d = None, None
    scx = stream.x + stream.width / 2
    scy = stream.y + stream.height / 2
    for (ox, oy), (w, h) in geoms.items():
        d = (scx - (ox + w / 2)) ** 2 + (scy - (oy + h / 2)) ** 2
        if best_d is None or d < best_d:
            best, best_d = (w, h), d
    return best
