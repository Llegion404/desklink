"""GStreamer video: PipeWire capture -> H.264 encode per monitor stream.

One pipeline per stream, created lazily on first subscriber, torn down when
the last subscriber leaves. Encoder preference: config (`auto` tries NVENC,
falls back to x264). Pipeline construction is string-based (parse_launch)
so encoder property nicks are handled by the GStreamer parser; failures fall
through to the next candidate, both at build time and at runtime (bus ERROR).
"""

from __future__ import annotations

import logging
import os

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo

from .config import Config
from .glibloop import GLibRunner
from .portal import StreamInfo

log = logging.getLogger("desklink.video")

Gst.init(None)


def _access_unit_has_idr(data: bytes) -> bool:
    """True if the Annex-B access unit contains an IDR slice (5) or SPS (7)."""
    n = len(data)
    i = 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= n:
            return False
        nal_type = data[i + 3] & 0x1F
        if nal_type == 5 or nal_type == 7:
            return True
        i += 3


def _encoder_chains(cfg: Config, gop: int) -> list[str]:
    kbps = cfg.bitrate_kbps
    if cfg.encoder == "nvenc":
        candidates = ["nvh264enc"]
    elif cfg.encoder == "x264":
        candidates = ["x264enc"]
    else:
        candidates = ["nvh264enc", "x264enc"]
    chains = []
    for name in candidates:
        if Gst.ElementFactory.find(name) is None:
            log.info("encoder %s not available", name)
            continue
        if name == "nvh264enc":
            chains.append(
                f"nvh264enc name=enc bitrate={kbps} gop-size={gop} "
                f"preset=low-latency-hq rc-mode=cbr")
        else:
            chains.append(
                f"x264enc name=enc tune=zerolatency speed-preset=veryfast "
                f"bitrate={kbps} key-int-max={gop} byte-stream=true "
                f"sliced-threads=true")
    if not chains:
        raise RuntimeError("no H.264 encoder available (tried nvh264enc, x264enc)")
    return chains


