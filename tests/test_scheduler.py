"""Stage-003 Scheduler tests (T301-T308). stdlib only, no network.

Completion timing is controlled by a gate, so concurrency, queueing and
refill behaviour are deterministic instead of timing-dependent.
"""
import threading
import time
import unittest

from core_manager import TaskManager
from core_scheduler import MAX_ACTIVE_TASKS, Scheduler


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class FakeEngine:
    """Engine stand-in whose completion timing is controlled by the test."""

    def __init__(self, harness):
        self.h = harness

    def run(self, task_id, url):
        h = self.h
        h.calls.append((task_id, url))
        h.max_active = max(h.max_active, h.scheduler.active_count())
        if url in h.raise_for:
            raise RuntimeError(f"engine exploded for {url}")
        h.manager.transition(task_id, "downloading")
        h.started.add(task_id)
        h.gate.wait(5)
        if task_id in h.fail_ids:
            h.manager.transition(task_id, "error", error_code="exit_code",
                                 error_message="fake failure")
        else:
            h.manager.transition(task_id, "completed", percent=100.0)


class Harness:
    """TaskManager + Scheduler + controllable fake engine."""

    def __init__(self, max_active=MAX_ACTIVE_TASKS):
        self.manager = TaskManager()
        self.gate = threading.Event()
        self.calls = []
        self.started = set()
        self.fail_ids = set()
        self.raise_for = set()
        self.max_active = 0
        self.scheduler = Scheduler(self.manager, lambda: FakeEngine(self),
                                   max_active=max_active,
                                   logger=lambda *args: None)

    def submit(self, tag):
        url = f"https://example.com/{tag}"
        task = self.scheduler.submit(url)
        return task["task_id"], url

    def status(self, task_id):
        task = self.manager.get(task_id)
        return task.status if task else None

    def release(self):
        self.gate.set()


class TestSubmitAndConcurrency(unittest.TestCase):
    def test_single_submit_runs_one_active_task(self):
        h = Harness()
        tid, _ = h.submit("one")
        self.assertTrue(wait_for(lambda: h.started == {tid}))
        self.assertEqual(h.scheduler.active_count(), 1)
        self.assertEqual(h.scheduler.queued_count(), 0)
        self.assertEqual(h.status(tid), "downloading")
        h.release()
        self.assertTrue(h.scheduler.wait_idle(5))
        self.assertEqual(h.status(tid), "completed")
        self.assertEqual(h.scheduler.active_count(), 0)

    def test_three_tasks_run_concurrently(self):
        h = Harness()
        ids = [h.submit(f"c{i}")[0] for i in range(3)]
        self.assertTrue(wait_for(lambda: h.started == set(ids)))
        self.assertEqual(h.scheduler.active_count(), 3)
        self.assertEqual(h.scheduler.queued_count(), 0)
        h.release()
        self.assertTrue(h.scheduler.wait_idle(5))
        for tid in ids:
            self.assertEqual(h.status(tid), "completed")
        self.assertLessEqual(h.max_active, MAX_ACTIVE_TASKS)

    def test_fourth_task_waits_in_fifo_queue(self):
        h = Harness()
        ids = [h.submit(f"q{i}")[0] for i in range(4)]
        self.assertTrue(wait_for(lambda: len(h.started) == 3))
        self.assertEqual(h.scheduler.active_count(), 3)
        self.assertEqual(h.scheduler.queued_count(), 1)
        self.assertEqual(h.scheduler.queued_ids(), [ids[3]])
        # 第 4 个任务不能被伪装成已开始
        self.assertEqual(h.status(ids[3]), "pending")
        self.assertNotIn(ids[3], h.started)
        h.release()
        self.assertTrue(h.scheduler.wait_idle(10))
        self.assertIn(ids[3], h.started)
        self.assertEqual(h.scheduler.queued_count(), 0)
        self.assertLessEqual(h.max_active, MAX_ACTIVE_TASKS)

    def test_active_limit_is_configurable(self):
        h = Harness(max_active=1)
        ids = [h.submit(f"s{i}")[0] for i in range(3)]
        self.assertTrue(wait_for(lambda: len(h.started) == 1))
        self.assertEqual(h.scheduler.active_count(), 1)
        self.assertEqual(h.scheduler.queued_count(), 2)
        self.assertEqual(h.scheduler.active_limit, 1)
        h.release()
        self.assertTrue(h.scheduler.wait_idle(10))
        self.assertEqual(sorted(h.started), sorted(ids))
        self.assertEqual(h.scheduler.active_count(), 0)
        self.assertEqual(h.scheduler.queued_count(), 0)


