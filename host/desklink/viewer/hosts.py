"""Saved hosts for the desktop viewer launcher.

~/.config/desklink/hosts.json, file mode 0600. The PIN is stored ONLY when
the user ticks "save PIN" in the launcher, and it sits in PLAINTEXT in that
file — anyone who can read your home directory can read it (same threat
model as sshpass-style workflows). Documented in the README.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..config import CONFIG_DIR

HOSTS_PATH = CONFIG_DIR / "hosts.json"
DEFAULT_PORT = 9090
MAX_HOSTS = 50


def parse_address(address: str) -> tuple[str, int] | None:
    """`IP[:port]` / `hostname[:port]` -> (host, port); None if invalid.
    IPv4/hostnames only (no [v6] bracket syntax)."""
    address = (address or "").strip()
    if not address:
        return None
    if ":" in address:
        host, _, port_s = address.rpartition(":")
        if not host or not port_s.isdigit():
            return None
        port = int(port_s)
    else:
        host, port = address, DEFAULT_PORT
    host = host.strip()
    if not host or not (1 <= port <= 65535):
        return None
    return host, port


class HostStore:
    def __init__(self, path: Path = HOSTS_PATH):
        self.path = Path(path)
        self._hosts: list[dict] = []

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._hosts = [h for h in data.get("hosts", [])
                           if parse_address(h.get("address", ""))]
        except FileNotFoundError:
            self._hosts = []
        except Exception:
            # a corrupt file must not brick the launcher
            self._hosts = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"hosts": self._hosts}, indent=2) + "\n",
            encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # --------------------------------------------------------------- entries
    def all(self) -> list[dict]:
        """Most recently used first."""
        return sorted(self._hosts, key=lambda h: -h.get("last", 0.0))

    def get(self, host: str, port: int) -> dict | None:
        return next((h for h in self._hosts
                     if h["host"] == host and h["port"] == port), None)

    def upsert(self, address: str, name: str = "", pin: str | None = None,
               save_pin: bool = False) -> tuple[str, int]:
        """Add or update; returns (host, port). `pin is None` leaves any
        stored PIN alone; an explicitly provided PIN is stored only when
        save_pin is set (typing a PIN with the box unticked forgets the old
        one — that's the feature, not a bug)."""
        parsed = parse_address(address)
        if parsed is None:
            raise ValueError(f"invalid address: {address!r}")
        host, port = parsed
        entry = self.get(host, port)
        if entry is None:
            if len(self._hosts) >= MAX_HOSTS:
                oldest = min(self._hosts, key=lambda h: h.get("last", 0.0))
                self._hosts.remove(oldest)
            entry = {"address": f"{host}:{port}", "host": host, "port": port,
                     "name": "", "pin": None, "last": 0.0}
            self._hosts.append(entry)
        if name.strip():
            entry["name"] = name.strip()
        if pin is not None:
            entry["pin"] = pin if save_pin else None
        entry["last"] = time.time()
        return host, port

    def remove(self, host: str, port: int) -> bool:
        entry = self.get(host, port)
        if entry is None:
            return False
        self._hosts.remove(entry)
        return True
