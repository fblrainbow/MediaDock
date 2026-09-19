"""Stage-005 storage tests (T501-T510, T522). No network, no real db file."""
import os
import sqlite3
import tempfile
import unittest

from core_store import (MIGRATIONS, Store, StoreError, TaskPersister,
                        open_store, resolve_db_path, restore_backup)


def tmp_path():
    directory = tempfile.mkdtemp(prefix="mediadock-store-")
    return os.path.join(directory, "tasks.db")


def task_dict(task_id="t1", **overrides):
    data = {
        "task_id": task_id, "type": "download", "status": "completed",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "platform": "youtube", "title": "Some Video", "percent": 100.0,
        "speed": "", "eta": "", "file_path": "downloads/x.mp4",
        "error_code": "", "error_message": "",
        "created_at": "2026-09-19T10:00:00", "started_at": "2026-09-19T10:00:01",
        "updated_at": "2026-09-19T10:00:09",
        "completed_at": "2026-09-19T10:00:09", "completion_order": 1,
    }
    data.update(overrides)
    return data


def open_store_at(path):
    store = Store(path, logger=lambda *a: None)
    store.connect()
    return store


class TestMigrations(unittest.TestCase):
    def test_first_initialize_creates_schema(self):
        path = tmp_path()
        store = open_store_at(path)
        try:
            version = store.initialize()
            self.assertEqual(version, store.target_version())
            self.assertEqual(store.version, version)
            names = {row[0] for row in store.connect().execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("tasks", "task_events", "schema_version"):
                self.assertIn(table, names)
            indexes = {row[0] for row in store.connect().execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            for index in ("idx_tasks_status", "idx_tasks_completed_at",
                          "idx_tasks_created_at"):
                self.assertIn(index, indexes)
        finally:
            store.close()

    def test_initialize_is_repeatable(self):
        path = tmp_path()
        store = open_store_at(path)
        try:
            first = store.initialize()
            store.save_task(task_dict())
            second = store.initialize()
            self.assertEqual(first, second)
            self.assertEqual(store.count_tasks(), 1)
        finally:
            store.close()

    def test_upgrade_from_v1_writes_backup(self):
        path = tmp_path()
        store = open_store_at(path)
        try:
            self.assertEqual(store.initialize(MIGRATIONS[:1]), 1)
            store.save_task(task_dict())
        finally:
            store.close()
        upgraded = open_store_at(path)
        try:
            self.assertEqual(upgraded.initialize(), upgraded.target_version())
            self.assertTrue(os.path.isfile(path + ".bak"))
            self.assertEqual(upgraded.count_tasks(), 1)
        finally:
            upgraded.close()

    def test_migration_failure_keeps_old_version(self):
        path = tmp_path()
        store = open_store_at(path)
        try:
            store.initialize(MIGRATIONS[:1])
            bad = [(1, MIGRATIONS[0][1]), (2, ("THIS IS NOT VALID SQL",))]
            with self.assertRaises(StoreError):
                store.initialize(bad)
            self.assertEqual(store.version, 1)
            self.assertTrue(store.degraded)
            self.assertEqual(store._current_version(), 1)
            # 修复后仍可正常升级，坏迁移没有留下半成品
            self.assertEqual(store.initialize(), store.target_version())
        finally:
            store.close()

    def test_missing_sqlite_path_degrades(self):
        directory = tempfile.mkdtemp(prefix="mediadock-store-")
        store, reason = open_store(os.path.join(directory, "sub", "nope.db"))
        self.assertIsNone(store)
        self.assertTrue(reason)


class TestTaskRoundTrip(unittest.TestCase):
    def test_save_and_load_all_fields(self):
        store = open_store_at(":memory:")
        try:
            store.initialize()
            data = task_dict()
            self.assertTrue(store.save_task(data))
            loaded = store.load_tasks()
            self.assertEqual(set(loaded), {"t1"})
            for column in ("task_id", "type", "status", "url", "platform",
                           "title", "speed", "eta", "file_path", "error_code",
                           "error_message", "created_at", "started_at",
                           "updated_at", "completed_at"):
                self.assertEqual(loaded["t1"][column], data[column], column)
            self.assertEqual(loaded["t1"]["percent"], 100.0)
            self.assertEqual(loaded["t1"]["completion_order"], 1)
        finally:
            store.close()

    def test_save_is_upsert(self):
        store = open_store_at(":memory:")
        try:
            store.initialize()
            store.save_task(task_dict())
            store.save_task(task_dict(status="error", error_code="exit_code",
                                      completion_order=None))
            self.assertEqual(store.count_tasks(), 1)
            loaded = store.load_tasks()["t1"]
            self.assertEqual(loaded["status"], "error")
            self.assertEqual(loaded["error_code"], "exit_code")
            self.assertIsNone(loaded["completion_order"])
        finally:
            store.close()

    def test_save_rejects_row_without_id(self):
        store = open_store_at(":memory:")
        try:
            store.initialize()
            self.assertFalse(store.save_task({"status": "completed"}))
            self.assertFalse(store.save_task(None))
            self.assertEqual(store.count_tasks(), 0)
        finally:
            store.close()

    def test_write_failure_is_swallowed(self):
        store = open_store_at(":memory:")
        store.initialize()
        store.close()
        # 关闭后重连得到的是空库，写回必须失败但不抛异常
        self.assertFalse(store.save_task(task_dict()))
        self.assertTrue(store.degraded)
        self.assertEqual(store.load_tasks(), {})

    def test_events_are_ordered(self):
        store = open_store_at(":memory:")
        try:
            store.initialize()
            store.record_event("t1", "created", "pending")
            store.record_event("t1", "transition", "pending->downloading")
            store.record_event("t1", "restart_interrupted", "downloading")
            store.record_event("t2", "created", "pending")
            events = store.events("t1")
            self.assertEqual([e["kind"] for e in events],
                             ["created", "transition", "restart_interrupted"])
            self.assertEqual([e["id"] for e in events],
                             sorted(e["id"] for e in events))
            self.assertEqual(len(store.events()), 4)
            self.assertTrue(store.has_events("t1"))
            self.assertFalse(store.has_events("nope"))
        finally:
            store.close()


class TestBackupRestoreAndCleanup(unittest.TestCase):
    def test_restore_backup_recovers_damaged_file(self):
        path = tmp_path()
        store = open_store_at(path)
        try:
            store.initialize()
            store.save_task(task_dict())
            self.assertTrue(os.path.isfile(store.backup()))
        finally:
            store.close()
        with open(path, "wb") as handle:
            handle.write(b"this is not a sqlite database at all")
        self.assertTrue(restore_backup(path))
        recovered = open_store_at(path)
        try:
            self.assertEqual(recovered.initialize(), recovered.target_version())
            self.assertIn("t1", recovered.load_tasks())
        finally:
            recovered.close()

    def test_restore_without_backup_is_noop(self):
        self.assertFalse(restore_backup(tmp_path()))

    def test_memory_backup_is_none(self):
        store = open_store_at(":memory:")
        try:
            store.initialize()
            self.assertIsNone(store.backup())
            self.assertFalse(restore_backup(":memory:"))
        finally:
            store.close()

    def test_delete_task_removes_row_and_events(self):
        store = open_store_at(":memory:")
        try:
            store.initialize()
            store.save_task(task_dict())
            store.record_event("t1", "created", "pending")
            self.assertTrue(store.delete_task("t1"))
            self.assertEqual(store.count_tasks(), 0)
            # 删除后只留下审计事件，原事件行被清掉
            self.assertEqual([e["kind"] for e in store.events("t1")],
                             ["deleted"])
            self.assertFalse(store.delete_task("t1"))
        finally:
            store.close()

    def test_purge_terminal_keeps_newest(self):
        store = open_store_at(":memory:")
        try:
            store.initialize()
            for index in range(5):
                store.save_task(task_dict(
                    task_id=f"t{index}", status="completed",
                    completed_at=f"2026-09-19T10:00:0{index}",
                    completion_order=index + 1))
                store.record_event(f"t{index}", "created", "pending")
            store.save_task(task_dict(task_id="live", status="downloading",
                                      completed_at="", completion_order=None))
            self.assertEqual(store.purge_terminal(keep=2), 3)
            self.assertEqual(set(store.load_tasks()), {"t3", "t4", "live"})
            # 清理不留孤儿事件（不写审计、原事件一并删除）
            self.assertFalse(store.has_events("t0"))
            self.assertTrue(store.has_events("t4"))
            self.assertEqual(len(store.events()), 2)
        finally:
            store.close()


class TestPersisterAndPath(unittest.TestCase):
    def test_resolve_db_path_precedence(self):
        self.assertEqual(resolve_db_path("X:/tmp/a.db"), "X:/tmp/a.db")
        old = os.environ.get("MEDIADOCK_DB")
        os.environ["MEDIADOCK_DB"] = ":memory:"
        try:
            self.assertEqual(resolve_db_path(), ":memory:")
        finally:
            if old is None:
                os.environ.pop("MEDIADOCK_DB", None)
            else:
                os.environ["MEDIADOCK_DB"] = old

    def test_persister_without_store_is_safe(self):
        persister = TaskPersister(None)
        self.assertFalse(persister.save_task(task_dict()))
        self.assertFalse(persister.delete_task("t1"))
        self.assertFalse(persister.record_event("t1", "created"))

    def test_open_store_success_reports_version(self):
        path = tmp_path()
        store, reason = open_store(path)
        try:
            self.assertIsNotNone(store)
            self.assertEqual(reason, "")
            self.assertEqual(store.version, store.target_version())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
