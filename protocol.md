# DeskLink wire protocol v1

One TCP connection per client. Little-endian unless noted; all integers
big-endian where marked BE.

## Framing

Every message after the connection is established:

```
[1 byte  frame type][4 bytes BE length][payload]
```

| Type | Name      | Payload |
|------|-----------|---------|
| 0x01 | JSON      | UTF-8 JSON object with a `"type"` field |
| 0x02 | video     | `[1B stream_id][1B flags][8B BE pts_us][H.264 Annex-B access unit]` |
| 0x03 | clipboard | reserved (sent as 0x01 JSON today) |
| 0x04 | file chunk| `[4B BE file_id][raw bytes]` |

Video flags: bit0 = keyframe (access unit contains an IDR/SPS). SPS/PPS are
present in-band before every IDR (encoder `config-interval=-1`), so a client
can start decoding at any keyframe.

## Handshake

```
server -> {"type":"greet","v":1,"salt":"<32 hex>","name":"<hostname>"}
client -> {"type":"auth","mac":"<hex>","client":"<user-agent>"}
server -> {"type":"hello","v":1,"host":"<name>","clipboard":bool,
           "streams":[{"id":<node_id>,"w":W,"h":H,"x":X,"y":Y}, ...]}
     or  {"type":"deny","msg":"..."}   (connection then closes)
```

`mac = HMAC-SHA256(sha256(pin), salt-as-utf8-bytes)` hex-encoded.
Stream `id` is a compact index (0..255) assigned by the host in this message
and used everywhere else (subscribe, motion, video frames) — NOT the portal's
PipeWire node id, which can exceed one byte. `w/h` are logical sizes
reported by the portal; the video itself may be higher resolution.

## Client → server JSON

| Message | Fields | Meaning |
|---|---|---|
| `subscribe` | `stream` | receive video for that stream (one at a time) |
| `unsubscribe` | | stop receiving video |
| `request-idr` | `stream` | force a keyframe (after lag/switch) |
| `set-quality` | `kbps, maxh` | **per-client** quality: this client's pipelines use these settings (bitrate 500–50000; `maxh` 0 = native). Other clients are unaffected. Clients with identical settings share one encode. |
| `motion` | `stream, x, y` | absolute pointer, x/y normalized 0..1 in stream space |
| `button` | `btn: left\|right\|middle, down: bool` | pointer button |
| `scroll` | `dx, dy` | discrete wheel steps |
| `key` | `code, down` | Linux evdev keycode |
| `text` | `s` | unicode string; host synthesizes keysym press/release per char |
| `clip-set` | `text` | set the host clipboard |
| `ping` | `t` | RTT probe |
| `fs-list` | `path, id` | list directory (`~` allowed) |
| `fs-get` | `path, id` | download a file |
| `fs-put` | `dir, name, size, id` | start an upload |
| `fs-put-end` | `id` | finish an upload |
| `bye` | | client is leaving |

## Server → client JSON

`hello`, `deny`, `pong` (echoes `t`), `clip` (`text`),
`quality` (`kbps, maxh, ok`) in reply to `set-quality`, echoing the
client's own settings,
`stream-info` (`stream, w, h` — the actual encoded resolution of the
subscribed stream, sent whenever it becomes known or changes: after
subscribe, after a quality change that alters the scale, and after a monitor
resolution change),
`fs-list-reply` (`id, path, entries:[{n,d,s}], error`),
`fs-data-begin` (`id, size, name`), `fs-data-end` (`id`),
`fs-put-ack` (`id, ok`), `fs-put-done` (`id, path, size`),
`fs-error` (`id, msg`), `stream-error` (`stream, msg` — also the reply to a
`subscribe` with an unknown stream id), `host-error` (`msg`),
`bye`.

## File transfer

Download: `fs-get` → `fs-data-begin` → N × frame 0x04 with `file_id = id` →
`fs-data-end`. Upload: `fs-put` → wait `fs-put-ack` → N × frame 0x04 →
`fs-put-end` → `fs-put-done`. Chunks are ≤ 64 KiB.

## Backpressure

Video frames may be dropped by the server under congestion (oldest first);
a dropped burst is followed by a forced keyframe. File chunks are never
dropped; the sender blocks on TCP backpressure.

## Session recovery

If the host's portal session dies (monitor unplug, compositor restart), the
host tears down its pipelines and automatically re-requests a new one — a
fresh consent dialog appears. TCP connections stay open: clients get a
`host-error` notice, then a new `hello` when the session is granted (and
re-subscribe). If the re-request fails or times out (120 s), the host sends
`host-error` and closes.

Monitor resolution changes mid-session do not restart anything: the host
updates the stream size in place, rebuilds the affected pipeline (brief
keyframe gap), and emits `stream-info` with the new encoded resolution.
