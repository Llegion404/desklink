"""Two-way clipboard sync via the org.freedesktop.portal.Clipboard interface
(on the RemoteDesktop session; KDE backs it with Klipper).

Push (client -> desktop):  SetSelection(mime_types), then serve the text via
  SelectionTransfer signal -> SelectionWrite(serial) -> write fd -> SelectionWriteDone.
Pull (desktop -> client):  SelectionOwnerChanged signal -> SelectionRead(mime)
  -> read fd -> on_text callback.
"""

from __future__ import annotations

import logging
import os

from gi.repository import Gio, GLib

from .portal import PORTAL_NAME, PORTAL_PATH, PortalSession

log = logging.getLogger("desklink.clipboard")

IFACE_CLIP = "org.freedesktop.portal.Clipboard"
TEXT_MIME = "text/plain;charset=utf-8"


class ClipboardBridge:
    def __init__(self, session: PortalSession, on_text):
        """on_text(str) is invoked (from the GLib thread) whenever the local
        clipboard changes."""
        self.session = session
        self.on_text = on_text
        self.enabled = False
        self._local_text: str | None = None
        self._subs: list[int] = []
        if not session.clipboard_requested:
            log.info("clipboard access was not granted; sync disabled")
            return

        self._subs.append(session.conn.signal_subscribe(
            PORTAL_NAME, IFACE_CLIP, "SelectionOwnerChanged", None, None,
            Gio.DBusSignalFlags.NONE, self._on_owner_changed))
        self._subs.append(session.conn.signal_subscribe(
            PORTAL_NAME, IFACE_CLIP, "SelectionTransfer", None, None,
            Gio.DBusSignalFlags.NONE, self._on_transfer))
        self.enabled = True
        log.info("clipboard bridge active")

    def close(self):
        for sub in self._subs:
            self.session.conn.signal_unsubscribe(sub)
        self._subs = []

    # ------------------------------------------------------------- helpers
    def _call(self, method: str, params: GLib.Variant):
        return self.session.conn.call_sync(
            PORTAL_NAME, PORTAL_PATH, IFACE_CLIP, method, params,
            None, Gio.DBusCallFlags.NONE, -1, None)

    def _call_fd(self, method: str, params: GLib.Variant) -> int:
        result, fd_list = self.session.conn.call_with_unix_fd_list_sync(
            PORTAL_NAME, PORTAL_PATH, IFACE_CLIP, method, params,
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
        (index,) = result.unpack()
        return fd_list.get(index)

    # ------------------------------------------------------------ push path
    def set_text(self, text: str):
        """Advertise `text` as the new clipboard owner (lazy: served on demand)."""
        if not self.enabled:
            return
        self._local_text = text
        try:
            self._call("SetSelection", GLib.Variant("(oa{sv})", (
                self.session.handle,
                {"mime_types": GLib.Variant("as", [TEXT_MIME])},
            )))
        except Exception:
            log.exception("SetClipboard failed")

    def _on_transfer(self, conn, sender, path, iface, signal, params):
        session_handle, mime_type, serial = params.unpack()
        if session_handle != self.session.handle:
            return
        if not mime_type.startswith("text/plain"):
            self._selection_write_done(serial, False)
            return
        if self._local_text is None:
            self._selection_write_done(serial, False)
            return
        try:
            fd = self._call_fd("SelectionWrite",
                               GLib.Variant("(ou)", (self.session.handle, serial)))
            data = self._local_text.encode("utf-8")
            with os.fdopen(os.dup(fd), "wb", closefd=True) as w:
                w.write(data)
            os.close(fd)
            self._selection_write_done(serial, True)
        except Exception:
            log.exception("SelectionWrite failed")
            self._selection_write_done(serial, False)

    def _selection_write_done(self, serial: int, success: bool):
        try:
            self._call("SelectionWriteDone",
                       GLib.Variant("(oub)", (self.session.handle, serial, success)))
        except Exception:
            log.exception("SelectionWriteDone failed")

    # ------------------------------------------------------------ pull path
    def _on_owner_changed(self, conn, sender, path, iface, signal, params):
        session_handle, options = params.unpack()
        if session_handle != self.session.handle:
            return
        if options.get("session_is_owner", False):
            return  # our own selection; would be an echo
        mimes = options.get("mime_types", [])
        if not any(str(m).startswith("text/plain") for m in mimes):
            return
        try:
            fd = self._call_fd("SelectionRead",
                               GLib.Variant("(os)", (self.session.handle, TEXT_MIME)))
            with os.fdopen(os.dup(fd), "rb", closefd=True) as r:
                data = r.read(4 * 1024 * 1024)
            os.close(fd)
            text = data.decode("utf-8", errors="replace")
            if text and self.on_text:
                self.on_text(text)
        except Exception:
            log.exception("SelectionRead failed")
