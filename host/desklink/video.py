"""GStreamer video: PipeWire capture -> H.264 encode per monitor stream.

ONE capture per portal node: KWin's screencast stream serves a single
consumer, so a second pipewiresrc on the same node kills the first one
(observed live: "streaming stopped, reason not-negotiated" on the healthy
pipeline as soon as a second consumer connected). Per-client quality is
therefore one capture fanned out through a `tee` into one encode branch per
distinct (max_h, kbps) subscriber key — clients with identical settings
share a branch, clients with different settings each get their own branch
off the shared capture.

Encoder preference: config (`auto` tries NVENC, falls back to x264). At most
ONE NVENC encode per process (two concurrent nvh264enc sessions segfault
some drivers — three times observed): the VideoManager hands a single NVENC
slot to one branch; every other branch runs x264. Branch sets change rarely
(subscribe / unsubscribe / quality change) and every change rebuilds the
capture on the GLib thread, so encoder teardown and init never race (the
NVENC SIGSEGV lesson). Clients see a brief keyframe gap on a rebuild.
"""

from __future__ import annotations

import logging
import os
import time

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


def _encoder_chains(cfg: Config, gop: int, kbps: int | None = None,
                    allow_nvenc: bool = True, enc_name: str = "enc") -> list[str]:
    kbps = kbps or cfg.bitrate_kbps
    if cfg.encoder == "nvenc":
        candidates = ["nvh264enc"]
    elif cfg.encoder == "x264":
        candidates = ["x264enc"]
    else:
        # one NVENC session per process: creating a second while one is live
        # segfaults the driver (SIGSEGV in nvh264enc, three times observed).
        # Extra concurrent branches use x264 — cheap at capped resolutions.
        candidates = ["nvh264enc"] if allow_nvenc else []
        candidates.append("x264enc")
    chains = []
    for name in candidates:
        if Gst.ElementFactory.find(name) is None:
            log.info("encoder %s not available", name)
            continue
        if name == "nvh264enc":
            chains.append(
                f"nvh264enc name={enc_name} bitrate={kbps} gop-size={gop} "
                f"preset=low-latency-hq rc-mode=cbr")
        else:
            chains.append(
                f"x264enc name={enc_name} tune=zerolatency speed-preset=veryfast "
                f"bitrate={kbps} key-int-max={gop} byte-stream=true "
                f"sliced-threads=true")
    if not chains:
        raise RuntimeError("no H.264 encoder available (tried nvh264enc, x264enc)")
    return chains


