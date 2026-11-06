#!/usr/bin/env python3
"""Multi-client E2E test for per-client quality.

Needs a running, consent-approved host and its PIN:

    DESKLINK_HOST=127.0.0.1 DESKLINK_PORT=9090 DESKLINK_PIN=xxxx \
        python3 e2e_multiclient.py

Scenario 1: A subscribes native, B caps itself to 1080p — both must flow at
their own encoded size, A unaffected by B, A still flowing after B leaves.

Scenario 2: C joins B's capped encode (shared branch), keeps flowing when B
leaves, then moves to native and joins A's encode.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from desklink import protocol as proto  # noqa: E402

HOST = os.environ.get("DESKLINK_HOST", "127.0.0.1")
PORT = int(os.environ.get("DESKLINK_PORT", "9090"))
PIN = os.environ.get("DESKLINK_PIN", "")


class Probe:
    def __init__(self, name: str):
        self.name = name
        self.frames = 0
        self.infos: list[tuple[int, int]] = []

    async def connect(self) -> dict:
        self.reader, self.writer = await asyncio.open_connection(HOST, PORT)
        _, payload = await proto.read_frame(self.reader)
        greet = json.loads(payload)
        mac = hmac.new(hashlib.sha256(PIN.encode()).digest(),
                       greet["salt"].encode(), hashlib.sha256).hexdigest()
        self.writer.write(proto.json_message(
            {"type": "auth", "mac": mac, "client": f"e2e-{self.name}"}))
        await self.writer.drain()
        _, payload = await proto.read_frame(self.reader)
        hello = json.loads(payload)
        assert hello["type"] == "hello", hello
        return hello

    def send(self, obj: dict):
        self.writer.write(proto.json_message(obj))

    async def drain(self, seconds: float):
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            try:
                ftype, payload = await asyncio.wait_for(
                    proto.read_frame(self.reader), 5)
            except asyncio.TimeoutError:
                break
            if ftype == proto.FT_VIDEO:
                self.frames += 1
            else:
                msg = json.loads(payload)
                if msg["type"] == "stream-info":
                    self.infos.append((msg["w"], msg["h"]))
                elif msg["type"] == "stream-error":
                    print(f"  [{self.name}] STREAM-ERROR: {msg['msg']}")


def last(info: Probe) -> tuple[int, int]:
    return info.infos[-1] if info.infos else (0, 0)


async def scenario_1(sid: int):
    a, b = Probe("A-native"), Probe("B-1080p")
    ha = await a.connect()
    assert ha["streams"][0]["id"] == sid
    a.send({"type": "subscribe", "stream": sid})
    await a.writer.drain()
    await a.drain(3)
    print(f"A: frames={a.frames} size={last(a)}")
    assert a.frames > 20 and last(a)[1] >= 2000, "A native"

    hb = await b.connect()
    b.send({"type": "subscribe", "stream": sid})
    b.send({"type": "set-quality", "kbps": 2000, "maxh": 1080})
    await b.writer.drain()
    await b.drain(5)
    print(f"B: frames={b.frames} size={last(b)}")
    assert b.frames > 20 and last(b)[1] == 1080, f"B capped, got {b.infos}"

    a.frames = 0
    a.infos.clear()
    await a.drain(3)
    print(f"A after B capped: frames={a.frames} size={last(a)}")
    assert a.frames > 20 and (not a.infos or last(a)[1] >= 2000)

    b.writer.close()
    a.frames = 0
    await a.drain(3)
    print(f"A after B gone: frames={a.frames}")
    assert a.frames > 20, "A unaffected by B leaving"
    a.writer.close()


async def scenario_2(sid: int):
    a = Probe("A2-native")
    await a.connect()
    a.send({"type": "subscribe", "stream": sid})
    await a.writer.drain()
    await a.drain(2)

    b, c = Probe("B2-1080p"), Probe("C2-1080p")
    for p in (b, c):
        await p.connect()
        p.send({"type": "subscribe", "stream": sid})
        p.send({"type": "set-quality", "kbps": 2000, "maxh": 1080})
        await p.writer.drain()
        await p.drain(4)
        print(f"{p.name}: frames={p.frames} size={last(p)}")
        assert p.frames > 20 and last(p)[1] == 1080, f"{p.name} capped"

    b.writer.close()
    c.frames = 0
    await c.drain(3)
    print(f"C after B left: frames={c.frames}")
    assert c.frames > 20, "C keeps the shared encode"

    c.send({"type": "set-quality", "kbps": 8000, "maxh": 0})
    await c.writer.drain()
    await c.drain(4)
    print(f"C moved to native: frames={c.frames} size={last(c)}")
    assert c.frames > 20 and last(c)[1] >= 2000, "C joins native"

    a.frames = 0
    await a.drain(3)
    print(f"A2 final: frames={a.frames}")
    assert a.frames > 20
    for p in (a, c):
        p.writer.close()


async def main():
    if not PIN:
        sys.exit("set DESKLINK_PIN (and optionally DESKLINK_HOST/PORT)")
    first = Probe("probe")
    hello = await first.connect()
    first.writer.close()
    sid = hello["streams"][0]["id"]
    await scenario_1(sid)
    await scenario_2(sid)
    print("MULTI-CLIENT QUALITY: PASS")


asyncio.run(main())
