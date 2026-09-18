"""MediaDock TaskStore/TaskManager boundary (Stage-002).

Thread-safe in-memory store. Handler and DownloadEngine must go through
TaskManager; nothing else writes task state.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from core_task import Task, _now_iso, new_task_id, task_from_dict

_ALLOWED = {
    "pending": ("downloading", "error"),
    "downloading": ("downloading", "completed", "error"),
    "completed": (),
    "error": (),
}


class IllegalTransition(Exception):
    def __init__(self, task_id: str, from_status: str, to_status: str):
        super().__init__(f"illegal transition {from_status} -> {to_status} "
                         f"for task {task_id}")
        self.task_id = task_id
        self.from_status = from_status
        self.to_status = to_status


def _check_transition(task_id: str, from_status: str, to_status: str) -> None:
    if to_status not in _ALLOWED.get(from_status, ()):
        raise IllegalTransition(task_id, from_status, to_status)


class TaskManager:
    def __init__(self,
                 id_factory: Callable[[], str] = new_task_id,
                 clock: Callable[[], str] = _now_iso):
        self._lock = threading.RLock()
        self._tasks: Dict[str, Task] = {}
        self._id_factory = id_factory
        self._clock = clock
        self._completion_seq = 0

    def _touch(self, task: Task) -> None:
        task.updated_at = self._clock()

    # -- lifecycle --------------------------------------------------
    def create(self, url: str, platform: str = "youtube") -> Task:
        task = Task(task_id=self._id_factory(), url=url, platform=platform,
                    status="pending",
                    created_at=self._clock(), updated_at=self._clock())
        with self._lock:
            self._tasks[task.task_id] = task
        return Task(**task.to_dict())

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
        """Internal/test helper. No HTTP API exposes deletion in Stage-002."""
        with self._lock:
            return self._tasks.pop(task_id, None) is not None

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
            return Task(**task.to_dict())

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
            return Task(**task.to_dict())

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
            return Task(**task.to_dict())
