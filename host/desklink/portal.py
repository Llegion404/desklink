"""xdg-desktop-portal integration: RemoteDesktop + ScreenCast session.

Implements the canonical portal dance:
  CreateSession -> SelectDevices -> SelectSources -> Start -> OpenPipeWireRemote

KDE (xdg-desktop-portal-kde) backs this with PipeWire (capture) and
libei (input injection). The user sees one consent dialog, unless a valid
restore token is available (persist_mode), in which case no dialog appears.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field

from gi.repository import Gio, GLib

from .glibloop import GLibRunner

log = logging.getLogger("desklink.portal")

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
IFACE_RD = "org.freedesktop.portal.RemoteDesktop"
IFACE_SC = "org.freedesktop.portal.ScreenCast"
IFACE_CLIPBOARD = "org.freedesktop.portal.Clipboard"
IFACE_REQUEST = "org.freedesktop.portal.Request"
IFACE_SESSION = "org.freedesktop.portal.Session"

# device bits
DEV_KEYBOARD = 1
DEV_POINTER = 2
DEV_TOUCHSCREEN = 4
# SelectSources type bits
TYPE_MONITOR = 1
# cursor modes
CURSOR_HIDDEN = 1
CURSOR_EMBEDDED = 2
# persist modes
PERSIST_NONE = 0
PERSIST_REVOKE = 2

# evdev button codes
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
# axes
AXIS_HORIZONTAL = 0
AXIS_VERTICAL = 1


class PortalError(Exception):
    pass


def _notify(title: str, body: str):
    """Best-effort desktop notification (libnotify) so consent dialogs
    waiting in the background are noticed."""
    import subprocess
    try:
        subprocess.run(["notify-send", "-a", "DeskLink", "-u", "critical",
                        title, body], timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class UserCancelled(PortalError):
    pass


def _normalize_path(reply) -> str | None:
    """Unwrap pygobject method replies ('(o)') down to the path string."""
    for _ in range(3):
        if isinstance(reply, GLib.Variant):
            reply = reply.unpack()
            continue
        if isinstance(reply, (tuple, list)):
            reply = reply[0] if reply else None
            continue
        break
    return reply if isinstance(reply, str) else None


@dataclass
class StreamInfo:
    node_id: int
    width: int
    height: int
    x: int = 0
    y: int = 0
    source_type: int = TYPE_MONITOR


def _variant(value):
    if isinstance(value, GLib.Variant):
        return value
    if isinstance(value, bool):
        return GLib.Variant("b", value)
    if isinstance(value, int):
        return GLib.Variant("u", value) if value >= 0 else GLib.Variant("i", value)
    if isinstance(value, str):
        return GLib.Variant("s", value)
    if isinstance(value, tuple):  # e.g. position (i,i)
        if len(value) == 2:
            return GLib.Variant("(ii)", tuple(int(v) for v in value))
    raise TypeError(f"cannot pack {value!r}")


def vdict(d: dict) -> dict:
    """a{sv} payload as a *plain dict* of Variants.

    pygobject's GLib.Variant constructor requires raw Python containers as
    elements; a pre-built Variant cannot be nested inside a tuple/dict
    constructor (it gets iterated instead of boxed)."""
    return {k: _variant(v) for k, v in d.items()}


EMPTY_VDICT: dict = {}


class PortalSession:
    """A started RemoteDesktop(+ScreenCast) session."""

    def __init__(self, conn: Gio.DBusConnection, glib: GLibRunner, handle: str):
        self.conn = conn
        self.glib = glib
        self.handle = handle
        self.streams: list[StreamInfo] = []
        self.pw_fd: int | None = None
        self.restore_token: str | None = None
        self.devices: int = 0
        self.absolute_motion: bool = False
        self.clipboard_requested: bool = False
        self.on_closed = None  # callable | None
        self._closed_sub = conn.signal_subscribe(
            PORTAL_NAME, IFACE_SESSION, "Closed", handle, None,
            Gio.DBusSignalFlags.NONE, self._on_closed_signal)

    # ------------------------------------------------------------- lifecycle
    def _on_closed_signal(self, conn, sender, path, iface, signal, params):
        log.warning("portal session closed by the desktop: %r", params.unpack())
        if self.on_closed:
            self.on_closed()

    def close(self):
        if self._closed_sub:
            self.conn.signal_unsubscribe(self._closed_sub)
            self._closed_sub = None

        def _close():
            try:
                self.conn.call_sync(
                    PORTAL_NAME, self.handle, IFACE_SESSION, "Close",
                    None, None, Gio.DBusCallFlags.NONE, -1, None)
            except Exception:
                log.debug("session Close: already gone", exc_info=True)

        self.glib.submit(_close)

    async def probe_capabilities(self):
        """Detect whether absolute pointer motion works.

        The Notify* methods live on the portal object (with session_handle as
        the first argument), not on the session object. We probe with one
        harmless absolute move to the center of the first stream.
        """
        if not self.streams:
            self.absolute_motion = False
            return
        s = self.streams[0]
        try:
            await self.glib.call(lambda: self.conn.call_sync(
                PORTAL_NAME, PORTAL_PATH, IFACE_RD, "NotifyPointerMotionAbsolute",
                GLib.Variant("(oa{sv}udd)", (self.handle, {},
                                             s.node_id, s.width / 2, s.height / 2)),
                None, Gio.DBusCallFlags.NONE, 3000, None))
            self.absolute_motion = True
        except Exception as exc:
            log.warning("absolute motion unsupported (%s); using relative model", exc)
            self.absolute_motion = False
        log.info("session capabilities: absolute_motion=%s", self.absolute_motion)

    # ------------------------------------------------------------- input API
    def _notify(self, method: str, params: GLib.Variant):
        """Fire-and-forget call on the portal's RemoteDesktop interface."""
        def _call():
            self.conn.call(
                PORTAL_NAME, PORTAL_PATH, IFACE_RD, method, params,
                None, Gio.DBusCallFlags.NONE, -1, None, None, None)
        self.glib.submit(_call)

    def notify_motion_absolute(self, node_id: int, x: float, y: float):
        self._notify("NotifyPointerMotionAbsolute",
                     GLib.Variant("(oa{sv}udd)", (self.handle, {}, node_id, x, y)))

    def notify_motion_relative(self, dx: float, dy: float):
        self._notify("NotifyPointerMotion",
                     GLib.Variant("(oa{sv}dd)", (self.handle, {}, dx, dy)))

    def notify_button(self, button: int, down: bool):
        self._notify("NotifyPointerButton",
                     GLib.Variant("(oa{sv}iu)", (self.handle, {}, button, 1 if down else 0)))

    def notify_keycode(self, keycode: int, down: bool):
        self._notify("NotifyKeyboardKeycode",
                     GLib.Variant("(oa{sv}iu)", (self.handle, {}, keycode, 1 if down else 0)))

    def notify_keysym(self, keysym: int, down: bool):
        self._notify("NotifyKeyboardKeysym",
                     GLib.Variant("(oa{sv}iu)", (self.handle, {}, keysym, 1 if down else 0)))

    def notify_axis_discrete(self, axis: int, steps: int):
        self._notify("NotifyPointerAxisDiscrete",
                     GLib.Variant("(oa{sv}ui)", (self.handle, {}, axis, steps)))

    def notify_axis(self, dx: float, dy: float):
        self._notify("NotifyPointerAxis",
                     GLib.Variant("(oa{sv}dd)", (self.handle, {}, dx, dy)))


