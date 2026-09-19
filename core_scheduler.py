"""MediaDock Scheduler boundary (Stage-003, extended Stage-004).

Owns the active-slot set (fixed limit 3), the FIFO pending queue, one-shot
run bookkeeping and the completion callback that refills free slots.

Stage-004 adds the control orchestration: `pause`/`resume`/`cancel`/`retry`.
A paused or cancelled task releases its slot (so the queue refills), and a
resumed task goes back through `admit`, which keeps the limit at 3.

Responsibility split (Stage-004.md 5.5):

    Scheduler      -> slots, FIFO queue, control orchestration, refill
    TaskManager    -> Task storage and every legal transition
    DownloadEngine -> one download; the only writer of downloading/paused/
                      cancelled/completed/error for its own run

The Scheduler never writes Task fields directly, never parses HTTP and
never renders UI. stdlib only.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core_control import ControlError, ControlRegistry
from core_files import cleanup_task_files, started_epoch
from core_manager import RETRY_RESET_FIELDS
from core_task import SETTLED_STATUSES

MAX_ACTIVE_TASKS = 3

# Statuses from which a task may sit in the FIFO queue: a brand new task is
# `pending`, a paused task waiting for a free slot stays `paused`.
QUEUE_STATUSES = ("pending", "paused")

# How long a control call waits for its target status before reporting
# `control_timeout`. The request is still applied either way.
CONTROL_TIMEOUT = 10.0
CONTROL_POLL = 0.01


def default_download_dir() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "downloads")


class Scheduler:
    """Admission control and lifecycle control for download tasks.

    Thread model: one daemon thread per active task. `_lock` is the single
    scheduling lock guarding `_active`, `_queue` and the one-shot records.
    TaskManager has its own RLock and is never held while waiting for a slot.
    """

    def __init__(self, manager, engine_factory: Callable[[], Any],
                 max_active: int = MAX_ACTIVE_TASKS,
                 logger: Callable[..., None] | None = None,
                 thread_factory: Callable[..., Any] | None = None,
                 control_registry: ControlRegistry | None = None,
                 download_dir: Optional[str] = None,
                 control_timeout: float = CONTROL_TIMEOUT):
        limit = int(max_active)
        if limit < 1:
            raise ValueError("max_active must be >= 1")
        self.manager = manager
        self._engine_factory = engine_factory
        self._max_active = limit
        self._log = logger or (lambda *args: None)
        self._thread_factory = thread_factory or self._default_thread
        self._lock = threading.RLock()
        self._active: Dict[str, str] = {}
        self._queue: List[str] = []
        self._urls: Dict[str, str] = {}
        self._formats: Dict[str, str] = {}
        self._started: set = set()
        self._finished: set = set()
        self._controls = control_registry or ControlRegistry(self._log)
        self._download_dir = download_dir or default_download_dir()
        self._control_timeout = float(control_timeout)

    @staticmethod
    def _default_thread(target, args):
        return threading.Thread(target=target, args=args, daemon=True)

    # -- wiring ------------------------------------------------------
    def set_engine_factory(self, engine_factory: Callable[[], Any]) -> None:
        """Test/DI seam: replace how engines are built for later runs."""
        with self._lock:
            self._engine_factory = engine_factory

    # -- submission --------------------------------------------------
    def submit(self, url: str, platform: str = "youtube",
               format_expr: str = "") -> Dict[str, Any]:
        """Create a Task, then start it now or park it in the FIFO queue.

        `format_expr` is the resolved yt-dlp `-f` expression (Stage-008); it is
        kept in the scheduler's send-time record, not in the Task, so the Task
        field/schema contract is unchanged. The Task is stored before any start
        decision, so the caller can always query the returned `task_id`.
        """
        task = self.manager.create(url, platform)
        self.admit(task.task_id, url, format_expr)
        snapshot = self.manager.get(task.task_id)
        return snapshot.to_dict() if snapshot else task.to_dict()

    def admit(self, task_id: str, url: str, format_expr: str = "") -> bool:
        """Atomically decide start-now vs queue for an already stored Task."""
        with self._lock:
            self._urls[task_id] = url
            expression = str(format_expr or "").strip()
            if expression:
                self._formats[task_id] = expression
            if len(self._active) < self._max_active:
                return self._start_locked(task_id)
            if task_id not in self._queue:
                self._queue.append(task_id)
            self._log(f"Scheduler queued {task_id} "
                      f"(active={len(self._active)}, queued={len(self._queue)})")
            return False

    def format_for(self, task_id: str) -> str:
        """`-f` expression recorded for this task (empty = default policy)."""
        with self._lock:
            return self._formats.get(task_id, "")

    def _start_locked(self, task_id: str) -> bool:
        if task_id in self._started or task_id in self._finished:
            self._log(f"Scheduler ignored duplicate start for {task_id}")
            return False
        if task_id in self._queue:
            self._queue.remove(task_id)
        url = self._urls.get(task_id, "")
        self._started.add(task_id)
        self._active[task_id] = url
        thread = self._thread_factory(target=self._run, args=(task_id, url))
        thread.start()
        self._log(f"Scheduler start {task_id} "
                  f"(active={len(self._active)}, queued={len(self._queue)})")
        return True

    # -- run / completion --------------------------------------------
    def _run(self, task_id: str, url: str) -> None:
        try:
            engine = self._engine_factory()
            control = self._controls.get_or_create(task_id)
            control.format_expr = self.format_for(task_id)
            engine.run(task_id, url, control)
        except Exception as exc:  # noqa: BLE001 - a slot must never leak
            self._fail(task_id, "scheduler_error", str(exc))
        finally:
            self._settle(task_id)
            self.on_finished(task_id)

    def _settle(self, task_id: str) -> None:
        """Defensive: a release must never leave a non-settled task behind.

        `paused` and `cancelled` are valid outcomes of a controlled run, so
        they must not be rewritten to `scheduler_incomplete`.
        """
        task = self.manager.get(task_id)
        if task is None:
            self._log(f"Scheduler lost Task {task_id}")
            return
        if task.status in SETTLED_STATUSES:
            return
        self._fail(task_id, "scheduler_incomplete",
                   f"engine ended while status={task.status}")

    def _fail(self, task_id: str, error_code: str, message: str) -> None:
        self._log(f"Scheduler error {task_id}: {error_code} {message}")
        try:
            self.manager.transition(task_id, "error", error_code=error_code,
                                    error_message=message)
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log(f"Scheduler could not mark {task_id} error: {exc}")

    def on_finished(self, task_id: str) -> bool:
        """One-shot completion callback: free the slot and refill the queue.

        Returns True only for the first call per task, so duplicate engine
        callbacks cannot release a slot twice or start a task twice.
        """
        with self._lock:
            if task_id in self._finished:
                self._log(f"Scheduler ignored duplicate finish for {task_id}")
                return False
            self._finished.add(task_id)
            self._active.pop(task_id, None)
            self._dispatch_locked()
            return True

    def _dispatch_locked(self) -> None:
        while self._queue and len(self._active) < self._max_active:
            nxt = self._queue.pop(0)
            if not self._start_locked(nxt):
                self._log(f"Scheduler skipped queue head {nxt}")

    # -- control (Stage-004) -----------------------------------------
    def wait_for_status(self, task_id: str, statuses, timeout=None):
        """Poll until the Task reaches one of `statuses` or the timeout ends.

        Returns the last observed status (None when the Task disappeared).
        """
        window = self._control_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + window
        while True:
            task = self.manager.get(task_id)
            if task is None:
                return None
            if task.status in statuses:
                return task.status
            if time.monotonic() >= deadline:
                return task.status
            time.sleep(CONTROL_POLL)

    def _require(self, task_id: str):
        task = self.manager.get(task_id)
        if task is None:
            raise ControlError("task_not_found", f"task {task_id} not found",
                               404, task_id)
        return task

    def _result(self, task_id: str, changed: bool = True, **extra):
        task = self.manager.get(task_id)
        payload = {"task_id": task_id,
                   "status": task.status if task else "unknown",
                   "changed": bool(changed)}
        payload.update(extra)
        return payload

    def _timeout_error(self, task_id: str, expected: str, actual) -> ControlError:
        return ControlError(
            "control_timeout",
            f"task {task_id} did not reach {expected} (status={actual})",
            409, task_id)

    def pause(self, task_id: str, timeout=None) -> Dict[str, Any]:
        """Pause an active download: stop the process tree, keep the files."""
        task = self._require(task_id)
        if task.status != "downloading":
            raise ControlError("not_pausable",
                               f"task {task_id} is not downloading "
                               f"(status={task.status})", 409, task_id)
        control = self._controls.get(task_id)
        if control is None:
            raise ControlError("not_pausable",
                               f"task {task_id} has no control context",
                               409, task_id)
        if not control.request_pause():
            raise ControlError("not_pausable",
                               f"task {task_id} already has a control intent",
                               409, task_id)
        final = self.wait_for_status(
            task_id, ("paused", "completed", "error", "cancelled"), timeout)
        if final != "paused":
            if final == "downloading":
                raise self._timeout_error(task_id, "paused", final)
            raise ControlError("not_pausable",
                               f"task {task_id} ended as {final} before it "
                               f"could be paused", 409, task_id)
        return self._result(task_id)

    def resume(self, task_id: str, timeout=None) -> Dict[str, Any]:
        """Continue a paused download; keeps the breakpoint files."""
        task = self._require(task_id)
        if task.status != "paused":
            raise ControlError("not_resumable",
                               f"task {task_id} is not paused "
                               f"(status={task.status})", 409, task_id)
        control = self._controls.get_or_create(task_id)
        control.clear_pause()
        url = self._urls.get(task_id) or task.url
        self._reset_marks(task_id)
        started = self.admit(task_id, url)
        if not started:
            # No free slot: the task stays `paused` until the queue reaches it.
            return self._result(task_id, queued=True)
        final = self.wait_for_status(task_id, ("downloading", "completed",
                                               "error", "cancelled"), timeout)
        if final not in ("downloading", "completed", "error", "cancelled"):
            raise self._timeout_error(task_id, "downloading", final)
        return self._result(task_id)

    def cancel(self, task_id: str, timeout=None) -> Dict[str, Any]:
        """Cancel a pending/active/paused Task and clean this run's files."""
        task = self._require(task_id)
        if task.status in ("completed", "cancelled", "error"):
            raise ControlError("not_cancellable",
                               f"task {task_id} is {task.status}",
                               409, task_id)
        if task.status == "downloading":
            control = self._controls.get(task_id)
            if control is None:
                raise ControlError("not_cancellable",
                                   f"task {task_id} has no control context",
                                   409, task_id)
            if not control.request_cancel():
                return self._result(task_id, changed=False)
            final = self.wait_for_status(
                task_id, ("cancelled", "completed", "error"), timeout)
            if final != "cancelled":
                raise self._timeout_error(task_id, "cancelled", final)
            return self._result(task_id)
        # pending (queued) or paused: nothing is running right now
        with self._lock:
            if task_id in self._queue:
                self._queue.remove(task_id)
            self._active.pop(task_id, None)
        artifacts = []
        control = self._controls.get(task_id)
        if control is not None:
            artifacts = control.artifacts()
        try:
            self.manager.transition(task_id, "cancelled")
        except Exception as exc:  # noqa: BLE001 - report as a business error
            raise ControlError("not_cancellable",
                               f"task {task_id}: {exc}", 409, task_id)
        self._cleanup_files(task_id, artifacts)
        self._controls.unregister(task_id)
        return self._result(task_id)

    def retry(self, task_id: str, timeout=None) -> Dict[str, Any]:
        """Re-run an error/cancelled Task with a fresh set of fields."""
        task = self._require(task_id)
        if task.status not in ("error", "cancelled"):
            raise ControlError("not_retryable",
                               f"task {task_id} is {task.status}", 409, task_id)
        url = self._urls.get(task_id) or task.url
        self._controls.unregister(task_id)
        self._reset_marks(task_id)
        self.manager.transition(task_id, "pending", **RETRY_RESET_FIELDS)
        started = self.admit(task_id, url)
        if not started:
            return self._result(task_id)
        final = self.wait_for_status(task_id, ("downloading", "completed",
                                               "error", "cancelled"), timeout)
        if final not in ("downloading", "completed", "error", "cancelled"):
            raise self._timeout_error(task_id, "downloading", final)
        return self._result(task_id)

    def _reset_marks(self, task_id: str) -> None:
        """Forget the one-shot run records so a Task may start again."""
        with self._lock:
            self._started.discard(task_id)
            self._finished.discard(task_id)
            if task_id in self._queue:
                self._queue.remove(task_id)

    def _cleanup_files(self, task_id: str, artifacts=()) -> List[str]:
        """Cancel-time file cleanup (D-009). Never raises.

        Without a `started_at` we cannot tell this run's files apart from an
        older download of the same video, so cleanup is skipped on purpose.
        """
        task = self.manager.get(task_id)
        if task is None:
            return []
        since = started_epoch(task.started_at)
        if since <= 0.0:
            self._log(f"Scheduler skipped cleanup for {task_id}: no started_at")
            return []
        try:
            return cleanup_task_files(self._download_dir, task.url,
                                      artifacts, since, self._log)
        except Exception as exc:  # noqa: BLE001 - cleanup must not break state
            self._log(f"Scheduler cleanup error for {task_id}: {exc}")
            return []

    # -- read-only views --------------------------------------------
    @property
    def active_limit(self) -> int:
        return self._max_active

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def queued_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def active_ids(self) -> List[str]:
        with self._lock:
            return list(self._active.keys())

    def queued_ids(self) -> List[str]:
        with self._lock:
            return list(self._queue)

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            return {"active_count": len(self._active),
                    "active_limit": self._max_active,
                    "queued_count": len(self._queue)}

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Regression helper: wait until no active task and empty queue."""
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            with self._lock:
                if not self._active and not self._queue:
                    return True
            time.sleep(0.01)
        return False