"""Stage-001 API + task model tests (stdlib only)."""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import server as srv


def is_valid_download_url(url):
    """Mirror of Handler /download validation (Stage-001 snapshot)."""
    if not url:
        return False
    return url.startswith("http://") or url.startswith("https://")


class TestTaskMemoryModel(unittest.TestCase):
    def test_legacy_compat_update_roundtrip(self):
        # Stage-002 keeps Stage-001's server._update/_get/_all working until
        # the migration below finishes; terminal states are frozen by the
        # new state machine.
        import uuid
        from core_task import Task
        tid = "t-" + str(uuid.uuid4())[:8]
        with srv.tasks_lock:
            srv.tasks[tid] = Task(task_id=tid, status="downloading",
                                  url="https://example.com/v").to_dict()
            srv.tasks[tid].update({"percent": 0.0, "speed": "", "eta": "",
                                   "created_at": "2026-09-19T00:00:00",
                                   "updated_at": "2026-09-19T00:00:00"})
        try:
            srv._update(tid, percent=8.8, speed="22.68KiB/s", eta="47:53")
            t = srv._get(tid)
            self.assertEqual(t["percent"], 8.8)
            self.assertEqual(t["speed"], "22.68KiB/s")
            self.assertIn(tid, srv._all())
            t["percent"] = 999
            self.assertNotEqual(srv._get(tid)["percent"], 999)
            srv._update(tid, status="completed", percent=100.0,
                        speed="", eta="")
            done = srv._get(tid)
            self.assertEqual(done["status"], "completed")
            self.assertEqual(done["percent"], 100.0)
            # completed is terminal: further transitions are refused
            srv._update(tid, status="error")
            self.assertEqual(srv._get(tid)["status"], "completed")
            json.dumps(srv._get(tid), ensure_ascii=False)
        finally:
            with srv.tasks_lock:
                srv.tasks.pop(tid, None)

    def test_get_missing_returns_none(self):
        self.assertIsNone(srv._get("no-such-task-id-xyz"))

    def test_task_ids_unique(self):
        import uuid
        ids = {str(uuid.uuid4())[:8] for _ in range(200)}
        self.assertEqual(len(ids), 200)


class TestUrlValidation(unittest.TestCase):
    def test_missing_url_invalid(self):
        self.assertFalse(is_valid_download_url(None))
        self.assertFalse(is_valid_download_url(""))

    def test_non_http_invalid(self):
        self.assertFalse(is_valid_download_url("ftp://example.com/x"))
        self.assertFalse(is_valid_download_url("javascript:alert(1)"))

    def test_http_ok(self):
        self.assertTrue(is_valid_download_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(is_valid_download_url("http://example.com/v.mp4"))


class TestLiveApi(unittest.TestCase):
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

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_health(self):
        code, body = self._get("/health")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["status"], "ok")

    def test_status_all_is_dict(self):
        code, body = self._get("/status")
        self.assertEqual(code, 200)
        self.assertIsInstance(json.loads(body.decode("utf-8")), dict)

    def test_status_missing_task_404(self):
        code, body404 = self._get("/status?id=no-such-task-id-xyz")
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(body404.decode("utf-8"))["error_code"],
                         "task_not_found")

    def test_download_missing_url_400(self):
        code, miss_body = self._get("/download")
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(miss_body.decode("utf-8"))["error_code"],
                         "missing_url")

    def test_download_invalid_url_400(self):
        code, bad_body = self._get("/download?url=ftp%3A%2F%2Fexample.com%2Fx")
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(bad_body.decode("utf-8"))["error_code"],
                         "invalid_url")


if __name__ == "__main__":
    unittest.main()
