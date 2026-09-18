"""MediaDock task list query boundary (Stage-003).

Single place that defines the ordering every page sees, plus the counters
of the shared task list API. Pure functions over Task dicts: no HTTP, no
threads, no mutation of the stored Task records.

Ordering contract (Stage-003.md 5.4 / plan-whole.md Stage-003 task 5):

1. unfinished tasks before completed tasks
2. unfinished sorted by `percent` descending
3. pending/error have no live progress -> 0%; same percent -> `created_at`
   newest first
4. completed sorted by `completed_at` descending, `completion_order` breaks
   same-second ties
5. `task_id` is the final stable tie-breaker

Stage-004: `paused`/`cancelled` carry no live progress, so they take part in
unfinished ordering as 0% (plan-whole.md 6.2) even if `percent` is stored.

Sorts are applied least-significant key first, which keeps every Python
stable sort deterministic without extra comparison helpers.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

COMPLETED = "completed"
LIVE_PROGRESS_STATUSES = ("downloading",)


def _percent(task: Dict[str, Any]) -> float:
    try:
        return float(task.get("percent") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _progress(task: Dict[str, Any]) -> float:
    """Only a downloading task contributes live progress to the ordering."""
    if task.get("status") not in LIVE_PROGRESS_STATUSES:
        return 0.0
    return _percent(task)


def _completion_order(task: Dict[str, Any]) -> int:
    try:
        return int(task.get("completion_order") or 0)
    except (TypeError, ValueError):
        return 0


def _text(task: Dict[str, Any], key: str) -> str:
    value = task.get(key)
    return "" if value is None else str(value)


def sort_tasks(tasks: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a new list ordered for display; input order is never trusted."""
    unfinished: List[Dict[str, Any]] = []
    finished: List[Dict[str, Any]] = []
    for raw in tasks:
        if not isinstance(raw, dict):
            continue
        if raw.get("status") == COMPLETED:
            finished.append(raw)
        else:
            unfinished.append(raw)
    unfinished.sort(key=lambda t: _text(t, "task_id"))
    unfinished.sort(key=lambda t: _text(t, "created_at"), reverse=True)
    unfinished.sort(key=_progress, reverse=True)
    finished.sort(key=lambda t: _text(t, "task_id"))
    finished.sort(key=lambda t: _completion_order(t), reverse=True)
    finished.sort(key=lambda t: _text(t, "completed_at"), reverse=True)
    return unfinished + finished


def build_task_list(manager, scheduler) -> Dict[str, Any]:
    """Payload for `GET /tasks`: ordered tasks plus scheduler counters."""
    stored = manager.all()
    tasks = [dict(task) for task in stored.values()]
    payload: Dict[str, Any] = {"tasks": sort_tasks(tasks)}
    payload.update(scheduler.summary())
    return payload