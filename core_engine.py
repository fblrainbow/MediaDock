"""MediaDock DownloadEngine boundary (Stage-002, extended Stage-004).

Builds the yt-dlp command (Stage-001 policy frozen) and runs it in a
subprocess, reporting parse events to a TaskManager. Never touches HTTP.

Stage-004: the engine receives the TaskControl for its task, attaches the
process handle, records artifact paths and turns a pause/cancel intent into
`paused`/`cancelled` plus the matching file policy.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional

from core_control import TaskControl, terminate_tree
from core_files import cleanup_task_files, started_epoch
from core_parse import ProgressEvent, parse_line

FORMAT_EXPR = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]"

_CANDIDATES = [
    r"C:\Tools\yt-dlp\yt-dlp.exe",
    "yt-dlp.exe",
    "yt-dlp",
]

# FFmpeg is only needed for merging; missing is a warning, not a failure.
_FFMPEG_CANDIDATES = [
    r"C:\Tools\ffmpeg\bin\ffmpeg.exe",
    r"C:\Tools\ffmpeg\ffmpeg.exe",
    "ffmpeg.exe",
    "ffmpeg",
]


def _search(candidates: List[str]) -> str:
    for c in candidates:
        if os.path.isabs(c):
            if os.path.isfile(c):
                return c
        else:
            for p in os.environ.get("PATH", "").split(os.pathsep):
                full = os.path.join(p.strip('"'), c)
                if os.path.isfile(full):
                    return full
    return ""


def resolve_ytdlp() -> str:
    found = _search(_CANDIDATES)
    return found or _CANDIDATES[0]


def resolve_ffmpeg() -> str:
    """Best-effort FFmpeg location; `""` when it cannot be found."""
    return _search(_FFMPEG_CANDIDATES)



def default_download_dir() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "downloads")
    os.makedirs(path, exist_ok=True)
    return path


def build_command(ytdlp: str, download_dir: str, url: str,
                  ffmpeg_path: str = "",
                  format_expr: str = "") -> List[str]:
    """argv list for one download; no shell, URL is always a single argument.

    `format_expr` comes from `core_formats.selector_for()` (constants or a
    validated format id), never from raw user text; empty keeps the frozen
    Stage-001 policy.
    """
    expression = str(format_expr or "").strip() or FORMAT_EXPR
    command = [ytdlp, "-f", expression, "--merge-output-format", "mp4",
               "--newline", "--no-playlist"]
    if ffmpeg_path:
        command += ["--ffmpeg-location", str(ffmpeg_path)]
    command += ["-P", download_dir, url]
    return command


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
                 logger: Optional[Callable[..., None]] = None,
                 ffmpeg: Optional[str] = None):
        self.manager = manager
        self.ytdlp = ytdlp or resolve_ytdlp()
        self.download_dir = download_dir or default_download_dir()
        self.ffmpeg = ffmpeg or ""
        self._popen_factory = popen_factory or subprocess.Popen
        self._log = logger or (lambda *a: None)

    def run(self, task_id: str, url: str,
            control: Optional[TaskControl] = None) -> EngineResult:
        if control is None:
            control = TaskControl(task_id, self._log)
        try:
            self.manager.transition(task_id, "downloading")
        except Exception as e:
            return EngineResult(task_id, -1, "start_failed", str(e))
        if control.cancel_requested():
            return self._finish_cancelled(task_id, control)
        if control.pause_requested():
            return self._finish_paused(task_id)
        command = build_command(self.ytdlp, self.download_dir, url,
                                self.ffmpeg,
                                getattr(control, "format_expr", ""))
        self._log(f"Task {task_id} start: {url}")
        self._log(f"yt-dlp: {self.ytdlp}")
        if getattr(control, "format_expr", ""):
            self._log(f"format: {control.format_expr}")
        if self.ffmpeg:
            self._log(f"ffmpeg: {self.ffmpeg}")
        try:
            kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                      "text": True, "encoding": "utf-8", "errors": "replace"}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess,
                                                  "CREATE_NO_WINDOW", 0)
            process = self._popen_factory(command, **kwargs)
            control.attach_process(process)
            for raw in process.stdout:
                if control.cancel_requested() or control.pause_requested():
                    terminate_tree(process, self._log)
                    break
                line = (raw or "").strip()
                if not line:
                    continue
                self._log(f"[{task_id}] {line}")
                event = parse_line(line)
                self._apply(task_id, event, control)
            process.wait()
            return self._finish_outcome(task_id, process, control)
        except FileNotFoundError:
            self.manager.transition(task_id, "error", error_code="ytdlp_missing",
                                    error_message=f"yt-dlp not found at {self.ytdlp}")
            self._log(f"Task {task_id} error: yt-dlp not found at {self.ytdlp}")
            return EngineResult(task_id, -1, "ytdlp_missing",
                                f"yt-dlp not found at {self.ytdlp}")
        except Exception as e:  # noqa: BLE001 - engine must not fake success
            if control.cancel_requested():
                return self._finish_cancelled(task_id, control)
            if control.pause_requested():
                return self._finish_paused(task_id)
            try:
                self.manager.transition(task_id, "error", error_code="engine_error",
                                        error_message=str(e))
            except Exception:
                pass
            self._log(f"Task {task_id} error: {e}")
            return EngineResult(task_id, -1, "engine_error", str(e))
        finally:
            control.detach_process()

    def _finish_outcome(self, task_id: str, process,
                        control: TaskControl) -> EngineResult:
        """Cancel beats pause beats return code: never fake a success."""
        if control.cancel_requested():
            return self._finish_cancelled(task_id, control)
        if control.pause_requested():
            return self._finish_paused(task_id)
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

    def _finish_paused(self, task_id: str) -> EngineResult:
        try:
            self.manager.transition(task_id, "paused")
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log(f"Task {task_id} could not be marked paused: {exc}")
        self._log(f"Task {task_id} paused (breakpoint files kept)")
        return EngineResult(task_id, -1, "paused")

    def _finish_cancelled(self, task_id: str,
                          control: TaskControl) -> EngineResult:
        try:
            self.manager.transition(task_id, "cancelled")
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log(f"Task {task_id} could not be marked cancelled: {exc}")
        deleted = self.cleanup(task_id, control)
        self._log(f"Task {task_id} cancelled (removed {len(deleted)} file(s))")
        return EngineResult(task_id, -1, "cancelled", f"removed={len(deleted)}")

    def cleanup(self, task_id: str, control: TaskControl) -> List[str]:
        """Delete this run's temp/output files (D-009). Never raises.

        Without a `started_at` we cannot tell this run's files apart from an
        older download of the same video, so cleanup is skipped on purpose.
        """
        task = self.manager.get(task_id)
        url = task.url if task else ""
        since = started_epoch(task.started_at) if task else 0.0
        if since <= 0.0:
            self._log(f"Task {task_id} cleanup skipped: no started_at")
            return []
        try:
            return cleanup_task_files(self.download_dir, url,
                                      control.artifacts(), since, self._log)
        except Exception as exc:  # noqa: BLE001 - cleanup must not break state
            self._log(f"Task {task_id} cleanup error: {exc}")
            return []

    def _apply(self, task_id: str, event: ProgressEvent,
               control: TaskControl) -> None:
        if event.kind == "progress" and event.percent is not None:
            self.manager.report_progress(task_id, event.percent,
                                         event.speed, event.eta)
        elif event.kind == "merging":
            self.manager.report_merging(task_id)
        elif event.kind == "merged":
            # 合并行同时代表进度(99%)与本次输出路径
            self.manager.report_merging(task_id)
            if event.path:
                control.record_artifacts(event.path)
        elif event.kind == "destination" and event.path:
            control.record_artifacts(event.path)
        elif event.kind == "title" and event.title:
            self.manager.report_title(task_id, event.title)
