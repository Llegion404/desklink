"""GTK4 viewer window for DeskLink (desktop-to-desktop client).

One window: letterboxed video, pointer/keyboard/scroll injection, monitor
picker, quality presets, resolution auto-fit, clipboard sync, reconnect.

Threading: the GTK main loop owns this thread; ViewerClient runs asyncio on
its own thread and marshals events here via GLib.idle_add. Video frames take
the hot path (reader thread -> appsrc) and only the finished textures come
back through idle_add, one at a time (latency beats completeness — the same
trade the Android decoder makes).
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, GLib, Gst, Gtk

from .decode import VideoDecoder
from .launcher import LauncherWindow
from .net import (ViewerClient, EvClip, EvClosed, EvError, EvHello,
                  EvPong, EvQuality, EvStreamInfo, EvSubscribed)
from .. import protocol as proto

log = logging.getLogger("desklink.viewer.app")

QUALITY_PRESETS = [                       # (kbps, label) — maxh is auto-fit's
    (1000, "Very low (1 Mbps)"),
    (2000, "Low (2 Mbps)"),
    (8000, "Balanced (8 Mbps)"),
    (20000, "High (20 Mbps)"),
]
SCROLL_STEP_PX = 80.0
FIT_DEBOUNCE_MS = 300


class AudioPlayback:
    """Desktop audio: Ogg-Opus pages from the wire -> oggdemux -> opusdec ->
    pulsesink. push() is called on the net reader thread (appsrc is
    thread-safe, same as the video hot path); start/stop on the GTK thread."""

    def __init__(self):
        self.pipeline = None
        self.src = None

    def start(self):
        if self.pipeline:
            return
        try:
            self.pipeline = Gst.parse_launch(
                "appsrc name=src is-live=true do-timestamp=true format=time "
                "! oggdemux ! opusdec ! audioconvert ! audioresample ! "
                "pipewiresink")
            self.src = self.pipeline.get_by_name("src")
            self.pipeline.set_state(Gst.State.PLAYING)
        except Exception:
            log.exception("audio pipeline failed to start")
            self.pipeline = None
            self.src = None

    def push(self, data: bytes):
        if self.src is not None:
            self.src.emit("push-buffer", Gst.Buffer.new_wrapped(data))

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.src = None


def run(args) -> int:
    """Entry point for `desklink view`."""
    app = ViewerApplication(args)
    return app.run(None)


class ViewerApplication(Gtk.Application):
    def __init__(self, args):
        super().__init__(application_id="com.desklink.viewer")
        self.args = args
        # launcher mode when no --host was given: the launcher is the home
        # screen, sessions open from it and return to it
        self.launcher_mode = args.host is None
        self.launcher: LauncherWindow | None = None
        self.win: ViewerWindow | None = None

    def do_activate(self):
        if self.launcher_mode:
            if self.launcher is None:
                self.launcher = LauncherWindow(self)
            self.launcher.present()
        else:
            self.open_session(self.args.host, self.args.port, self.args.pin)

    def open_session(self, host: str, port: int, pin: str):
        """Open a session window for (host, port); hides the launcher while
        it is up. CLI direct mode just opens the window."""
        opts = SimpleNamespace(host=host, port=port, pin=pin,
                               fullscreen=getattr(self.args, "fullscreen",
                                                  False),
                               view_only=getattr(self.args, "view_only",
                                                 False),
                               no_auto_fit=getattr(self.args, "no_auto_fit",
                                                   False))
        if self.launcher is not None:
            self.launcher.set_visible(False)
        if self.win is not None:
            # defensive: a session is already up — replace it
            self.win.on_closed_cb = None
            self.win.close()
        try:
            self.win = ViewerWindow(self, opts)
        except Exception:
            log.exception("session window failed to open for %s:%d",
                          host, port)
            self.win = None
            if self.launcher is not None:
                self.launcher.set_visible(True)
                self.launcher.present()
                self.launcher.session_returned(f"could not open viewer "
                                               f"for {host}:{port}")
            return
        self.win.on_closed_cb = self.session_closed
        self.win.present()

    def session_closed(self, reason: str | None):
        """Session window closed — back to the launcher in GUI mode."""
        self.win = None
        if self.launcher_mode and self.launcher is not None:
            self.launcher.set_visible(True)
            self.launcher.present()
            self.launcher.session_returned(reason)


class ViewerWindow(Gtk.ApplicationWindow):
    def __init__(self, app: ViewerApplication, args):
        super().__init__(application=app)
        self.args = args
        self._pin = args.pin
        self.client: ViewerClient | None = None
        self.decoder: VideoDecoder | None = None
        # called with the final disconnect reason when this window closes
        # (launcher mode uses it to show the error on the home screen)
        self.on_closed_cb = None
        self._last_error: str | None = None

        # session state
        self.host_name = args.host
        self.streams: list = []
        self.current_stream: int | None = None
        self.stream_w = 0
        self.stream_h = 0
        self.enc_w = 0
        self.enc_h = 0
        self.clipboard_ok = False
        self.connected = False

        # quality / auto-fit
        self.kbps = 8000
        self.maxh_sent: int | None = None
        self.auto_fit = not args.no_auto_fit

        # audio (host capability arrives in hello; on = user preference)
        self.audio_ok = False
        self.audio_on = True
        self.audio_pb = AudioPlayback()

        # view state
        self.view_only = bool(args.view_only)
        self.fps = 0
        self.kbps_shown = 0
        self.rtt_ms: int | None = None
        self._frames = 0
        self._bytes = 0
        self._stats_t0 = time.monotonic()
        self._frame_pending = False
        self._clipbox_until = 0.0
        self._last_fit: tuple[int, int] | None = None

        self._build_ui()
        self._install_controllers()
        self._start_client()

        if args.fullscreen:
            self.fullscreen()

        GLib.timeout_add(1000, self._stats_tick)
        GLib.timeout_add(200, self._fit_tick)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.set_title("DeskLink Viewer")
        self.set_default_size(1280, 720)

        # black letterbox background for the video area
        provider = Gtk.CssProvider()
        css = ".video-box { background: black; }"
        try:
            provider.load_from_string(css)          # GTK >= 4.12
        except AttributeError:
            provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.video = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN,
                                 can_shrink=True)
        box = Gtk.Box()
        box.add_css_class("video-box")
        box.append(self.video)
        box.set_hexpand(True)
        box.set_vexpand(True)
        # the host streams the cursor embedded in the video, so the local
        # pointer would show twice (view-only restores it)
        if not self.view_only:
            box.set_cursor(Gdk.Cursor.new_from_name("none"))

        self.stats_label = Gtk.Label(label="connecting…", halign=Gtk.Align.START)
        stats_box = Gtk.Box(margin_start=8, margin_top=8, margin_bottom=8)
        stats_box.append(self.stats_label)
        stats_box.add_css_class("osd")
        stats_box.set_valign(Gtk.Align.START)
        stats_box.set_halign(Gtk.Align.START)

        overlay = Gtk.Overlay()
        overlay.set_child(box)
        overlay.add_overlay(stats_box)

        header = Gtk.HeaderBar()
        title = Gtk.Label(label=self.host_name, halign=Gtk.Align.CENTER)
        header.set_title_widget(title)
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_popover(self._build_menu())
        header.pack_end(menu_btn)

        self.set_titlebar(header)
        self.set_child(overlay)
        self.set_focusable(True)

    def _build_menu(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=10, margin_bottom=10,
                      margin_start=10, margin_end=10)

        # -- monitor picker (populated on hello)
        self.stream_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(Gtk.Label(label="Monitor", halign=Gtk.Align.START,
                             xalign=0))
        box.append(self.stream_box)

        def sep():
            hr = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            box.append(hr)

        sep()
        box.append(Gtk.Label(label="Quality", halign=Gtk.Align.START, xalign=0))
        self._quality_buttons: list[Gtk.CheckButton] = []
        group: Gtk.CheckButton | None = None
        for kbps, label in QUALITY_PRESETS:
            btn = Gtk.CheckButton(label=label)
            if group is None:
                btn.set_active(True)
                group = btn
            else:
                btn.set_group(group)
            btn.connect("toggled", self._on_preset, kbps)
            self._quality_buttons.append(btn)
            box.append(btn)

        self.autofit_btn = Gtk.CheckButton(label="Auto-fit resolution",
                                           active=self.auto_fit)
        self.autofit_btn.connect("toggled", self._on_autofit)
        box.append(self.autofit_btn)

        self.audio_btn = Gtk.CheckButton(label="Audio", active=self.audio_on)
        self.audio_btn.connect("toggled", self._on_audio_toggle)
        box.append(self.audio_btn)

        sep()
        self.viewonly_btn = Gtk.CheckButton(label="View only",
                                            active=self.view_only)
        self.viewonly_btn.connect("toggled", self._on_viewonly)
        box.append(self.viewonly_btn)

        fullscreen_btn = Gtk.Button(label="Toggle fullscreen (F11)")
        fullscreen_btn.connect("clicked",
                               lambda *_: self._close_menu_and(self._toggle_fullscreen))
        box.append(fullscreen_btn)

        shot_btn = Gtk.Button(label="Save screenshot")
        shot_btn.set_tooltip_text("Save the current frame to ~/Pictures")
        shot_btn.connect("clicked",
                         lambda *_: self._close_menu_and(self._save_screenshot))
        box.append(shot_btn)

        self.reconnect_btn = Gtk.Button(label="Reconnect", visible=False)
        self.reconnect_btn.connect("clicked",
                                   lambda *_: self._close_menu_and(self._start_client))
        box.append(self.reconnect_btn)

        sep()
        leave = Gtk.Button(
            label="Disconnect" if self._in_launcher_mode() else "Close window")
        leave.add_css_class("destructive-action")
        leave.connect("clicked", lambda *_: self._close_menu_and(self.close))
        box.append(leave)

        popover = Gtk.Popover()
        popover.set_child(box)
        self._menu_popover = popover
        return popover

    # ---------------------------------------------------------- controllers
    def _in_launcher_mode(self) -> bool:
        app = self.get_application()
        return bool(app is not None and getattr(app, "launcher_mode", False))

    def _install_controllers(self):
        box = self.video

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_button, True)
        click.connect("released", self._on_button, False)
        box.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        box.add_controller(motion)

        scroll = Gtk.EventControllerScroll(
            flags=(Gtk.EventControllerScrollFlags.BOTH_AXES |
                   Gtk.EventControllerScrollFlags.DISCRETE))
        scroll.connect("scroll", self._on_scroll)
        box.add_controller(scroll)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key, True)
        keys.connect("key-released", self._on_key, False)
        self.add_controller(keys)

        self.connect("close-request", self._on_close)

    # -------------------------------------------------------------- client
    def _start_client(self):
        if self.client:
            self.client.close("restart")
        if self.decoder:
            self.decoder.stop()      # never leak the old pipeline
            self.decoder = None
        self.reconnect_btn.set_visible(False)
        self._set_status("connecting…")
        self.decoder = VideoDecoder(self._on_decoded_frame)
        if not self.decoder.start():
            self._set_status("no H.264 decoder available")
            return
        self.client = ViewerClient(self.args.host, self.args.port, self._pin,
                                   on_event=self._on_event,
                                   on_frame=self._on_net_frame,
                                   on_audio=self._on_net_audio)
        self.client.start_in_thread()

    def _on_close(self, *_):
        if self.client:
            self.client.close("window closed")
        if self.decoder:
            self.decoder.stop()
        self.audio_pb.stop()
        # return to the launcher BEFORE destroying this window so the app
        # never drops to zero windows mid-transition (that exits Gtk)
        if self.on_closed_cb is not None:
            cb, self.on_closed_cb = self.on_closed_cb, None
            cb(self._last_error)
        self.destroy()
        return True

    # ---------------------------------------------------------- input math
    def _map_dims(self) -> tuple[int, int]:
        return (self.enc_w or self.stream_w, self.enc_h or self.stream_h)

    def _to_norm(self, x: float, y: float) -> tuple[float, float] | None:
        """Map widget coords to normalized stream coords (letterbox fit)."""
        sw, sh = self._map_dims()
        if not sw or not sh:
            return None
        vw = float(self.video.get_allocated_width())
        vh = float(self.video.get_allocated_height())
        if vw <= 1 or vh <= 1:
            return None
        fit = min(vw / sw, vh / sh)
        draw_w, draw_h = sw * fit, sh * fit
        off_x = (vw - draw_w) / 2.0
        off_y = (vh - draw_h) / 2.0
        nx = min(max((x - off_x) / draw_w, 0.0), 1.0)
        ny = min(max((y - off_y) / draw_h, 0.0), 1.0)
        return nx, ny

    def _input_ok(self) -> bool:
        # current_stream guard: a motion with stream=None would crash the
        # host's index translation and drop the connection
        return (self.connected and not self.view_only
                and self.current_stream is not None)

    # -------------------------------------------------------- input events
    def _on_motion(self, _ctl, x, y):
        if not self._input_ok():
            return
        n = self._to_norm(x, y)
        if n:
            self.client.send_json(
                {"type": "motion", "stream": self.current_stream,
                 "x": n[0], "y": n[1]})

    def _on_button(self, gesture, _n, x, y, down):
        if not self._input_ok():
            return
        btn = {1: "left", 2: "middle", 3: "right"}.get(
            gesture.get_current_button())
        if btn is None:
            return
        n = self._to_norm(x, y)
        if n:
            self.client.send_json(
                {"type": "motion", "stream": self.current_stream,
                 "x": n[0], "y": n[1]})
        self.client.send_json({"type": "button", "btn": btn, "down": down})

    def _on_scroll(self, _ctl, dx, dy, unit):
        if not self._input_ok():
            return
        steps_y = self._scroll_steps(dy, unit, 'y')
        steps_x = self._scroll_steps(dx, unit, 'x')
        if steps_y:
            self.client.send_json(
                {"type": "scroll", "dx": 0, "dy": int(steps_y)})
        if steps_x:
            self.client.send_json(
                {"type": "scroll", "dx": int(steps_x), "dy": 0})

    def _scroll_steps(self, delta: float, unit, axis: str) -> float:
        # Gdk.ScrollUnit.WHEEL: delta is already whole notches; smooth
        # (touchpad) deltas accumulate into discrete steps
        if unit == Gdk.ScrollUnit.WHEEL:
            return delta
        attr = f"_scroll_acc_{axis}"
        acc = getattr(self, attr, 0.0) + delta
        steps = 0
        while acc >= SCROLL_STEP_PX:
            acc -= SCROLL_STEP_PX
            steps += 1
        while acc <= -SCROLL_STEP_PX:
            acc += SCROLL_STEP_PX
            steps -= 1
        setattr(self, attr, acc)
        return steps

    def _on_key(self, _ctl, keyval, _keycode, _state, down):
        if keyval == Gdk.KEY_F11 and down:
            self._toggle_fullscreen()
            return True
        if not self._input_ok():
            return False
        uni = Gdk.keyval_to_unicode(keyval)
        ctrl_ish = bool(_state & (Gdk.ModifierType.CONTROL_MASK |
                                  Gdk.ModifierType.ALT_MASK |
                                  Gdk.ModifierType.META_MASK))
        if uni and uni >= 32 and not ctrl_ish:
            # printable: send as text (Unicode keysym path on the host)
            if down:
                self.client.send_json({"type": "text", "s": chr(uni)})
            return True
        # modifiers, arrows, F-keys, Enter, …: X keysym press/release
        self.client.send_json({"type": "keysym", "sym": int(keyval),
                               "down": down})
        return True

    def _toggle_fullscreen(self):
        if self.fullscreened:
            self.unfullscreen()
        else:
            self.fullscreen()

    # ------------------------------------------------------------- UI menu
    def _on_preset(self, _btn, kbps):
        self.kbps = kbps
        if self.client and self.connected:
            # maxh stays owned by auto-fit (or native when disabled)
            self.client.set_quality(kbps, self._fit_maxh() or 0)
        self._update_subtitle()

    def _on_autofit(self, btn):
        self.auto_fit = btn.get_active()
        self._last_fit = None          # force a re-send on the next tick
        self._fit_tick()

    def _on_viewonly(self, btn):
        self.view_only = btn.get_active()
        # local cursor: shown in view-only (nothing follows the remote one),
        # hidden while controlling — the host embeds it in the stream
        self.video.get_parent().set_cursor(
            None if self.view_only else Gdk.Cursor.new_from_name("none"))

    def _on_audio_toggle(self, btn):
        self.audio_on = btn.get_active()
        self._apply_audio_state()

    def _apply_audio_state(self):
        """Sync the user's audio preference with the (new) connection —
        audio subscriptions are per-connection, so every hello re-applies."""
        if not self.audio_on or not self.audio_ok:
            self.audio_pb.stop()
            if self.client and self.connected:
                self.client.set_audio(False, "ogg")
            return
        self.audio_pb.start()
        if self.client and self.connected:
            self.client.set_audio(True, "ogg")

    def _on_net_audio(self, kind, data):
        # reader thread — Ogg pages straight into the decoder's appsrc
        if kind == proto.AUDIO_OGG and self.audio_on and self.audio_ok:
            self.audio_pb.push(data)

    def _close_menu_and(self, fn):
        if getattr(self, "_menu_popover", None):
            self._menu_popover.popdown()
        fn()

    def _pick_stream(self, sid):
        if sid == self.current_stream or self.client is None:
            return
        self.enc_w = self.enc_h = 0
        self._last_fit = None          # re-fit for the new stream
        self._scroll_acc_x = self._scroll_acc_y = 0.0
        self.client.subscribe(sid)

    # ------------------------------------------------------------ auto-fit
    def _fit_maxh(self) -> int:
        """Device-pixel height of the video area (0 = native / auto-fit off).

        Always the window height — the host's videoscale never upscales, so
        a window taller than the stream simply results in native. Comparing
        against the stream size here would oscillate (capped stream-info
        would look like the native size)."""
        if not self.auto_fit:
            return 0
        vh = self.video.get_allocated_height()
        scale = 1.0
        surface = self.get_surface()
        if surface is not None:
            scale = surface.get_scale() or 1.0
        return max(0, int(vh * scale))

    def _fit_tick(self) -> bool:
        if not (self.client and self.connected and self.current_stream is not None):
            return True
        target = self._fit_maxh()
        native = self._map_dims()[1]
        key = (self.kbps, target)
        if key == self._last_fit:
            return True
        self._last_fit = key
        self.client.set_quality(self.kbps, target)
        if target:
            log.info("auto-fit: %d kbps, max height %d (native %s)",
                     self.kbps, target, native or "?")
        return True

    # ------------------------------------------------------------- frames
    def _on_net_frame(self, stream, keyframe, pts_us, data):
        # reader thread: straight into the decoder (hot path)
        self._frames += 1
        self._bytes += len(data)
        if self.decoder:
            self.decoder.push(data, pts_us)

    def _on_decoded_frame(self, w, h, stride, data):
        # GStreamer streaming thread — pace to one queued frame max
        if self._frame_pending:
            return
        self._frame_pending = True
        GLib.idle_add(self._show_frame, w, h, stride, data)

    def _show_frame(self, w, h, stride, data):
        self._frame_pending = False
        try:
            # GStreamer "BGRA" byte order == GDK B8G8R8A8; the video is
            # opaque (alpha 255) so premultiplied is identical
            texture = Gdk.MemoryTexture.new(
                w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                GLib.Bytes.new(data), stride)
            self.video.set_paintable(texture)
            self._last_texture = texture   # for Save screenshot
        except Exception:
            log.exception("frame show failed", exc_info=True)
        return False                    # remove idle source

    def _save_screenshot(self):
        texture = getattr(self, "_last_texture", None)
        if texture is None:
            self._set_status("no frame to save yet")
            return
        from pathlib import Path
        import time as _t
        pics = Path.home() / "Pictures"
        pics.mkdir(exist_ok=True)
        path = pics / f"desklink-{_t.strftime('%Y%m%d-%H%M%S')}.png"
        try:
            texture.save_to_png(str(path))      # Gdk.Texture, GTK >= 4.6
            self._set_status(f"screenshot saved: {path.name}")
        except Exception:
            log.exception("screenshot save failed")
            self._set_status("screenshot failed")

    # -------------------------------------------------------------- events
    def _on_event(self, evt):
        try:
            self._dispatch_event(evt)
        except Exception:
            log.exception("event handler failed for %r", evt)
        return False                    # idle_add source removal

    def _dispatch_event(self, evt):
        kind = type(evt)
        if kind is EvHello:
            self.connected = True
            self.host_name = evt.host
            self.clipboard_ok = evt.clipboard
            self.audio_ok = evt.audio
            self.streams = evt.streams
            self._rebuild_stream_picker()
            target = (self.current_stream if any(
                s.id == self.current_stream for s in evt.streams)
                else (evt.streams[0].id if evt.streams else None))
            self.stream_w = next((s.w for s in evt.streams
                                  if s.id == target), 0)
            self.stream_h = next((s.h for s in evt.streams
                                  if s.id == target), 0)
            if target is not None:
                self.current_stream = target
                self.client.subscribe(target)
            self._set_status("connected")
            self._wire_clipboard()
            self._apply_audio_state()   # per-connection: re-subscribe audio
        elif kind is EvSubscribed:
            self.current_stream = evt.stream
            # no decoder flush here: sending flush events through an appsrc
            # leaves the pads stuck in FLUSHING; the decoder resyncs at the
            # next IDR (forced right below) instead
            self.client.request_idr(evt.stream)
            self._last_fit = None       # send auto-fit for the new pipeline
            self._fit_tick()
        elif kind is EvStreamInfo:
            if evt.stream == self.current_stream:
                self.enc_w, self.enc_h = evt.w, evt.h
        elif kind is EvQuality:
            self.maxh_sent = evt.maxh
        elif kind is EvPong:
            self.rtt_ms = evt.rtt_ms
        elif kind is EvClip:
            if evt.text and time.monotonic() > self._clipbox_until:
                self._clipbox_until = time.monotonic() + 2.0
                provider = Gdk.ContentProvider.new_for_value(evt.text)
                Gdk.Display.get_default().get_clipboard().set_content(provider)
        elif kind is EvError:
            self._last_error = evt.msg
            self._set_status(evt.msg)
        elif kind is EvClosed:
            self.connected = False
            self._scroll_acc_x = self._scroll_acc_y = 0.0
            self.reconnect_btn.set_visible(not evt.will_retry)
            if not evt.will_retry:
                self._last_error = evt.reason
                self._set_status(evt.reason)
        self._update_subtitle()

    # --------------------------------------------------------------- stats
    def _stats_tick(self) -> bool:
        now = time.monotonic()
        dur = now - self._stats_t0
        if dur >= 0.5:
            self.fps = round(self._frames / dur)
            self.kbps_shown = int(self._bytes * 8 / 1000 / dur)
            self._frames = 0
            self._bytes = 0
            self._stats_t0 = now
        if self.client and self.client.connected:
            self.client.ping()
        self._update_subtitle()
        return True

    def _update_subtitle(self):
        mbps = f"{self.kbps_shown / 1000:.1f}" if self.kbps_shown else "0"
        rtt = f"{self.rtt_ms}ms" if self.rtt_ms is not None else "–"
        fit = f" · fit {self.maxh_sent}p" if self.maxh_sent else ""
        self.stats_label.set_text(
            f"{self.fps}fps {mbps}Mb {rtt}  ·  {self._dims_text()}{fit}")

    def _dims_text(self) -> str:
        w, h = self._map_dims()
        return f"{w}×{h}" if w and h else "–"

    def _set_status(self, text: str):
        title = self.get_titlebar()
        if isinstance(title, Gtk.HeaderBar):
            lbl = title.get_title_widget()
            if isinstance(lbl, Gtk.Label):
                lbl.set_text(f"{self.host_name} — {text}")

    # ------------------------------------------------------------ clipboard
    def _wire_clipboard(self):
        if not self.clipboard_ok or getattr(self, "_clipboard_wired", False):
            return
        self._clipboard_wired = True
        cb = Gdk.Display.get_default().get_clipboard()
        cb.connect("changed", self._on_local_clipboard)

    def _on_local_clipboard(self, clipboard):
        if not (self.clipboard_ok and self.client and self.client.connected):
            return
        if time.monotonic() < self._clipbox_until:
            return
        clipboard.read_text_async(None, self._on_clip_read)

    def _on_clip_read(self, clipboard, result, _user=None):
        try:
            text = clipboard.read_text_finish(result)
        except Exception:
            return
        if text and self.client and self.client.connected:
            self._clipbox_until = time.monotonic() + 2.0
            self.client.clip_set(text)

    # ------------------------------------------------------------ picker UI
    def _rebuild_stream_picker(self):
        while child := self.stream_box.get_first_child():
            self.stream_box.remove(child)
        group: Gtk.CheckButton | None = None
        for s in self.streams:
            label = (f"{s.w}×{s.h}" if len(self.streams) < 2
                     else f"#{self.streams.index(s) + 1}  {s.w}×{s.h}")
            btn = Gtk.CheckButton(label=label)
            if group is None:
                group = btn
            else:
                btn.set_group(group)      # behave as a radio set
            btn.set_active(s.id == self.current_stream)
            btn.connect("toggled", self._on_stream_btn, s.id)
            self.stream_box.append(btn)

    def _on_stream_btn(self, btn, sid):
        if btn.get_active():
            self._pick_stream(sid)
