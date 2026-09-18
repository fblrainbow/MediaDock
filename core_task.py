"""MediaDock core Task model (Stage-002).

Single source of truth for Task fields, defaults, copy semantics and
JSON serialization. stdlib only.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict
import uuid


TASK_STATUSES = ("pending", "downloading", "completed", "error")

# Stage-001 carried these fields in every task dict. They must keep
# working through the Stage-002 migration.
LEGACY_FIELDS = ("status", "percent", "speed", "eta", "url", "title",
                 "created_at", "updated_at")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_task_id() -> str:
    return str(uuid.uuid4())[:8]


@dataclass
class Task:
    task_id: str = field(default_factory=new_task_id)
    type: str = "download"
    status: str = "pending"
    url: str = ""
    platform: str = "youtube"
    title: str = ""
    percent: float = 0.0
    speed: str = ""
    eta: str = ""
    file_path: str = ""
    error_code: str = ""
    error_message: str = ""
    created_at: str = field(default_factory=_now_iso)
    started_at: str = ""
    updated_at: str = field(default_factory=_now_iso)
    completed_at: str = ""
    completion_order: Any = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a deep copy suitable for JSON responses."""
        return deepcopy(asdict(self))

    def snapshot(self) -> Dict[str, Any]:
        return self.to_dict()


def task_to_dict(task: Task) -> Dict[str, Any]:
    return task.to_dict()


def task_from_dict(data: Dict[str, Any]) -> Task:
    """Build a Task from a stored/legacy dict, tolerating unknowns."""
    known = {f for f in Task.__dataclass_fields__}
    clean = {k: deepcopy(v) for k, v in data.items() if k in known}
    if "percent" in clean:
        try:
            clean["percent"] = float(clean["percent"])
        except (TypeError, ValueError):
            clean["percent"] = 0.0
    return Task(**clean)


def legacy_view(task: Task) -> Dict[str, Any]:
    """Stage-001-compatible field subset used by old API consumers."""
    full = task.to_dict()
    return {k: full[k] for k in LEGACY_FIELDS if k in full}
