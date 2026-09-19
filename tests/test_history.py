"""Stage-005 history/events/delete API tests (T519-T521). Real HTTP."""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server as srv
from tests.helpers import (CancelableEngine, InstantEngine, install_factory,
                           restore_engine)


def http_json(base, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers,
                                     method="POST" if payload is not None
                                     else "GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


class HistoryApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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

    def setUp(self):
        srv.bootstrap(":memory:")
        self._old_factory = install_factory(lambda: InstantEngine())

    def tearDown(self):
        for task_id in list(srv.scheduler.active_ids()) + \
                list(srv.scheduler.queued_ids()):
            try:
                srv.scheduler.cancel(task_id, timeout=5)
            except Exception:  # noqa: BLE001 - test cleanup only
                pass
        srv.scheduler.wait_idle(10)
        restore_engine(self._old_factory)

    def download(self, tag):
        code, body = http_json(
            self.base,
            f"/download?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D{tag}")
        self.assertEqual(code, 200, body)
        task_id = body["task_id"]
        self.assertTrue(srv.scheduler.wait_idle(20))
        return task_id

    def post(self, path, payload):
        return http_json(self.base, path, payload)


class TestHistoryEndpoint(HistoryApiBase):
    def test_empty_history(self):
        code, data = http_json(self.base, "/history")
        self.assertEqual(code, 200)
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["returned"], 0)
        self.assertEqual(data["limit"], 20)
        self.assertEqual(data["storage"]["kind"], "sqlite")

    def test_history_is_newest_first(self):
        first = self.download("h1")
        second = self.download("h2")
        code, data = http_json(self.base, "/history")
        self.assertEqual(code, 200)
        self.assertEqual([t["task_id"] for t in data["tasks"]],
                         [second, first])
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["returned"], 2)

    def test_history_limit_and_filter(self):
        self.download("h3")
        self.download("h4")
        code, data = http_json(self.base, "/history?limit=1")
        self.assertEqual((code, data["returned"], data["total"]), (200, 1, 2))
        code, data = http_json(self.base, "/history?status=completed")
        self.assertEqual((code, data["total"]), (200, 2))
        code, data = http_json(self.base, "/history?status=error")
        self.assertEqual((code, data["total"]), (200, 0))

    def test_invalid_history_params(self):
        for query, expected in (("?limit=0", "invalid_limit"),
                                ("?limit=abc", "invalid_limit"),
                                ("?limit=500", "invalid_limit"),
                                ("?status=downloading", "invalid_status")):
            code, data = http_json(self.base, "/history" + query)
            self.assertEqual(code, 400, query)
            self.assertEqual(data["error_code"], expected, query)


class TestEventsEndpoint(HistoryApiBase):
    def test_events_are_recorded_in_order(self):
        task_id = self.download("e1")
        code, data = http_json(self.base, f"/events?id={task_id}")
        self.assertEqual(code, 200)
        kinds = [event["kind"] for event in data["events"]]
        self.assertEqual(kinds[0], "created")
        self.assertIn("transition", kinds)
        self.assertEqual(data["returned"], len(data["events"]))
        ids = [event["id"] for event in data["events"]]
        self.assertEqual(ids, sorted(ids))

    def test_events_unknown_task(self):
        code, data = http_json(self.base, "/events?id=no-such-task")
        self.assertEqual(code, 404)
        self.assertEqual(data["error_code"], "task_not_found")

    def test_events_missing_and_invalid_id(self):
        code, data = http_json(self.base, "/events")
        self.assertEqual((code, data["error_code"]), (400, "missing_task_id"))
        code, data = http_json(self.base, "/events?id=../etc/passwd")
        self.assertEqual((code, data["error_code"]), (400, "invalid_task_id"))


class TestDeleteEndpoint(HistoryApiBase):
    def test_delete_finished_task(self):
        task_id = self.download("d1")
        code, data = self.post("/delete", {"task_id": task_id})
        self.assertEqual(code, 200, data)
        self.assertTrue(data["deleted"])
        code, _ = http_json(self.base, "/status?id=" + task_id)
        self.assertEqual(code, 404)
        _, history = http_json(self.base, "/history")
        self.assertEqual(history["tasks"], [])

    def test_delete_running_task_is_rejected(self):
        factory = install_factory(lambda: CancelableEngine(hold_seconds=20.0))
        try:
            code, body = http_json(
                self.base,
                "/download?url=https%3A%2F%2Fwww.youtube.com"
                "%2Fwatch%3Fv%3Drun")
            self.assertEqual(code, 200)
            task_id = body["task_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if srv.manager.get(task_id).status == "downloading":
                    break
                time.sleep(0.02)
            code, data = self.post("/delete", {"task_id": task_id})
            self.assertEqual(code, 409, data)
            self.assertEqual(data["error_code"], "not_deletable")
        finally:
            restore_engine(factory)

    def test_delete_unknown_and_invalid(self):
        code, data = self.post("/delete", {"task_id": "no-such-task"})
        self.assertEqual((code, data["error_code"]), (404, "task_not_found"))
        code, data = self.post("/delete", {})
        self.assertEqual((code, data["error_code"]), (400, "missing_task_id"))
        code, data = self.post("/delete", {"task_id": "a b/c"})
        self.assertEqual((code, data["error_code"]), (400, "invalid_task_id"))

    def test_health_and_tasks_expose_storage(self):
        code, health = http_json(self.base, "/health")
        self.assertEqual(code, 200)
        self.assertEqual(health["storage"]["kind"], "sqlite")
        self.assertEqual(health["storage"]["schema_version"], 2)
        self.assertFalse(health["storage"]["degraded"])
        code, tasks = http_json(self.base, "/tasks")
        self.assertEqual(code, 200)
        self.assertIn("storage", tasks)
        self.assertEqual(tasks["active_limit"], 3)

    def test_legacy_get_regression(self):
        for path in ("/health", "/status", "/tasks", "/history"):
            code, _ = http_json(self.base, path)
            self.assertEqual(code, 200, path)


if __name__ == "__main__":
    unittest.main()
