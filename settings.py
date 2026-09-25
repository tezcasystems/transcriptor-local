"""Data locations and persisted user preferences."""

from __future__ import annotations

import ctypes
import json
import sys
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path


def _documents_dir() -> Path:
    if sys.platform == "win32":
        buf = ctypes.create_unicode_buffer(260)
        CSIDL_PERSONAL = 5  # follows OneDrive/folder redirection
        if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, 0, buf) == 0:
            return Path(buf.value)
    return Path.home() / "Documents"


MEETINGS_DIR = _documents_dir() / "Reuniones"
CONFIG_PATH = MEETINGS_DIR / "config.json"
RETENTION_CHOICES = [("7 días", 7), ("30 días", 30), ("90 días", 90), ("Nunca borrar", 0)]


@dataclass(frozen=True)
class UserConfig:
    model_size: str = "small"
    language: str = "es-MX"
    retention_days: int = 30  # 0 = keep audio forever
    last_mode: str = "web"
    last_mic: str = ""


_lock = threading.Lock()


_ALLOWED = {
    "model_size": {"base", "small", "medium"},
    "retention_days": {days for _, days in RETENTION_CHOICES},
    "last_mode": {"web", "presencial"},
}


def load_config() -> UserConfig:
    """Invalid or unknown values (a hand-edited file, an older version) fall back
    to defaults instead of breaking the UI at startup."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UserConfig()
    if not isinstance(data, dict):
        return UserConfig()
    defaults = UserConfig()
    clean = {}
    for f in fields(UserConfig):
        value = data.get(f.name, getattr(defaults, f.name))
        allowed = _ALLOWED.get(f.name)
        valid_type = isinstance(value, type(getattr(defaults, f.name)))
        clean[f.name] = value if valid_type and (allowed is None or value in allowed) else getattr(defaults, f.name)
    return UserConfig(**clean)


def update_config(**changes) -> UserConfig:
    with _lock:
        config = replace(load_config(), **changes)
        MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        return config
