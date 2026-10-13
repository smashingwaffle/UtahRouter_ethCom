"""Persistent settings (JSON) for Utah eth2RS422."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def default_config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "UtahEth2RS422"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "utah-eth2rs422"


@dataclass
class Config:
    # Serial side (SC-40)
    serial_port: str = "COM5"
    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"          # N, E, O
    stopbits: float = 1        # 1, 1.5, 2

    # Network side (Cerebrum connects here)
    listen_host: str = "0.0.0.0"
    listen_port: int = 5101

    # Behaviour
    frame_mode: bool = True    # forward complete RCP-1 reports only (fixes Cerebrum drops)
    frame_idle_ms: int = 40    # flush partial data after this quiet gap
    frame_max_ms: int = 300    # never hold data longer than this
    one_based: bool = True     # show numbers 1-based in the log (matches Cerebrum)
    log_detail: str = "normal"  # "normal" = takes/unsolicited/events, "all" = every poll
    log_to_file: bool = True
    log_dir: str = ""          # empty = <config dir>/logs
    log_keep_days: int = 14
    autostart: bool = True     # start the bridge when the app launches

    # Web UI
    ui_host: str = "127.0.0.1"
    ui_port: int = 8422

    def resolved_log_dir(self) -> Path:
        return Path(self.log_dir) if self.log_dir else default_config_dir() / "logs"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        known = {f.name: f for f in fields(cls)}
        kwargs = {}
        for k, v in d.items():
            if k not in known:
                continue
            default = getattr(cls(), k)
            try:
                if isinstance(default, bool):
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                elif isinstance(default, int) and not isinstance(default, bool):
                    v = int(v)
                elif isinstance(default, float):
                    v = float(v)
                else:
                    v = str(v)
            except (TypeError, ValueError):
                continue
            kwargs[k] = v
        cfg = cls(**kwargs)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.parity not in ("N", "E", "O"):
            raise ValueError("parity must be N, E or O")
        if self.bytesize not in (5, 6, 7, 8):
            raise ValueError("data bits must be 5-8")
        if self.stopbits not in (1, 1.5, 2):
            raise ValueError("stop bits must be 1, 1.5 or 2")
        if not (1 <= self.listen_port <= 65535) or not (1 <= self.ui_port <= 65535):
            raise ValueError("ports must be 1-65535")
        if self.listen_port == self.ui_port:
            raise ValueError("bridge port and web UI port must differ")
        if self.log_detail not in ("normal", "all"):
            raise ValueError("log detail must be 'normal' or 'all'")


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_config_dir() / "config.json"

    def load(self) -> Config:
        try:
            with open(self.path, encoding="utf-8") as f:
                return Config.from_dict(json.load(f))
        except FileNotFoundError:
            return Config()
        except (ValueError, OSError):
            return Config()

    def save(self, cfg: Config) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg.to_dict(), f, indent=2)
        os.replace(tmp, self.path)
