"""Stage-003 task list ordering tests (T309) and API tests (T310-T312)."""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import server as srv
from core_listing import build_task_list, sort_tasks
from core_manager import TaskManager
from core_scheduler import Scheduler
from tests.helpers import (InstantEngine, drop_when_terminal, install_engine,
                           restore_engine)


def task(task_id, status="downloading", percent=0.0, created_at="",
         completed_at="", completion_order=0):
    return {"task_id": task_id, "status": status, "percent": percent,
            "created_at": created_at, "completed_at": completed_at,
            "completion_order": completion_order}


class TestSorting(unittest.TestCase):
    def test_unfinished_before_completed(self):
        tasks = [task("done", "completed", 100.0),
                 task("live", "downloading", 5.0)]
        self.assertEqual([t["task_id"] for t in sort_tasks(tasks)],
                         ["live", "done"])

    def test_unfinished_sorted_by_percent_desc(self):
        tasks = [task("a", "downloading", 10.0),
                 task("b", "downloading", 80.0),
                 task("c", "downloading", 40.0)]
        self.assertEqual([t["task_id"] for t in sort_tasks(tasks)],
                         ["b", "c", "a"])

    def test_pending_and_error_have_zero_percent(self):
        tasks = [task("p", "pending", 0.0, created_at="2026-09-19T00:00:01"),
                 task("e", "error", 0.0, created_at="2026-09-19T00:00:02"),
                 task("d", "downloading", 0.5)]
        self.assertEqual([t["task_id"] for t in sort_tasks(tasks)],
                         ["d", "e", "p"])

    def test_paused_and_cancelled_sort_as_zero_progress(self):
        # Stage-004: 暂停/取消没有实时进度，即使存有 percent 也按 0% 参与排序
        tasks = [task("paused-high", "paused", 90.0,
                      created_at="2026-09-19T00:00:01"),
                 task("live-low", "downloading", 1.0),
                 task("cancelled", "cancelled", 50.0,
                      created_at="2026-09-19T00:00:02")]
        self.assertEqual([t["task_id"] for t in sort_tasks(tasks)],
                         ["live-low", "cancelled", "paused-high"])

    def test_completed_sorted_by_completed_at_desc(self):
        tasks = [task("old", "completed", 100.0,
                      completed_at="2026-09-19T00:00:01"),
                 task("new", "completed", 100.0,
                      completed_at="2026-09-19T00:00:09")]
        self.assertEqual([t["task_id"] for t in sort_tasks(tasks)],
                         ["new", "old"])

    def test_completion_order_breaks_same_second_ties(self):
        tasks = [task("first", "completed", 100.0,
                      completed_at="2026-09-19T00:00:05", completion_order=1),
                 task("second", "completed", 100.0,
                      completed_at="2026-09-19T00:00:05", completion_order=2)]
        self.assertEqual([t["task_id"] for t in sort_tasks(tasks)],
                         ["second", "first"])

    def test_task_id_is_stable_tie_breaker(self):
        # 契约 5.4 第 5 条只要求稳定：同 percent、同 created_at 时按
        # `task_id` 升序，DOM 行不因取数顺序不同而跳动到别的任务。
        tasks = [task("zz", "downloading", 50.0, created_at="2026-09-19T00:00:01"),
                 task("aa", "downloading", 50.0, created_at="2026-09-19T00:00:01")]
        self.assertEqual([t["task_id"] for t in sort_tasks(tasks)], ["aa", "zz"])
        again = sort_tasks(list(reversed(tasks)))
        self.assertEqual([t["task_id"] for t in again], ["aa", "zz"])

    def test_sort_does_not_mutate_input(self):
        tasks = [task("b", "downloading", 10.0), task("a", "downloading", 90.0)]
        before = [dict(t) for t in tasks]
        sort_tasks(tasks)
        self.assertEqual(tasks, before)

    def test_sort_tolerates_bad_rows(self):
        tasks = [None, "junk", task("ok", "downloading", 1.0)]
        self.assertEqual([t["task_id"] for t in sort_tasks(tasks)], ["ok"])


class GatedEngine:
    """Engine whose completion is held open until the test releases it."""

    def __init__(self, gate, manager):
        self._gate = gate
        self._manager = manager

    def run(self, task_id, url, control=None):
        self._manager.transition(task_id, "downloading")
        self._gate.wait(10)
        self._manager.transition(task_id, "completed", percent=100.0)


