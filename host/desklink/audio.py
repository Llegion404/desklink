"""Host audio: default monitor capture -> Opus -> per-client framing.

One Opus encode of the desktop audio (PipeWire monitor of the default
sink — `pulsesrc` is often missing, `pipewiresrc` always is), fanned out
through a tee into TWO framings from the same encode:

  raw branch: one Opus packet per frame (kind AUDIO_OPUS) — plus a
  synthesized OpusHead (AUDIO_OPUS_HEAD) as MediaCodec csd-0, because
  Android's decoder requires it and PyGObject cannot read the encoder's
  streamheader caps field.

  ogg branch: Ogg pages (AUDIO_OGG) — self-describing, so the GTK viewer's
  oggdemux initializes the decoder without streamheader juggling (PyGObject
  cannot WRITE the streamheader GValueArray either — both halves of the
  plain-Python path are blocked by the same binding gap).

Clients pick a format via `set-audio {on, format}`; opusenc's 6.5 ms
algorithmic delay = preskip 312 samples at 48 kHz, which is what the
synthesized header says. All lifecycle on the GLib thread like video.py.
"""

from __future__ import annotations

import json
import logging
import subprocess

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from .glibloop import GLibRunner

log = logging.getLogger("desklink.audio")

Gst.init(None)

BITRATE_KBPS = 96       # stereo 48 kHz, 20 ms frames
FRAME_MS = 20
# libopus algorithmic delay: 6.5 ms = 312 samples at 48 kHz
PRESKIP = 312

OPUS_HEAD = (b"OpusHead" + bytes([1, 2]) + PRESKIP.to_bytes(2, "little")
             + (48000).to_bytes(4, "little") + (0).to_bytes(2, "little")
             + bytes([0]))


def default_sink_node() -> int | None:
    """PipeWire node id of the default sink (capturing it = its monitor)."""
    try:
        info = subprocess.run(["pactl", "info"], capture_output=True,
                              text=True, timeout=5).stdout
        sink = next((l.split(":", 1)[1].strip()
                     for l in info.splitlines() if l.startswith("Default Sink")),
                    None)
        if not sink:
            return None
        out = subprocess.run(["pw-dump"], capture_output=True, text=True,
                             timeout=10).stdout
        for o in json.loads(out):
            props = (o.get("info") or {}).get("props") or {}
            if (props.get("media.class") == "Audio/Sink"
                    and props.get("node.name") == sink):
                return int(o["id"])
    except Exception:
        return None
    return None


def available() -> bool:
    for name in ("pipewiresrc", "opusenc", "oggmux", "audioconvert",
                 "audioresample"):
        if Gst.ElementFactory.find(name) is None:
            log.info("audio disabled: %s not available", name)
            return False
    return True


class AudioManager:
    def __init__(self, glib: GLibRunner):
        self.glib = glib
        self.pipeline = None
        self._bus_id = 0
        self.on_raw = None       # (opus_packet: bytes) GLib thread
        self.on_ogg = None       # (ogg_page: bytes)    GLib thread

    # ------------------------------------------------------------- lifecycle
    def start(self):
        """Build + start the capture. GLib thread. Returns True on success."""
        node = default_sink_node()
        if node is None:
            log.warning("audio disabled: could not resolve the default "
                        "sink's monitor node")
            return False
        desc = (
            f"pipewiresrc path={node} keepalive-time=5 ! audioconvert ! "
            f"audioresample ! opusenc frame-size={FRAME_MS} "
            f"bitrate={BITRATE_KBPS * 1000} ! "   # opusenc bitrate is in bps
            "tee name=t allow-not-linked=true "
            "t. ! queue leaky=downstream max-size-time=200000000 ! "
            "appsink name=rawsink emit-signals=true sync=false "
            "max-buffers=3 drop=true "
            "t. ! queue leaky=downstream max-size-time=200000000 ! "
            "oggmux max-delay=40000000 max-page-delay=40000000 ! "
            "appsink name=ogsink emit-signals=true sync=false "
            "max-buffers=3 drop=true")
        # the tee and its branches join with a SPACE (see video.py)
        try:
            pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            log.warning("audio pipeline parse failed: %s", exc.message)
            return False
        ok = pipeline.set_state(Gst.State.PLAYING)
        if ok == Gst.StateChangeReturn.ASYNC:
            ok, state, _ = pipeline.get_state(5_000_000_000)
        if ok != Gst.StateChangeReturn.SUCCESS or state != Gst.State.PLAYING:
            pipeline.set_state(Gst.State.NULL)
            log.warning("audio pipeline failed to reach PLAYING")
            return False
        self.pipeline = pipeline
        pipeline.get_by_name("rawsink").connect(
            "new-sample", self._sample(self.on_raw))
        pipeline.get_by_name("ogsink").connect(
            "new-sample", self._sample(self.on_ogg))
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_id = bus.connect("message::error", self._on_bus_error)
        log.info("audio: monitor of sink node %d -> opus %d kbps stereo "
                 "(%d ms frames)", node, BITRATE_KBPS, FRAME_MS)
        return True

    def stop(self):
        if self._bus_id:
            bus = self.pipeline.get_bus()
            bus.disconnect(self._bus_id)
            bus.remove_signal_watch()
            self._bus_id = 0
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _sample(self, cb):
        def cb_sample(sink):
            sample = sink.emit("pull-sample")
            if sample is None or cb is None:
                return Gst.FlowReturn.OK
            buf = sample.get_buffer()
            data = buf.extract_dup(0, buf.get_size())
            cb(data)
            return Gst.FlowReturn.OK
        return cb_sample

    def _on_bus_error(self, bus, message):
        err, _ = message.parse_error()
        log.warning("audio pipeline error: %s — stopping audio", err.message)
        self.stop()
