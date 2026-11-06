"""Wire protocol shared by host and clients.

Structure
=========
One TCP connection per client. All messages after the initial JSON greeting
exchange use binary framing:

    [1 byte  frame type][4 bytes BE length][payload]

Frame types
-----------
0x01 JSON control message (UTF-8, both directions)
0x02 video frame      payload = [1B stream_id][1B flags][8B pts_us][H.264 Annex-B]
0x03 clipboard        payload = UTF-8 JSON (alias of 0x01, reserved for clarity)
0x04 file chunk       payload = [4B BE file_id][raw bytes]

Handshake
---------
server -> {"type":"greet","v":1,"salt":"<32 hex>","name":"<host>"}
client -> {"type":"auth","mac":"<hmac-sha256-hex(sha256(pin), salt)>","client":"..."}
server -> {"type":"hello","streams":[{"id":0,"w":W,"h":H,"x":X,"y":Y},...],
           "clipboard":true,"host":"..."}
      or {"type":"deny","msg":"..."} then close.
A fresh hello is re-sent to connected clients when a dead portal session has
been replaced (auto re-request; clients re-subscribe).

Notable client->host JSON
-------------------------
{"type":"subscribe","stream":0}
{"type":"request-idr","stream":0}
{"type":"set-quality","kbps":8000,"maxh":1080}    # maxh 0 = native
{"type":"motion","stream":0,"x":0.5,"y":0.5}      # normalized coords
{"type":"button","btn":"left|right|middle","down":true}
{"type":"scroll","dx":0,"dy":-1}                  # discrete steps
{"type":"key","code":28,"down":true}              # Linux evdev keycode
{"type":"text","s":"hello"}                       # unicode text
{"type":"clip-set","text":"..."}                  # push clipboard to host
{"type":"ping","t":<ms>}                          # host echoes {"type":"pong",...}

Notable server->host JSON
-------------------------
{"type":"stream-info","stream":0,"w":1920,"h":1080}  # actual encoded size;
#   emitted when it becomes known or changes (subscribe, quality preset with
#   a resolution cap, monitor resolution change)

File transfer JSON
------------------
{"type":"fs-list","path":"~","id":1}   -> {"type":"fs-list-reply",...}
{"type":"fs-get","path":"~/a.pdf","id":2}
    -> {"type":"fs-data-begin","id":2,"size":N,"name":"a.pdf"}
       0x04 frames with file_id=2 ...
       {"type":"fs-data-end","id":2}
{"type":"fs-put","dir":"~","name":"pic.jpg","size":N,"id":3}
    -> {"type":"fs-put-ack","id":3,"ok":true}  then 0x04 frames (file_id=3)
       {"type":"fs-put-end","id":3}
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import struct
import time

PROTO_VERSION = 1

FT_JSON = 0x01
FT_VIDEO = 0x02
FT_CLIPBOARD = 0x03
FT_FILE = 0x04

FLAG_KEYFRAME = 0x01

MAX_FRAME = 16 * 1024 * 1024  # hard cap on any single frame payload


def frame(ftype: int, payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME:
        raise ValueError(f"frame too large: {len(payload)}")
    return struct.pack(">BI", ftype, len(payload)) + payload


def video_payload(stream_id: int, flags: int, pts_us: int, data: bytes) -> bytes:
    # stream ids are compact indices (0..255) assigned in `hello`; clamp so a
    # stray large id can never crash the pack (PipeWire node ids are unbounded)
    stream_id = max(0, min(255, stream_id))
    return struct.pack(">BBQ", stream_id, flags, pts_us) + data


def parse_video_payload(payload: bytes) -> tuple[int, int, int, bytes]:
    stream_id, flags, pts_us = struct.unpack_from(">BBQ", payload, 0)
    return stream_id, flags, pts_us, payload[10:]


def file_payload(file_id: int, data: bytes) -> bytes:
    return struct.pack(">I", file_id) + data


def parse_file_payload(payload: bytes) -> tuple[int, bytes]:
    (file_id,) = struct.unpack_from(">I", payload, 0)
    return file_id, payload[4:]


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(5)
    ftype, length = struct.unpack(">BI", header)
    if length > MAX_FRAME:
        raise ConnectionError(f"frame length {length} exceeds cap")
    payload = await reader.readexactly(length) if length else b""
    return ftype, payload


# ------------------------------------------------------------------ auth
def new_salt() -> str:
    return secrets.token_hex(16)


def compute_mac(pin_key: bytes, salt: str) -> str:
    return hmac.new(pin_key, salt.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_mac(pin_key: bytes, salt: str, mac_hex: str) -> bool:
    return hmac.compare_digest(compute_mac(pin_key, salt), mac_hex)


# ------------------------------------------------------------------ misc
def json_message(obj: dict) -> bytes:
    import json
    return frame(FT_JSON, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def now_ms() -> int:
    return int(time.time() * 1000)
