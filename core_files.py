"""MediaDock download file-safety boundary (Stage-004).

Path boundary checks plus the keep/delete policy for yt-dlp artifacts:

    pause / resume / retry -> never delete anything (breakpoint data stays)
    cancel                 -> delete this run's temp and output files only

Nothing here writes Task fields or parses HTTP. stdlib only.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Callable, Iterable, List, Optional, Set

# yt-dlp leaves these behind while a download is incomplete.
TEMP_SUFFIXES = (".part", ".ytdl", ".temp")

# yt-dlp default output template is "<title> [<id>].<ext>", so every file
# belonging to one video carries its id in brackets.
_ID_BRACKET = "[{vid}]"

_WATCH_ID = re.compile(r"[?&]v=([A-Za-z0-9_-]{6,})")
_SHORTS_ID = re.compile(r"/shorts/([A-Za-z0-9_-]{6,})")
_YOUTU_BE = re.compile(r"youtu\.be/([A-Za-z0-9_-]{6,})")


def video_id_from_url(url: str) -> str:
    """Best-effort YouTube video id; empty string when it cannot be known."""
    text = str(url or "")
    for pattern in (_WATCH_ID, _SHORTS_ID, _YOUTU_BE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def is_inside(root: str, path: str) -> bool:
    """True only when `path` really lives inside `root` (no `..` escapes)."""
    if not root or not path:
        return False
    try:
        root_real = os.path.normcase(os.path.realpath(root))
        path_real = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return False
    if root_real == path_real:
        return True
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:
        return False


def started_epoch(iso_text: Any) -> float:
    """Epoch seconds for a Task `started_at`; 0.0 when unknown/invalid."""
    if not iso_text:
        return 0.0
    if isinstance(iso_text, (int, float)):
        try:
            return float(iso_text)
        except (TypeError, ValueError):
            return 0.0
    try:
        return datetime.fromisoformat(str(iso_text)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def is_temp_file(path: str) -> bool:
    name = os.path.basename(str(path or "")).lower()
    return name.endswith(TEMP_SUFFIXES) or ".part-frag" in name


def _list_dir(download_dir: str) -> List[str]:
    try:
        with os.scandir(download_dir) as entries:
            return [entry.path for entry in entries if entry.is_file()]
    except OSError:
        return []


def discover_task_files(download_dir: str, url: str,
                        artifacts: Iterable[str] = (),
                        since_epoch: float = 0.0) -> List[str]:
    """Files this run may delete: inside `download_dir`, same video, fresh.

    `artifacts` are paths recorded from live yt-dlp output; directory
    scanning is the fallback for temp files whose names yt-dlp never printed.
    """
    vid = video_id_from_url(url)
    seen: Set[str] = set()
    found: List[str] = []

    candidates: List[str] = []
    for raw in artifacts or ():
        if raw:
            candidates.append(str(raw))
    if vid:
        marker = _ID_BRACKET.format(vid=vid)
        for path in _list_dir(download_dir):
            if marker in os.path.basename(path):
                candidates.append(path)

    for path in candidates:
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            continue
        key = os.path.normcase(real)
        if key in seen:
            continue
        if not is_inside(download_dir, real):
            continue
        if not os.path.isfile(real):
            continue
        if since_epoch:
            try:
                if os.path.getmtime(real) + 0.001 < since_epoch:
                    continue
            except OSError:
                continue
        seen.add(key)
        found.append(real)
    return found


def cleanup_task_files(download_dir: str, url: str,
                       artifacts: Iterable[str] = (),
                       since_epoch: float = 0.0,
                       logger: Optional[Callable[..., None]] = None) -> List[str]:
    """Delete this run's temp/output files; returns the paths actually removed.

    `since_epoch` is mandatory in practice: with no `started_at` window we
    cannot tell this run's files apart from an older download of the same
    video, so nothing is deleted.
    """
    log = logger or (lambda *a: None)
    try:
        window = float(since_epoch or 0.0)
    except (TypeError, ValueError):
        window = 0.0
    if window <= 0.0:
        log("cleanup skipped: no started_at window")
        return []
    deleted: List[str] = []
    for path in discover_task_files(download_dir, url, artifacts, window):
        try:
            os.remove(path)
            deleted.append(path)
            log(f"cleanup removed {path}")
        except OSError as exc:
            log(f"cleanup failed for {path}: {exc}")
    return deleted
