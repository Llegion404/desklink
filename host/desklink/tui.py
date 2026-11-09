"""`desklink tui` — curses dashboard for a running DeskLink host.

Reads the host's control socket (see control.py) once per refresh tick and
paints streams/branches and connected clients. Keys:

  q / Esc   quit
  k <id>    kick a client (Enter confirms)
  i <#>     force a keyframe on stream #
  + / -     faster / slower refresh (0.5–5 s)
  space     pause/resume polling

Dependency-free (stdlib curses); the same `status` JSON is available to
scripts — see README ("Host monitoring").
"""

from __future__ import annotations

import curses
import json
import socket
import time

from .control import socket_path

MIN_INTERVAL = 0.5
MAX_INTERVAL = 5.0


class HostConnection:
    """One request-per-connection client for the control socket (the host
    closes after each reply; polling once a second costs nothing)."""

    def __init__(self, path):
        self.path = str(path)

    def request(self, obj: dict, timeout: float = 3.0) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(self.path)
                s.sendall((json.dumps(obj) + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            return json.loads(buf.decode())
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}


def _fmt_bytes(kilobits_out: int) -> str:
    kb = kilobits_out / 8.0
    if kb < 1024:
        return f"{kb:.0f} KB"
    if kb < 1024 * 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb / 1024 / 1024:.2f} GB"


