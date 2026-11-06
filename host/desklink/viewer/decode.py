"""Viewer-side H.264 decode: appsrc -> h264parse -> decoder -> appsink.

Decoded BGRA frames are handed to `on_frame(width, height, stride, data)`
on a GStreamer streaming thread; app.py turns them into Gdk.MemoryTextures
on the GTK main thread. push() is called from the network reader thread and
is thread-safe (appsrc is).
"""

from __future__ import annotations

import logging

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

log = logging.getLogger("desklink.viewer.decode")

Gst.init(None)


def _decoder_chains() -> list[str]:
    """HW first, SW fallback — same candidate pattern as the host encoder."""
    chains = []
    if (Gst.ElementFactory.find("nvh264dec") is not None
            and Gst.ElementFactory.find("cudaconvert") is not None):
        # NVDEC outputs CUDA memory; cudaconvert brings it back to system
        chains.append("nvh264dec ! cudaconvert")
    if Gst.ElementFactory.find("avdec_h264") is not None:
        chains.append("avdec_h264")
    if not chains:
        raise RuntimeError("no H.264 decoder available "
                           "(tried nvh264dec, avdec_h264)")
    return chains


class VideoDecoder:
    def __init__(self, on_frame):
        self.on_frame = on_frame      # (w, h, stride, bytes)
        self.pipeline = None
        self.appsrc = None
        self.appsink = None
        self._bus_id = 0
        self._width = 0
        self._height = 0
        self._stride = 0
        self._last_pts = 0            # host pts resets on pipeline rebuilds;
                                      # enforce monotonic so avdec stays sane

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        src = ("appsrc name=src is-live=true format=time "
               "caps=video/x-h264,stream-format=byte-stream,alignment=au")
        sink = ("appsink name=sink emit-signals=true max-buffers=3 drop=true "
                "sync=false caps=video/x-raw,format=BGRA")
        for dec in _decoder_chains():
            desc = f"{src} ! h264parse config-interval=-1 ! {dec} ! " \
                   f"videoconvert ! {sink}"
            try:
                pipeline = Gst.parse_launch(desc)
            except GLib.Error as exc:
                log.warning("decoder chain failed to parse (%s): %s",
                            dec, exc.message)
                continue
            ok = pipeline.set_state(Gst.State.PLAYING)
            # ASYNC is fine: an appsrc pipeline prerolls (PAUSED->PLAYING)
            # only when the first frame arrives
            if ok == Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(Gst.State.NULL)
                log.warning("decoder chain failed to start: %s", dec)
                continue
            self.pipeline = pipeline
            self.appsrc = pipeline.get_by_name("src")
            self.appsink = pipeline.get_by_name("sink")
            self.appsink.connect("new-sample", self._on_sample)
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            self._bus_id = bus.connect("message::error", self._on_error)
            log.info("viewer decoder starting via %s", dec.split()[0])
            return True
        log.error("no decoder could start")
        return False

    def stop(self):
        if self.pipeline:
            if self._bus_id:
                bus = self.pipeline.get_bus()
                bus.disconnect(self._bus_id)
                bus.remove_signal_watch()
                self._bus_id = 0
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.appsrc = None
            self.appsink = None

    # ---------------------------------------------------------------- feed
    def push(self, data: bytes, pts_us: int):
        """Feed one H.264 access unit (Annex-B). Thread-safe."""
        if self.appsrc is None:
            return
        buf = Gst.Buffer.new_wrapped(data)
        pts = pts_us * 1000 if pts_us > 0 else 0
        if pts <= self._last_pts:
            pts = self._last_pts + 33_333    # 30 fps fallback cadence
        self._last_pts = pts
        buf.pts = pts
        self.appsrc.emit("push-buffer", buf)

    # -------------------------------------------------------------- samples
    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps()
        if caps is not None and caps.get_size():
            s = caps.get_structure(0)
            self._width = int(s.get_value("width"))
            self._height = int(s.get_value("height"))
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            # BGRA: GStreamer rowstride is 4-byte aligned and 4 bytes/px,
            # so it is exactly width*4 (GstVideo.VideoMeta.get has no
            # PyGObject binding, and gst_video_info_from_caps info is
            # unnecessary for a fixed-format sink)
            stride = self._width * 4
            self.on_frame(self._width, self._height, stride, info.data)
        finally:
            buf.unmap(info)
        return Gst.FlowReturn.OK

    def _on_error(self, _bus, message):
        err, _dbg = message.parse_error()
        log.error("viewer decoder error: %s", err.message)
