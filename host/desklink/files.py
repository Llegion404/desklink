"""Sandboxed file browsing + transfer.

Clients may only touch paths under the configured roots (default: $HOME).
Uploads land in cfg.downloads_dir. Downloads stream in 64 KiB chunks.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("desklink.files")

CHUNK = 64 * 1024
MAX_LIST = 2000
MAX_DOWNLOAD = 8 * 1024 * 1024 * 1024  # 8 GiB


class SandboxViolation(Exception):
    pass


class FileManager:
    def __init__(self, roots: list[Path], downloads_dir: Path):
        self.roots = [r.resolve() for r in roots]
        self.downloads_dir = downloads_dir

    def resolve(self, user_path: str) -> Path:
        expanded = Path(user_path).expanduser()
        p = expanded if expanded.is_absolute() else Path.home() / expanded
        p = p.resolve()
        if not any(p == r or r in p.parents for r in self.roots):
            raise SandboxViolation(f"{user_path} is outside the allowed roots")
        return p

    def list_dir(self, user_path: str) -> dict:
        try:
            base = self.resolve(user_path)
            entries = []
            with os.scandir(base) as it:
                for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower()))[:MAX_LIST]:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            entries.append({"n": entry.name, "d": True, "s": 0})
                        else:
                            entries.append({
                                "n": entry.name, "d": False,
                                "s": entry.stat(follow_symlinks=False).st_size})
                    except OSError:
                        continue
            hidden = [e for e in entries if e["n"].startswith(".")]
            normal = [e for e in entries if not e["n"].startswith(".")]
            return {"path": str(base),
                    "entries": normal + hidden[:200],
                    "error": None}
        except SandboxViolation as exc:
            return {"path": user_path, "entries": [], "error": str(exc)}
        except OSError as exc:
            return {"path": user_path, "entries": [], "error": str(exc)}

    def open_read(self, user_path: str):
        p = self.resolve(user_path)
        st = p.stat()
        if not p.is_file():
            raise SandboxViolation("not a regular file")
        if st.st_size > MAX_DOWNLOAD:
            raise SandboxViolation("file too large")
        return p, st.st_size

    def open_write(self, name: str) -> Path:
        safe = os.path.basename(name.replace("\\", "/")) or "upload.bin"
        target = self.downloads_dir / safe
        stem, ext = os.path.splitext(target.name)
        i = 1
        while target.exists():
            target = self.downloads_dir / f"{stem} ({i}){ext}"
            i += 1
        return target
