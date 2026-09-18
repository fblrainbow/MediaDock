"""Shared stdlib-only test helpers for MediaDock.

Keeps `srv.scheduler`'s engine factory swappable so unit tests never spawn
a real yt-dlp process or touch the network (Stage-003.md 7.3). Every
helper uses the global server module, because the HTTP handler, scheduler
and manager under test are the real singletons.

Stage-004: engine stubs take the optional `control` argument and must honour
the pause/cancel intent exactly like `DownloadEngine` does.
"""
import time

import server as srv

TERMINAL_STATUSES = ("completed", "error", "cancelled")
# Statuses after which a Task no longer runs a process (safe test cleanup).
STOPPED_STATUSES = ("completed", "error", "cancelled", "paused")


class InstantEngine:
    """Engine stub that finishes a Task immediately (no real yt-dlp)."""

    def run(self, task_id, url, control=None):
        srv.manager.transition(task_id, "downloading")
        srv.manager.transition(task_id, "completed", percent=100.0)


class FailingEngine:
    """Engine stub that fails a Task immediately (no real yt-dlp)."""

    def __init__(self, error_code="exit_code", message="stubbed failure"):
        self._code = error_code
        self._message = message

    def run(self, task_id, url, control=None):
        srv.manager.transition(task_id, "downloading")
        srv.manager.transition(task_id, "error", error_code=self._code,
                               error_message=self._message)


class CancelableEngine:
    """Engine stub that stays `downloading` until paused or cancelled.

    Control tests need a run that holds its slot and polls the control
    context; this is exactly the contract `DownloadEngine` implements.
    """

    def __init__(self, hold_seconds=5.0, poll=0.01):
        self._hold = float(hold_seconds)
        self._poll = float(poll)

    def run(self, task_id, url, control=None):
        srv.manager.transition(task_id, "downloading")
        deadline = time.monotonic() + self._hold
        while time.monotonic() < deadline:
            if control is not None and control.cancel_requested():
                srv.manager.transition(task_id, "cancelled")
                return
            if control is not None and control.pause_requested():
                srv.manager.transition(task_id, "paused")
                return
            time.sleep(self._poll)
        srv.manager.transition(task_id, "completed", percent=100.0)


def install_engine(engine_cls):
    """Replace the scheduler's engine factory. Returns the previous one."""
    old = srv.scheduler._engine_factory
    srv.scheduler.set_engine_factory(lambda: engine_cls())
    return old


def install_factory(factory):
    """Install a custom engine factory (e.g. shared state across runs)."""
    old = srv.scheduler._engine_factory
    srv.scheduler.set_engine_factory(factory)
    return old


def restore_engine(old):
    """Put back a factory returned by `install_engine`."""
    srv.scheduler.set_engine_factory(old)


def wait_stopped(task_id, timeout=15.0):
    """Wait until a Task stops running (terminal or paused)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = srv.manager.get(task_id)
        if task is None or task.status in STOPPED_STATUSES:
            return True
        time.sleep(0.02)
    return False


def wait_terminal(task_id, timeout=15.0):
    """Wait until a Task reaches a terminal status (or disappears)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = srv.manager.get(task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return True
        time.sleep(0.02)
    return False


def drop_when_terminal(task_id, timeout=15.0):
    """Test cleanup: never remove a Task while its engine still runs."""
    wait_stopped(task_id, timeout)
    srv.manager.drop(task_id)


def wait_idle(timeout=20.0):
    """Wait until the scheduler has no active task and an empty queue."""
    return srv.scheduler.wait_idle(timeout)