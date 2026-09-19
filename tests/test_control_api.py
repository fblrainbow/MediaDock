"""Stage-004 control API tests (T412-T419) over real HTTP + stub engines."""
import json
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import server as srv
from tests.helpers import (CancelableEngine, FailingEngine, InstantEngine,
                           install_factory, restore_engine)


def http_json(base, path, payload=None, method=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def wait_status(task_id, statuses, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = srv.manager.get(task_id)
        if task is not None and task.status in statuses:
            return task.status
        time.sleep(0.02)
    task = srv.manager.get(task_id)
    return task.status if task else None


class ControlApiBase(unittest.TestCase):
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
        self._old_factory = install_factory(
            lambda: CancelableEngine(hold_seconds=20.0))

    def tearDown(self):
        # Cancel everything (active first, then queued) until the scheduler is
        # idle; a freed slot can promote a queued task, so loop.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            pending = list(srv.scheduler.active_ids()) + \
                list(srv.scheduler.queued_ids())
            if not pending:
                break
            for task_id in pending:
                try:
                    srv.scheduler.cancel(task_id, timeout=5)
                except Exception:  # noqa: BLE001 - test cleanup only
                    pass
            time.sleep(0.05)
        restore_engine(self._old_factory)
        self.assertTrue(srv.scheduler.wait_idle(20),
                        f"scheduler not idle: {srv.scheduler.summary()}")
        for task_id in list(srv.manager.ids()):
            srv.manager.drop(task_id)

    def download(self, tag):
        code, body = http_json(self.base, f"/download?url=https%3A%2F%2F"
                                           f"www.youtube.com%2Fwatch%3Fv%3D{tag}")
        self.assertEqual(code, 200, body)
        return body["task_id"]

    def post(self, path, payload):
        return http_json(self.base, path, payload)


class TestControlSuccess(ControlApiBase):
    def test_pause_then_resume_roundtrip(self):
        task_id = self.download("pause1")
        self.assertEqual(wait_status(task_id, ("downloading",)), "downloading")
        code, data = self.post("/pause", {"task_id": task_id})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["status"], "paused")
        self.assertTrue(data["changed"])
        self.assertEqual(srv.manager.get(task_id).status, "paused")
        self.assertEqual(srv.scheduler.active_count(), 0)

        code, data = self.post("/resume", {"task_id": task_id})
        self.assertEqual(code, 200, data)
        self.assertEqual(wait_status(task_id, ("downloading",)), "downloading")
        self.assertGreaterEqual(srv.scheduler.active_count(), 1)

    def test_cancel_active_task(self):
        task_id = self.download("cancel1")
        self.assertEqual(wait_status(task_id, ("downloading",)), "downloading")
        code, data = self.post("/cancel", {"task_id": task_id})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(srv.manager.get(task_id).status, "cancelled")
        self.assertEqual(srv.scheduler.active_count(), 0)

    def test_cancel_queued_task_never_starts(self):
        ids = [self.download(f"q{i}") for i in range(4)]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(wait_status(ids[0], ("downloading",)), "downloading")
        self.assertIn(ids[3], srv.scheduler.queued_ids())
        code, data = self.post("/cancel", {"task_id": ids[3]})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["status"], "cancelled")
        self.assertNotIn(ids[3], srv.scheduler.queued_ids())
        self.assertEqual(srv.manager.get(ids[3]).status, "cancelled")
        time.sleep(0.2)
        self.assertEqual(srv.manager.get(ids[3]).status, "cancelled")

    def test_pause_releases_slot_and_refills_queue(self):
        ids = [self.download(f"s{i}") for i in range(4)]
        self.assertEqual(wait_status(ids[0], ("downloading",)), "downloading")
        self.assertEqual(srv.scheduler.active_count(), 3)
        self.assertEqual(srv.scheduler.queued_count(), 1)
        code, data = self.post("/pause", {"task_id": ids[0]})
        self.assertEqual(code, 200, data)
        self.assertEqual(wait_status(ids[3], ("downloading",)), "downloading")
        self.assertEqual(srv.scheduler.active_count(), 3)
        self.assertEqual(srv.scheduler.queued_count(), 0)

    def test_retry_after_error_completes(self):
        runs = {"n": 0}
        marker = srv.scheduler._engine_factory

        def factory():
            runs["n"] += 1
            if runs["n"] == 1:
                return FailingEngine()
            return InstantEngine()

        srv.scheduler.set_engine_factory(factory)
        try:
            task_id = self.download("retry1")
            self.assertEqual(wait_status(task_id, ("error",)), "error")
            code, data = self.post("/retry", {"task_id": task_id})
            self.assertEqual(code, 200, data)
            self.assertEqual(wait_status(task_id, ("completed",)), "completed")
            stored = srv.manager.get(task_id)
            self.assertEqual(stored.percent, 100.0)
            self.assertEqual(stored.error_code, "")
            self.assertEqual(stored.error_message, "")
            # 重试后重新完成：拿到新的完成序号而不是复用失败前的值
            self.assertIsNotNone(stored.completion_order)
        finally:
            srv.scheduler.set_engine_factory(marker)

    def test_control_is_isolated_to_target_task(self):
        first = self.download("iso1")
        second = self.download("iso2")
        self.assertEqual(wait_status(first, ("downloading",)), "downloading")
        self.assertEqual(wait_status(second, ("downloading",)), "downloading")
        before = srv.manager.get(second).to_dict()
        code, _ = self.post("/pause", {"task_id": first})
        self.assertEqual(code, 200)
        after = srv.manager.get(second).to_dict()
        self.assertEqual(after["status"], "downloading")
        self.assertEqual(after["percent"], before["percent"])
        self.assertEqual(after["file_path"], before["file_path"])

    def test_options_allows_post(self):
        request = urllib.request.Request(self.base + "/pause", method="OPTIONS")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("POST",
                          response.headers.get("Access-Control-Allow-Methods"))