class TestListPayload(unittest.TestCase):
    def test_payload_has_tasks_and_scheduler_counters(self):
        manager = TaskManager()
        scheduler = Scheduler(manager, lambda: None, logger=lambda *a: None)
        manager.create("https://example.com/a")
        payload = build_task_list(manager, scheduler)
        self.assertEqual(sorted(payload.keys()),
                         ["active_count", "active_limit", "queued_count",
                          "tasks"])
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertEqual(payload["active_limit"], 3)
        self.assertEqual(payload["active_count"], 0)
        self.assertEqual(payload["queued_count"], 0)

    def test_counts_track_active_and_queued_tasks(self):
        manager = TaskManager()
        gate = threading.Event()
        scheduler = Scheduler(manager, lambda: GatedEngine(gate, manager),
                              logger=lambda *a: None)
        for i in range(5):
            task = manager.create(f"https://example.com/{i}")
            scheduler.admit(task.task_id, task.url)
        payload = build_task_list(manager, scheduler)
        self.assertEqual(payload["active_limit"], 3)
        self.assertEqual(payload["active_count"], 3)
        self.assertEqual(payload["queued_count"], 2)
        for name in ("active_count", "queued_count"):
            self.assertEqual(payload[name], scheduler.summary()[name])
        try:
            for item in payload["tasks"]:
                self.assertIn(item["status"], ("pending", "downloading"))
            self.assertEqual(
                len([t for t in payload["tasks"] if t["status"] == "pending"]), 2)
        finally:
            gate.set()
            self.assertTrue(scheduler.wait_idle(10))
            self.assertEqual(scheduler.active_count(), 0)
            self.assertEqual(scheduler.queued_count(), 0)


class TestTasksApi(unittest.TestCase):
    """`GET /tasks` over real HTTP, with the process spawn stubbed out."""

    @classmethod
    def setUpClass(cls):
        cls._old_factory = install_engine(InstantEngine)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        restore_engine(cls._old_factory)

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _json(self, path):
        code, body = self._get(path)
        return code, json.loads(body.decode("utf-8"))

    def test_tasks_endpoint_shape(self):
        code, data = self._json("/tasks")
        self.assertEqual(code, 200)
        self.assertIsInstance(data["tasks"], list)
        self.assertEqual(data["active_limit"], 3)
        self.assertIn("active_count", data)
        self.assertIn("queued_count", data)

    def test_multi_download_returns_distinct_ids(self):
        ids = []
        try:
            for i in range(4):
                code, body = self._get(
                    f"/download?url=https%3A%2F%2Fwww.youtube.com"
                    f"%2Fwatch%3Fv%3Dm{i}")
                self.assertEqual(code, 200)
                ids.append(json.loads(body.decode("utf-8"))["task_id"])
            self.assertEqual(len(set(ids)), 4)
            _, data = self._json("/tasks")
            listed = {t["task_id"] for t in data["tasks"]}
            for tid in ids:
                self.assertIn(tid, listed)
                code, one = self._json("/status?id=" + tid)
                self.assertEqual(code, 200)
                self.assertEqual(one["task_id"], tid)
            # 契约 5.1：返回后可立即查询，且最终进入终态、槽位不泄漏
            self.assertTrue(srv.scheduler.wait_idle(15))
            self.assertEqual(srv.scheduler.active_count(), 0)
            self.assertEqual(srv.scheduler.queued_count(), 0)
            _, data = self._json("/tasks")
            final = {t["task_id"]: t["status"] for t in data["tasks"]}
            for tid in ids:
                self.assertEqual(final.get(tid), "completed")
            self.assertEqual(data["active_count"], 0)
            self.assertEqual(data["queued_count"], 0)
        finally:
            for tid in ids:
                drop_when_terminal(tid)

    def test_status_single_task_matches_tasks_list(self):
        code, body = self._get(
            "/download?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dsolo")
        self.assertEqual(code, 200)
        tid = json.loads(body.decode("utf-8"))["task_id"]
        try:
            _, data = self._json("/tasks")
            listed = [t for t in data["tasks"] if t["task_id"] == tid]
            self.assertEqual(len(listed), 1)
            _, one = self._json("/status?id=" + tid)
            self.assertEqual(one["status"], listed[0]["status"])
            self.assertEqual(one["percent"], listed[0]["percent"])
        finally:
            drop_when_terminal(tid)

    def test_legacy_status_unknown_task_still_404(self):
        code, data = self._json("/status?id=no-such-task-id-xyz")
        self.assertEqual(code, 404)
        self.assertEqual(data["error_code"], "task_not_found")

    def test_tasks_counters_match_scheduler(self):
        code, data = self._json("/tasks")
        self.assertEqual(code, 200)
        self.assertEqual(data["active_count"], srv.scheduler.active_count())
        self.assertEqual(data["queued_count"], srv.scheduler.queued_count())
        self.assertEqual(data["active_limit"], srv.scheduler.active_limit)
        self.assertEqual(data["active_count"] + data["queued_count"],
                         len(srv.scheduler.active_ids())
                         + len(srv.scheduler.queued_ids()))

    def test_legacy_status_all_still_works(self):
        code, data = self._json("/status")
        self.assertEqual(code, 200)
        self.assertIsInstance(data, dict)

    def test_unknown_path_still_json(self):
        code, data = self._json("/nope")
        self.assertEqual(code, 404)
        self.assertEqual(data["error_code"], "not_found")


if __name__ == "__main__":
    unittest.main()