"""Configuration: ~/.config/desklink/config.toml (flat TOML, no external deps)."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("DESKLINK_CONFIG_DIR", "~/.config/desklink")).expanduser()
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULTS = {
    "port": 9090,
    # hex of sha256(pin) — set via `desklink set-pin`; never store the raw PIN
    "pin_key_hex": "",
    "bitrate_kbps": 8000,
    "framerate": 30,          # 0 = native (monitor refresh)
    "encoder": "auto",        # auto | nvenc | x264
    "gop_seconds": 2,         # keyframe interval
    "downloads_dir": "~/Downloads/desklink",
    "allow_clipboard": True,
    "allowed_fs_roots": "~",  # comma-separated list of roots the client may browse
    "host_name": "",          # shown to clients; empty = hostname
    # restore token for the portal consent dialog ("remember" support)
    "restore_token": "",
}


@dataclass
class Config:
    port: int = DEFAULTS["port"]
    pin_key_hex: str = ""
    bitrate_kbps: int = 8000
    framerate: int = 30
    encoder: str = "auto"
    gop_seconds: int = 2
    downloads_dir: str = "~/Downloads/desklink"
    allow_clipboard: bool = True
    allowed_fs_roots: str = "~"
    host_name: str = ""
    restore_token: str = ""

    # ---------------------------------------------------------------- helpers
    @property
    def pin_key(self) -> bytes | None:
        return bytes.fromhex(self.pin_key_hex) if self.pin_key_hex else None

    @property
    def name(self) -> str:
        import socket
        return self.host_name or socket.gethostname()

    @property
    def downloads_path(self) -> Path:
        p = Path(self.downloads_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def fs_roots(self) -> list[Path]:
        return [Path(r.strip()).expanduser() for r in self.allowed_fs_roots.split(",") if r.strip()]

    def to_dict(self) -> dict:
        return asdict(self)


def _serialize(cfg: Config) -> str:
    """Flat TOML writer — schema is simple scalars only, avoids a tomli_w dep."""
    out = ["# DeskLink host configuration (managed by `desklink set-pin`/edit by hand)", ""]
    for f in fields(Config):
        if f.name == "restore_token":
            continue  # written only by the server at runtime, keep out of user noise
        v = getattr(cfg, f.name)
        if isinstance(v, bool):
            out.append(f"{f.name} = {'true' if v else 'false'}")
        elif isinstance(v, int):
            out.append(f"{f.name} = {v}")
        else:
            esc = str(v).replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'{f.name} = "{esc}"')
    return "\n".join(out) + "\n"


def load() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        import tomllib
        with open(CONFIG_PATH, "rb") as fh:
            data = tomllib.load(fh)
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    return cfg


def save(cfg: Config, include_restore_token: bool = False) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    text = _serialize(cfg)
    if include_restore_token and cfg.restore_token:
        esc = cfg.restore_token.replace('"', '\\"')
        text += f'\n# portal restore token (auto-managed)\nrestore_token = "{esc}"\n'
    tmp = CONFIG_PATH.parent / ".config.toml.tmp"
    tmp.write_text(text)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def pin_to_key(pin: str) -> str:
    """HMAC key derived from the PIN. Clients compute the same: sha256(pin)."""
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def set_pin(pin: str) -> Config:
    cfg = load()
    cfg.pin_key_hex = pin_to_key(pin)
    save(cfg)
    return cfg
