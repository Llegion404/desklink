# DeskLink

Self-hosted remote desktop for KDE Plasma (Wayland): control your Linux
desktop from an **Android phone** or another **Linux machine** — over LAN or
Tailscale, with no cloud, no accounts and no relay servers. One small Python
host, two clients, one documented wire protocol.

```
Android / Linux client                        Linux (KDE Wayland) host
┌──────────────────────────┐    TCP 9090   ┌───────────────────────────────┐
│ MediaCodec / GStreamer   │◄── H.264 ─────│ pipewiresrc → NVENC/x264 →    │
│ low-latency H.264 decode │── input ─────►│ appsink   (per-client encode) │
│ touch / mouse / keyboard │◄── clip ──────│ XDG RemoteDesktop portal      │
│ clipboard · file xfer    │── JSON/IPC ──►│ (ScreenCast + input inject)   │
└──────────────────────────┘               └───────────────────────────────┘
```

The host uses the XDG **RemoteDesktop + ScreenCast portals** — no root, no
keyloggers, no virtual displays. Capture, encoding and input injection all
run inside your user session; the desktop shows a consent dialog when a
session is created.

## Features

- **30 fps H.264 streaming** with hardware encode (NVENC) and automatic
  x264 fallback; per-frame backpressure with forced keyframes on drops
- **Per-client quality** — every client picks its own bitrate and resolution
  cap (`set-quality`); the host keeps **one capture per monitor** and builds
  one encode branch per distinct (resolution-cap, bitrate) pair off it, so a
  phone at 2 Mbps doesn't disturb a viewer at native 4K
- **Resolution auto-fit** (desktop viewer) — the stream is downscaled to the
  window's device size, so a 1080p laptop can watch a 4K host cheaply
- **Live display changes** — monitor resolution changes are detected and
  handled; an unplug triggers an automatic portal re-request and clients
  resume after approving the new consent dialog
- **Direct input** — absolute pointer, click/drag, wheel, full keyboard
  (Unicode keysym path: client and host keyboard layouts can differ)
- **Android**: pinch-zoom that actually zooms, two-finger scroll, keyboard
  sheet with sticky modifiers, hardware-key passthrough, multi-monitor
  picker, two-way clipboard, streamed file transfer (no memory cap),
  auto-reconnect, saved hosts
- **Desktop viewer (GTK4)**: connect GUI with saved hosts, aspect-correct
  letterboxed display, monitor picker, quality presets, view-only,
  fullscreen, two-way clipboard, auto-reconnect
- **Security**: per-connection HMAC-SHA256 challenge over a random salt —
  the PIN never crosses the wire
- Works over **plain LAN and Tailscale** with zero extra infrastructure

## Requirements

**Host** (the machine being controlled):
- KDE Plasma 6 on Wayland with `xdg-desktop-portal-kde`
- Python 3.10+ with PyGObject (`python-gobject`)
- GStreamer 1.x: `gst-plugins-good` (pipewiresrc), `gst-plugins-bad`
  (NVENC, optional), `gst-plugins-ugly` (x264)
- PipeWire

Arch/CachyOS:

```bash
sudo pacman -S --needed python python-gobject gst-plugins-good gst-plugins-bad gst-plugins-ugly pipewire
```

**Android client**: Android 8.0+ (minSdk 26); build with JDK 17 + Android
SDK 35.

**Desktop viewer**: GTK4 + GStreamer introspection (`gtk4`, `gst-python`).

## Quick start — host

```bash
git clone https://github.com/Llegion404/desklink.git
cd desklink/host
python3 -m desklink set-pin            # choose a PIN (≥4 chars)
python3 -m desklink serve              # a consent dialog appears — approve it
python3 -m desklink status             # prints LAN/Tailscale addresses
```

Optional: auto-start at login with the bundled systemd user unit
(see `host/contrib/desklink.service`; edit `WorkingDirectory` to your
checkout).

## Android client

```bash
cd desklink/android
./gradlew assembleDebug     # → app/build/outputs/apk/debug/app-debug.apk
./gradlew installDebug      # device attached via adb
```

Open the app, enter `host:port` + PIN, connect. You can also launch
pre-connected:

```bash
adb shell am start -n com.desklink.client/.MainActivity \
  --es host "192.168.1.20:9090" --es pin "1234"
```

