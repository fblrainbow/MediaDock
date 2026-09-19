"""Stage-005 restart-matrix tests (T514-T518). Real SQLite temp files."""
import os
import tempfile
import unittest

import server as srv
from core_listing import build_task_list
from tests.helpers import InstantEngine, install_factory, restore_engine


def order_of(payload):
    return [task["task_id"] for task in payload["tasks"]]


class RestartBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mediadock-restart-")
        self.db = os.path.join(self.tmp, "tasks.db")
        srv.bootstrap(self.db)
        self._old_factory = install_factory(lambda: InstantEngine())

    def tearDown(self):
        restore_engine(self._old_factory)
        srv.bootstrap()          # 回到测试专用的内存库

    def restart(self):
        srv.bootstrap(self.db)
        install_factory(lambda: InstantEngine())
        return srv.manager


class TestRestartMatrix(RestartBase):
    def test_completed_record_survives_restart(self):
        created = srv.scheduler.submit("https://example.com/done")
        task_id = created["task_id"]
        self.assertTrue(srv.scheduler.wait_idle(10))
        before = srv.manager.get(task_id)
        self.assertEqual(before.status, "completed")

        manager = self.restart()
        after = manager.get(task_id)
        self.assertIsNotNone(after, "completed record disappeared after restart")
        self.assertEqual(after.status, "completed")
        self.assertEqual(after.percent, 100.0)
        self.assertEqual(after.completed_at, before.completed_at)
        self.assertEqual(after.completion_order, before.completion_order)

    def test_downloading_becomes_interrupted(self):
        task = srv.manager.create("https://example.com/running")
        srv.manager.transition(task.task_id, "downloading", percent=42.0)

        manager = self.restart()
        restored = manager.get(task.task_id)
        self.assertEqual(restored.status, "error")
        self.assertEqual(restored.error_code, "interrupted")
        self.assertTrue(restored.error_message)
        self.assertTrue(restored.completed_at)
        events = srv.storage.events(task.task_id)
        self.assertIn("restart_interrupted",
                      [event["kind"] for event in events])
        self.assertEqual(srv.scheduler.active_count(), 0)
        self.assertEqual(srv.scheduler.queued_count(), 0)

    def test_pending_becomes_interrupted(self):
        task = srv.manager.create("https://example.com/queued")
        manager = self.restart()
        restored = manager.get(task.task_id)
        self.assertEqual(restored.status, "error")
        self.assertEqual(restored.error_code, "interrupted")

    def test_paused_stays_paused_and_does_not_hold_a_slot(self):
        task = srv.manager.create("https://example.com/paused")
        srv.manager.transition(task.task_id, "downloading")
        srv.manager.transition(task.task_id, "paused")

        manager = self.restart()
        restored = manager.get(task.task_id)
        self.assertEqual(restored.status, "paused")
        self.assertEqual(restored.error_code, "")
        self.assertEqual(srv.scheduler.active_count(), 0)
        self.assertNotIn(task.task_id, srv.scheduler.active_ids())

    def test_task_order_is_stable_across_restart(self):
        ids = [srv.scheduler.submit(f"https://example.com/o{i}")["task_id"]
               for i in range(3)]
        self.assertTrue(srv.scheduler.wait_idle(15))
        before = order_of(build_task_list(srv.manager, srv.scheduler))

        self.restart()
        after = order_of(build_task_list(srv.manager, srv.scheduler))
        self.assertEqual(after, before)
        self.assertEqual(set(after), set(ids))

    def test_history_sees_restored_records(self):
        task_id = srv.scheduler.submit("https://example.com/h1")["task_id"]
        self.assertTrue(srv.scheduler.wait_idle(10))
        self.restart()
        from core_listing import build_history
        payload = build_history(srv.manager, limit=20)
        self.assertEqual([t["task_id"] for t in payload["tasks"]], [task_id])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(srv.manager.get(task_id).status, "completed")

    def test_bootstrap_marks_only_live_tasks(self):
        finished = srv.scheduler.submit("https://example.com/f")["task_id"]
        self.assertTrue(srv.scheduler.wait_idle(10))
        running = srv.manager.create("https://example.com/r")
        srv.manager.transition(running.task_id, "downloading")
        cancelled = srv.manager.create("https://example.com/c")
        srv.manager.transition(cancelled.task_id, "cancelled")

        manager = self.restart()
        self.assertEqual(manager.get(finished).status, "completed")
        self.assertEqual(manager.get(running.task_id).status, "error")
        self.assertEqual(manager.get(cancelled.task_id).status, "cancelled")


class TestDegradedStorage(RestartBase):
    def test_corrupt_database_starts_in_memory_mode(self):
        srv.manager.create("https://example.com/old")
        srv.storage.close()
        with open(self.db, "wb") as handle:
            handle.write(b"corrupted on purpose")

        srv.bootstrap(self.db)
        info = srv.storage_info()
        self.assertIsNone(srv.storage)
        self.assertEqual(info["kind"], "memory")
        self.assertTrue(info["degraded"])
        self.assertTrue(info["reason"])
        # 降级后仍然可以正常下载（内存模式），只是不持久化
        factory = install_factory(lambda: InstantEngine())
        try:
            task_id = srv.scheduler.submit("https://example.com/new")["task_id"]
            self.assertTrue(srv.scheduler.wait_idle(10))
            self.assertEqual(srv.manager.get(task_id).status, "completed")
        finally:
            restore_engine(factory)

    def test_memory_mode_is_not_marked_degraded(self):
        srv.bootstrap(":memory:")
        info = srv.storage_info()
        self.assertEqual(info["kind"], "sqlite")
        self.assertFalse(info["degraded"])
        self.assertEqual(info["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
