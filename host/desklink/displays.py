"""Display-geometry introspection for live logical-size changes.

The portal reports stream sizes once, at session start. Many compositors
keep an active screencast stream alive across monitor mode changes (they
scale into the negotiated buffer), so the video survives — but the
*logical* desktop size changes, and that is what input mapping scales
normalized coordinates into. There is no portal signal for this, so while
clients are subscribed we poll the compositor's own introspection and
refresh StreamInfo logical sizes in place.

Backends, tried by XDG_CURRENT_DESKTOP first, then by tool presence:
  KDE / Plasma   kscreen-doctor -o
  Hyprland       hyprctl monitors
  wlroots        wlr-randr        (sway, river, wayfire, ...)
  GNOME          busctl -> org.gnome.Mutter.DisplayConfig.GetCurrentState (JSON)

All parsers are pure functions over tool output so they can be unit-tested
without the compositor. Anything that fails returns {} — polling degrades
to a no-op and portal-reported sizes stay in effect (best effort; tested
live on KDE, implemented per tool output spec for the others).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("desklink.displays")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

Geoms = dict[tuple[int, int], tuple[int, int]]


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except Exception as exc:  # missing binary, timeouts, ...
        log.debug("%s failed: %s", cmd[0], exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def match_stream(stream, geoms: Geoms):
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


# ------------------------------------------------------------------ KDE
_OUT = re.compile(r"Output:\s+\d+\s+(\S+)")
_GEOM = re.compile(r"Geometry:\s+(-?\d+),(-?\d+)\s+(\d+)x(\d+)")


def parse_kscreen(text: str) -> Geoms:
    cur_enabled = False
    geoms: Geoms = {}
    for raw in text.splitlines():
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


# ------------------------------------------------------------- Hyprland
_HYPR_HEAD = re.compile(r"^Monitor\s+\S+\s+\(ID \d+\):")
_HYPR_MODE = re.compile(r"^\s+(\d+)x(\d+)@\S+\s+at\s+(-?\d+)x(-?\d+)")
_HYPR_SCALE = re.compile(r"^\s+scale:\s+([\d.]+)")
_HYPR_DISABLED = re.compile(r"^\s+disabled:\s+yes")


def parse_hyprctl(text: str) -> Geoms:
    geoms: Geoms = {}
    disabled = False
    mode = scale = pos = None
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        if _HYPR_HEAD.search(line):
            # flush the previous block
            if mode and pos and not disabled:
                s = scale or 1.0
                geoms[pos] = (round(mode[0] / s), round(mode[1] / s))
            disabled, mode, scale, pos = False, None, None, None
            continue
        if (m := _HYPR_MODE.search(line)):
            mode = (int(m.group(1)), int(m.group(2)))
            pos = (int(m.group(3)), int(m.group(4)))
        elif (m := _HYPR_SCALE.search(line)):
            scale = float(m.group(1))
        elif _HYPR_DISABLED.search(line):
            disabled = True
    if mode and pos and not disabled:
        s = scale or 1.0
        geoms[pos] = (round(mode[0] / s), round(mode[1] / s))
    return geoms


# --------------------------------------------------------------- wlroots
_WLR_NAME = re.compile(r"^\S+\s+\"")
_WLR_MODE = re.compile(r"^\s+(\d+)x(\d+)@\S+.*\(current\)")
_WLR_POS = re.compile(r"^\s*Position:\s*(-?\d+),\s*(-?\d+)")
_WLR_SCALE = re.compile(r"^\s*Scale:\s*([\d.]+)")
_WLR_ENABLED = re.compile(r"^\s*Enabled:\s*(yes|no)")


def parse_wlr_randr(text: str) -> Geoms:
    geoms: Geoms = {}
    enabled = True
    mode = scale = pos = None
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        if _WLR_NAME.search(line):
            if mode and pos and enabled:
                s = scale or 1.0
                geoms[pos] = (round(mode[0] / s), round(mode[1] / s))
            enabled, mode, scale, pos = True, None, None, None
            continue
        if (m := _WLR_ENABLED.search(line)):
            enabled = m.group(1) == "yes"
        elif (m := _WLR_MODE.search(line)):
            mode = (int(m.group(1)), int(m.group(2)))
        elif (m := _WLR_POS.search(line)):
            pos = (int(m.group(1)), int(m.group(2)))
        elif (m := _WLR_SCALE.search(line)):
            scale = float(m.group(1))
    if mode and pos and enabled:
        s = scale or 1.0
        geoms[pos] = (round(mode[0] / s), round(mode[1] / s))
    return geoms


# ----------------------------------------------------------------- GNOME
# busctl --json gives GetCurrentState as plain JSON — no GVariant syntax to
# wrestle. data = [serial, monitors, logical_monitors, properties]:
#   monitors[i] = [name, vendor, product, serial,
#                  [[mode, w, h, refresh, scale, [scales], {props}], ...], {...}]
#   logical[j]  = [x, y, scale, transform, primary,
#                  [[name, vendor, product, serial, mode, ...], ...]]


def parse_mutter(text: str) -> Geoms:
    import json
    state = json.loads(text).get("data", [])
    if len(state) < 3:
        return {}
    try:
        modes: dict[str, dict[str, tuple[int, int]]] = {}
        for mon in state[1]:
            mname = mon[0]
            modes[mname] = {m[0]: (int(m[1]), int(m[2])) for m in mon[4]}
        geoms: Geoms = {}
        for lg in state[2]:
            x, y, scale, transform = int(lg[0]), int(lg[1]), float(lg[2]), int(lg[3])
            first = lg[5][0]           # [name, vendor, product, serial, mode, ...]
            mname, mode_name = first[0], first[4]
            w, h = modes.get(mname, {}).get(mode_name, (0, 0))
            if not w:
                continue
            if scale:
                w, h = w / scale, h / scale
            if transform in (1, 3):    # 90°/270°
                w, h = h, w
            geoms[(x, y)] = (round(w), round(h))
        return geoms
    except Exception:
        return {}


# -------------------------------------------------------------- selection
class Backend:
    def __init__(self, name, tool, cmd, parser):
        self.name = name
        self.tool = tool            # binary the parser reads
        self.cmd = cmd
        self.parse = parser
        self.env_keys = ()          # XDG_CURRENT_DESKTOP keywords

    def tool_ok(self) -> bool:
        return shutil.which(self.tool) is not None

    def query(self) -> Geoms:
        text = _run(self.cmd)
        if text is None:
            return {}
        try:
            return self.parse(text)
        except Exception:
            log.debug("%s parse failed", self.name, exc_info=True)
            return {}


_BACKENDS = [
    Backend("KDE (kscreen-doctor)", "kscreen-doctor", ["kscreen-doctor", "-o"],
            parse_kscreen),
    Backend("Hyprland (hyprctl)", "hyprctl", ["hyprctl", "monitors"],
            parse_hyprctl),
    Backend("wlroots (wlr-randr)", "wlr-randr", ["wlr-randr"], parse_wlr_randr),
]


def _gdbus_backend():
    if shutil.which("busctl") is None:
        return None
    return Backend("GNOME (Mutter DisplayConfig)", "busctl",
                   ["busctl", "call", "--user", "--json=short",
                    "org.gnome.Mutter.DisplayConfig",
                    "/org/gnome/Mutter/DisplayConfig",
                    "org.gnome.Mutter.DisplayConfig", "GetCurrentState"],
                   parse_mutter)


def best_backend():
    """Pick the display backend for the running session (None = none)."""
    env = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    mutter = _gdbus_backend()
    order = [
        ("kde", _BACKENDS[0]), ("plasma", _BACKENDS[0]),
        ("hyprland", _BACKENDS[1]),
        ("sway", _BACKENDS[2]), ("wlroots", _BACKENDS[2]),
        ("wayfire", _BACKENDS[2]), ("river", _BACKENDS[2]),
        ("labwc", _BACKENDS[2]),
        ("gnome", mutter),
    ]
    for keyword, backend in order:
        if keyword in env and backend is not None and backend.tool_ok():
            return backend
    # no (or unknown) desktop env: probe tools — but only mutter when GNOME
    # bits exist, because GetCurrentState on a non-GNOME session errors out
    for backend in _BACKENDS:
        if backend.tool_ok():
            return backend
    if mutter is not None and os.environ.get("WAYLAND_DISPLAY"):
        return mutter
    return None


# ---------------------------------------------- backwards-compat helpers
def kscreen_available() -> bool:
    return shutil.which("kscreen-doctor") is not None


def query_output_geometries() -> Geoms:
    backend = best_backend()
    return backend.query() if backend else {}
