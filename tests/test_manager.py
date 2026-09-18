"""Stage-002 TaskManager/state-machine tests (T204-T206)."""
import threading
import unittest

from core_manager import IllegalTransition, TaskManager


class TestTransitions(unittest.TestCase):
    def test_happy_path(self):
        m = TaskManager()
        t = m.create("https://example.com/v")
        self.assertEqual(t.status, "pending")
        m.transition(t.task_id, "downloading", percent=8.8)
        self.assertEqual(m.get(t.task_id).status, "downloading")
        self.assertTrue(m.get(t.task_id).started_at)
        m.transition(t.task_id, "completed", percent=100.0)
        done = m.get(t.task_id)
        self.assertEqual(done.status, "completed")
        self.assertTrue(done.completed_at)
        self.assertEqual(done.completion_order, 1)

    def test_error_path(self):
        m = TaskManager()
        t = m.create("https://example.com/v")
        m.transition(t.task_id, "downloading")
        m.transition(t.task_id, "error", error_code="exit_code",
                     error_message="returncode=1")
        err = m.get(t.task_id)
        self.assertEqual(err.status, "error")
        self.assertEqual(err.error_code, "exit_code")

    def test_illegal_transitions(self):
        m = TaskManager()
        t = m.create("https://example.com/v")
        with self.assertRaises(IllegalTransition):
            m.transition(t.task_id, "completed")
        m.transition(t.task_id, "downloading")
        m.transition(t.task_id, "completed")
        with self.assertRaises(IllegalTransition):
            m.transition(t.task_id, "downloading")
        with self.assertRaises(IllegalTransition):
            m.transition(t.task_id, "error")

    def test_missing_task(self):
        m = TaskManager()
        self.assertIsNone(m.get("nope"))
        with self.assertRaises(KeyError):
            m.transition("nope", "downloading")

    def test_progress_reports(self):
        m = TaskManager()
        t = m.create("https://example.com/v")
        self.assertIsNone(m.report_progress(t.task_id, 10.0))
        m.transition(t.task_id, "downloading")
        m.report_progress(t.task_id, 8.8, "1KiB/s", "00:01")
        self.assertAlmostEqual(m.get(t.task_id).percent, 8.8)
        m.report_merging(t.task_id)
        self.assertEqual(m.get(t.task_id).speed, "merging")
        m.report_title(t.task_id, "Some Title")
        self.assertEqual(m.get(t.task_id).title, "Some Title")

    def test_concurrent_writes(self):
        m = TaskManager()
        t = m.create("https://example.com/v")
        m.transition(t.task_id, "downloading")
        errors = []

        def worker(i):
            try:
                for j in range(50):
                    m.report_progress(t.task_id, float((i + j) % 100))
                    m.get(t.task_id)
                    m.all()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertFalse(errors)
        import json
        json.dumps(m.get(t.task_id).to_dict())


class TestStage004Transitions(unittest.TestCase):
    """T401-T405: paused / cancelled / retry state machine."""

    def test_pause_resume_complete(self):
        m = TaskManager()
        task = m.create("https://example.com/v")
        m.transition(task.task_id, "downloading")
        m.transition(task.task_id, "paused")
        paused = m.get(task.task_id)
        self.assertEqual(paused.status, "paused")
        self.assertEqual(paused.error_code, "")
        self.assertTrue(paused.started_at)
        m.transition(task.task_id, "downloading")
        m.transition(task.task_id, "completed", percent=100.0)
        done = m.get(task.task_id)
        self.assertEqual(done.status, "completed")
        self.assertEqual(done.percent, 100.0)

    def test_cancel_from_every_live_state(self):
        m = TaskManager()
        pending = m.create("https://example.com/a")
        m.transition(pending.task_id, "cancelled")
        self.assertEqual(m.get(pending.task_id).status, "cancelled")

        running = m.create("https://example.com/b")
        m.transition(running.task_id, "downloading")
        m.transition(running.task_id, "cancelled")
        self.assertEqual(m.get(running.task_id).status, "cancelled")

        paused = m.create("https://example.com/c")
        m.transition(paused.task_id, "downloading")
        m.transition(paused.task_id, "paused")
        m.transition(paused.task_id, "cancelled")
        self.assertEqual(m.get(paused.task_id).status, "cancelled")

    def test_retry_resets_fields(self):
        m = TaskManager()
        task = m.create("https://example.com/v")
        m.transition(task.task_id, "downloading", percent=42.0)
        m.transition(task.task_id, "error", error_code="exit_code",
                     error_message="boom")
        self.assertIsNotNone(m.get(task.task_id).completed_at)
        m.transition(task.task_id, "pending", percent=0.0, speed="", eta="",
                     error_code="", error_message="", completed_at="",
                     completion_order=None)
        retried = m.get(task.task_id)
        self.assertEqual(retried.status, "pending")
        self.assertEqual(retried.percent, 0.0)
        self.assertEqual(retried.error_code, "")
        self.assertEqual(retried.error_message, "")
        self.assertEqual(retried.completed_at, "")
        self.assertIsNone(retried.completion_order)

    def test_retry_from_cancelled(self):
        m = TaskManager()
        task = m.create("https://example.com/v")
        m.transition(task.task_id, "downloading")
        m.transition(task.task_id, "cancelled")
        m.transition(task.task_id, "pending")
        self.assertEqual(m.get(task.task_id).status, "pending")

    def test_cancelled_does_not_take_a_completion_slot(self):
        m = TaskManager()
        first = m.create("https://example.com/a")
        m.transition(first.task_id, "downloading")
        m.transition(first.task_id, "cancelled")
        second = m.create("https://example.com/b")
        m.transition(second.task_id, "downloading")
        m.transition(second.task_id, "completed")
        self.assertEqual(m.get(second.task_id).completion_order, 1)

    def test_completed_stays_terminal(self):
        m = TaskManager()
        task = m.create("https://example.com/v")
        m.transition(task.task_id, "downloading")
        m.transition(task.task_id, "completed")
        for target in ("downloading", "paused", "cancelled", "error",
                       "pending"):
            with self.assertRaises(IllegalTransition):
                m.transition(task.task_id, target)

    def test_unknown_status_is_rejected(self):
        m = TaskManager()
        task = m.create("https://example.com/v")
        with self.assertRaises(IllegalTransition):
            m.transition(task.task_id, "downloaded")


if __name__ == "__main__":
    unittest.main()