Controls: tap = left click · long-press = right click · drag = move-drag ·
two-finger drag = scroll · pinch = zoom · keyboard icon = text, special keys
and sticky Ctrl/Alt/Shift/Super · folder icon = file transfer · ☰ menu =
monitor picker, quality, view-only.

## Desktop viewer (desktop → desktop)

```bash
cd desklink/host
python3 -m desklink view                    # connect GUI — saved hosts + PIN
python3 -m desklink view --host 192.168.1.20 --pin 1234   # direct connect
# --port --fullscreen --view-only --no-auto-fit
```

The **connect GUI** keeps a saved-host list (add with `+`, connect with
Enter or double-click, `Ctrl+N`, `Delete`, optional *Save PIN* per host,
Disconnect returns to the list). Hosts live in
`~/.config/desklink/hosts.json` (mode 0600); a saved PIN is stored in
**plaintext** in that file — tick *Save PIN* only on machines where that's
acceptable.

The session window letterboxes the stream to any window shape and, by
default (**auto-fit**), asks the host to downscale to the window's device
size — a 1080p laptop watching a 4K host decodes ~2 Mpixel frames instead
of 8. Pointer, wheel and keyboard are injected; printable keys go through a
Unicode keysym path, so the client and host keyboard layouts can differ.
F11 toggles fullscreen; the ☰ menu has monitors, quality, view-only and
Disconnect.

## Quality and resolution handling

- Quality is **per client**: pick Low/High in the menu, or let the viewer
  auto-fit. The host keeps **one capture per monitor** (KWin's screencast
  streams are single-consumer) and builds one encode branch per distinct
  (resolution-cap, bitrate) pair off it via a `tee`; clients with the same
  settings share a branch, a quality change rebuilds the capture (brief
  keyframe gap)
- Under congestion the host drops the oldest frames and forces a keyframe
- Monitor resolution changes are picked up live (the stream rebuilds and
  clients receive the new encoded size); a monitor unplug restarts the
  portal session and every connected client resumes after you approve the
  dialog
- One NVENC session is used per process (concurrent NVENC sessions crash
  some drivers); additional simultaneous streams encode with x264

## Connecting from outside your LAN

Tailscale works with zero configuration: install Tailscale on the host and
the client, connect both to your tailnet, and use the host's Tailscale IP.
Traffic is end-to-end encrypted by WireGuard; DeskLink's PIN handshake sits
on top. Plain port-forwarding on your router also works but exposes the
PIN authentication directly to the internet — Tailscale is recommended.

## Security notes

- The PIN is stored as `sha256(pin)` (file mode 0600) and used as an HMAC
  key over a per-connection random salt — no plaintext PIN, no replay
- Clients are untrusted until authenticated; video only flows to
  authenticated subscribers
- The portal consent dialog gates every capture session; the host can never
  capture without it

## Troubleshooting

- **Consent dialog on every (re)start** — XDG portal policy for
  remote-desktop sessions; it cannot be skipped. Denied/timeout ⇒ the host
  retries or exits
- **Keyboard/mouse not working** — make sure the consent dialog you approved
  included *remote control*, not just screen sharing
- **Second stream is x264** — that's the single-NVENC-session guard; the
  first-connected stream keeps NVENC
- **Samsung phone kills the app in background** — add DeskLink to
  Settings → Battery → *Never sleeping apps*
- **No audio** — out of scope for now

## Project layout

```
host/desklink/     Python host
  portal.py        RemoteDesktop + ScreenCast + Clipboard portal dance
  video.py         per-client GStreamer pipelines, encoder fallback, NVENC slot
  input.py         absolute/relative pointer model, keysym text input
  server.py        asyncio TCP server, auth, fan-out, file transfer
  displays.py      kscreen-doctor polling for live monitor changes
  viewer/          GTK4 desktop viewer (net, decode, app, launcher, hosts)
  testclient.py    reference client (dumps H.264, sends scripted input)
  e2e_multiclient.py  multi-client quality test (needs a running host + PIN)
android/app/       Kotlin/Compose client (MediaCodec decode, Compose UI)
protocol.md        wire protocol spec (source of truth for both sides)
host/contrib/      systemd user service + autostart desktop file (optional)
```

## Limitations / roadmap

- No audio
- One NVENC encode per process (others fall back to x264)
- KDE-centric host (portal + kscreen-doctor); GNOME/Wayland would need a
  display-size backend swap
- Roadmap ideas: per-session file-transfer UI in the desktop viewer, saved
  hosts in the viewer, audio

## License

MIT — see [LICENSE](LICENSE).
