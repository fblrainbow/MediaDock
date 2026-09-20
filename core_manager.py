"""MediaDock TaskStore/TaskManager boundary (Stage-002, extended Stage-004).

Thread-safe in-memory store. Handler and DownloadEngine must go through
TaskManager; nothing else writes task state.

Stage-004 extends the state machine with `paused` and `cancelled` plus the
retry transition back to `pending`. `completed` stays terminal.

Stage-005 adds an optional `persister`: TaskManager stays the only writer of
state and hands snapshots to the store. Persistence failures are swallowed
so a broken database can never break a download.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core_task import TASK_STATUSES, Task, _now_iso, new_task_id, task_from_dict

# Progress is chatty; only snapshots away from a state change are throttled.
PROGRESS_PERSIST_SECONDS = 1.0

_ALLOWED = {
    "pending": ("downloading", "cancelled", "error"),
    "downloading": ("downloading", "paused", "completed", "cancelled",
                    "error"),
    "paused": ("downloading", "cancelled", "error"),
    "completed": (),
    "error": ("pending", "downloading"),
    "cancelled": ("pending", "downloading"),
}

# Fields a retry must clear so the Task looks like a fresh run. Kept here so
# every retry path resets exactly the same set.
RETRY_RESET_FIELDS: Dict[str, Any] = {
    "percent": 0.0,
    "speed": "",
    "eta": "",
    "file_path": "",
    "error_code": "",
    "error_message": "",
    "completed_at": "",
    "completion_order": None,
}


class IllegalTransition(Exception):
    def __init__(self, task_id: str, from_status: str, to_status: str):
        super().__init__(f"illegal transition {from_status} -> {to_status} "
                         f"for task {task_id}")
        self.task_id = task_id
        self.from_status = from_status
        self.to_status = to_status


def _check_transition(task_id: str, from_status: str, to_status: str) -> None:
    if to_status not in TASK_STATUSES:
        raise IllegalTransition(task_id, from_status, to_status)
    if to_status not in _ALLOWED.get(from_status, ()):
        raise IllegalTransition(task_id, from_status, to_status)


class TaskManager:
    def __init__(self,
                 id_factory: Callable[[], str] = new_task_id,
                 clock: Callable[[], str] = _now_iso,
                 persister: Any = None,
                 persist_interval: float = PROGRESS_PERSIST_SECONDS):
        self._lock = threading.RLock()
        self._tasks: Dict[str, Task] = {}
        self._id_factory = id_factory
        self._clock = clock
        self._completion_seq = 0
        self.persister = persister
        self._persist_interval = float(persist_interval)
        self._last_persist: Dict[str, float] = {}

    # -- persistence hooks (Stage-005) ------------------------------
    def _persist_save(self, task: Task, force: bool = True) -> None:
        """Hand a snapshot to the persister; never raise, never block state."""
        saver = getattr(self.persister, "save_task", None) if self.persister \
            else None
        if saver is None:
            return
        if not force and not self._persist_due(task.task_id):
            return
        self._last_persist[task.task_id] = time.monotonic()
        try:
            saver(task.to_dict())
        except Exception:  # noqa: BLE001 - persistence must never break state
            pass

    def _persist_due(self, task_id: str) -> bool:
        last = self._last_persist.get(task_id, 0.0)
        return (time.monotonic() - last) >= self._persist_interval

    def _persist_delete(self, task_id: str) -> None:
        deleter = getattr(self.persister, "delete_task", None) \
            if self.persister else None
        if deleter is None:
            return
        self._last_persist.pop(task_id, None)
        try:
            deleter(task_id)
        except Exception:  # noqa: BLE001 - persistence must never break state
            pass

    def event(self, task_id: str, kind: str, detail: str = "") -> None:
        """Record a state event when the persister supports it."""
        writer = getattr(self.persister, "record_event", None) \
            if self.persister else None
        if writer is None:
            return
        try:
            writer(task_id, kind, detail)
        except Exception:  # noqa: BLE001
            pass

    def _touch(self, task: Task) -> None:
        task.updated_at = self._clock()

    # -- lifecycle --------------------------------------------------
    def create(self, url: str, platform: str = "youtube",
               task_type: str = "download") -> Task:
        kind = str(task_type or "download").strip() or "download"
        task = Task(task_id=self._id_factory(), url=url, platform=platform,
                    type=kind, status="pending",
                    created_at=self._clock(), updated_at=self._clock())
        with self._lock:
            self._tasks[task.task_id] = task
        self._persist_save(task)
        self.event(task.task_id, "created", task.status)
        return Task(**task.to_dict())

    def load_task(self, data: Dict[str, Any]) -> Task:
        """Restore a persisted Task (Stage-005 startup path)."""
        payload = dict(data or {})
        if not payload.get("task_id"):
            raise ValueError("persisted task without task_id")
        task = task_from_dict(payload)
        with self._lock:
            self._tasks[task.task_id] = task
            order = task.completion_order
            if isinstance(order, int) and order > self._completion_seq:
                self._completion_seq = order
        return Task(**task.to_dict())

    def completion_seq(self) -> int:
        with self._lock:
            return self._completion_seq

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if isinstance(task, Task):
                return Task(**task.to_dict())
            if isinstance(task, dict):
                return task_from_dict(task)
            return None

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            out: Dict[str, Dict[str, Any]] = {}
            for tid, raw in list(self._tasks.items()):
                task = self._coerce(raw)
                self._store(tid, task)
                out[tid] = task.to_dict()
            return out

    def drop(self, task_id: str) -> bool:
        """Remove a Task record (Stage-005 also drops it from the store)."""
        with self._lock:
            removed = self._tasks.pop(task_id, None) is not None
        if removed:
            self._persist_delete(task_id)
        return removed

    def get_or_create(self, task_id: str, url: str,
                      platform: str = "youtube") -> Task:
        """Legacy direct-call path: ensure a pending task with given id."""
        with self._lock:
            existing = self._coerce(self._tasks.get(task_id))
            if existing is not None:
                self._store(task_id, existing)
                return Task(**existing.to_dict())
            now = self._clock()
            task = Task(task_id=task_id, url=url, platform=platform,
                        status="pending", created_at=now, updated_at=now)
            self._tasks[task_id] = task
            return Task(**task.to_dict())

    def _coerce(self, task) -> Optional[Task]:
        if task is None:
            return None
        if isinstance(task, Task):
            return task
        if isinstance(task, dict):
            coerced = task_from_dict(task)
            return coerced
        return None

    def _store(self, task_id: str, task: Task) -> None:
        self._tasks[task_id] = task

    def ids(self) -> List[str]:
        with self._lock:
            return list(self._tasks.keys())

    # -- transitions ------------------------------------------------
    def transition(self, task_id: str, to_status: str, **fields: Any) -> Task:
        with self._lock:
            raw = self._tasks.get(task_id)
            if raw is None:
                raise KeyError(task_id)
            task = self._coerce(raw)
            self._store(task_id, task)
            from_status = task.status
            _check_transition(task_id, task.status, to_status)
            task.status = to_status
            now = self._clock()
            if to_status == "downloading" and not task.started_at:
                task.started_at = now
            if to_status in ("completed", "error"):
                task.completed_at = now
                if to_status == "completed":
                    self._completion_seq += 1
                    task.completion_order = self._completion_seq
            for key, value in fields.items():
                if key in ("task_id", "created_at"):
                    continue
                if hasattr(task, key):
                    setattr(task, key, value)
            self._touch(task)
            self._persist_save(task)
        self.event(task_id, "transition", f"{from_status}->{to_status}")
        return Task(**task.to_dict())

    def report_progress(self, task_id: str, percent: float,
                        speed: str = "", eta: str = "") -> Optional[Task]:
        with self._lock:
            task = self._coerce(self._tasks.get(task_id))
            if task is None:
                return None
            self._store(task_id, task)
            if task.status != "downloading":
                return None
            try:
                task.percent = float(percent)
            except (TypeError, ValueError):
                return Task(**task.to_dict())
            task.speed = speed
            task.eta = eta
            self._touch(task)
            snapshot = Task(**task.to_dict())
        self._persist_save(snapshot, force=False)
        return snapshot

    def report_merging(self, task_id: str) -> Optional[Task]:
        with self._lock:
            task = self._coerce(self._tasks.get(task_id))
            if task is None:
                return None
            self._store(task_id, task)
            if task.status != "downloading":
                return None
            task.percent = 99.0
            task.speed = "merging"
            task.eta = ""
            self._touch(task)
            snapshot = Task(**task.to_dict())
        self._persist_save(snapshot, force=False)
        return snapshot

    def report_title(self, task_id: str, title: str) -> Optional[Task]:
        with self._lock:
            task = self._coerce(self._tasks.get(task_id))
            if task is None:
                return None
            self._store(task_id, task)
            if not title or task.title:
                return None
            task.title = title
            self._touch(task)
            snapshot = Task(**task.to_dict())
        self._persist_save(snapshot)
        return snapshot
