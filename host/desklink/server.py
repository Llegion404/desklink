"""Asyncio TCP server tying everything together.

Per client: PIN challenge-response handshake, control-message dispatch
(input / clipboard / files / stream subscription), and a Sender that
multiplexes video (lossy, drop-oldest) and data frames (lossless,
backpressured) onto the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque

from . import config as cfgmod
from . import displays
from . import protocol as proto
from .audio import (AudioManager, OPUS_HEAD, default_sink_node as
                    audio_default_sink_node, available as audio_available)
from .clipboard import ClipboardBridge
from .files import FileManager, SandboxViolation
from .glibloop import GLibRunner
from .input import InputRouter
from .portal import Portal, PortalSession, UserCancelled
from .video import VideoManager

log = logging.getLogger("desklink.server")

AUTH_TIMEOUT = 10.0
VIDEO_QUEUE = 30            # ~1s at 30fps — shallow so drops happen here,
                            # not in TCP buffers (relay paths bufferbloat)
IDR_THROTTLE = 1.0          # seconds between forced keyframes (large frames —
                            # forcing them often amplifies congestion)


def _desktop_notify(title: str, body: str, urgent: bool = False):
    """Best-effort libnotify popup (QoL: see who is connected)."""
    import subprocess
    try:
        subprocess.run(
            ["notify-send", "-a", "DeskLink", "-t", "4000"] +
            (["-u", "critical"] if urgent else []) + [title, body],
            timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class Sender:
    """Lossy video lane + lossless data lane over one StreamWriter."""

    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.video: deque[bytes] = deque()
        self.data: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._wakeup = asyncio.Event()
        self.closed = False
        self.bytes_out = 0
        self.frames_out = 0
        self.dropped = 0
        self._task = asyncio.create_task(self._run())

    def offer_video(self, payload: bytes) -> bool:
        """Queue a video frame. Returns True if an older frame was dropped."""
        self.video.append(payload)
        if len(self.video) > VIDEO_QUEUE:
            self.video.popleft()
            self.dropped += 1
            self._wakeup.set()
            return True
        self._wakeup.set()
        return False

    async def put_data(self, payload: bytes):
        await self.data.put(payload)
        self._wakeup.set()

    async def send_json(self, obj: dict):
        await self.put_data(proto.json_message(obj))

    async def _run(self):
        try:
            while True:
                await self._wakeup.wait()
                self._wakeup.clear()
                while self.video:
                    payload = self.video.popleft()
                    self.writer.write(payload)
                    self.bytes_out += len(payload)
                    self.frames_out += 1
                while not self.data.empty():
                    payload = self.data.get_nowait()
                    self.writer.write(payload)
                    self.bytes_out += len(payload)
                await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("sender loop crashed")
        finally:
            with contextlib.suppress(Exception):
                self.writer.close()

    async def close(self):
        self.closed = True
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


class Client:
    _next_id = 1

    def __init__(self, server: "DeskLinkServer", writer: asyncio.StreamWriter):
        self.id = Client._next_id
        Client._next_id += 1
        self.server = server
        self.writer = writer
        self.addr = writer.get_extra_info("peername")
        self.sender = Sender(writer)
        self.authenticated = False
        self.name = ""                      # user-agent from the auth msg
        self.audio: tuple[bool, str] | None = None   # (on, "raw"|"ogg")
        self.audio_hdr_sent = False           # got the replayed Ogg headers
        self.stream: int | None = None      # subscribed stream (node_id)
        self.quality = (server.cfg.bitrate_kbps, 0)  # (kbps, maxh) per client
        self.pkey: tuple | None = None      # pipeline key = (node, maxh, kbps)
        self.uploads: dict[int, dict] = {}  # file_id -> {fh, path, remaining, req_id}
        self.last_json_log = 0.0

    @property
    def alive(self) -> bool:
        return not self.sender._task.done()


class DeskLinkServer:
    def __init__(self, cfg: cfgmod.Config):
        self.cfg = cfg
        self.glib: GLibRunner | None = None
        self.session: PortalSession | None = None
        self.video: VideoManager | None = None
        self.input: InputRouter | None = None
        self.clipboard: ClipboardBridge | None = None
        self.files = FileManager(cfg.fs_roots(), cfg.downloads_path)
        self.clients: set[Client] = set()
        # PipeWire node ids are unbounded (this session: 299/301) but the
        # wire format carries stream ids in one byte — expose compact
        # indices to clients and translate at the edges. Rebuilt per session.
        self.node_to_idx: dict[int, int] = {}
        self.idx_to_node: dict[int, int] = {}
        self._idr_last: dict[tuple, float] = {}
        # last known encoded size per pipeline key — a client joining an
        # already-running encode must be told the size (the pipeline itself
        # only reports on CHANGE)
        self._encoded: dict[tuple, tuple] = {}
        self._server: asyncio.AbstractServer | None = None
        self._stopping = asyncio.Event()
        self._restarting = False
        self._aloop: asyncio.AbstractEventLoop | None = None
        self._display_task: asyncio.Task | None = None
        self._display_backend = None      # displays.Backend (WM-specific)
        self._control = None                # ControlServer (desklink tui)
        self.audio_mgr: AudioManager | None = None
        self._audio_ogg_hdrs: list[bytes] = []   # captured Ogg header pages
        self._adapt: dict[tuple, dict] = {}  # branch key -> adapt state
        self._adapt_task: asyncio.Task | None = None
        self._started = time.monotonic()

    # ------------------------------------------------------------ lifecycle
    async def run(self) -> int:
        aloop = asyncio.get_running_loop()
        self.glib = GLibRunner(aloop)
        self.glib.start()
        self._aloop = aloop
        try:
            await self._start_portal_session()
            self._server = await asyncio.start_server(
                self._handle_client, host="0.0.0.0", port=self.cfg.port)
            if self.cfg.control_socket:
                from .control import ControlServer
                self._control = ControlServer(self)
                await self._control.start()
            log.info("listening on 0.0.0.0:%d (streams: %s)",
                     self.cfg.port,
                     [f"{s.node_id}:{s.width}x{s.height}" for s in self.session.streams])
            await self._stopping.wait()
            return 0
        finally:
            await self.shutdown()

    async def _start_portal_session(self):
        """Request a portal session and (re)wire everything that depends on
        it. Also used by the auto-restart path after a session dies; keeps
        TCP clients connected and re-announces streams via a fresh hello."""
        aloop = self._aloop
        portal = Portal(self.glib, aloop)
        log.info("requesting remote-desktop portal session "
                 "(consent dialog may appear)...")
        session = await portal.start(allow_clipboard=self.cfg.allow_clipboard)
        self.session = session
        session.on_closed = lambda: aloop.call_soon_threadsafe(
            self._on_session_closed)

        self.video = VideoManager(self.glib, self.cfg)
        self.video.setup(session)
        self.node_to_idx = {s.node_id: i for i, s in enumerate(session.streams)}
        self.idx_to_node = {i: s.node_id for i, s in enumerate(session.streams)}
        self.video.on_frame = lambda sid, kf, pts, data: \
            aloop.call_soon_threadsafe(self._distribute, sid, kf, pts, data)
        # fires from the GLib thread (pipeline start failure / encoder retry)
        # — must be marshaled like every other cross-thread callback
        self.video.on_stream_error = lambda key, msg: \
            aloop.call_soon_threadsafe(self._on_stream_error, key, msg)
        self.video.on_encoded_size = lambda sid, w, h: \
            aloop.call_soon_threadsafe(self._on_encoded_size, sid, w, h)
        self.video.on_capture_lost = lambda sid, msg: \
            aloop.call_soon_threadsafe(self._on_capture_lost, sid, msg)

        self.input = InputRouter(session, self.glib)

        if session.clipboard_requested and self.cfg.allow_clipboard:
            self.clipboard = ClipboardBridge(
                session,
                on_text=lambda text: aloop.call_soon_threadsafe(
                    self._broadcast_clipboard, text))

        # desktop audio is independent of the portal session but rebuilt
        # with it for one consistent lifecycle. The sink-node lookup runs a
        # subprocess (pw-dump) — resolve it HERE, off the GLib thread; only
        # report audio capability if the pipeline actually started.
        self.audio_mgr = None
        if audio_available():
            node = await asyncio.to_thread(audio_default_sink_node)
            mgr = AudioManager(self.glib)
            mgr.on_raw = lambda data: aloop.call_soon_threadsafe(
                self._distribute_audio, proto.AUDIO_OPUS, data)
            mgr.on_ogg = lambda data: aloop.call_soon_threadsafe(
                self._distribute_audio, proto.AUDIO_OGG, data)
            try:
                ok = await self.glib.call(lambda: mgr.start(node))
            except Exception:
                log.exception("audio manager start failed")
                ok = False
            if ok:
                self.audio_mgr = mgr
            else:
                log.info("audio disabled: pipeline did not start")

        # clients that survived a session restart re-announce + resubscribe
        hello = self._hello_msg()
        for client in list(self.clients):
            if client.authenticated:
                await client.sender.send_json(hello)

        self._display_backend = displays.best_backend()
        if self._display_backend:
            log.info("display-size tracking via %s", self._display_backend.name)
            if self._display_task:
                self._display_task.cancel()
            self._display_task = asyncio.ensure_future(self._poll_display_sizes())
        else:
            log.info("no display-size backend (portal-reported sizes only)")

        if self.cfg.adaptive_bitrate:
            self.video.on_branch_reset = lambda key: \
                aloop.call_soon_threadsafe(self._adapt.pop, key, None)
            if self._adapt_task:
                self._adapt_task.cancel()
            self._adapt_task = asyncio.ensure_future(self._adapt_loop())

    def _hello_msg(self) -> dict:
        return {
            "type": "hello",
            "v": proto.PROTO_VERSION,
            "host": self.cfg.name,
            "clipboard": bool(self.clipboard and self.clipboard.enabled),
            "audio": self.audio_mgr is not None,
            "streams": [{"id": self.node_to_idx[s.node_id],
                         "w": s.width, "h": s.height,
                         "x": s.x, "y": s.y} for s in self.session.streams],
        }

    def _distribute_audio(self, kind: int, data: bytes):
        """Fan desktop audio out to clients that enabled it; each client gets
        exactly its chosen framing (raw Opus packets or Ogg pages).

        Ogg pages with granulepos 0 are the stream header pages (BOS +
        OpusTags) — oggmux emitted them once at pipeline start, so they must
        be captured and replayed to every Ogg client BEFORE its live pages,
        or oggdemux never builds the chain."""
        if kind == proto.AUDIO_OGG:
            granule = int.from_bytes(data[6:14], "little", signed=True)
            if granule == 0:
                if (len(self._audio_ogg_hdrs) < 4
                        and data not in self._audio_ogg_hdrs):
                    self._audio_ogg_hdrs.append(data)
                return                     # headers are per-client, not live
        payload = proto.frame(proto.FT_AUDIO, proto.audio_payload(kind, data))
        for client in list(self.clients):
            if not client.authenticated or client.audio is None:
                continue
            on, fmt = client.audio
            if not on:
                continue
            if (kind == proto.AUDIO_OGG) != (fmt == "ogg"):
                continue
            if fmt == "ogg" and not client.audio_hdr_sent:
                if not self._audio_ogg_hdrs:
                    continue               # headers not seen yet — hold pages
                for h in self._audio_ogg_hdrs:
                    asyncio.ensure_future(client.sender.put_data(proto.frame(
                        proto.FT_AUDIO,
                        proto.audio_payload(proto.AUDIO_OGG, h))))
                client.audio_hdr_sent = True
            asyncio.ensure_future(client.sender.put_data(payload))

    async def _send_audio_config(self, client: Client):
        """OpusHead preface for raw-format clients (MediaCodec csd-0)."""
        payload = proto.frame(
            proto.FT_AUDIO,
            proto.audio_payload(proto.AUDIO_OPUS_HEAD, OPUS_HEAD))
        await client.sender.put_data(payload)

    def _teardown_session_objects(self):
        """Tear down everything bound to the current portal session (safe to
        call repeatedly; nulls refs so request paths can guard on them)."""
        if self._display_task:
            self._display_task.cancel()
            self._display_task = None
        if self._adapt_task:
            self._adapt_task.cancel()
            self._adapt_task = None
        self._adapt.clear()
        if self.input:
            self.input.release_all()
            self.input = None
        if self.video:
            self.video.stop_all()
            self.video = None
        if self.clipboard:
            self.clipboard.close()
            self.clipboard = None
        if self.audio_mgr:
            self.glib.submit(self.audio_mgr.stop)
            self.audio_mgr = None
        self._audio_ogg_hdrs.clear()
        if self.session:
            self.session.close()
            self.session = None
        self.node_to_idx = {}
        self.idx_to_node = {}
        # node ids change across sessions — stale entries would only mislead
        self._encoded.clear()
        self._idr_last.clear()

    async def shutdown(self):
        log.info("shutting down")
        if self._control:
            await self._control.stop()
            self._control = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for client in list(self.clients):
            with contextlib.suppress(Exception):
                await client.sender.send_json({"type": "bye", "msg": "server stopping"})
                await asyncio.wait_for(client.sender.close(), 2)
        self._teardown_session_objects()
        if self.glib:
            await asyncio.sleep(0.1)
            self.glib.stop()

    def _on_session_closed(self):
        if self._stopping.is_set() or self._restarting:
            return
        log.warning("portal session closed; re-requesting a new session")
        self._teardown_session_objects()
        for client in list(self.clients):
            with contextlib.suppress(Exception):
                asyncio.ensure_future(client.sender.send_json(
                    {"type": "host-error",
                     "msg": "display session closed — re-requesting "
                            "(approve the dialog)"}))
        self._restarting = True
        asyncio.ensure_future(self._restart_portal())

    def _on_capture_lost(self, stream_id: int, message: str):
        """PipeWire capture died (portal backend crash or a stream revoked
        without a Closed signal) — same recovery as a closed session."""
        log.warning("capture lost on node %d (%s); treating as session loss",
                    stream_id, message)
        self._on_session_closed()

    async def _restart_portal(self):
        try:
            await asyncio.wait_for(self._start_portal_session(), timeout=120)
            log.info("portal session restored; clients can resume")
        except Exception as exc:
            log.error("portal re-request failed: %s", exc)
            for client in list(self.clients):
                with contextlib.suppress(Exception):
                    await client.sender.send_json(
                        {"type": "host-error",
                         "msg": f"portal re-request failed — stopping ({exc})"})
            self._stopping.set()
        finally:
            self._restarting = False

    async def _poll_display_sizes(self):
        """Compositors keep screencast streams alive across monitor mode
        changes (they scale into the negotiated buffer), so video survives —
        but the *logical* desktop size changes and input mapping scales
        normalized coordinates into it. No portal signal exists for that, so
        poll the compositor's introspection tool (displays backend) while
        someone is subscribed."""
        await asyncio.sleep(2.0)
        while not self._stopping.is_set():
            if (self.session is not None and self.video is not None
                    and any(c.stream is not None for c in self.clients)
                    and self._display_backend):
                try:
                    geoms = await asyncio.to_thread(
                        self._display_backend.query)
                except Exception:
                    geoms = {}
                if geoms and self.session is not None:
                    for s in self.session.streams:
                        size = displays.match_stream(s, geoms)
                        if size and (s.width, s.height) != size:
                            log.info("stream node %d logical size %dx%d -> "
                                     "%dx%d (display changed)",
                                     s.node_id, s.width, s.height, *size)
                            s.width, s.height = size
            await asyncio.sleep(3.0)

    async def _adapt_loop(self):
        """Per-branch adaptive bitrate: drops mean some subscriber's link
        can't keep up with the branch's encode, so lower its live bitrate;
        long clean periods claw it back toward the configured value. The
        branch KEY (and thus sharing) is untouched — only the encoder's
        setpoint moves."""
        win_s = 5.0
        await asyncio.sleep(4)
        while not self._stopping.is_set():
            await asyncio.sleep(win_s)
            if not self.video or self._restarting:
                self._adapt.clear()
                continue
            for key, st in list(self._adapt.items()):
                frames, drops = st["frames"], st["drops"]
                st["frames"] = st["drops"] = 0
                base = key[2]
                if frames < 30:          # too quiet to judge this window
                    continue
                rate = drops / frames
                if rate > 0.02 and st["kbps"] > 500:
                    st["kbps"] = max(500, int(st["kbps"] * 0.7))
                    st["idle"] = 0.0
                    log.info("adaptive: branch %r -> %d kbps "
                             "(%.0f%% frames dropped)", (key[1], key[2]),
                             st["kbps"], rate * 100)
                    self.video.set_branch_bitrate(key, st["kbps"])
                elif rate == 0:
                    st["idle"] += win_s
                    if st["idle"] >= 30 and st["kbps"] < base:
                        st["kbps"] = min(base, int(st["kbps"] * 1.25))
                        st["idle"] = 0.0
                        log.info("adaptive: branch %r recovered -> %d kbps",
                                 (key[1], key[2]), st["kbps"])
                        self.video.set_branch_bitrate(key, st["kbps"])
                else:
                    st["idle"] = 0.0

    def _on_stream_error(self, key: tuple, message: str):
        idx = self.node_to_idx.get(key[0], key[0])
        for client in list(self.clients):
            if client.pkey == key:
                asyncio.ensure_future(client.sender.send_json(
                    {"type": "stream-error", "stream": idx, "msg": message}))

    def _on_encoded_size(self, key: tuple, w: int, h: int):
        """Actual encoded resolution (after any max_h downscale) — pushed to
        the clients on this pipeline so they can letterbox + label correctly."""
        self._encoded[key] = (w, h)
        idx = self.node_to_idx.get(key[0], key[0])
        log.info("stream %d encoded size %dx%d (key %r)", idx, w, h,
                 (key[1], key[2]))
        for client in list(self.clients):
            if client.authenticated and client.pkey == key:
                asyncio.ensure_future(client.sender.send_json(
                    {"type": "stream-info", "stream": idx, "w": w, "h": h}))

    def stop(self):
        self._stopping.set()

    # ------------------------------------------------- management (control)
    def snapshot(self) -> dict:
        """State for `desklink tui` / the control socket. Cheap, read-only;
        copies what it reads so the caller never sees live containers."""
        streams = []
        if self.session:
            for s in self.session.streams:
                branches = []
                cap = self.video._captures.get(s.node_id) if self.video else None
                if cap is not None:
                    for key in list(cap._order):
                        b = cap.branches.get(key, {})
                        branches.append({
                            "maxh": key[1], "kbps": b.get("cur_kbps", key[2]),
                            "encoder": b.get("enc_name", ""),
                            "size": list(b.get("dims") or (0, 0)),
                            "clients": self.video._refcounts.get(key, 0),
                        })
                streams.append({
                    "id": self.node_to_idx.get(s.node_id, -1),
                    "node": s.node_id,
                    "logical": [s.width, s.height],
                    "branches": branches,
                })
        clients = []
        for c in list(self.clients):
            cstream = (self.node_to_idx.get(c.stream, -1)
                       if c.stream is not None else None)
            q = len(c.sender.video) if c.sender else 0
            clients.append({
                "id": c.id, "name": c.name,
                "addr": f"{c.addr[0]}:{c.addr[1]}" if c.addr else "?",
                "auth": c.authenticated, "stream": cstream,
                "audio": ({"on": c.audio[0], "format": c.audio[1]}
                          if c.audio else None),
                "kbps": c.quality[0], "maxh": c.quality[1],
                "kbps_out": int(c.sender.bytes_out * 8 / 1000)
                if c.sender else 0,
                "frames": c.sender.frames_out if c.sender else 0,
                "dropped": c.sender.dropped if c.sender else 0,
                "video_q": q,
            })
        return {
            "v": 1,
            "host": self.cfg.name,
            "port": self.cfg.port,
            "uptime_s": int(time.monotonic() - self._started),
            "pin_set": self.cfg.pin_key is not None,
            "clipboard": bool(self.clipboard and self.clipboard.enabled),
            "restarting": self._restarting,
            "streams": streams,
            "clients": clients,
        }

    async def kick(self, client_id: int) -> dict:
        """Disconnect one client (bye + socket close; the regular disconnect
        path then releases its input/pipeline refs)."""
        for c in list(self.clients):
            if c.id == client_id:
                log.info("client #%d (%s) kicked via control socket",
                         c.id, c.name or "?")
                with contextlib.suppress(Exception):
                    await c.sender.send_json(
                        {"type": "bye", "msg": "kicked by host"})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(c.sender.close(), 2)
                return {"ok": True, "kicked": client_id}
        return {"ok": False, "error": f"no client #{client_id}"}

    def force_keyframe(self, stream_idx: int) -> dict:
        """Force an IDR on every branch of the given stream (control socket
        and `desklink tui`)."""
        node = self.idx_to_node.get(stream_idx)
        cap = self.video._captures.get(node) if (node is not None
                                                 and self.video) else None
        if cap is None:
            return {"ok": False,
                    "error": f"no capture on stream #{stream_idx}"}
        self.glib.submit(cap.force_idr, None)   # None = every branch
        return {"ok": True}

    # ------------------------------------------------------------ fan-out
    def _distribute(self, key: tuple, keyframe: bool, pts_us: int, data: bytes):
        if not self.clients:
            return
        idx = self.node_to_idx.get(key[0], 0)  # compact id fits one byte
        payload = proto.frame(
            proto.FT_VIDEO,
            proto.video_payload(idx,
                                 proto.FLAG_KEYFRAME if keyframe else 0,
                                 pts_us, data))
        st = self._adapt.get(key)
        if st is None:
            st = {"frames": 0, "drops": 0, "kbps": key[2], "idle": 0.0}
            self._adapt[key] = st
        st["frames"] += 1
        for client in self.clients:
            if client.authenticated and client.pkey == key:
                dropped = client.sender.offer_video(payload)
                if dropped:
                    self._maybe_force_idr(key)
                    st["drops"] += 1

    def _maybe_force_idr(self, key: tuple):
        now = time.monotonic()
        if now - self._idr_last.get(key, 0.0) < IDR_THROTTLE:
            return
        self._idr_last[key] = now
        if self.video:
            self.video.force_idr(key)

    def _broadcast_clipboard(self, text: str):
        log.info("host clipboard changed (%d chars) -> clients", len(text))
        for client in list(self.clients):
            if client.authenticated:
                asyncio.ensure_future(client.sender.send_json(
                    {"type": "clip", "text": text}))

    # ------------------------------------------------------------ clients
    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        client = Client(self, writer)
        self.clients.add(client)
        log.info("client #%d connected from %s", client.id, client.addr)
        try:
            ok, msg = await self._handshake(reader, writer, client)
            if not ok:
                return
            await self._client_loop(reader, client)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("client #%d handler error", client.id)
        finally:
            log.info("client #%d disconnected (out %d KB, %d frames, %d dropped)",
                     client.id, client.sender.bytes_out // 1024,
                     client.sender.frames_out, client.sender.dropped)
            self.clients.discard(client)
            if client.authenticated and not self.clients:
                _desktop_notify("DeskLink", "All clients disconnected")
                self._maybe_lock_session()
            if client.pkey and self.video:
                self.video.unsubscribe(client.pkey)
                self._adapt.pop(client.pkey, None)
                client.stream = None
                client.pkey = None
            for up in client.uploads.values():
                with contextlib.suppress(Exception):
                    up["fh"].close()
            # never leave injected inputs held down on the desktop
            if self.input:
                self.glib.submit(self.input.release_all)
            await client.sender.close()

    def _maybe_lock_session(self):
        """Optional privacy: lock the session when the last authenticated
        client leaves (config `lock_on_disconnect`)."""
        if not (self.cfg.lock_on_disconnect and not self._stopping.is_set()):
            return
        import subprocess
        try:
            r = subprocess.run(["loginctl", "lock-session"], timeout=5,
                               capture_output=True, text=True)
            if r.returncode == 0:
                log.info("session locked (last client disconnected)")
            else:
                log.warning("lock-session failed: %s", (r.stderr or "").strip())
        except Exception as exc:
            log.warning("lock-session failed: %s", exc)

    async def _handshake(self, reader, writer, client) -> tuple[bool, str]:
        salt = proto.new_salt()
        writer.write(proto.json_message(
            {"type": "greet", "v": proto.PROTO_VERSION, "salt": salt,
             "name": self.cfg.name}))
        await writer.drain()
        try:
            ftype, payload = await asyncio.wait_for(
                proto.read_frame(reader), AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            return False, "auth timeout"
        if ftype != proto.FT_JSON:
            return False, "expected auth"
        msg = json.loads(payload)
        if msg.get("type") != "auth":
            return False, "expected auth"
        if self.cfg.pin_key is None:
            await client.sender.send_json(
                {"type": "deny", "msg": "no PIN configured; run desklink set-pin"})
            return False, "no pin"
        if not proto.verify_mac(self.cfg.pin_key, salt, str(msg.get("mac", ""))):
            await client.sender.send_json({"type": "deny", "msg": "bad pin"})
            log.warning("client #%d failed auth", client.id)
            return False, "bad pin"
        client.authenticated = True
        client.name = str(msg.get("client", "?"))
        _desktop_notify("DeskLink",
                        f"Client connected from {client.addr[0]} "
                        f"({msg.get('client', '?')})")
        # during a portal re-request there is no session yet — hold the hello
        # briefly so clients survive the consent-dialog gap, then give up
        # (they see "connection lost" and auto-retry)
        for _ in range(90):
            if self.session is not None or self._stopping.is_set():
                break
            await asyncio.sleep(0.5)
        if self.session is None:
            log.info("client #%d dropping: no display session available",
                     client.id)
            return False, "no display session"
        await client.sender.send_json(self._hello_msg())
        log.info("client #%d authenticated (%s)", client.id, msg.get("client", "?"))
        return True, "ok"

    async def _client_loop(self, reader, client):
        while True:
            ftype, payload = await proto.read_frame(reader)
            if ftype == proto.FT_JSON:
                await self._handle_control(json.loads(payload), client)
            elif ftype == proto.FT_FILE:
                self._handle_file_chunk(payload, client)
            # video/clipboard frames from clients are ignored

    # ------------------------------------------------------------ control
    async def _handle_control(self, msg: dict, client: Client):
        mtype = msg.get("type")
        if mtype == "subscribe":
            node = self.idx_to_node.get(int(msg.get("stream", -1)))
            if node is None or not self.video:
                await client.sender.send_json(
                    {"type": "stream-error", "stream": msg.get("stream"),
                     "msg": f"unknown stream {msg.get('stream')}"})
                return
            if client.stream is not None and client.stream != node:
                if client.pkey:
                    self.video.unsubscribe(client.pkey)
                client.stream = None
                client.pkey = None
            if client.stream != node:
                client.pkey = self.video.subscribe(node, *client.quality)
                client.stream = node
                log.info("client #%d subscribed node %d (maxh %s, %d kbps)",
                         client.id, node, client.quality[1] or "native",
                         client.quality[0])
            await client.sender.send_json(
                {"type": "subscribed", "stream": self.node_to_idx[node]})
            # joining an already-running encode: push the cached encoded size
            dims = self._encoded.get(client.pkey)
            if dims:
                await client.sender.send_json(
                    {"type": "stream-info", "stream": self.node_to_idx[node],
                     "w": dims[0], "h": dims[1]})
        elif mtype == "set-quality":
            # per-client: this client's pipeline is (re)built with its own
            # settings; other clients keep theirs — no more global cap
            kbps = max(500, min(50000, int(msg.get("kbps", 8000) or 8000)))
            maxh = max(0, int(msg.get("maxh", 0) or 0))
            if (self.video and client.stream is not None
                    and client.pkey is not None):
                self._adapt.pop(client.pkey, None)   # old branch's stats
                client.pkey = self.video.move_quality(
                    client.stream, client.pkey, kbps, maxh)
                # moving onto an already-running encode: push its cached size
                dims = self._encoded.get(client.pkey)
                if dims:
                    await client.sender.send_json(
                        {"type": "stream-info",
                         "stream": self.node_to_idx[client.stream],
                         "w": dims[0], "h": dims[1]})
            client.quality = (kbps, maxh)
            log.info("client #%d quality -> %d kbps, max height %s",
                     client.id, kbps, maxh or "native")
            await client.sender.send_json(
                {"type": "quality", "kbps": kbps, "maxh": maxh, "ok": True})
        elif mtype == "set-audio":
            on = bool(msg.get("on"))
            fmt = "ogg" if msg.get("format") == "ogg" else "raw"
            ok = self.audio_mgr is not None
            client.audio = (on, fmt) if ok else None
            client.audio_hdr_sent = False
            if ok and on and fmt == "raw":
                await self._send_audio_config(client)
            await client.sender.send_json(
                {"type": "audio", "on": on, "format": fmt, "ok": ok})
        elif mtype == "unsubscribe":
            if client.pkey and self.video:
                self.video.unsubscribe(client.pkey)
                self._adapt.pop(client.pkey, None)
            client.stream = None
            client.pkey = None
        elif mtype == "request-idr":
            if client.pkey and self.video:
                self.video.force_idr(client.pkey)
        elif mtype == "ping":
            await client.sender.send_json(
                {"type": "pong", "t": msg.get("t"), "host": proto.now_ms()})
        elif mtype in ("motion", "button", "scroll", "key", "keysym", "text"):
            if self.input is None:
                return
            if mtype == "motion":
                msg["stream"] = self.idx_to_node.get(
                    int(msg.get("stream", -1)),
                    next(iter(self.node_to_idx), None))
            await self.input.handle(msg)
        elif mtype == "clip-set":
            if self.clipboard and self.clipboard.enabled:
                text = str(msg.get("text", ""))[:1_000_000]
                self.glib.submit(self.clipboard.set_text, text)
        elif mtype == "fs-list":
            reply = self.files.list_dir(str(msg.get("path", "~")))
            reply.update({"type": "fs-list-reply", "id": msg.get("id")})
            await client.sender.send_json(reply)
        elif mtype == "fs-get":
            await self._serve_file(msg, client)
        elif mtype == "fs-put":
            await self._begin_upload(msg, client)
        elif mtype == "fs-put-end":
            await self._end_upload(msg, client)
        elif mtype == "bye":
            raise ConnectionResetError("client said bye")
        else:
            log.debug("client #%d unknown message %r", client.id, mtype)

    # ------------------------------------------------------------ files
    async def _serve_file(self, msg: dict, client: Client):
        req_id = int(msg.get("id", 0))
        try:
            path, size = self.files.open_read(str(msg.get("path", "")))
        except (SandboxViolation, OSError) as exc:
            await client.sender.send_json(
                {"type": "fs-error", "id": req_id, "msg": str(exc)})
            return
        await client.sender.send_json(
            {"type": "fs-data-begin", "id": req_id, "size": size,
             "name": path.name, "path": str(path)})
        fh = await asyncio.get_running_loop().run_in_executor(None, open, path, "rb")
        try:
            while True:
                chunk = await asyncio.get_running_loop().run_in_executor(
                    None, fh.read, self.files.CHUNK)
                if not chunk:
                    break
                await client.sender.put_data(
                    proto.frame(proto.FT_FILE, proto.file_payload(req_id & 0xFFFFFFFF, chunk)))
            await client.sender.send_json({"type": "fs-data-end", "id": req_id})
            log.info("served %s (%d bytes) to client #%d", path, size, client.id)
        finally:
            fh.close()

    async def _begin_upload(self, msg: dict, client: Client):
        req_id = int(msg.get("id", 0))
        size = int(msg.get("size", 0))
        try:
            target = self.files.open_write(str(msg.get("name", "upload.bin")))
            fh = open(target, "wb")
        except OSError as exc:
            await client.sender.send_json(
                {"type": "fs-error", "id": req_id, "msg": str(exc)})
            return
        client.uploads[req_id] = {"fh": fh, "path": target,
                                  "remaining": size, "req_id": req_id}
        await client.sender.send_json({"type": "fs-put-ack", "id": req_id, "ok": True})

    def _handle_file_chunk(self, payload: bytes, client: Client):
        file_id, chunk = proto.parse_file_payload(payload)
        up = client.uploads.get(file_id)
        if up is None:
            return
        up["fh"].write(chunk)
        up["remaining"] -= len(chunk)

    async def _end_upload(self, msg: dict, client: Client):
        req_id = int(msg.get("id", 0))
        up = client.uploads.pop(req_id, None)
        if up is None:
            return
        up["fh"].close()
        await client.sender.send_json(
            {"type": "fs-put-done", "id": req_id, "path": str(up["path"]),
             "size": os.path.getsize(up["path"])})
        log.info("upload from client #%d saved: %s", client.id, up["path"])


async def serve(cfg: cfgmod.Config) -> int:
    server = DeskLinkServer(cfg)
    loop = asyncio.get_running_loop()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, server.stop)
    try:
        return await server.run()
    except UserCancelled:
        log.error("portal consent denied or cancelled; exiting")
        return 2