class VideoStreamPipeline:
    """One capture per portal node.

    `pipewiresrc → videoconvert → [rate] → tee` with one encode branch per
    subscriber key; each branch is
    `queue(leaky) → [videoscale → caps WxH] → encoder → h264parse → appsink`.
    """

    def __init__(self, stream: StreamInfo, fd: int, cfg: Config,
                 branch_skips: dict[tuple, str], nvenc_branch: tuple | None,
                 on_frame, on_error, on_encoded_size=None,
                 on_native_change=None, on_capture_lost=None, on_retry=None):
        self.stream = stream
        self.node_id = stream.node_id
        self.fd = fd
        self.fd_open = True
        self.cfg = cfg
        self.nvenc_branch = nvenc_branch
        self.on_frame = on_frame                    # (key, keyframe, pts_us, bytes)
        self.on_error = on_error                    # (key, message)
        self.on_encoded_size = on_encoded_size      # (key, w, h) encoded size
        self.on_native_change = on_native_change    # (node_id,)
        self.on_capture_lost = on_capture_lost      # (node_id, msg)
        self.on_retry = on_retry                    # (node_id, key, enc, msg)
        self.pipeline = None
        self._bus_id = 0
        self._src_pad = None
        self._caps_id = 0
        self._rebuild_timer = 0
        # physical capture dims (PipeWire) — portal StreamInfo is logical and
        # must NOT be compared against it directly
        self._native_dims: tuple[int, int] | None = None
        self.gop = max(1, int((cfg.framerate or 30) * cfg.gop_seconds))
        self.branches: dict[tuple, dict] = {}
        # stable branch order (sorted keys) for names sink0/sink1/enc0/...
        self._order = sorted(branch_skips)
        for i, key in enumerate(self._order):
            skip = branch_skips[key]
            kbps = key[2]
            cands = [c for c in
                     _encoder_chains(cfg, self.gop, kbps or cfg.bitrate_kbps,
                                     allow_nvenc=(key == nvenc_branch),
                                     enc_name=f"enc{i}")
                     if c.split()[0] != skip]
            if not cands:
                raise RuntimeError(f"no encoder left for branch {key!r}")
            self.branches[key] = {"cands": cands, "enc_name": "", "dims": None,
                                  "enc": None, "sink": None}

    @property
    def branch_keys(self) -> set:
        return set(self._order)

    def branch_encoder(self, key: tuple) -> str:
        return self.branches.get(key, {}).get("enc_name", "")

    # ---------------------------------------------------------------- build
    def _launch_str(self, rate: bool, picks: dict) -> str:
        src = (f"pipewiresrc name=src fd={self.fd} path={self.node_id} "
               f"keepalive-time=5 always-copy=true")
        # NV12 handoff is mandatory for NVENC and cheapest for x264; when the
        # framerate cap is disabled the plain format caps still fix the format
        if rate and self.cfg.framerate > 0:
            pre = (f"videorate drop-only=true ! "
                   f"video/x-raw,format=NV12,framerate={self.cfg.framerate}/1")
        else:
            pre = "video/x-raw,format=NV12"
        parts = [src, "videoconvert", pre, "tee name=t allow-not-linked=true"]
        branches = []
        for i, key in enumerate(self._order):
            max_h = key[1]
            # downscale cap for constrained links (relay/remote); keeps aspect.
            # Width is derived from StreamInfo (portal logical size) — with
            # fractional scaling its aspect can differ from the physical
            # capture by <0.1%, which is invisible after scaling.
            if max_h and self.stream.height > max_h:
                w = max(self.stream.width * max_h // self.stream.height, 2) & ~1
                scale = (f"videoscale ! "
                         f"video/x-raw,format=NV12,width={w},height={max_h} ! ")
            else:
                scale = ""
            chain = self.branches[key]["cands"][picks[key]]
            branches.append(
                f"t. ! queue leaky=downstream max-size-buffers=3 "
                f"max-size-bytes=0 max-size-time=0 ! {scale}{chain} ! "
                f"h264parse config-interval=-1 ! appsink name=sink{i} "
                f"emit-signals=true max-buffers=3 drop=true sync=false")
        # the tee and its branches join with a SPACE: "tee name=t ... ! t. !"
        # is a syntax error, "tee name=t ... t. ! ..." is not
        return " ! ".join(parts) + " " + " ".join(branches)

    def start(self) -> bool:
        """Build + start the capture. Tries the framerate cap first (and
        without it as fallback); on a parse failure walks each branch's
        encoder candidates. Runs on the GLib thread."""
        for rate in ([True, False] if self.cfg.framerate > 0 else [False]):
            picks = {key: 0 for key in self._order}
            pipeline = None
            while True:
                try:
                    pipeline = Gst.parse_launch(self._launch_str(rate, picks))
                    break
                except GLib.Error as exc:
                    if not self._advance_picks(picks):
                        log.warning("capture parse failed (rate-cap=%s): %s",
                                    rate, exc.message)
                        pipeline = None
                        break
            if pipeline is None:
                continue
            state = None
            ok = pipeline.set_state(Gst.State.PLAYING)
            if ok == Gst.StateChangeReturn.ASYNC:
                ok, state, _ = pipeline.get_state(5_000_000_000)
            if (ok != Gst.StateChangeReturn.SUCCESS
                    or state != Gst.State.PLAYING):
                # a node whose capture JUST stopped can refuse an immediate
                # restart (synchronous FAILURE observed once right after
                # teardown) — one short retry before giving up
                pipeline.set_state(Gst.State.NULL)
                time.sleep(0.3)
                state = None
                ok = pipeline.set_state(Gst.State.PLAYING)
                if ok == Gst.StateChangeReturn.ASYNC:
                    ok, state, _ = pipeline.get_state(5_000_000_000)
            if ok == Gst.StateChangeReturn.SUCCESS and state == Gst.State.PLAYING:
                self.pipeline = pipeline
                for i, key in enumerate(self._order):
                    b = self.branches[key]
                    b["enc_name"] = b["cands"][picks[key]].split()[0]
                    sink = pipeline.get_by_name(f"sink{i}")
                    sink.connect("new-sample", self._sample_cb(key))
                    b["sink"] = sink
                    b["enc"] = pipeline.get_by_name(f"enc{i}")
                src = pipeline.get_by_name("src")
                pad = src.get_static_pad("src") if src else None
                if pad:
                    self._src_pad = pad
                    self._caps_id = pad.connect("notify::caps", self._on_src_caps)
                bus = pipeline.get_bus()
                bus.add_signal_watch()
                self._bus_id = bus.connect("message::error", self._on_bus_error)
                log.info("stream %d (node %d %dx%d) playing: %s (rate-cap=%s)",
                         self.node_id, self.node_id, self.stream.width,
                         self.stream.height,
                         ", ".join(f"{k[1] or 'native'}p/{k[2]}k="
                                   f"{self.branches[k]['enc_name']}"
                                   for k in self._order), rate)
                return True
            pipeline.set_state(Gst.State.NULL)
            log.warning("capture failed to play (rate-cap=%s): %s/%s",
                        rate, ok, state)
        return False

    def _advance_picks(self, picks: dict) -> bool:
        """Move one branch to its next encoder candidate (parse-fallback)."""
        for key in self._order:
            if picks[key] + 1 < len(self.branches[key]["cands"]):
                picks[key] += 1
                return True
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
                os.close(self.fd)  # our os.dup() from VideoManager
            except OSError:
                pass
            self.fd_open = False

    # ---------------------------------------------------------------- frames
    def _sample_cb(self, key: tuple):
        def cb(sink):
            sample = sink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.OK
            b = self.branches[key]
            # report encoded dims on change — compare values, Gst.Caps get a
            # new Python wrapper per access so identity comparison never sticks
            caps = sample.get_caps()
            if caps is not None and caps.get_size():
                s = caps.get_structure(0)
                w, h = s.get_value("width"), s.get_value("height")
                if w and h and (w, h) != b["dims"]:
                    b["dims"] = (w, h)
                    if self.on_encoded_size:
                        self.on_encoded_size(key, int(w), int(h))
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
                self.on_frame(key, keyframe, pts // 1000, data)
            finally:
                buf.unmap(map_info)
            return Gst.FlowReturn.OK
        return cb

    # ------------------------------------------------------- resolution changes
    def _on_src_caps(self, pad, pspec):
        """GLib thread. The first observed caps (physical capture size) seed
        the baseline — notify usually fires too late for the initial caps, so
        they are read as "already current". A later change means the monitor
        resolution changed: StreamInfo (portal logical size, used for input
        mapping) is ratio-scaled to stay in step, and the capture is rebuilt
        (debounced) so scale caps / encoders pick up the new size."""
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
                 "(logical now %dx%d)", self.node_id, ow, oh, w, h,
                 self.stream.width, self.stream.height)
        if self._rebuild_timer:
            GLib.source_remove(self._rebuild_timer)
        self._rebuild_timer = GLib.timeout_add(400, self._fire_native_change)

    def _fire_native_change(self):
        self._rebuild_timer = 0
        if self.on_native_change:
            self.on_native_change(self.node_id)

    # ------------------------------------------------------------ bus errors
    def _is_pipewiresrc(self, obj) -> bool:
        try:
            while obj is not None:
                factory = obj.get_factory()
                if factory is not None and factory.get_name() == "pipewiresrc":
                    return True
                obj = obj.get_parent()
        except Exception:
            pass
        return False

    def _branch_for(self, src) -> tuple | None:
        try:
            obj = src
            while obj is not None:
                name = obj.get_name() or ""
                for i, key in enumerate(self._order):
                    if name in (f"enc{i}", f"sink{i}"):
                        return key
                obj = obj.get_parent()
        except Exception:
            pass
        return None

    def _on_bus_error(self, bus, message):
        err, debug = message.parse_error()
        log.error("stream %d bus error from %s: %s (%s)",
                  self.node_id, message.src.get_name() if message.src else "?",
                  err.message, debug)
        self.stop()
        if self._is_pipewiresrc(message.src):
            # a dead pipewiresrc means the capture session is gone (portal
            # backend crash, stream revoked without a Closed signal) —
            # encoder fallback can't help, the session must be re-requested
            if self.on_capture_lost:
                self.on_capture_lost(self.node_id, err.message)
            return
        key = self._branch_for(message.src)
        if self.on_retry and key is not None:
            # one encode branch failed — retry it on the fallback encoder
            self.on_retry(self.node_id, key, self.branches[key]["enc_name"],
                          err.message)
        elif self.on_error:
            for k in list(self._order):
                self.on_error(k, err.message)

    def force_idr(self, key: tuple | None = None):
        if not self.pipeline:
            return
        keys = [key] if key in self.branches else list(self._order)
        for k in keys:
            enc = self.branches[k].get("enc")
            if not enc:
                continue
            try:
                ev = GstVideo.event_new_downstream_force_key_unit(
                    Gst.CLOCK_TIME_NONE, True, 0)
                if isinstance(ev, tuple):
                    ev = ev[1]
                enc.send_event(ev)
            except Exception:
                log.debug("force-key-unit failed", exc_info=True)

    def set_branch_bitrate(self, key: tuple, kbps: int) -> bool:
        """Live bitrate change (both encoders take kbit/s in 'bitrate').
        The branch KEY keeps its original kbps as identity; this only moves
        the encoder's setpoint (adaptive bitrate)."""
        enc = self.branches.get(key, {}).get("enc")
        if not enc or not self.pipeline:
            return False
        try:
            enc.set_property("bitrate", max(500, int(kbps)))
            self.branches[key]["cur_kbps"] = int(kbps)
            return True
        except Exception:
            log.debug("branch bitrate change failed", exc_info=True)
            return False


class VideoManager:
    """One capture per portal node, one encode branch per distinct
    (node, max_h, kbps) subscriber key.

    Branch identity is `key = (node_id, max_h, kbps)`: clients with the same
    settings share one branch; a client changing quality moves to another
    branch while everyone else keeps theirs. All pipeline lifecycle runs on
    the GLib thread (encoder teardown and init never race — the NVENC SIGSEGV
    lesson); the asyncio thread only mutates refcounts and submits syncs.
    """

    def __init__(self, glib: GLibRunner, cfg: Config):
        self.glib = glib
        self.cfg = cfg
        self.session = None
        self.streams: list[StreamInfo] = []
        self.on_frame = None          # (key, keyframe, pts_us, bytes)
        self.on_stream_error = None   # (key, message)
        self.on_encoded_size = None   # (key, w, h) actual encoded size
        self.on_capture_lost = None   # (node_id, msg) capture session gone
        self.on_branch_reset = None   # (key,) branch rebuilt -> adapt state reset
        self._captures: dict[int, VideoStreamPipeline] = {}
        self._refcounts: dict[tuple, int] = {}
        self._retries: dict[tuple, int] = {}
        self._skip: dict[tuple, str] = {}
        # NVENC slot: key of the one branch allowed to use nvh264enc. Two
        # concurrent NVENC sessions segfault the driver, so every other
        # concurrent branch runs x264.
        self._nvenc_key: tuple | None = None

    def setup(self, session):
        self.session = session
        self.streams = session.streams

    # ------------------------------------------------- public (asyncio thread)
    # refcount bookkeeping + GLib submits only — no GStreamer calls here

    def subscribe(self, stream_id: int, kbps: int = 0, max_h: int = 0) -> tuple:
        """Subscribe to stream_id at (max_h, kbps); returns the branch key."""
        kbps = kbps or self.cfg.bitrate_kbps
        key = (stream_id, max_h, kbps)
        if stream_id not in {s.node_id for s in self.streams}:
            return key
        self._refcounts[key] = self._refcounts.get(key, 0) + 1
        self.glib.submit(self._sync_capture, stream_id)
        return key

    def unsubscribe(self, key: tuple):
        count = self._refcounts.get(key, 0)
        if count <= 1:
            self._refcounts.pop(key, None)
            self._retries.pop(key, None)
            self._skip.pop(key, None)
            self.glib.submit(self._sync_capture, key[0])
        elif count:
            self._refcounts[key] = count - 1

    def move_quality(self, stream_id: int, old_key: tuple,
                     kbps: int, max_h: int) -> tuple:
        """Move one client from its current branch to one with new settings.
        Any stop is queued on the GLib thread before the replacement's start,
        so encoder teardown and init never race (NVENC SIGSEGV lesson)."""
        if old_key[0] != stream_id:
            return old_key
        if self._refcounts.get(old_key, 0) <= 1 and self._nvenc_key == old_key:
            # exclusive and owns the NVENC slot: hand it to the replacement —
            # the rebuild stops the old encode before starting the new one.
            # If the branch is SHARED it keeps running with NVENC and the
            # replacement takes x264: releasing the slot here would let the
            # rebuild start a second concurrent NVENC session, which
            # segfaults the driver.
            self._nvenc_key = None
        self.unsubscribe(old_key)
        return self.subscribe(stream_id, kbps, max_h)

    def force_idr(self, key: tuple):
        cap = self._captures.get(key[0])
        if cap:
            self.glib.submit(cap.force_idr, key)

    def set_branch_bitrate(self, key: tuple, kbps: int):
        cap = self._captures.get(key[0])
        if cap:
            self.glib.submit(cap.set_branch_bitrate, key, kbps)

    def stop_all(self):
        self.glib.submit(self._stop_all)

    # --------------------------------------------- GLib thread: capture cycle

    def _release_nvenc(self, node_id: int):
        if self._nvenc_key and self._nvenc_key[0] == node_id:
            self._nvenc_key = None

    def _sync_capture(self, node_id: int):
        """Reconcile the node's capture with the current refcount table."""
        keys = sorted(k for k in self._refcounts if k[0] == node_id)
        cap = self._captures.get(node_id)
        if not keys:
            if cap:
                cap.stop()
                self._captures.pop(node_id, None)
                self._release_nvenc(node_id)
            return
        if cap is None or set(keys) != cap.branch_keys:
            self._build_and_start(node_id, keys, reset_retries=True)

    def _build_and_start(self, node_id: int, keys: list,
                         reset_retries: bool):
        """Stop any existing capture, then build + start one with exactly the
        given branch set. GLib thread; the only place pipelines are created."""
        old = self._captures.pop(node_id, None)
        if old:
            old.stop()
            self._release_nvenc(node_id)
        if node_id not in {s.node_id for s in self.streams}:
            return
        if reset_retries:
            for k in keys:
                self._retries.pop(k, None)
        info = next(s for s in self.streams if s.node_id == node_id)
        claim = None
        if self._nvenc_key is None:
            # heaviest branch wins NVENC: highest bitrate, native (maxh=0)
            # before capped — the deterministic pick for the shared capture
            claim = max(keys, key=lambda k: (k[2], k[1] == 0, -k[1]))
            self._nvenc_key = claim
        fd = os.dup(self.session.pw_fd)
        try:
            cap = VideoStreamPipeline(
                info, fd, self.cfg,
                {k: self._skip.get(k, "") for k in keys}, claim,
                on_frame=self._frame, on_error=self._fail_stream,
                on_encoded_size=self._encoded_size,
                on_native_change=self._native_change,
                on_capture_lost=self._capture_lost, on_retry=self._retry)
        except Exception:
            log.exception("capture creation failed for node %d", node_id)
            try:
                os.close(fd)
            except OSError:
                pass
            self._release_nvenc(node_id)
            for k in keys:
                self._fail_stream(k, "capture creation failed")
            return
        self._captures[node_id] = cap
        if not cap.start():
            cap.stop()
            self._captures.pop(node_id, None)
            if claim:
                # don't re-pick the same failing encoder on the next rebuild
                self._skip[claim] = "nvh264enc"
            self._release_nvenc(node_id)
            for k in keys:
                self._fail_stream(k, "no encoder pipeline could start")
            return
        if claim and cap.branch_encoder(claim) != "nvh264enc":
            self._release_nvenc(node_id)  # fell back at build time
        # fresh encode: the adaptive controller starts from the configured
        # bitrate again
        for k in keys:
            if self.on_branch_reset:
                self.on_branch_reset(k)

    def _retry(self, node_id: int, key: tuple, failed_enc: str, msg: str):
        """GLib thread: an encode branch errored at runtime — one fallback
        retry, then the branch is dropped and its clients notified."""
        if key not in self._refcounts:
            return
        retries = self._retries.get(key, 0)
        if retries >= 1:
            self._retries.pop(key, None)
            self._refcounts.pop(key, None)
            self._skip.pop(key, None)
            self._fail_stream(key, msg)
            self._sync_capture(node_id)
            return
        self._retries[key] = retries + 1
        self._skip[key] = failed_enc or "nvh264enc"
        log.info("branch %r retrying with fallback encoder (dropped %s)",
                 key, failed_enc or "?")
        self._build_and_start(
            node_id, sorted(k for k in self._refcounts if k[0] == node_id),
            reset_retries=False)

    def _native_change(self, node_id: int):
        """GLib thread: monitor resolution changed — rebuild the capture so
        scale caps / encoders match the new size."""
        if node_id in self._captures:
            self._build_and_start(
                node_id, sorted(k for k in self._refcounts if k[0] == node_id),
                reset_retries=True)

    def _fail_stream(self, key: tuple, msg: str):
        log.error("branch %r fatal: %s", (key,), msg)
        if self.on_stream_error:
            self.on_stream_error(key, msg)

    def _frame(self, key: tuple, keyframe: bool, pts_us: int, data: bytes):
        if self.on_frame:
            self.on_frame(key, keyframe, pts_us, data)

    def _encoded_size(self, key: tuple, w: int, h: int):
        if self.on_encoded_size:
            self.on_encoded_size(key, w, h)

    def _capture_lost(self, node_id: int, msg: str):
        log.error("capture lost on node %d: %s", node_id, msg)
        if self.on_capture_lost:
            self.on_capture_lost(node_id, msg)

    def _stop_all(self):
        for cap in self._captures.values():
            cap.stop()
        self._captures.clear()
        self._refcounts.clear()
        self._retries.clear()
        self._skip.clear()
        self._nvenc_key = None