class VideoStreamPipeline:
    def __init__(self, stream: StreamInfo, fd: int, cfg: Config,
                 on_frame, on_error, max_h: int = 0, skip_encoder: str = "",
                 on_encoded_size=None, on_native_change=None,
                 on_capture_lost=None, on_retry=None):
        self.stream = stream
        self.fd = fd
        self.fd_open = True
        self.cfg = cfg
        self.max_h = max_h  # 0 = native resolution
        self.on_frame = on_frame            # (stream_id, keyframe, pts_us, bytes)
        self.on_error = on_error            # (stream_id, message)
        self.on_encoded_size = on_encoded_size  # (stream_id, w, h) actual encoded
        self.on_native_change = on_native_change  # (stream_id,) monitor res changed
        self.on_capture_lost = on_capture_lost  # (stream_id, msg) PipeWire died
        self.on_retry = on_retry            # (stream_id, failed_encoder, msg)
        self.pipeline = None
        self.appsink = None
        self._bus_id = 0
        self._src_pad = None
        self._caps_id = 0
        self._rebuild_timer = 0
        self._enc_dims: tuple[int, int] | None = None  # last reported encoded size
        # physical capture dims (PipeWire) — portal StreamInfo is logical and
        # must NOT be compared against it directly
        self._native_dims: tuple[int, int] | None = None
        self.gop = max(1, int((cfg.framerate or 30) * cfg.gop_seconds))
        self._candidates = [c for c in _encoder_chains(cfg, self.gop)
                            if c.split()[0] != skip_encoder]
        self.encoder_name = ""

    # ---------------------------------------------------------------- build
    def _launch_str(self, enc_chain: str, rate: bool) -> str:
        src = (f"pipewiresrc name=src fd={self.fd} path={self.stream.node_id} "
               f"keepalive-time=5 always-copy=true")
        # downscale cap for constrained links (relay/remote); keeps aspect.
        # Width is derived from StreamInfo (portal logical size) — with
        # fractional scaling its aspect can differ from the physical capture
        # by <0.1%, which is invisible after scaling.
        if self.max_h and self.stream.height > self.max_h:
            w = max(self.stream.width * self.max_h // self.stream.height, 2) & ~1
            caps = (f"videoscale name=scale ! "
                    f"video/x-raw,format=NV12,width={w},height={self.max_h}")
        else:
            caps = ""
        if rate and self.cfg.framerate > 0:
            rate_chain = f"videorate drop-only=true ! video/x-raw,format=NV12,framerate={self.cfg.framerate}/1"
        else:
            rate_chain = ""
        parts = [p for p in [
            src,
            "videoconvert",
            caps,
            rate_chain,
            enc_chain,
            "h264parse config-interval=-1",
            "appsink name=sink emit-signals=true max-buffers=3 drop=true sync=false",
        ] if p]
        return " ! ".join(parts)

    def start(self) -> bool:
        """Try candidates (with/without videorate) until one reaches PLAYING."""
        for enc_chain in self._candidates:
            for rate in ([True, False] if self.cfg.framerate > 0 else [False]):
                desc = self._launch_str(enc_chain, rate)
                try:
                    pipeline = Gst.parse_launch(desc)
                except GLib.Error as exc:
                    log.warning("pipeline parse failed (%s): %s", rate, exc.message)
                    continue
                ok, state, pending = pipeline.set_state(Gst.State.PLAYING), None, None
                if ok == Gst.StateChangeReturn.ASYNC:
                    ok, state, pending = pipeline.get_state(5_000_000_000)
                if ok == Gst.StateChangeReturn.SUCCESS and state == Gst.State.PLAYING:
                    self.pipeline = pipeline
                    self.appsink = pipeline.get_by_name("sink")
                    self.appsink.connect("new-sample", self._on_sample)
                    self.encoder_name = enc_chain.split()[0]
                    src = pipeline.get_by_name("src")
                    pad = src.get_static_pad("src") if src else None
                    if pad:
                        self._src_pad = pad
                        self._caps_id = pad.connect("notify::caps", self._on_src_caps)
                    bus = pipeline.get_bus()
                    bus.add_signal_watch()
                    self._bus_id = bus.connect("message::error", self._on_bus_error)
                    log.info("stream %d (node %d %dx%d) playing via %s (rate-cap=%s)",
                             self.stream.node_id, self.stream.node_id,
                             self.stream.width, self.stream.height,
                             self.encoder_name, rate)
                    return True
                pipeline.set_state(Gst.State.NULL)
                log.warning("pipeline failed to play (rate-cap=%s): %s/%s",
                            rate, ok, state)
        self.on_error(self.stream.node_id, "no encoder pipeline could start")
        return False

    def stop(self):
        if self._rebuild_timer:
            GLib.source_remove(self._rebuild_timer)
            self._rebuild_timer = 0
        if self._caps_id and self._src_pad:
            try:
                self._src_pad.disconnect(self._caps_id)
            except Exception:
                pass
            self._caps_id = 0
            self._src_pad = None
        if self.pipeline:
            if self._bus_id:
                bus = self.pipeline.get_bus()
                bus.disconnect(self._bus_id)
                bus.remove_signal_watch()
                self._bus_id = 0
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.fd_open:
            try:
                os.close(self.fd)  # our os.dup() from VideoManager.subscribe
            except OSError:
                pass
            self.fd_open = False

    def set_bitrate(self, kbps: int) -> bool:
        """Change the encoder bitrate on a live pipeline (both nvh264enc and
        x264enc expose 'bitrate' in kbit/s and accept runtime changes)."""
        if not self.pipeline:
            return False
        enc = self.pipeline.get_by_name("enc")
        if not enc:
            return False
        try:
            enc.set_property("bitrate", max(500, int(kbps)))
            log.info("stream %d bitrate -> %d kbps", self.stream.node_id, kbps)
            return True
        except Exception:
            log.exception("set_bitrate failed")
            return False

    # ---------------------------------------------------------------- frames
    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        # report encoded dims on change — compare values, Gst.Caps get a new
        # Python wrapper per access so identity comparison never sticks
        caps = sample.get_caps()
        if caps is not None and caps.get_size():
            s = caps.get_structure(0)
            w, h = s.get_value("width"), s.get_value("height")
            if w and h and (w, h) != self._enc_dims:
                self._enc_dims = (w, h)
                if self.on_encoded_size:
                    self.on_encoded_size(self.stream.node_id, int(w), int(h))
        buf = sample.get_buffer()
        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else 0
            # DELTA_UNIT flags proved unreliable through h264parse/NVENC;
            # ground truth is an IDR slice (5) or in-band SPS (7) — with
            # config-interval=-1 SPS/PPS precede every IDR.
            data = map_info.data
            keyframe = _access_unit_has_idr(data)
            self.on_frame(self.stream.node_id, keyframe, pts // 1000, data)
        finally:
            buf.unmap(map_info)
        return Gst.FlowReturn.OK

    # ------------------------------------------------------- resolution changes
    def _on_src_caps(self, pad, pspec):
        """GLib thread. The first observed caps (physical capture size) seed
        the baseline — notify usually fires too late for the initial caps, so
        they are read as "already current". A later change means the monitor
        resolution changed: StreamInfo (portal logical size, used for input
        mapping) is ratio-scaled to stay in step, and the pipeline is rebuilt
        (debounced) so scale caps / encoder pick up the new size."""
        try:
            caps = pad.get_current_caps() or pad.get_allowed_caps()
            if not caps or not caps.get_size():
                return
            s = caps.get_structure(0)
            w, h = int(s.get_value("width")), int(s.get_value("height"))
            if not w or not h:
                return
        except Exception:
            return
        if self._native_dims is None:
            self._native_dims = (w, h)
            return
        if (w, h) == self._native_dims:
            return
        ow, oh = self._native_dims
        self._native_dims = (w, h)
        self.stream.width = max(1, round(self.stream.width * w / ow))
        self.stream.height = max(1, round(self.stream.height * h / oh))
        log.info("stream %d capture resolution %dx%d -> %dx%d "
                 "(logical now %dx%d)", self.stream.node_id, ow, oh, w, h,
                 self.stream.width, self.stream.height)
        if self._rebuild_timer:
            GLib.source_remove(self._rebuild_timer)
        self._rebuild_timer = GLib.timeout_add(400, self._fire_native_change)

    def _fire_native_change(self):
        self._rebuild_timer = 0
        if self.on_native_change:
            self.on_native_change(self.stream.node_id)

    def _on_bus_error(self, bus, message):
        err, debug = message.parse_error()
        log.error("stream %d bus error from %s: %s (%s)",
                  self.stream.node_id, message.src.get_name() if message.src else "?",
                  err.message, debug)
        self.stop()
        # a dead pipewiresrc means the capture session is gone (portal
        # backend crash, stream revoked without a Closed signal) — encoder
        # fallback can't help, the whole session must be re-requested
        src_died = False
        try:
            src_died = (message.src is not None
                        and message.src.get_factory().get_name() == "pipewiresrc")
        except Exception:
            pass
        if src_died and self.on_capture_lost:
            self.on_capture_lost(self.stream.node_id, err.message)
            return
        # retry bookkeeping lives in VideoManager (it owns the pipeline table)
        if self.on_retry:
            self.on_retry(self.stream.node_id, self.encoder_name, err.message)
        else:
            self.on_error(self.stream.node_id, err.message)

    def force_idr(self):
        if not self.pipeline:
            return
        enc = self.pipeline.get_by_name("enc")
        if not enc:
            return
        try:
            ev = GstVideo.event_new_downstream_force_key_unit(
                Gst.CLOCK_TIME_NONE, True, 0)
            if isinstance(ev, tuple):
                ev = ev[1]
            enc.send_event(ev)
        except Exception:
            log.debug("force-key-unit failed", exc_info=True)


class GLibError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class VideoManager:
    """Lazy per-stream pipelines + fan-out bookkeeping."""

    def __init__(self, glib: GLibRunner, cfg: Config):
        self.glib = glib
        self.cfg = cfg
        self.session = None
        self.streams: list[StreamInfo] = []
        self.on_frame = None          # (stream_id, keyframe, pts_us, bytes)
        self.on_stream_error = None   # (stream_id, message)
        self.on_encoded_size = None   # (stream_id, w, h) actual encoded size
        self.on_capture_lost = None   # (stream_id, msg) capture session gone
        self._pipelines: dict[int, VideoStreamPipeline] = {}
        self._refcounts: dict[int, int] = {}
        self._retries: dict[int, int] = {}
        self.current_max_h = 0        # resolution cap; 0 = native

    def setup(self, session):
        self.session = session
        self.streams = session.streams

    def subscribe(self, stream_id: int):
        if stream_id not in {s.node_id for s in self.streams}:
            return
        self._refcounts[stream_id] = self._refcounts.get(stream_id, 0) + 1
        if self._refcounts[stream_id] == 1:
            try:
                pipe = self._make_pipeline(stream_id)
            except Exception:
                # never leave the refcount incremented — the next subscriber
                # must still get a pipeline
                log.exception("pipeline creation failed for stream %d",
                              stream_id)
                self._refcounts.pop(stream_id, None)
                self._fail_stream(stream_id, "pipeline creation failed")
                return
            self.glib.submit(pipe.start)

    def _make_pipeline(self, stream_id: int, skip_encoder: str = "",
                       fresh_retry: bool = True) -> VideoStreamPipeline:
        info = next(s for s in self.streams if s.node_id == stream_id)
        fd = os.dup(self.session.pw_fd)
        if fresh_retry:
            self._retries.pop(stream_id, None)
        pipe = VideoStreamPipeline(info, fd, self.cfg, self._frame,
                                   self._fail_stream,
                                   max_h=self.current_max_h,
                                   skip_encoder=skip_encoder,
                                   on_encoded_size=self._encoded_size,
                                   on_native_change=self._native_change,
                                   on_capture_lost=self._capture_lost,
                                   on_retry=self._retry)
        self._pipelines[stream_id] = pipe
        return pipe

    def _fail_stream(self, sid: int, msg: str):
        log.error("stream %d fatal: %s", sid, msg)
        if self.on_stream_error:
            self.on_stream_error(sid, msg)

    def _encoded_size(self, sid: int, w: int, h: int):
        if self.on_encoded_size:
            self.on_encoded_size(sid, w, h)

    def _capture_lost(self, sid: int, msg: str):
        log.error("capture lost on node %d: %s", sid, msg)
        if self.on_capture_lost:
            self.on_capture_lost(sid, msg)

    def _native_change(self, sid: int):
        """GLib thread: monitor resolution changed — rebuild this pipeline so
        scale caps / encoder match the new native size."""
        if sid in self._pipelines:
            self._rebuild(sid)

    def _retry(self, sid: int, failed_encoder: str, msg: str):
        """GLib thread: bus error on a running pipeline — one fallback-encoder
        retry, then give up (client gets stream-error)."""
        if sid not in self._pipelines:
            return
        retries = self._retries.get(sid, 0)
        if retries >= 1:
            self._pipelines.pop(sid, None)
            self._retries.pop(sid, None)
            self._fail_stream(sid, msg)
            return
        self._retries[sid] = retries + 1
        log.info("stream %d retrying with fallback encoder (dropped %s)",
                 sid, failed_encoder or "?")
        self._make_pipeline(sid, skip_encoder=failed_encoder,
                            fresh_retry=False).start()

    def unsubscribe(self, stream_id: int):
        count = self._refcounts.get(stream_id, 0)
        if count <= 1:
            self._refcounts.pop(stream_id, None)
            self._retries.pop(stream_id, None)
            pipe = self._pipelines.pop(stream_id, None)
            if pipe:
                self.glib.submit(pipe.stop)
        elif count:
            self._refcounts[stream_id] = count - 1

    def _frame(self, stream_id: int, keyframe: bool, pts_us: int, data: bytes):
        if self.on_frame:
            self.on_frame(stream_id, keyframe, pts_us, data)

    def force_idr(self, stream_id: int):
        pipe = self._pipelines.get(stream_id)
        if pipe:
            self.glib.submit(pipe.force_idr)

    def set_bitrate(self, kbps: int) -> bool:
        return self.set_quality(kbps, self.current_max_h)

    def set_quality(self, kbps: int, max_h: int = 0) -> bool:
        """Live quality change: bitrate on the running encoders, and — when
        the resolution cap changes — pipelines are rebuilt (scaled) for every
        subscribed stream."""
        kbps = max(500, min(50000, int(kbps)))
        max_h = max(0, int(max_h))
        res_changed = max_h != self.current_max_h
        self.current_max_h = max_h
        self.cfg.bitrate_kbps = kbps  # in-memory; future pipelines match
        for sid in list(self._pipelines):
            pipe = self._pipelines.get(sid)
            if pipe is None:
                continue
            if res_changed:
                # rebuild on the GLib thread: NVENC teardown on the asyncio
                # thread while GLib-side callbacks touch the old pipeline
                # segfaulted the driver (SIGSEGV, silent process death)
                self.glib.submit(self._rebuild, sid)
            else:
                self.glib.submit(pipe.set_bitrate, kbps)
        return True

    def _rebuild(self, sid: int):
        """GLib thread: stop + fresh pipeline (quality cap change, monitor
        resolution change, encoder fallback). Everything on one thread so
        encoder teardown and init never race."""
        pipe = self._pipelines.get(sid)
        if pipe is not None:
            pipe.stop()
        self._make_pipeline(sid).start()

    def stop_all(self):
        for pipe in list(self._pipelines.values()):
            self.glib.submit(pipe.stop)
        self._pipelines.clear()
        self._refcounts.clear()
        self._retries.clear()
