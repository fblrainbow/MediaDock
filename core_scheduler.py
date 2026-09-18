"""MediaDock Scheduler boundary (Stage-003).

Owns the active-slot set (fixed limit 3), the FIFO pending queue, one-shot
run bookkeeping and the completion callback that refills free slots.

Responsibility split (Stage-003.md 5.6):

    Scheduler      -> slots, FIFO queue, launching threads, refill
    TaskManager    -> Task storage and every legal transition
    DownloadEngine -> one download, and the only owner of pending->downloading

The Scheduler never writes Task fields directly, never parses HTTP and
never renders UI. stdlib only.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List

MAX_ACTIVE_TASKS = 3

TERMINAL_STATUSES = ("completed", "error")


class Scheduler:
    """Admission control for download tasks.

    Thread model: one daemon thread per active task. `_lock` is the single
    scheduling lock guarding `_active`, `_queue` and the one-shot records.
    TaskManager has its own RLock and is never held while waiting for a slot.
    """

    def __init__(self, manager, engine_factory: Callable[[], Any],
                 max_active: int = MAX_ACTIVE_TASKS,
                 logger: Callable[..., None] | None = None,
                 thread_factory: Callable[..., Any] | None = None):
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
        self._started: set = set()
        self._finished: set = set()

    @staticmethod
    def _default_thread(target, args):
        return threading.Thread(target=target, args=args, daemon=True)

    # -- wiring ------------------------------------------------------
    def set_engine_factory(self, engine_factory: Callable[[], Any]) -> None:
        """Test/DI seam: replace how engines are built for later runs."""
        with self._lock:
            self._engine_factory = engine_factory

    # -- submission --------------------------------------------------
    def submit(self, url: str, platform: str = "youtube") -> Dict[str, Any]:
        """Create a Task, then start it now or park it in the FIFO queue.

        The Task is stored before any start decision, so the caller can
        always query the returned `task_id` immediately.
        """
        task = self.manager.create(url, platform)
        self.admit(task.task_id, url)
        snapshot = self.manager.get(task.task_id)
        return snapshot.to_dict() if snapshot else task.to_dict()

    def admit(self, task_id: str, url: str) -> bool:
        """Atomically decide start-now vs queue for an already stored Task."""
        with self._lock:
            self._urls[task_id] = url
            if len(self._active) < self._max_active:
                return self._start_locked(task_id)
            if task_id not in self._queue:
                self._queue.append(task_id)
            self._log(f"Scheduler queued {task_id} "
                      f"(active={len(self._active)}, queued={len(self._queue)})")
            return False

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
            engine.run(task_id, url)
        except Exception as exc:  # noqa: BLE001 - a slot must never leak
            self._fail(task_id, "scheduler_error", str(exc))
        finally:
            self._settle(task_id)
            self.on_finished(task_id)

    def _settle(self, task_id: str) -> None:
        """Defensive: a release must never leave a non-terminal task behind."""
        task = self.manager.get(task_id)
        if task is None:
            self._log(f"Scheduler lost Task {task_id}")
            return
        if task.status in TERMINAL_STATUSES:
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