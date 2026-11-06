"""Built-in test client: authenticate, dump a stream to a .h264 file, run
scripted input, print stats. Verifiable with ffprobe/ffplay."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import time

from . import protocol as proto


async def run(args) -> int:
    reader, writer = await asyncio.open_connection(args.host, args.port)

    # -- handshake
    ftype, payload = await proto.read_frame(reader)
    greet = json.loads(payload)
    assert greet["type"] == "greet", greet
    pin_key = hashlib.sha256(args.pin.encode()).digest()
    mac = hmac.new(pin_key, greet["salt"].encode(), hashlib.sha256).hexdigest()
    writer.write(proto.json_message({"type": "auth", "mac": mac,
                                     "client": "desklink-testclient"}))
    await writer.drain()

    ftype, payload = await proto.read_frame(reader)
    hello = json.loads(payload)
    if hello["type"] != "hello":
        print(f"auth failed: {hello}")
        return 1
    print(f"connected to {hello['host']!r} clipboard={hello['clipboard']}")
    for s in hello["streams"]:
        print(f"  stream {s['id']}: {s['w']}x{s['h']} at +{s['x']},+{s['y']}")

    if args.subscribe is None:
        args.subscribe = hello["streams"][0]["id"]
    writer.write(proto.json_message({"type": "subscribe", "stream": args.subscribe}))
    await writer.drain()

    # -- reader / stats
    dump = open(args.dump, "wb") if args.dump else None
    stats = {"frames": 0, "bytes": 0, "keyframes": 0, "t0": None, "tlast": None}
    stop_at = time.monotonic() + args.seconds
    actions_done = False
    rtt = None

    async def actions():
        nonlocal actions_done
        await asyncio.sleep(1.5)  # let the stream settle
        if args.click:
            x, y = args.click
            print(f"action: motion to {x},{y} + left click")
            writer.write(proto.json_message(
                {"type": "motion", "stream": args.subscribe, "x": x, "y": y}))
            await writer.drain()
            await asyncio.sleep(0.3)
            for down in (True, False):
                writer.write(proto.json_message(
                    {"type": "button", "btn": "left", "down": down}))
                await writer.drain()
                await asyncio.sleep(0.08)
        if args.text:
            print(f"action: typing {args.text!r}")
            writer.write(proto.json_message({"type": "text", "s": args.text}))
            await writer.drain()
        if args.rtt:
            for _ in range(3):
                writer.write(proto.json_message(
                    {"type": "ping", "t": int(time.time() * 1000)}))
                await writer.drain()
                await asyncio.sleep(0.5)
        actions_done = True

    asyncio.create_task(actions())

    try:
        while time.monotonic() < stop_at:
            ftype, payload = await asyncio.wait_for(proto.read_frame(reader), 5)
            if ftype == proto.FT_VIDEO:
                stream_id, flags, pts, data = proto.parse_video_payload(payload)
                stats["frames"] += 1
                stats["bytes"] += len(data)
                if flags & proto.FLAG_KEYFRAME:
                    stats["keyframes"] += 1
                now = time.monotonic()
                if stats["t0"] is None:
                    stats["t0"] = now
                stats["tlast"] = now
                if dump:
                    dump.write(data)
            elif ftype == proto.FT_JSON:
                msg = json.loads(payload)
                if msg.get("type") == "pong":
                    rtt = proto.now_ms() - int(msg.get("t", 0))
                    print(f"  pong rtt={rtt} ms")
                elif msg.get("type") == "hello":
                    # session was re-requested host-side — resubscribe
                    print("  re-hello (session restored) — resubscribing")
                    sid = msg["streams"][0]["id"]
                    writer.write(proto.json_message(
                        {"type": "subscribe", "stream": sid}))
                    await writer.drain()
                elif msg.get("type") == "clip":
                    print(f"  clipboard from host: {msg['text'][:80]!r}")
                else:
                    print(f"  msg: {msg}")
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        if dump:
            dump.close()
        writer.close()

    dur = (stats["tlast"] or time.monotonic()) - (stats["t0"] or time.monotonic())
    fps = stats["frames"] / dur if dur > 0.3 else 0.0
    kbps = stats["bytes"] * 8 / 1000 / dur if dur > 0.3 else 0.0
    print(f"\nstats: {stats['frames']} frames ({stats['keyframes']} key) "
          f"in {dur:.1f}s = {fps:.1f} fps, {kbps:.0f} kbps"
          + (f", rtt {rtt} ms" if rtt is not None else ""))
    if args.dump:
        print(f"dumped to {args.dump} — verify with: ffplay {args.dump}  "
              f"or ffprobe {args.dump}")
    return 0 if stats["frames"] > 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="desklink testclient")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--pin", required=True)
    parser.add_argument("--subscribe", type=int, default=None,
                        help="stream/monitor id (default: first)")
    parser.add_argument("--dump", default=None, help="write H.264 Annex-B to file")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--click", nargs=2, type=float, metavar=("X", "Y"),
                        help="normalized coords, e.g. 0.5 0.5")
    parser.add_argument("--text", help="type this text on the host")
    parser.add_argument("--rtt", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
