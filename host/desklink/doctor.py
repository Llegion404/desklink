"""`desklink doctor` — environment check for any distro / compositor.

Prints every dependency DeskLink cares about with a PASS/WARN/MISS line and
a fix hint, so "it doesn't work on my machine" turns into a one-command
diagnosis. Exit code 1 when a hard requirement is missing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from . import __version__
from . import config as cfgmod
from . import displays


def _session() -> tuple[str, bool]:
    """(session type, is_wayland)"""
    stype = (os.environ.get("XDG_SESSION_TYPE")
             or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else "?"))
    return stype, stype.lower() == "wayland"


def _gst_elements() -> dict[str, bool]:
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        return {name: Gst.ElementFactory.find(name) is not None
                for name in (
                    "pipewiresrc", "videoconvert", "videoscale",
                    "x264enc", "nvh264enc", "h264parse",
                    "opusenc", "oggmux", "oggdemux", "opusdec",
                    "audioconvert", "audioresample")}
    except Exception:
        return {}


def _portals() -> list[str]:
    """Interfaces the desktop portal actually offers (ScreenCast matters)."""
    if shutil.which("gdbus") is None:
        return []
    try:
        proc = subprocess.run(
            ["gdbus", "introspect", "--session", "--dest",
             "org.freedesktop.portal.Desktop", "--object-path",
             "/org/freedesktop/portal/desktop"], capture_output=True,
            text=True, timeout=5)
        text = proc.stdout
    except Exception:
        return []
    return [n for n in ("org.freedesktop.portal.ScreenCast",
                        "org.freedesktop.portal.RemoteDesktop",
                        "org.freedesktop.portal.Clipboard")
            if n in text]


def _installed_portal_backends() -> list[str]:
    found = []
    for base in ("/usr/share/xdg-desktop-portal/portals",
                 "/usr/local/share/xdg-desktop-portal/portals"):
        if os.path.isdir(base):
            for f in sorted(os.listdir(base)):
                if f.endswith(".portal"):
                    found.append(f[:-7])
    return found


def _pipewire_running() -> bool:
    if shutil.which("pactl"):
        try:
            proc = subprocess.run(["pactl", "info"], capture_output=True,
                                  text=True, timeout=5)
            return "PipeWire" in proc.stdout or proc.returncode == 0
        except Exception:
            pass
    return shutil.which("pipewire") is not None


def run() -> int:
    problems: list[str] = []

    def line(status: str, label: str, hint: str = ""):
        print(f"  [{status:>5}] {label}" + (f"  — {hint}" if hint else ""))
        if status == "MISS":
            problems.append(label)

    print(f"DeskLink doctor v{__version__}")

    # ---- session
    stype, wayland = _session()
    de = os.environ.get("XDG_CURRENT_DESKTOP") or "?"
    line("info", f"session: {stype}, desktop: {de}")
    if not wayland:
        line("MISS", "not a Wayland session",
             "capture + input run through Wayland portals; X11 is not "
             "supported")

    # ---- python / bindings
    v = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.version_info >= (3, 10):
        line("pass", f"python {v}")
    else:
        line("MISS", f"python {v}", "3.10+ required")
    try:
        import gi  # noqa: F401
        line("pass", "PyGObject")
    except ImportError:
        line("MISS", "PyGObject", "python3-gobject / python3-gi")

    # ---- gstreamer
    els = _gst_elements()
    if not els:
        line("MISS", "GStreamer introspection", "gstreamer + gst-python / "
             "gir1.2-gstreamer-1.0")
    else:
        for name, need, hint in (
            ("pipewiresrc", True, "gst-plugins-good / gstreamer1.0-plugins-good"),
            ("x264enc", True, "gst-plugins-ugly / gstreamer1.0-plugins-ugly"),
            ("videoconvert", True, "gst-plugins-base"),
            ("h264parse", True, "gst-plugins-bad / -good"),
            ("opusenc", True, "gst-plugins-base (opus)"),
            ("oggmux", True, "gst-plugins-base"),
            ("oggdemux", True, "gst-plugins-base"),
            ("opusdec", True, "gst-plugins-base (opus)"),
            ("audioconvert", True, "gst-plugins-base"),
            ("nvh264enc", False, "optional NVENC: gst-plugins-bad + NVIDIA"),
        ):
            got = els.get(name, False)
            if got:
                line("pass", f"gstreamer element {name}")
            elif need:
                line("MISS", f"gstreamer element {name}", hint)
            else:
                line("warn", f"gstreamer element {name} (optional)", hint)

    # ---- portals
    portals = _portals()
    for want, label in (("org.freedesktop.portal.ScreenCast", "ScreenCast portal"),
                        ("org.freedesktop.portal.RemoteDesktop", "RemoteDesktop portal")):
        if want in portals:
            line("pass", label)
        else:
            line("MISS", label, "install a desktop portal backend: "
                 "xdg-desktop-portal-kde (Plasma), -gnome (GNOME), "
                 "-wlr (sway/Hyprland), -hyprland (Hyprland)")
    if want not in portals or True:
        backends = _installed_portal_backends()
        if backends:
            line("pass", "portal backends installed: " + ", ".join(backends))
        else:
            line("warn", "no .portal files found under "
                 "/usr/share/xdg-desktop-portal/portals")

    # ---- display-size backend
    backend = displays.best_backend()
    if backend:
        line("pass", f"display-size tracking via {backend.name}")
    else:
        line("warn", "no display-size backend",
             "logical-size changes mid-session won't be tracked "
             "(portal-reported sizes still work)")

    # ---- audio prerequisites
    if _pipewire_running():
        line("pass", "PipeWire running (desktop audio available)")
    else:
        line("warn", "PipeWire not detected", "audio streaming needs it")

    # ---- misc tools
    for tool, label, needed in (("loginctl", "loginctl (lock-on-disconnect)", False),
                                ("notify-send", "notify-send (client "
                                 "connect popups)", False)):
        if shutil.which(tool):
            line("pass", label)
        elif needed:
            line("MISS", label)
        else:
            line("warn", label, "optional")

    # ---- config
    cfg = cfgmod.load()
    line("info", f"config: {cfgmod.CONFIG_PATH}")
    if cfg.pin_key:
        line("pass", "PIN is set")
    else:
        line("warn", "no PIN set", "python3 -m desklink set-pin")

    if problems:
        print(f"\n{len(problems)} problem(s): " + "; ".join(problems))
        return 1
    print("\nall hard requirements satisfied")
    return 0
