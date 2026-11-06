# DeskLink

AnyDesk-style remote desktop for **KDE Plasma on Wayland** (host) and
**Android** (client). Screen capture and input injection go through the
official XDG desktop portal (PipeWire + libei) — no root, no X11 hacks.

```
┌─ Android client ───────────┐          ┌─ Linux host (KDE Plasma 6 / Wayland) ─┐
│ Kotlin + Compose           │          │ Python 3 + PyGObject + GStreamer      │
│ MediaCodec H.264 hw decode │◄─TCP:9090─┤ pipewiresrc → NVENC/x264 (zerolatency)│
│ touch → pointer/keyboard   │─control──►│ RemoteDesktop portal: absolute pointer,│
│ clipboard + file manager   │◄─2-way──► │ keysyms, Clipboard portal, file sandbox│
└────────────────────────────┘          └───────────────────────────────────────┘
```

## Features
- Live H.264 screen streaming (hardware NVENC when available, x264 fallback),
  30 fps, ~150 ms LAN latency
- Full remote control: absolute pointer, clicks, drag, scroll, keyboard text
  (any language via IME), special keys + sticky Ctrl/Alt/Shift/Super
- **Hardware-keyboard passthrough** — plug a keyboard into the phone/tablet;
  letters, digits and Ctrl/Alt/Super combos go straight to the host
- **Auto-reconnect** — network blips recover in 1.5–12 s with backoff, same
  stream and monitor
- **Saved hosts** — one-tap reconnect to known machines (add/remove in the
  connect screen)
- **View-only mode** — watch without sending any input
- **Live quality switching** — Very low (1 Mbps · 720p) / Low (2 Mbps ·
  1080p) / Balanced / High from the session menu, applied to the running
  encoder instantly; the client letterboxes to the real encoded resolution
- **Aspect-correct display** — video is letterboxed (never stretched) and
  touch coordinates match what you see; pinch to zoom, two fingers to
  scroll, double-finger spread switches to zoom mode automatically
- **Resolution-change resilience** — changing the monitor resolution or
  unplugging a monitor mid-session rebuilds the stream and keeps clients
  connected (a new consent dialog may appear; approve it to resume)
- **Immersive mode** — fullscreen with hidden system bars (swipe from the
  edge to reveal; toggle in the session menu)
- Multi-monitor: pick which screen to control; monitor switching on the fly
- Two-way clipboard sync (text) via the Clipboard portal
- File transfer: browse your home dir, download to the phone's Downloads
  (streamed to disk — no memory cap), upload to `~/Downloads/desklink`;
  live progress in the files sheet
- Desktop notifications when a client connects and when the last one leaves
- PIN authentication (challenge–response HMAC; the PIN itself is never sent)
- Transport: direct TCP. Works on LAN and over Tailscale with zero extra setup.

## Host setup (this machine)

Requirements (already present on this install): Python 3 + PyGObject,
GStreamer 1.x with `gst-plugins-good` (pipewiresrc), `gst-plugins-bad`
(NVENC) or `gst-plugins-ugly` (x264), PipeWire, xdg-desktop-portal-kde ≥ 6.

```bash
cd ~/personal/desklink/host
python3 -m desklink set-pin          # choose a PIN (≥4 chars)
python3 -m desklink serve            # a KDE consent dialog appears — click Share
```

> The XDG portal does not allow "remember" for remote-desktop sessions, so
> the consent dialog appears every time the host starts. If you run the host
> as a systemd service, that is one click per boot.

Other commands:

```bash
python3 -m desklink status                 # show LAN + Tailscale addresses
python3 -m desklink.testclient --help      # test client (dump stream to .h264)
```

Configuration lives at `~/.config/desklink/config.toml` (port, bitrate,
framerate, encoder, downloads dir, allowed file roots).

## Android client

```bash
cd ~/personal/desklink/android
./gradlew installDebug        # device connected via adb
# or: ./gradlew assembleDebug → app/build/outputs/apk/debug/app-debug.apk
```

In the app: enter `host:port` + PIN → Connect.

- Tap = left click · double-tap = double click · long-press = right click
- 1-finger drag = click-drag · 2-finger drag = scroll + pan · pinch = zoom
- Keyboard icon = text input + special keys and sticky modifiers
- Folder icon = file manager (download/upload)
- On the Android emulator, connect to `10.0.2.2:9090` (host loopback).

## Desktop viewer (desktop → desktop)

Control one desktop from another Linux machine — or the same one. GTK4 +
GStreamer, ships with the host package:

```bash
python3 -m desklink view --host 192.168.5.70 --pin 1234
# --port, --fullscreen, --view-only, --no-auto-fit also available
```

- **Resolution auto-fit** — the viewer asks the host to downscale the stream
  to its window size (device pixels), so a 1080p laptop viewing a 4K host
  decodes ~2 Mpixel frames instead of 8. Disable with `--no-auto-fit` or the
  menu toggle; manual quality presets set the bitrate either way.
- Letterboxed rendering at any window shape; pointer, wheel and keyboard are
  injected (printable keys via the host's Unicode keysym path, so client and
  host layouts can differ).
- Monitor picker, View-only, Fullscreen (F11), live stats, two-way clipboard.
- Auto-reconnect with backoff; survives host session auto-restarts via the
  re-`hello` (clients re-subscribe automatically).

## Connecting from outside your LAN

You already run Tailscale. Connect the phone to the same tailnet and use the
Tailscale IP (`100.65.7.64`) — traffic is encrypted by WireGuard; DeskLink's
PIN handshake sits on top. `python3 -m desklink status` prints the addresses.

## Security notes

- The PIN is stored as `sha256(pin)` in the config (mode 0600) and used as an
  HMAC key over a per-connection random salt — no replay, no plaintext PIN.
- TCP traffic itself is **not** additionally encrypted. On LAN or Tailscale
  this is fine; if you expose the port to a hostile network, wrap it (e.g.
  `systemd-socket-proxyd` + TLS, or an SSH tunnel) — proper TLS is on the
  roadmap.
- File browsing is sandboxed to the roots in `config.toml` (default: `~`).

## Project layout

```
host/desklink/     Python host
  portal.py        RemoteDesktop + ScreenCast + Clipboard portal dance
  video.py         per-monitor GStreamer pipelines, encoder fallback
  input.py         absolute/relative pointer model, keysym text input
  server.py        asyncio TCP server, auth, fan-out, file transfer
  displays.py      kscreen-doctor polling for live monitor changes
  viewer/          GTK4 desktop viewer (net, decode, app)
  testclient.py    reference client (dumps H.264, sends scripted input)
android/app/       Kotlin/Compose client
protocol.md        wire protocol spec (source of truth for both sides)
host/contrib/      systemd user service + autostart desktop file (optional)
```

## Known limitations (v1)

- One consent dialog per host start (portal policy, see note above).
- No audio streaming, no session recording, no ID/relay server (use
  Tailscale for NAT traversal).
- The stream resolution is the monitor's physical size (e.g. 3840×2160);
  the portal reports logical (scaled) sizes — aspect ratios match, so the
  client maps coordinates correctly regardless. Clients learn the actual
  encoded size (after quality caps) via `stream-info`.
- If the portal session is revoked (e.g. via the system tray indicator),
  the host re-requests it automatically — approve the consent dialog that
  appears and connected clients resume. If you deny it (or 120 s pass),
  the host exits.
