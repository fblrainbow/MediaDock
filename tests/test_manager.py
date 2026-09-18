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


if __name__ == "__main__":
    unittest.main()
