"""Local management socket for the running host (`desklink tui` + scripts).

A unix datagram-free stream socket at $XDG_RUNTIME_DIR/desklink.sock (falls
back to /tmp/desklink-<uid>.sock), mode 0600 — same-user access only. One
JSON line in, one JSON line out:

  {"cmd":"status"}                    -> full state snapshot (see
                                         DeskLinkServer.snapshot())
  {"cmd":"kick","client":<id>}        -> disconnect that client
  {"cmd":"idr","stream":<idx>}        -> force a keyframe on all branches
                                         of that stream
  {"cmd":"ping"}                      -> {"ok":true}

Read-mostly by design: the TUI polls `status` once a second; kick and idr
are the only mutations. The socket is created/removed by the host lifecycle
and rebuilt on portal restarts — nothing here touches GLib directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("desklink.control")

MAX_LINE = 1 << 20          # a status snapshot fits far below this


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "desklink.sock"
    return Path(f"/tmp/desklink-{os.getuid()}.sock")


class ControlServer:
    def __init__(self, host_server):
        self.host = host_server            # DeskLinkServer, for snapshot/kick
        self.path = socket_path()
        self._server: asyncio.base_events.Server | None = None

    async def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()             # stale socket from a killed host
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.path))
        os.chmod(self.path, 0o600)
        log.info("control socket: %s", self.path)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > MAX_LINE or not line.strip():
                return
            reply = await self._dispatch(line)
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except asyncio.TimeoutError:
            pass
        except Exception:
            log.exception("control request failed")
            try:
                writer.write(b'{"ok":false,"error":"internal"}\n')
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    async def _dispatch(self, line: bytes) -> dict:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "error": "bad json"}
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True}
        if cmd == "status":
            return {"ok": True, **self.host.snapshot()}
        if cmd == "kick":
            cid = req.get("client")
            if not isinstance(cid, int):
                return {"ok": False, "error": "client id must be int"}
            return await self.host.kick(cid)
        if cmd == "idr":
            sid = req.get("stream")
            if not isinstance(sid, int):
                return {"ok": False, "error": "stream id must be int"}
            return self.host.force_keyframe(sid)
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}
