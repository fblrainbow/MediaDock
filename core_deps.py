"""Dependency and environment diagnostics (Stage-006).

Startup checks that must never block the server:

    yt-dlp      -> configured path, else PATH candidates
    FFmpeg      -> configured path, else PATH; missing is a warning (merge only)
    download dir -> created when missing, then a write probe
    disk space  -> free space against a threshold

`run_checks(cfg, deep=False)` returns a JSON-ready snapshot for
`/health.dependencies` and `python server.py --check-config`. Version probing
spawns a process, so it is opt-in (`deep=True` / `--probe`).

stdlib only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core_config import Config
from core_engine import resolve_ffmpeg, resolve_ytdlp

OK = "ok"
MISSING = "missing"
WARN = "warn"
ERROR = "error"

# Statuses that keep `/health.dependencies.ok` true: only hard errors fail.
HEALTHY = (OK, WARN)

MIN_FREE_MB = 500
VERSION_TIMEOUT = 10.0


def _check(name: str, status: str, path: str = "", message: str = ""
           ) -> Dict[str, Any]:
    return {"name": name, "status": status, "path": path, "message": message}


def check_executable(name: str, configured: str, resolver: Callable[[], str],
                     missing_message: str) -> Dict[str, Any]:
    """Configured path wins; a directory is accepted (e.g. FFmpeg bin dir)."""
    value = str(configured or "")
    if value:
        if os.path.isdir(value):
            return _check(name, OK, value, "directory configured")
        if os.path.isfile(value):
            return _check(name, OK, value, "configured path")
        found = resolver()
        if found and os.path.isfile(found):
            return _check(name, ERROR, found,
                          f"configured path does not exist ({value}); "
                          "using the detected executable instead")
        return _check(name, ERROR, value,
                      f"configured path does not exist ({value})")
    found = resolver() or ""
    if found and os.path.isfile(found):
        return _check(name, OK, found, "detected")
    return _check(name, MISSING, "", missing_message)


def check_ytdlp(config: Config) -> Dict[str, Any]:
    return check_executable("yt-dlp", config.ytdlp_path, resolve_ytdlp,
                            "yt-dlp was not found; downloads cannot start")


def check_ffmpeg(config: Config) -> Dict[str, Any]:
    result = check_executable("ffmpeg", config.ffmpeg_path, resolve_ffmpeg,
                              "FFmpeg was not found; merged MP4 output "
                              "may fail")
    if result["status"] == MISSING:
        result["status"] = WARN
    return result


def check_download_dir(config: Config,
                       logger: Optional[Callable[..., None]] = None
                       ) -> Dict[str, Any]:
    """Create the directory when missing and prove it is writable."""
    log = logger or (lambda *a: None)
    path = str(config.download_dir or "")
    if not path:
        return _check("download_dir", ERROR, "", "download_dir is empty")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        log(f"download_dir unavailable ({path}): {exc}")
        return _check("download_dir", ERROR, path, f"cannot create: {exc}")
    if not os.path.isdir(path):
        return _check("download_dir", ERROR, path, "not a directory")
    probe = None
    try:
        probe = tempfile.NamedTemporaryFile(prefix=".mediadock-probe-",
                                            dir=path, delete=True)
        probe.write(b"ok")
        probe.flush()
    except OSError as exc:
        log(f"download_dir not writable ({path}): {exc}")
        return _check("download_dir", ERROR, path, f"not writable: {exc}")
    finally:
        if probe is not None:
            try:
                probe.close()
            except OSError:
                pass
    return _check("download_dir", OK, path, "writable")


def check_disk_space(path: str, min_free_mb: int = MIN_FREE_MB
                     ) -> Dict[str, Any]:
    """Free space of the volume holding `path`; low space is a warning."""
    target = str(path or "")
    if not target or not os.path.isdir(target):
        return _check("disk_space", ERROR, target, "path does not exist")
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return _check("disk_space", ERROR, target, f"cannot read usage: {exc}")
    free_mb = int(usage.free // (1024 * 1024))
    message = f"{free_mb} MiB free (threshold {min_free_mb} MiB)"
    if free_mb < int(min_free_mb):
        return _check("disk_space", WARN, target, message)
    return _check("disk_space", OK, target, message)


def probe_version(path: str, name: str = "version",
                  timeout: float = VERSION_TIMEOUT) -> Dict[str, Any]:
    """Run `<path> --version`; only used for `--check-config --probe`."""
    target = str(path or "")
    if not target or os.path.isdir(target) or not os.path.isfile(target):
        return _check(name, WARN, target, "version probe skipped")
    try:
        completed = subprocess.run(
            [target, "--version"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, check=False,
            text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return _check(name, ERROR, target, f"version probe failed: {exc}")
    first_line = (completed.stdout or "").strip().splitlines()
    version = first_line[0] if first_line else ""
    if completed.returncode != 0:
        return _check(name, ERROR, target,
                      f"--version returned {completed.returncode}")
    return _check(name, OK, target, version or "version unknown")


def run_checks(config: Config,
               logger: Optional[Callable[..., None]] = None,
               deep: bool = False,
               min_free_mb: int = MIN_FREE_MB) -> Dict[str, Any]:
    """Snapshot for `/health.dependencies`; never raises, never blocks."""
    checks: List[Dict[str, Any]] = [
        check_ytdlp(config),
        check_ffmpeg(config),
        check_download_dir(config, logger),
        check_disk_space(config.download_dir, min_free_mb),
    ]
    if deep:
        for entry in list(checks[:2]):
            if entry["status"] == OK and entry["path"]:
                checks.append(probe_version(entry["path"], entry["name"]))
    return {
        "ok": all(entry["status"] in HEALTHY for entry in checks),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
    }


def failed_checks(snapshot: Dict[str, Any]) -> List[str]:
    """Names of checks that are not `ok`/`warn` (for logs and CLI output)."""
    return [entry["name"] for entry in snapshot.get("checks", [])
            if entry.get("status") not in HEALTHY]