class TestControlErrors(ControlApiBase):
    def test_unknown_task(self):
        code, data = self.post("/pause", {"task_id": "no-such-task-id"})
        self.assertEqual(code, 404)
        self.assertEqual(data["error_code"], "task_not_found")

    def test_missing_task_id(self):
        code, data = self.post("/pause", {})
        self.assertEqual(code, 400)
        self.assertEqual(data["error_code"], "missing_task_id")

    def test_invalid_task_id(self):
        code, data = self.post("/pause", {"task_id": "../../etc/passwd"})
        self.assertEqual(code, 400)
        self.assertEqual(data["error_code"], "invalid_task_id")

    def test_bad_json(self):
        request = urllib.request.Request(
            self.base + "/pause", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertEqual(json.loads(exc.read().decode("utf-8"))["error_code"],
                             "bad_request")

    def test_empty_body(self):
        request = urllib.request.Request(self.base + "/pause", data=b"",
                                         method="POST")
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertEqual(json.loads(exc.read().decode("utf-8"))["error_code"],
                             "bad_request")

    def test_json_array_body(self):
        code, data = http_json(self.base, "/pause", ["nope"])
        self.assertEqual(code, 400)
        self.assertEqual(data["error_code"], "bad_request")

    def test_pause_queued_task_is_rejected(self):
        queued = None
        for i in range(4):
            task_id = self.download(f"np{i}")
            if i == 3:
                queued = task_id
        self.assertEqual(srv.scheduler.queued_count(), 1)
        code, data = self.post("/pause", {"task_id": queued})
        self.assertEqual(code, 409)
        self.assertEqual(data["error_code"], "not_pausable")

    def test_resume_non_paused_is_rejected(self):
        task_id = self.download("nr1")
        self.assertEqual(wait_status(task_id, ("downloading",)), "downloading")
        code, data = self.post("/resume", {"task_id": task_id})
        self.assertEqual(code, 409)
        self.assertEqual(data["error_code"], "not_resumable")

    def test_cancel_completed_is_rejected(self):
        task_id = self.download("nc1")
        self.assertEqual(wait_status(task_id, ("downloading",)), "downloading")
        self.assertEqual(self.post("/cancel", {"task_id": task_id})[0], 200)
        code, data = self.post("/cancel", {"task_id": task_id})
        self.assertEqual(code, 409)
        self.assertEqual(data["error_code"], "not_cancellable")

    def test_retry_running_is_rejected(self):
        task_id = self.download("ny1")
        self.assertEqual(wait_status(task_id, ("downloading",)), "downloading")
        code, data = self.post("/retry", {"task_id": task_id})
        self.assertEqual(code, 409)
        self.assertEqual(data["error_code"], "not_retryable")

    def test_unknown_control_path(self):
        code, data = self.post("/purge", {"task_id": "abc"})
        self.assertEqual(code, 404)
        self.assertEqual(data["error_code"], "not_found")

    def test_get_regression_still_works(self):
        for path in ("/health", "/status", "/tasks"):
            code, data = http_json(self.base, path)
            self.assertEqual(code, 200, path)


if __name__ == "__main__":
    unittest.main()
