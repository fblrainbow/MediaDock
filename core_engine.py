"""MediaDock DownloadEngine boundary (Stage-002).

Builds the yt-dlp command (Stage-001 policy frozen) and runs it in a
subprocess, reporting parse events to a TaskManager. Never touches HTTP.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional

from core_parse import ProgressEvent, parse_line

FORMAT_EXPR = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]"

_CANDIDATES = [
    r"C:\Tools\yt-dlp\yt-dlp.exe",
    "yt-dlp.exe",
    "yt-dlp",
]


def resolve_ytdlp() -> str:
    for c in _CANDIDATES:
        if os.path.isabs(c):
            if os.path.isfile(c):
                return c
        else:
            for p in os.environ.get("PATH", "").split(os.pathsep):
                full = os.path.join(p.strip('"'), c)
                if os.path.isfile(full):
                    return full
    return _CANDIDATES[0]


def default_download_dir() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "downloads")
    os.makedirs(path, exist_ok=True)
    return path


def build_command(ytdlp: str, download_dir: str, url: str) -> List[str]:
    return [ytdlp, "-f", FORMAT_EXPR, "--merge-output-format", "mp4",
            "--newline", "--no-playlist", "-P", download_dir, url]


@dataclass
class EngineResult:
    task_id: str
    returncode: int
    error_code: str = ""
    error_message: str = ""


class DownloadEngine:
    def __init__(self, manager, ytdlp: Optional[str] = None,
                 download_dir: Optional[str] = None,
                 popen_factory: Optional[Callable] = None,
                 logger: Optional[Callable[..., None]] = None):
        self.manager = manager
        self.ytdlp = ytdlp or resolve_ytdlp()
        self.download_dir = download_dir or default_download_dir()
        self._popen_factory = popen_factory or subprocess.Popen
        self._log = logger or (lambda *a: None)

    def run(self, task_id: str, url: str) -> EngineResult:
        try:
            self.manager.transition(task_id, "downloading")
        except Exception as e:
            return EngineResult(task_id, -1, "start_failed", str(e))
        command = build_command(self.ytdlp, self.download_dir, url)
        self._log(f"Task {task_id} start: {url}")
        self._log(f"yt-dlp: {self.ytdlp}")
        try:
            kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                      "text": True, "encoding": "utf-8", "errors": "replace"}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess,
                                                  "CREATE_NO_WINDOW", 0)
            process = self._popen_factory(command, **kwargs)
            for raw in process.stdout:
                line = (raw or "").strip()
                if not line:
                    continue
                self._log(f"[{task_id}] {line}")
                event = parse_line(line)
                self._apply(task_id, event)
            process.wait()
            if process.returncode == 0:
                self.manager.transition(task_id, "completed", percent=100.0,
                                        speed="", eta="")
                self._log(f"Task {task_id} completed")
                return EngineResult(task_id, 0)
            self.manager.transition(task_id, "error", error_code="exit_code",
                                    error_message=f"returncode={process.returncode}")
            self._log(f"Task {task_id} failed: returncode={process.returncode}")
            return EngineResult(task_id, process.returncode, "exit_code",
                                f"returncode={process.returncode}")
        except FileNotFoundError:
            self.manager.transition(task_id, "error", error_code="ytdlp_missing",
                                    error_message=f"yt-dlp not found at {self.ytdlp}")
            self._log(f"Task {task_id} error: yt-dlp not found at {self.ytdlp}")
            return EngineResult(task_id, -1, "ytdlp_missing",
                                f"yt-dlp not found at {self.ytdlp}")
        except Exception as e:  # noqa: BLE001 - engine must not fake success
            try:
                self.manager.transition(task_id, "error", error_code="engine_error",
                                        error_message=str(e))
            except Exception:
                pass
            self._log(f"Task {task_id} error: {e}")
            return EngineResult(task_id, -1, "engine_error", str(e))

    def _apply(self, task_id: str, event: ProgressEvent) -> None:
        if event.kind == "progress" and event.percent is not None:
            self.manager.report_progress(task_id, event.percent,
                                         event.speed, event.eta)
        elif event.kind == "merging":
            self.manager.report_merging(task_id)
        elif event.kind == "title" and event.title:
            self.manager.report_title(task_id, event.title)
