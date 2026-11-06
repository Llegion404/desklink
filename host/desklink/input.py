"""Route client input events to the RemoteDesktop portal.

The portal's NotifyMotion is relative-only on some backends; when
NotifyMotionAbsolute is available we use it (no drift). Otherwise a
host-side cursor model converts absolute targets into relative deltas.

Motion events are coalesced through a GLib idle callback so a flood of
touch-move events results in at most one D-Bus call per main-loop tick.
"""

from __future__ import annotations

import logging

from .glibloop import GLibRunner
from .portal import (
    AXIS_HORIZONTAL, AXIS_VERTICAL,
    BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, PortalSession,
)

log = logging.getLogger("desklink.input")

BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}

# evdev keycodes for control-ish characters
KEY_ENTER = 28
KEY_TAB = 15
KEY_BACKSPACE = 14


def char_to_keysym(ch: str) -> int:
    cp = ord(ch)
    if cp < 0x20 or cp == 0x7F:
        return 0
    if cp < 0x100:
        return cp            # Latin-1 range: X keysym == codepoint
    return 0x01000000 | cp   # Unicode keysym


class InputRouter:
    def __init__(self, session: PortalSession, glib: GLibRunner):
        self.session = session
        self.glib = glib
        # fallback cursor model per stream: {(node_id): [x, y]}
        self._cursor: dict[int, list[float]] = {
            s.node_id: [s.width / 2, s.height / 2] for s in session.streams
        }
        self._pending: tuple[int, float, float] | None = None
        self._flush_scheduled = False
        # everything currently held down, so we can release on disconnect —
        # a stuck modifier makes the whole desktop misbehave
        self._held_buttons: set[int] = set()
        self._held_keys: set[int] = set()
        self._held_keysyms: set[int] = set()

    # -------------------------------------------------------------- dispatch
    async def handle(self, msg: dict) -> None:
        mtype = msg.get("type")
        handler = getattr(self, f"_on_{mtype.replace('-', '_')}", None) if mtype else None
        if handler is None:
            log.debug("ignoring unknown control message: %r", msg)
            return
        try:
            handler(msg)
        except Exception:
            log.exception("input handler failed for %r", msg)

    # --------------------------------------------------------------- motion
    def _on_motion(self, msg: dict):
        node = int(msg.get("stream", 0))
        stream = next((s for s in self.session.streams if s.node_id == node), None)
        if stream is None:
            return
        w, h = max(stream.width, 1), max(stream.height, 1)
        px = min(max(float(msg.get("x", 0.5)), 0.0), 1.0) * (w - 1)
        py = min(max(float(msg.get("y", 0.5)), 0.0), 1.0) * (h - 1)
        self._pending = (node, px, py)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            self.glib.submit(self._flush_motion)

    def _flush_motion(self):
        self._flush_scheduled = False
        pending, self._pending = self._pending, None
        if pending is None:
            return
        node, px, py = pending
        if self.session.absolute_motion:
            self.session.notify_motion_absolute(node, px, py)
            return
        cur = self._cursor.get(node)
        if cur is None:
            return
        dx, dy = px - cur[0], py - cur[1]
        cur[0], cur[1] = px, py
        if dx or dy:
            self.session.notify_motion_relative(dx, dy)

    # -------------------------------------------------------------- buttons
    def _on_button(self, msg: dict):
        btn = BUTTONS.get(str(msg.get("btn", "")))
        down = bool(msg.get("down"))
        if btn is not None:
            self.session.notify_button(btn, down)
            if down:
                self._held_buttons.add(btn)
            else:
                self._held_buttons.discard(btn)

    def _on_scroll(self, msg: dict):
        dx, dy = int(msg.get("dx", 0) or 0), int(msg.get("dy", 0) or 0)
        if dy:
            self.session.notify_axis_discrete(AXIS_VERTICAL, dy)
        if dx:
            self.session.notify_axis_discrete(AXIS_HORIZONTAL, dx)

    # ------------------------------------------------------------- keyboard
    def _on_key(self, msg: dict):
        code = int(msg.get("code", 0) or 0)
        down = bool(msg.get("down"))
        if code > 0:
            self.session.notify_keycode(code, down)
            if down:
                self._held_keys.add(code)
            else:
                self._held_keys.discard(code)

    def _on_keysym(self, msg: dict):
        sym = int(msg.get("sym", 0) or 0)
        down = bool(msg.get("down"))
        if sym > 0:
            self.session.notify_keysym(sym, down)
            # tracked so release_all() can lift stuck modifier keysyms too
            # (desktop viewers send Shift/Ctrl etc. as keysyms)
            if down:
                self._held_keysyms.add(sym)
            else:
                self._held_keysyms.discard(sym)

    def _on_text(self, msg: dict):
        text = str(msg.get("s", ""))
        for ch in text:
            if ch == "\n":
                self.session.notify_keycode(KEY_ENTER, True)
                self.session.notify_keycode(KEY_ENTER, False)
            elif ch == "\t":
                self.session.notify_keycode(KEY_TAB, True)
                self.session.notify_keycode(KEY_TAB, False)
            elif ch == "\b":
                self.session.notify_keycode(KEY_BACKSPACE, True)
                self.session.notify_keycode(KEY_BACKSPACE, False)
            else:
                sym = char_to_keysym(ch)
                if sym:
                    self.session.notify_keysym(sym, True)
                    self.session.notify_keysym(sym, False)

    # --------------------------------------------------------------- safety
    def release_all(self):
        """Release every held button/key (call on client disconnect/shutdown)
        so the desktop is never left with a stuck modifier or drag."""
        n_keys = len(self._held_keys) + len(self._held_keysyms)
        n_buttons = len(self._held_buttons)
        for code in list(self._held_keys):
            self.session.notify_keycode(code, False)
        self._held_keys.clear()
        for sym in list(self._held_keysyms):
            self.session.notify_keysym(sym, False)
        self._held_keysyms.clear()
        for btn in list(self._held_buttons):
            self.session.notify_button(btn, False)
        self._held_buttons.clear()
        if n_keys or n_buttons:
            log.info("released %d held keys and %d held buttons",
                     n_keys, n_buttons)
