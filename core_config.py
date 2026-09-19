"""MediaDock configuration layer (Stage-006).

Single source of truth for every value that used to be hard-coded in
`server.py`: host, port, download dir, yt-dlp/FFmpeg paths, log level, log
file, database path, concurrency limit and request/log limits.

Precedence (Stage-006.md 5.1):

    explicit path > environment variables > config.json > built-in defaults

An invalid value never blocks startup: it is reported in `Config.errors`
and the built-in default is used instead. The result is exposed through
`/health.config` and `python server.py --check-config`.

stdlib only (`json`, `os`, `dataclasses`).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from core_security import sanitize_path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_ENV_VAR = "MEDIADOCK_CONFIG"
CONFIG_FILE_NAME = "config.json"

LOG_LEVELS = ("debug", "info", "warning", "error")
# Only these names are accepted as a bind address without `allow_lan`.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

DEFAULTS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8765,
    "allow_lan": False,
    "download_dir": os.path.join(BASE_DIR, "downloads"),
    "ytdlp_path": "",
    "ffmpeg_path": "",
    "log_level": "info",
    "log_file": os.path.join(BASE_DIR, "MediaDock-server.log"),
    "db_path": "",
    "max_active_tasks": 3,
    "purge_keep": 200,
    "request_max_bytes": 65536,
    "log_line_max": 4000,
}

# Environment variable -> config key. `MEDIADOCK_DB` is deliberately absent:
# the database path keeps its Stage-005 contract in `core_store.resolve_db_path`.
ENV_KEYS: Dict[str, str] = {
    "MEDIADOCK_HOST": "host",
    "MEDIADOCK_PORT": "port",
    "MEDIADOCK_ALLOW_LAN": "allow_lan",
    "MEDIADOCK_DOWNLOAD_DIR": "download_dir",
    "MEDIADOCK_YTDLP": "ytdlp_path",
    "MEDIADOCK_FFMPEG": "ffmpeg_path",
    "MEDIADOCK_LOG_LEVEL": "log_level",
    "MEDIADOCK_LOG_FILE": "log_file",
    "MEDIADOCK_MAX_ACTIVE": "max_active_tasks",
}

_STR_KEYS = ("host", "ytdlp_path", "ffmpeg_path", "db_path")
_PATH_KEYS = ("download_dir", "log_file", "ytdlp_path", "ffmpeg_path")
_INT_RANGES: Dict[str, Tuple[int, int]] = {
    "port": (1, 65535),
    "max_active_tasks": (1, 8),
    "purge_keep": (1, 10000),
    "request_max_bytes": (1024, 1048576),
    "log_line_max": (200, 20000),
}


def resolve_path(value: str, base: str = BASE_DIR) -> str:
    """Absolute, expanded path; relative values are repo-relative."""
    text = os.path.expanduser(str(value))
    if not text:
        return text
    if not os.path.isabs(text):
        text = os.path.join(base, text)
    return os.path.normpath(text)


def _coerce_bool(raw: Any) -> Optional[bool]:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    if isinstance(raw, int):
        return bool(raw)
    return None


def _coerce_int(raw: Any) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        try:
            return int(text, 10)
        except ValueError:
            return None
    return None


def coerce_value(key: str, raw: Any) -> Tuple[Any, Optional[str]]:
    """Return `(value, error_or_None)`; never raises on bad input."""
    if key == "allow_lan":
        value = _coerce_bool(raw)
        if value is None:
            return None, f"{key} must be a boolean"
        return value, None
    if key in _INT_RANGES:
        value = _coerce_int(raw)
        if value is None:
            return None, f"{key} must be an integer"
        low, high = _INT_RANGES[key]
        if not low <= value <= high:
            return None, f"{key} must be between {low} and {high}"
        return value, None
    if key == "log_level":
        text = str(raw).strip().lower()
        if text not in LOG_LEVELS:
            return None, ("log_level must be one of " + ", ".join(LOG_LEVELS))
        return text, None
    # remaining keys are plain strings
    if not isinstance(raw, str):
        return None, f"{key} must be a string"
    text = raw.strip()
    if key == "host" and not text:
        return None, "host must be a non-empty string"
    if key in _PATH_KEYS:
        text = resolve_path(text)
    return text, None


@dataclass(frozen=True)
class Config:
    """Validated configuration snapshot; `errors`/`warnings` are diagnostics."""

    host: str = DEFAULTS["host"]
    port: int = DEFAULTS["port"]
    allow_lan: bool = DEFAULTS["allow_lan"]
    download_dir: str = DEFAULTS["download_dir"]
    ytdlp_path: str = DEFAULTS["ytdlp_path"]
    ffmpeg_path: str = DEFAULTS["ffmpeg_path"]
    log_level: str = DEFAULTS["log_level"]
    log_file: str = DEFAULTS["log_file"]
    db_path: str = DEFAULTS["db_path"]
    max_active_tasks: int = DEFAULTS["max_active_tasks"]
    purge_keep: int = DEFAULTS["purge_keep"]
    request_max_bytes: int = DEFAULTS["request_max_bytes"]
    log_line_max: int = DEFAULTS["log_line_max"]
    source: str = "defaults"
    path: str = ""
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    def value(self, key: str) -> Any:
        return getattr(self, key)

    def values(self) -> Dict[str, Any]:
        return {key: getattr(self, key) for key in DEFAULTS}

    def ok(self) -> bool:
        return not self.errors


def _read_file(path: str, errors: List[str]) -> Optional[Dict[str, Any]]:
    """Parse `config.json`; parse problems become diagnostics, not exceptions."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        errors.append(f"cannot read config file {path}: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"config file {path} must contain a JSON object")
        return None
    return data


