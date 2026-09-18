"""MediaDock progress/title parsing boundary (Stage-002).

Pure functions: yt-dlp text lines in, events out. No tasks, no HTTP.
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


@dataclass(frozen=True)
class ProgressEvent:
    kind: str  # "progress" | "merging" | "title" | "ignored"
    percent: Optional[float] = None
    speed: str = ""
    eta: str = ""
    title: str = ""


def parse_line(line: str) -> ProgressEvent:
    text = (line or "").strip()
    if not text:
        return ProgressEvent(kind="ignored")
    if MERGE_RE.search(text):
        return ProgressEvent(kind="merging")
    if "[download] Destination:" in text:
        return ProgressEvent(kind="ignored")
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
