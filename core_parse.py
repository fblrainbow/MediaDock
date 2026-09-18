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
MERGE_RE = re.compile(r"\[Merger\]|Merging formats", re.IGNORECASE)
TITLE_RE = re.compile(r"\[info\]\s+(.+?):\s+Downloading")
DEST_RE = re.compile(r"\[download\]\s+Destination:\s*(.+?)\s*$")
ALREADY_RE = re.compile(r"\[download\]\s+(.+?)\s+has already been downloaded")
MERGE_PATH_RE = re.compile(r"\[Merger\]\s+Merging formats into\s+\"(.+?)\"")


@dataclass(frozen=True)
class ProgressEvent:
    kind: str  # progress | merging | merged | destination | title | ignored
    percent: Optional[float] = None
    speed: str = ""
    eta: str = ""
    title: str = ""
    path: str = ""


def parse_line(line: str) -> ProgressEvent:
    text = (line or "").strip()
    if not text:
        return ProgressEvent(kind="ignored")
    merged = MERGE_PATH_RE.search(text)
    if merged:
        return ProgressEvent(kind="merged", path=merged.group(1).strip())
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
        return ProgressEvent(kind="progress", percent=pct,
                             speed=m.group(2).strip(),
                             eta=m.group(3).strip())
    if text.startswith("[info]"):
        t = TITLE_RE.search(text)
        if t:
            return ProgressEvent(kind="title", title=t.group(1).strip())
    return ProgressEvent(kind="ignored")

