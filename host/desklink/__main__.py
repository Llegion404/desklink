"""CLI entry point.

Usage:
  python3 -m desklink serve                  # run the host
  python3 -m desklink set-pin [--pin ...]    # set the access PIN
  python3 -m desklink status                 # show addresses
  python3 -m desklink view                   # connect GUI (saved hosts)
  python3 -m desklink view --host IP --pin X # connect directly to one host
  python3 -m desklink.testclient --help      # built-in test client
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import subprocess
import sys

from . import __version__, config as cfgmod


def cmd_serve(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S")
    logging.getLogger("desklink.portal").setLevel(
        logging.DEBUG if args.debug else logging.INFO)
    cfg = cfgmod.load()
    if cfg.pin_key is None:
        print("No PIN configured. Run:  python3 -m desklink set-pin", file=sys.stderr)
        return 3
    if args.no_control:
        cfg.control_socket = False
    from .server import serve
    return asyncio.run(serve(cfg))


def cmd_set_pin(args) -> int:
    if args.pin:
        pin = args.pin
    else:
        pin = getpass.getpass("Set DeskLink PIN: ")
        if pin != getpass.getpass("Confirm PIN: "):
            print("PINs do not match", file=sys.stderr)
            return 1
    if len(pin) < 4:
        print("PIN must be at least 4 characters", file=sys.stderr)
        return 1
    cfg = cfgmod.set_pin(pin)
    print(f"PIN saved to {cfgmod.CONFIG_PATH}")
    return 0


def cmd_status(args) -> int:
    cfg = cfgmod.load()
    print(f"DeskLink v{__version__}")
    print(f"  config   : {cfgmod.CONFIG_PATH}")
    print(f"  pin set  : {'yes' if cfg.pin_key else 'NO (run set-pin)'}")
    print(f"  port     : {cfg.port}")
    print(f"  encoder  : {cfg.encoder} ({cfg.bitrate_kbps} kbps, {cfg.framerate or 'native'} fps)")
    print(f"  downloads: {cfg.downloads_dir}")
    try:
        out = subprocess.run(["ip", "-4", "-j", "addr"], capture_output=True,
                             text=True, timeout=5).stdout
        for iface in json.loads(out):
            name = iface.get("ifname", "?")
            if name == "lo" or name.startswith(("docker", "br-", "veth", "virbr")):
                continue
            for addr in iface.get("addr_info", []):
                ip = addr.get("local")
                if ip:
                    tag = {"tailscale0": " (Tailscale)"}.get(name, "")
                    print(f"  {name:<12} {ip}:{cfg.port}{tag}")
    except Exception as exc:
        print(f"  (could not list addresses: {exc})")
    return 0


def cmd_view(args) -> int:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gst", "1.0")
    except (ImportError, ValueError) as exc:
        print(f"The desktop viewer needs GTK4 + GStreamer introspection: {exc}",
              file=sys.stderr)
        return 3
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S")
    if args.host is None:
        # no --host: open the connect GUI (saved hosts + PIN entry)
        if args.pin:
            parser_warn = ("--pin without --host is ignored; pick a host in "
                           "the window")
            print(f"note: {parser_warn}", file=sys.stderr)
    else:
        # accept host:port in --host as well as separate --port
        if ":" in args.host:
            args.host, _, p = args.host.rpartition(":")
            args.port = int(p)
        if not args.pin:
            args.pin = getpass.getpass(f"PIN for {args.host}: ")
    from .viewer.app import run
    return run(args)


def cmd_tui(args) -> int:
    import os
    from .control import socket_path
    path = args.socket or socket_path()
    if not os.path.exists(path):
        print(f"No control socket at {path}.\n"
              f"Is the host running? Start it with: python3 -m desklink serve",
              file=sys.stderr)
        return 3
    from .tui import run
    return run(interval=args.interval, path=path)


def cmd_doctor(args) -> int:
    from .doctor import run
    return run()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="desklink",
                                     description="DeskLink remote desktop host")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the host server")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--no-control", action="store_true",
                   help="disable the local control socket (desklink tui)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("tui", help="monitor the running host in a terminal")
    p.add_argument("--socket", help="control socket path "
                                    "(default: $XDG_RUNTIME_DIR/desklink.sock)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="refresh seconds (default 1.0)")
    p.set_defaults(func=cmd_tui)

    p = sub.add_parser("set-pin", help="set/replace the access PIN")
    p.add_argument("--pin", help="read PIN from argument instead of prompt")
    p.set_defaults(func=cmd_set_pin)

    p = sub.add_parser("status", help="show config and connection addresses")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="check this machine for everything "
                                      "DeskLink needs (any distro/WM)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("view", help="open the desktop viewer (client)")
    p.add_argument("--host", help="host IP[:port] — omit for the connect GUI "
                                  "with saved hosts")
    p.add_argument("--port", type=int, default=9090)
    p.add_argument("--pin", help="access PIN (prompted if omitted)")
    p.add_argument("--fullscreen", action="store_true")
    p.add_argument("--view-only", action="store_true",
                   help="watch without sending input")
    p.add_argument("--no-auto-fit", action="store_true",
                   help="keep the stream at native resolution (default: fit "
                        "the window, saving decode + bandwidth)")
    p.set_defaults(func=cmd_view)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