def load_config(path: Optional[str] = None, env: Optional[Dict[str, str]] = None,
                logger: Optional[Callable[..., None]] = None) -> Config:
    """Build a validated `Config` without ever raising (Stage-006.md 5.1)."""
    log = logger or (lambda *a: None)
    env_map: Dict[str, str] = dict(os.environ) if env is None else dict(env)

    explicit = bool(path)
    env_path = env_map.get(CONFIG_ENV_VAR) or ""
    cfg_path = path or env_path or os.path.join(BASE_DIR, CONFIG_FILE_NAME)

    raw: Dict[str, Any] = dict(DEFAULTS)
    source = "defaults"
    errors: List[str] = []
    warnings: List[str] = []
    used_path = ""

    if os.path.isfile(cfg_path):
        data = _read_file(cfg_path, errors)
        used_path = cfg_path
        if data is not None:
            source = "file"
            for key in sorted(data):
                if key not in DEFAULTS:
                    errors.append(f"unknown_config_key: {key}")
                    continue
                value, error = coerce_value(key, data[key])
                if error:
                    errors.append(f"{error} (in config file)")
                else:
                    raw[key] = value
    elif explicit or env_path:
        errors.append(f"config file not found: {cfg_path}")

    for env_name in sorted(ENV_KEYS):
        if env_name not in env_map or env_map[env_name] == "":
            continue
        key = ENV_KEYS[env_name]
        value, error = coerce_value(key, env_map[env_name])
        if error:
            errors.append(f"{error} (from {env_name})")
        else:
            raw[key] = value
            source = "env"

    host = str(raw.get("host") or "")
    if host.lower() not in LOOPBACK_HOSTS:
        if raw.get("allow_lan"):
            warnings.append(
                f"host {host} is not loopback and allow_lan=true: "
                "the API is reachable from the local network")
        else:
            errors.append(
                f"host {host} is not a loopback address; "
                "set allow_lan=true to accept it (falling back to 127.0.0.1)")
            raw["host"] = DEFAULTS["host"]

    config = Config(source=source, path=used_path, errors=tuple(errors),
                    warnings=tuple(warnings), **raw)
    for message in config.errors:
        log(f"config error: {message}")
    for message in config.warnings:
        log(f"config warning: {message}")
    return config


def config_public(config: Config) -> Dict[str, Any]:
    """`/health` payload; paths are shortened (`~`) before leaving the process."""
    values: Dict[str, Any] = {}
    for key, value in config.values().items():
        values[key] = sanitize_path(value) if isinstance(value, str) else value
    return {
        "source": config.source,
        "path": sanitize_path(config.path) if config.path else "",
        "ok": config.ok(),
        "errors": list(config.errors),
        "warnings": list(config.warnings),
        "values": values,
    }