class TestRefillAndFailure(unittest.TestCase):
    def test_slot_refill_starts_next_queued_task(self):
        h = Harness()
        ids = [h.submit(f"r{i}")[0] for i in range(5)]
        self.assertTrue(wait_for(lambda: len(h.started) == 3))
        h.release()
        self.assertTrue(h.scheduler.wait_idle(15))
        self.assertEqual(sorted(h.started), sorted(ids))
        self.assertEqual(h.scheduler.active_count(), 0)
        self.assertEqual(h.scheduler.queued_count(), 0)
        for tid in ids:
            self.assertEqual(h.status(tid), "completed")
        self.assertLessEqual(h.max_active, MAX_ACTIVE_TASKS)

    def test_failure_releases_slot_and_does_not_touch_others(self):
        h = Harness()
        ids = [h.submit(f"f{i}")[0] for i in range(4)]
        self.assertTrue(wait_for(lambda: len(h.started) == 3))
        h.fail_ids.add(ids[1])
        h.release()
        self.assertTrue(h.scheduler.wait_idle(15))
        failed = h.manager.get(ids[1])
        self.assertEqual(failed.status, "error")
        self.assertEqual(failed.error_code, "exit_code")
        for idx in (0, 2, 3):
            self.assertEqual(h.status(ids[idx]), "completed")
        self.assertEqual(h.scheduler.active_count(), 0)

    def test_duplicate_finish_callback_is_ignored(self):
        h = Harness()
        tid, _ = h.submit("dup")
        self.assertTrue(wait_for(lambda: tid in h.started))
        h.release()
        self.assertTrue(h.scheduler.wait_idle(5))
        self.assertFalse(h.scheduler.on_finished(tid))
        self.assertEqual(h.scheduler.active_count(), 0)
        self.assertEqual(h.scheduler.queued_count(), 0)
        # 重复回调不得启动任何任务
        self.assertEqual(h.calls, [(tid, "https://example.com/dup")])

    def test_engine_exception_marks_error_and_keeps_scheduling(self):
        h = Harness()
        h.raise_for.add("https://example.com/boom")
        boom, _ = h.submit("boom")
        ok, _ = h.submit("ok")
        h.release()
        self.assertTrue(h.scheduler.wait_idle(10))
        self.assertEqual(h.status(boom), "error")
        self.assertEqual(h.manager.get(boom).error_code, "scheduler_error")
        self.assertEqual(h.status(ok), "completed")
        self.assertEqual(h.scheduler.active_count(), 0)

    def test_engine_without_terminal_status_is_not_faked(self):
        class SilentEngine:
            def run(self, task_id, url):
                return None

        manager = TaskManager()
        scheduler = Scheduler(manager, lambda: SilentEngine(), logger=lambda *a: None)
        task = scheduler.submit("https://example.com/silent")
        self.assertTrue(scheduler.wait_idle(5))
        stored = manager.get(task["task_id"])
        self.assertEqual(stored.status, "error")
        self.assertEqual(stored.error_code, "scheduler_incomplete")

    def test_concurrent_submit_keeps_ids_unique_and_counts_consistent(self):
        h = Harness()
        ids = []
        lock = threading.Lock()

        def worker(i):
            tid, _ = h.submit(f"p{i}")
            with lock:
                ids.append(tid)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(set(ids)), 12)
        self.assertLessEqual(h.scheduler.active_count(), MAX_ACTIVE_TASKS)
        self.assertEqual(h.scheduler.active_count() + h.scheduler.queued_count(), 12)
        self.assertLessEqual(h.max_active, MAX_ACTIVE_TASKS)
        h.release()
        self.assertTrue(h.scheduler.wait_idle(20))
        self.assertEqual(h.scheduler.active_count(), 0)
        self.assertEqual(h.scheduler.queued_count(), 0)
        self.assertGreaterEqual(h.scheduler.active_count(), 0)
        for tid in ids:
            self.assertEqual(h.status(tid), "completed")


if __name__ == "__main__":
    unittest.main()