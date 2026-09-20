"""MediaDock progress/title parsing boundary (Stage-002, extended Stage-004).

Pure functions: yt-dlp text lines in, events out. No tasks, no HTTP.

Stage-004 adds path events so the engine can record which files the current
run produced; `cancel` uses them for precise cleanup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

PROGRESS_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+.*?at\s+(.+?)\s+ETA\s+(.+)"
)
# Stage-013: yt-dlp prints the total size as `of ~ 50.00MiB`. Kept as its own
# pattern so PROGRESS_RE's capture groups (asserted by tests) stay unchanged.
SIZE_RE = re.compile(
    r"\[download\]\s+\d+(?:\.\d+)?%\s+of\s+~?\s*([\d.]+\s*[KMGT]?i?B)",
    re.IGNORECASE,
)
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGT]?)(i?)B\s*$", re.IGNORECASE)
MERGE_RE = re.compile(r"\[Merger\]|Merging formats", re.IGNORECASE)
TITLE_RE = re.compile(r"\[info\]\s+(.+?):\s+Downloading")
DEST_RE = re.compile(r"\[download\]\s+Destination:\s*(.+?)\s*$")
ALREADY_RE = re.compile(r"\[download\]\s+(.+?)\s+has already been downloaded")
MERGE_PATH_RE = re.compile(r"\[Merger\]\s+Merging formats into\s+\"(.+?)\"")
# Stage-012: yt-dlp's audio extraction (`-x`) reports the final file this way.
EXTRACT_PATH_RE = re.compile(r"\[ExtractAudio\]\s+Destination:\s*(.+?)\s*$")


@dataclass(frozen=True)
class ProgressEvent:
    kind: str  # progress | merging | merged | destination | title | ignored
    percent: Optional[float] = None
    speed: str = ""
    eta: str = ""
    title: str = ""
    path: str = ""
    size: str = ""  # Stage-013: total size token, e.g. "50.00MiB"


def parse_size(text: str) -> int:
    """`"1.5MiB"` -> bytes; `0` when the token cannot be understood.

    yt-dlp uses binary units (`MiB`); plain `MB` is treated as decimal.
    """
    match = _SIZE_RE.match(str(text or ""))
    if not match:
        return 0
    try:
        value = float(match.group(1))
    except ValueError:
        return 0
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}.get(
        match.group(2).upper(), 0)
    base = 1024 if match.group(3) else 1000
    return int(value * (base ** power))


def parse_line(line: str) -> ProgressEvent:
    text = (line or "").strip()
    if not text:
        return ProgressEvent(kind="ignored")
    merged = MERGE_PATH_RE.search(text)
    if merged:
        return ProgressEvent(kind="merged", path=merged.group(1).strip())
    # 提取音频与合并同义：都是「本次运行的最终产物路径」
    extracted = EXTRACT_PATH_RE.search(text)
    if extracted:
        return ProgressEvent(kind="merged", path=extracted.group(1).strip())
    if MERGE_RE.search(text):
        return ProgressEvent(kind="merging")
    already = ALREADY_RE.search(text)
    if already:
        return ProgressEvent(kind="destination",
                             path=already.group(1).strip())
    dest = DEST_RE.search(text)
    if dest:
        return ProgressEvent(kind="destination", path=dest.group(1).strip())
    m = PROGRESS_RE.search(text)
    if m:
        try:
            pct = float(m.group(1))
        except ValueError:
            return ProgressEvent(kind="ignored")
        sized = SIZE_RE.search(text)
        return ProgressEvent(kind="progress", percent=pct,
                             speed=m.group(2).strip(),
                             eta=m.group(3).strip(),
                             size=sized.group(1).strip() if sized else "")
    if text.startswith("[info]"):
        t = TITLE_RE.search(text)
        if t:
            return ProgressEvent(kind="title", title=t.group(1).strip())
    return ProgressEvent(kind="ignored")

