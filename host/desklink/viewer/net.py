"""Viewer networking: framed TCP client with PIN auth + auto-reconnect.

Same wire protocol as every other client (see protocol.py). Runs on an
asyncio loop owned by this class (started from the GTK main via
`start_in_thread()`); events are marshalled onto the GTK main loop with
`GLib.idle_add` so app.py never touches threads.

Hot path: video frames do NOT go through GLib.idle_add — `on_frame` is
called directly on the reader thread (appsrc.push_buffer is thread-safe).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
from dataclasses import dataclass

from gi.repository import GLib

from .. import protocol as proto

log = logging.getLogger("desklink.viewer")

MAX_RECONNECTS = 4
CONNECT_TIMEOUT = 8.0
READ_STALL_TIMEOUT = 30.0   # half-open connections must not stall forever


# ----------------------------------------------------------------- events
@dataclass
class StreamDesc:
    id: int
    w: int
    h: int


@dataclass
class EvHello:
    host: str
    clipboard: bool
    audio: bool
    streams: list[StreamDesc]


@dataclass
class EvSubscribed:
    stream: int


@dataclass
class EvStreamInfo:
    stream: int
    w: int
    h: int


@dataclass
class EvQuality:
    kbps: int
    maxh: int
    ok: bool


@dataclass
class EvPong:
    rtt_ms: int


@dataclass
class EvClip:
    text: str


@dataclass
class EvError:
    msg: str          # stream-error / host-error / deny, for the status line


@dataclass
class EvClosed:
    reason: str
    will_retry: bool


class ViewerClient:
    """One client per viewer session; survives reconnects internally."""

    def __init__(self, host: str, port: int, pin: str,
                 on_event, on_frame, on_audio=None):
        self.host = host
        self.port = port
        self.pin = pin
        self.on_event = on_event        # (event) -> None, on GTK thread
        self.on_frame = on_frame        # (stream, keyframe, pts_us, b) reader thread
        self.on_audio = on_audio        # (kind, data) reader thread
        self.aloop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._stopping = False
        self._connected = False
        self._reconnects = 0

    # ------------------------------------------------------------ lifecycle
    def start_in_thread(self):
        self.aloop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._thread_main, name="viewer-net", daemon=True)
        self._thread.start()

    def _thread_main(self):
        asyncio.set_event_loop(self.aloop)
        try:
            self.aloop.run_until_complete(self._run_forever())
        except Exception:
            log.exception("viewer net loop crashed")

    async def _run_forever(self):
        while not self._stopping:
            try:
                await self._connect_and_read()
            except _AuthDenied as exc:
                self._emit(EvError(f"denied: {exc}"))
                self._emit(EvClosed(str(exc), will_retry=False))
                return
            except Exception as exc:
                reason = _reason_of(exc)
                log.info("viewer connection lost: %s", reason)
            self._connected = False
            if self._stopping:
                return
            if self._reconnects >= MAX_RECONNECTS:
                self._emit(EvClosed("disconnected", will_retry=False))
                return
            wait = 1.5 * (2 ** self._reconnects)
            self._reconnects += 1
            self._emit(EvError(f"reconnecting in {wait:.0f}s "
                               f"({self._reconnects}/{MAX_RECONNECTS})"))
            await asyncio.sleep(wait)

    async def _connect_and_read(self):
        log.info("connecting to %s:%d", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), 8)

        ftype, payload = await proto.read_frame(self._reader)
        greet = json.loads(payload)
        if greet.get("type") != "greet":
            raise ConnectionError("no greet")
        pin_key = hashlib.sha256(self.pin.encode()).digest()
        mac = proto.compute_mac(pin_key, greet["salt"])
        self._writer.write(proto.json_message(
            {"type": "auth", "mac": mac, "client": "desklink-viewer"}))
        await self._writer.drain()

        ftype, payload = await proto.read_frame(self._reader)
        msg = json.loads(payload)
        if msg.get("type") == "deny":
            raise _AuthDenied(msg.get("msg", "denied"))
        if msg.get("type") != "hello":
            raise ConnectionError(f"unexpected {msg.get('type')} after auth")

        self._connected = True
        self._reconnects = 0    # a healthy session resets the backoff
        self._emit(EvHello(msg.get("host", "?"),
                           bool(msg.get("clipboard")),
                           bool(msg.get("audio")),
                           _parse_streams(msg)))
        await self._read_loop()

    async def _read_loop(self):
        while not self._stopping:
            # stall guard: a half-open connection (dropped tunnel, silent
            # network loss) must surface as Closed and trigger a reconnect
            ftype, payload = await asyncio.wait_for(
                proto.read_frame(self._reader), READ_STALL_TIMEOUT)
            if ftype == proto.FT_VIDEO:
                stream, flags, pts, data = proto.parse_video_payload(payload)
                self.on_frame(stream, bool(flags & proto.FLAG_KEYFRAME),
                              pts, data)
            elif ftype == proto.FT_AUDIO:
                if self.on_audio is not None:
                    kind, data = proto.parse_audio_payload(payload)
                    self.on_audio(kind, data)
            elif ftype == proto.FT_JSON:
                msg = json.loads(payload)
                for evt in _json_event(msg):
                    self._emit(evt)
                if msg.get("type") == "bye":
                    raise ConnectionError("server said bye")

    # ------------------------------------------------------------ control
    def _ensure_call(self, coro_factory):
        """Run a coroutine on the net loop from any thread (fire-and-forget)."""
        loop = self.aloop
        if loop is None or not loop.is_running():
            return

        def _submit():
            if self._stopping or self._writer is None:
                return
            loop.create_task(coro_factory())

        loop.call_soon_threadsafe(_submit)

    def send_json(self, obj: dict):
        async def _send():
            self._writer.write(proto.json_message(obj))
            await self._writer.drain()
        self._ensure_call(_send)

    def subscribe(self, stream: int):
        self.send_json({"type": "subscribe", "stream": stream})

    def unsubscribe(self):
        self.send_json({"type": "unsubscribe"})

    def set_quality(self, kbps: int, maxh: int):
        self.send_json({"type": "set-quality", "kbps": kbps, "maxh": maxh})

    def request_idr(self, stream: int):
        self.send_json({"type": "request-idr", "stream": stream})

    def set_audio(self, on: bool, fmt: str):
        self.send_json({"type": "set-audio", "on": on, "format": fmt})

    def ping(self):
        self.send_json({"type": "ping", "t": proto.now_ms()})

    def clip_set(self, text: str):
        self.send_json({"type": "clip-set", "text": text[:1_000_000]})

    @property
    def connected(self) -> bool:
        return self._connected

    # --------------------------------------------------------------- close
    def close(self, reason: str = "viewer closed"):
        self._stopping = True
        loop = self.aloop
        if loop is not None and loop.is_running():
            def _shutdown():
                async def _do():
                    try:
                        if self._writer is not None:
                            self._writer.write(
                                proto.json_message({"type": "bye"}))
                            with _suppress():
                                await asyncio.wait_for(
                                    self._writer.drain(), 1)
                    finally:
                        # unblocks the reader parked in read_frame
                        if self._writer is not None:
                            self._writer.close()
                loop.create_task(_do())
            loop.call_soon_threadsafe(_shutdown)

    # -------------------------------------------------------------- helpers
    def _emit(self, evt):
        try:
            GLib.idle_add(self.on_event, evt)
        except Exception:
            log.exception("event emit failed")


class _AuthDenied(Exception):
    pass


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _parse_streams(msg: dict) -> list[StreamDesc]:
    return [StreamDesc(int(s["id"]), int(s["w"]), int(s["h"]))
            for s in msg.get("streams", [])]


def _json_event(msg: dict):
    mtype = msg.get("type")
    if mtype == "hello":
        # session was re-requested host-side: re-announce + resubscribe
        return [EvHello(msg.get("host", "?"), bool(msg.get("clipboard")),
                        bool(msg.get("audio")), _parse_streams(msg))]
    if mtype == "subscribed":
        return [EvSubscribed(int(msg.get("stream", -1)))]
    if mtype == "stream-info":
        return [EvStreamInfo(int(msg.get("stream", -1)),
                             int(msg.get("w", 0)), int(msg.get("h", 0)))]
    if mtype == "quality":
        return [EvQuality(int(msg.get("kbps", 0)), int(msg.get("maxh", 0)),
                          bool(msg.get("ok")))]
    if mtype == "pong":
        return [EvPong(max(0, proto.now_ms() - int(msg.get("t", 0))))]
    if mtype == "clip":
        return [EvClip(str(msg.get("text", "")))]
    if mtype == "stream-error":
        return [EvError(f"stream error: {msg.get('msg', '?')}")]
    if mtype == "host-error":
        return [EvError(str(msg.get("msg", "host error")))]
    return []


def _reason_of(exc: Exception) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "connect timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                        asyncio.IncompleteReadError)):
        return "connection lost"
    return str(exc) or exc.__class__.__name__
