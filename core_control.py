"""MediaDock process control boundary (Stage-004).

Per-task control context: the yt-dlp process handle, the pause/cancel
intent and the artifacts the current run produced. The Scheduler owns one
`ControlRegistry`; `DownloadEngine` receives the `TaskControl` for the task
it runs and is the only place that turns an intent into a Task status.

Nothing here writes Task fields or parses HTTP. stdlib only.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

TERMINATE_TIMEOUT = 5.0


class ControlError(Exception):
    """Business error for a control request (Stage-004.md 5.2)."""

    def __init__(self, code: str, message: str, http_status: int = 409,
                 task_id: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.task_id = task_id


def _wait_quietly(process: Any, timeout: float) -> None:
    """Wait for a terminated process without assuming `wait(timeout)` exists."""
    wait = getattr(process, "wait", None)
    if wait is None:
        return
    try:
        wait(timeout=timeout)
    except TypeError:
        try:
            wait()
        except Exception:  # noqa: BLE001 - best effort
            pass
    except Exception:  # noqa: BLE001 - already gone / timeout
        pass


def terminate_tree(process: Any,
                   logger: Optional[Callable[..., None]] = None,
                   timeout: float = TERMINATE_TIMEOUT) -> bool:
    """Kill a process *and its children*; never raise.

    yt-dlp spawns FFmpeg as a child, so killing only the parent can orphan a
    transcoding process that keeps the output file locked. On Windows the
    whole tree is killed with `taskkill /F /T`; elsewhere we terminate then
    kill. Returns True when a stop was attempted.
    """
    log = logger or (lambda *a: None)
    if process is None:
        return False
    if getattr(process, "returncode", None) is not None:
        return False
    pid = getattr(process, "pid", None)
    stopped = False
    if sys.platform == "win32" and isinstance(pid, int) and pid > 0:
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False)
            log(f"taskkill /F /T /PID {pid} rc={completed.returncode}")
            stopped = True
        except Exception as exc:  # noqa: BLE001 - fall back to terminate()
            log(f"taskkill failed for pid={pid}: {exc}")
    if not stopped:
        try:
            process.terminate()
            stopped = True
        except Exception as exc:  # noqa: BLE001 - process may already be gone
            log(f"terminate failed for pid={pid}: {exc}")
    _wait_quietly(process, timeout)
    if getattr(process, "returncode", None) is None and sys.platform != "win32":
        try:
            process.kill()
        except Exception:  # noqa: BLE001 - best effort
            pass
    return stopped


class TaskControl:
    """Pause/cancel intent, process handle and artifact list for one Task.

    Stage-008 adds `format_expr`: the resolved yt-dlp `-f` expression for this
    run. The Scheduler sets it from its own send-time record, so the engine
    never has to trust a raw HTTP value and stub engines may ignore it.
    """

    def __init__(self, task_id: str,
                 logger: Optional[Callable[..., None]] = None):
        self.task_id = task_id
        self.format_expr = ""
        self._lock = threading.RLock()
        self._pause = False
        self._cancel = False
        self._process: Any = None
        self._artifacts: List[str] = []
        self._log = logger or (lambda *a: None)

    # -- intent ------------------------------------------------------
    @property
    def paused(self) -> bool:
        with self._lock:
            return self._pause

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancel

    def pause_requested(self) -> bool:
        return self.paused

    def cancel_requested(self) -> bool:
        return self.cancelled

    def request_pause(self) -> bool:
        """Set the pause intent once and stop the running process."""
        with self._lock:
            if self._pause or self._cancel:
                return False
            self._pause = True
            process = self._process
        if process is not None:
            terminate_tree(process, self._log)
        return True

    def request_cancel(self) -> bool:
        """Set the cancel intent once and stop the running process."""
        with self._lock:
            if self._cancel:
                return False
            self._cancel = True
            process = self._process
        if process is not None:
            terminate_tree(process, self._log)
        return True

    def clear_pause(self) -> None:
        """Drop the pause intent so a resume can start a fresh run."""
        with self._lock:
            self._pause = False

    # -- process handle ---------------------------------------------
    def attach_process(self, process: Any) -> None:
        with self._lock:
            self._process = process
            stop = self._pause or self._cancel
        if stop:
            terminate_tree(process, self._log)

    def detach_process(self) -> None:
        with self._lock:
            self._process = None

    def has_process(self) -> bool:
        with self._lock:
            return self._process is not None

    # -- artifacts ---------------------------------------------------
    def record_artifacts(self, paths: Any) -> None:
        """Remember files this run touched (yt-dlp Destination / merged)."""
        if paths is None:
            return
        if isinstance(paths, str):
            candidates = [paths]
        else:
            try:
                candidates = list(paths)
            except TypeError:
                return
        with self._lock:
            for path in candidates:
                if path and path not in self._artifacts:
                    self._artifacts.append(path)

    def artifacts(self) -> List[str]:
        with self._lock:
            return list(self._artifacts)


class ControlRegistry:
    """Thread-safe `task_id -> TaskControl` map owned by the Scheduler."""

    def __init__(self, logger: Optional[Callable[..., None]] = None):
        self._lock = threading.RLock()
        self._controls: Dict[str, TaskControl] = {}
        self._log = logger or (lambda *a: None)

    def get_or_create(self, task_id: str) -> TaskControl:
        with self._lock:
            control = self._controls.get(task_id)
            if control is None:
                control = TaskControl(task_id, self._log)
                self._controls[task_id] = control
            return control

    def get(self, task_id: str) -> Optional[TaskControl]:
        with self._lock:
            return self._controls.get(task_id)

    def unregister(self, task_id: str) -> Optional[TaskControl]:
        with self._lock:
            return self._controls.pop(task_id, None)

    def ids(self) -> List[str]:
        with self._lock:
            return list(self._controls.keys())