def _fmt_uptime(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _branch_label(maxh: int) -> str:
    return "native" if not maxh else f"{maxh}p"


def _quality_label(kbps: int, maxh: int) -> str:
    return f"{kbps}k/{_branch_label(maxh)}"


def collect(conn: HostConnection) -> dict:
    return conn.request({"cmd": "status"})


def render(scr, snap: dict | None, error: str | None, state: dict):
    scr.erase()
    rows, cols = scr.getmaxyx()
    y = 0

    def line(text: str, style: int = 0):
        nonlocal y
        if y < rows - 1:
            scr.addnstr(y, 0, text, cols - 1, style)
            y += 1

    # ---- header
    if snap:
        marks = []
        marks.append("PIN ✓" if snap.get("pin_set") else "PIN ✗")
        marks.append("clipboard ✓" if snap.get("clipboard") else "clipboard –")
        if snap.get("restarting"):
            marks.append("PORTAL RESTARTING")
        txt = (f" DeskLink host {snap.get('host', '?')}  ·  "
               f"up {_fmt_uptime(snap.get('uptime_s', 0))}  ·  "
               f":{snap.get('port', '?')}  ·  {' · '.join(marks)}  ·  "
               f"{len(snap.get('clients', []))} client(s)")
        line(txt, curses.A_BOLD | curses.color_pair(1))
    else:
        line(" DeskLink host — no data", curses.A_BOLD | curses.color_pair(3))
    y += 1

    # ---- streams
    line(" STREAMS", curses.A_UNDERLINE)
    for st in (snap or {}).get("streams", []):
        line(f"  #{st['id']}  node {st['node']}  logical "
             f"{st['logical'][0]}×{st['logical'][1]}")
        branches = st.get("branches", [])
        for i, b in enumerate(branches):
            tee = "└" if i == len(branches) - 1 else "├"
            w, h = b.get("size", (0, 0))
            dims = f"{w}×{h}" if w else "…"
            n = b.get("clients", 0)
            line(f"     {tee} {_branch_label(b.get('maxh', 0)):<6} "
                 f"{b.get('kbps', 0):>5} kbps  {b.get('encoder', '?'):<10} "
                 f"{dims:<10} {n} client{'s' if n != 1 else ''}")
        if not branches:
            line("     (idle — no subscribers)")
    if not (snap or {}).get("streams"):
        line("  (no display session)")
    y += 1

    # ---- clients
    line(" CLIENTS", curses.A_UNDERLINE)
    # column segments laid out left to right within the terminal width —
    # the least important (frames, queue) drop off first on narrow windows
    segs = [  # (header, value_fn, width)
        ("id", lambda c: f"{c['id']:>3}", 4),
        ("name", lambda c: f"{c.get('name', '?')[:20]:<20}", 21),
        ("address", lambda c: f"{c.get('addr', '?')[:22]:<22}", 23),
        ("watch", lambda c: ("–" if c.get("stream") is None
                             else f"#{c['stream']}"), 6),
        ("quality", lambda c: _quality_label(c.get("kbps", 0),
                                             c.get("maxh", 0)), 14),
        ("out", lambda c: _fmt_bytes(c.get("kbps_out", 0)), 9),
        ("frames", lambda c: f"{c.get('frames', 0)}", 8),
        ("drop", lambda c: f"{c.get('dropped', 0)}", 6),
        ("q", lambda c: f"{c.get('video_q', 0)}", 3),
    ]
    usable = max(40, cols - 1)          # same limit addnstr truncates at
    header, hwidth = "  ", 2
    for h, _fn, w in segs:
        if hwidth + w > usable:
            break
        header += f"{h:>{w}}" if h in ("id", "out", "frames", "drop", "q") \
            else f"{h:<{w}}"
        hwidth += w
    line(header, curses.A_DIM)
    for c in (snap or {}).get("clients", []):
        row, rwidth = "  ", 2
        for _h, fn, w in segs:
            if rwidth + w > usable:
                break
            row += f"{fn(c):>{w}}" if _h in ("id", "out", "frames", "drop",
                                             "q") else f"{fn(c):<{w}}"
            rwidth += w
        style = curses.color_pair(3) if c.get("dropped", 0) > 20 else 0
        line(row, style)
    if not (snap or {}).get("clients"):
        line("  (none connected)")
    y += 1

    # ---- footer
    rate = state["interval"]
    mode = "PAUSED" if state["paused"] else f"refresh {rate:.1f}s"
    line(f" [q]uit  [k <id>] kick  [i <#>] keyframe  [+/-] rate  "
         f"[space] pause   ({mode})", curses.A_DIM)

    if state["message"]:
        line(f" {state['message']}", curses.color_pair(2))
    if error:
        line(f" {error}", curses.color_pair(3))
    scr.refresh()


def _prompt(scr, label: str) -> str:
    rows, cols = scr.getmaxyx()
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    curses.echo()
    scr.addnstr(rows - 1, 0, f" {label}: ", cols - 1, curses.A_BOLD)
    scr.clrtoeol()
    scr.timeout(-1)                     # blocking: the polling timeout(200)
    try:                                # would cut getstr short and eat input
        raw = scr.getstr(rows - 1, len(label) + 3, 12).decode().strip()
    finally:
        scr.timeout(200)
        curses.noecho()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
    return raw


def run(interval: float = 1.0, path=None) -> int:
    conn = HostConnection(path or socket_path())
    state = {"interval": interval, "paused": False, "message": ""}
    snap: dict | None = None
    error: str | None = None
    last_poll = 0.0

    def poll(force: bool):
        nonlocal snap, error, last_poll
        now = time.monotonic()
        if not (force or (not state["paused"]
                          and now - last_poll >= state["interval"])):
            return
        last_poll = now
        reply = collect(conn)
        if reply.get("ok"):
            snap, error = reply, None
        else:
            error = (f"host unreachable at {conn.path} — {reply.get('error')} "
                     f"(retrying)")

    def ui(scr):
        try:
            curses.curs_set(0)           # cosmetic; fails on some terminals
        except curses.error:
            pass
        scr.keypad(True)
        scr.timeout(200)
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                curses.init_pair(3, curses.COLOR_RED, -1)
            except curses.error:
                pass
        poll(force=True)
        while True:
            poll(force=False)
            render(scr, snap, error, state)
            key = scr.getch()
            if key in (ord("q"), 27):            # Esc too
                return 0
            if key == ord(" "):
                state["paused"] = not state["paused"]
                state["message"] = ""
            elif key in (ord("+"), ord("=")):
                state["interval"] = min(MAX_INTERVAL, state["interval"] / 2)
                state["message"] = ""
            elif key == ord("-"):
                state["interval"] = max(MIN_INTERVAL, state["interval"] * 2)
                state["message"] = ""
            elif key == ord("k"):
                raw = _prompt(scr, "kick client id")
                if raw.isdigit():
                    reply = conn.request({"cmd": "kick",
                                          "client": int(raw)})
                    state["message"] = ("kicked " + raw
                                        if reply.get("ok")
                                        else f"kick failed: "
                                             f"{reply.get('error')}")
                    poll(force=True)
            elif key == ord("i"):
                raw = _prompt(scr, "keyframe stream #")
                if raw.isdigit():
                    reply = conn.request({"cmd": "idr",
                                          "stream": int(raw)})
                    state["message"] = ("keyframe sent to #" + raw
                                        if reply.get("ok")
                                        else f"keyframe failed: "
                                             f"{reply.get('error')}")
            elif key in (curses.KEY_RESIZE, -1):
                pass                              # repaint next tick

    return curses.wrapper(ui)