class Portal:
    def __init__(self, glib: GLibRunner, aloop: asyncio.AbstractEventLoop):
        self.glib = glib
        self.aloop = aloop
        self.conn: Gio.DBusConnection | None = None
        self.sender: str = ""  # unique-name fragment used in request paths

    # ------------------------------------------------------------- plumbing
    async def _connect(self):
        def _do():
            return Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.conn = await self.glib.call(_do)
        unique = self.conn.get_unique_name()  # ":1.42"
        self.sender = unique.lstrip(":").replace(".", "_")
        log.debug("session bus %s (sender token %s)", unique, self.sender)

    def _request_path(self, token: str) -> str:
        return f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"

    async def _request(self, iface: str, method: str, build_params,
                       timeout: float = 20.0, dialog: bool = False) -> dict:
        """Call a portal method following the request/response pattern.

        `build_params(handle_token)` must embed the given token as the
        `handle_token` option — the Response signal arrives at the request
        path derived from that exact token, which we subscribe BEFORE the
        call to avoid the reply-vs-subscribe race.
        """
        token = f"desklink{secrets.token_hex(8)}"
        request_path = self._request_path(token)
        fut: asyncio.Future = self.aloop.create_future()
        sub_ids: list[int] = []

        def on_response(conn, sender, path, iface_name, signal, params_var):
            code, results = params_var.unpack()

            def _resolve():
                if fut.done():
                    return
                if code == 0:
                    fut.set_result(results)
                else:
                    fut.set_exception(UserCancelled(f"portal dialog response {code}"))

            self.aloop.call_soon_threadsafe(_resolve)

        sub_ids.append(self.conn.signal_subscribe(
            PORTAL_NAME, IFACE_REQUEST, "Response", request_path, None,
            Gio.DBusSignalFlags.NONE, on_response))

        params = build_params(token)

        def _call():
            return self.conn.call_sync(
                PORTAL_NAME, PORTAL_PATH, iface, method, params,
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None)

        grace = 900.0 if dialog else 20.0
        try:
            reply = await asyncio.wait_for(self.glib.call(_call), timeout)
            handle = _normalize_path(reply)
            if handle and handle != request_path:
                # portal rewrote the token; catch up (tiny race, best effort)
                log.warning("request handle rewritten: predicted %s, got %s",
                            request_path, handle)
                sub_ids.append(self.conn.signal_subscribe(
                    PORTAL_NAME, IFACE_REQUEST, "Response", handle, None,
                    Gio.DBusSignalFlags.NONE, on_response))
            if dialog:
                log.info("consent dialog should be visible — waiting for approval "
                         "(up to %.0f s)", grace)
            return await asyncio.wait_for(fut, grace)
        finally:
            for sub in sub_ids:
                self.conn.signal_unsubscribe(sub)

    # ------------------------------------------------------------ the dance
    async def start(self, allow_clipboard: bool = True) -> PortalSession:
        if self.conn is None:
            await self._connect()
        return await self._start_attempt(allow_clipboard)

    async def _start_attempt(self, allow_clipboard: bool = True) -> PortalSession:
        session_token = f"desklink{secrets.token_hex(8)}"
        results = await self._request(
            IFACE_RD, "CreateSession",
            lambda tok: GLib.Variant("(a{sv})", (vdict({
                "handle_token": tok,
                "session_handle_token": session_token,
            }),)))
        session_handle = results.get("session_handle")
        if not session_handle:
            raise PortalError(f"no session_handle in CreateSession response: {results}")
        session = PortalSession(self.conn, self.glib, session_handle)
        log.info("portal session created: %s", session_handle)

        # -- devices (keyboard + pointer + touchscreen)
        # NOTE 1: despite the spec XML saying "devices", the frontend's option
        # filter (and libportal) use "types" — "devices" is silently dropped
        # and the backend then sees no devices.
        # NOTE 2: xdg-desktop-portal refuses persist_mode/restore_token on
        # RemoteDesktop sessions ("Remote desktop sessions cannot persist"),
        # so the consent dialog appears on every host start.
        await self._request(
            IFACE_RD, "SelectDevices",
            lambda tok: GLib.Variant("(oa{sv})", (
                session_handle,
                vdict({
                    "handle_token": tok,
                    "types": DEV_KEYBOARD | DEV_POINTER | DEV_TOUCHSCREEN,
                }),
            )))

        # -- sources (monitors, embedded cursor, multi-select)
        await self._request(
            IFACE_SC, "SelectSources",
            lambda tok: GLib.Variant("(oa{sv})", (
                session_handle,
                vdict({
                    "handle_token": tok,
                    "types": TYPE_MONITOR,
                    "multiple": True,
                    "cursor_mode": CURSOR_EMBEDDED,
                }),
            )))

        # -- clipboard access must be requested BEFORE Start (no dialog; the
        #    consent dialog covers it). Degrades gracefully if unsupported.
        if allow_clipboard:
            try:
                await self.glib.call(lambda: self.conn.call_sync(
                    PORTAL_NAME, PORTAL_PATH, IFACE_CLIPBOARD, "RequestClipboard",
                    GLib.Variant("(oa{sv})", (session_handle, vdict({}))),
                    None, Gio.DBusCallFlags.NONE, -1, None))
                session.clipboard_requested = True
                log.info("clipboard access requested")
            except Exception as exc:
                log.warning("clipboard portal unavailable, sync disabled: %s", exc)

        # -- start (this is where the consent dialog may appear)
        _notify("DeskLink", "Screen-sharing consent dialog is waiting for you")
        results = await self._request(
            IFACE_RD, "Start",
            lambda tok: GLib.Variant("(osa{sv})", (
                session_handle,
                "",
                vdict({"handle_token": tok}),
            )), timeout=30.0, dialog=True)

        session.devices = int(results.get("devices", 0))
        # the dialog itself may have declined clipboard even if we asked
        if session.clipboard_requested and results.get("clipboard_enabled") is False:
            session.clipboard_requested = False
            log.info("clipboard declined in the consent dialog")
        for node_id, props in results.get("streams", []):
            pos = props.get("position", (0, 0))
            size = props.get("size", (0, 0))
            session.streams.append(StreamInfo(
                node_id=node_id,
                width=int(size[0]), height=int(size[1]),
                x=int(pos[0]), y=int(pos[1]),
                source_type=int(props.get("source_type", TYPE_MONITOR))))
        if not session.streams:
            raise PortalError(f"portal returned no streams: {results}")
        log.info("portal streams: %s", session.streams)

        # -- PipeWire remote fd
        session.pw_fd = await self._open_pipewire_remote(session_handle)
        await session.probe_capabilities()
        return session

    async def _open_pipewire_remote(self, session_handle: str) -> int:
        # Modern portals return the fd directly: (h)
        def _direct():
            result, fd_list = self.conn.call_with_unix_fd_list_sync(
                PORTAL_NAME, PORTAL_PATH, IFACE_SC, "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (session_handle, vdict({}))),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
            (index,) = result.unpack()
            return fd_list.get(index)

        try:
            return await self.glib.call(_direct)
        except Exception as exc:
            # Pre-1.10 portals delivered the fd via the Response signal's fd
            # list, which pygobject cannot expose. KDE 6.x always returns it
            # directly, so treat failure as fatal with a clear message.
            raise PortalError(
                f"OpenPipeWireRemote failed (portal too old or denied): {exc}") from exc
