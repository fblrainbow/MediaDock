"""Stage-002 API regression tests: JSON errors + compat (T212-T216)."""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import server as srv
from tests.helpers import (InstantEngine, drop_when_terminal, install_engine,
                           restore_engine)


class TestLiveApiV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Stage-003: 单元测试不启动真实 yt-dlp，也不依赖网络
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

    def test_health(self):
        code, data = self._json("/health")
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "ok")

    def test_missing_url_json(self):
        code, data = self._json("/download")
        self.assertEqual(code, 400)
        self.assertEqual(data["error_code"], "missing_url")
        self.assertIn("message", data)

    def test_invalid_url_json(self):
        code, data = self._json("/download?url=ftp%3A%2F%2Fexample.com%2Fx")
        self.assertEqual(code, 400)
        self.assertEqual(data["error_code"], "invalid_url")

    def test_unknown_task_json(self):
        code, data = self._json("/status?id=no-such-task-id-xyz")
        self.assertEqual(code, 404)
        self.assertEqual(data["error_code"], "task_not_found")
        self.assertEqual(data["task_id"], "no-such-task-id-xyz")

    def test_status_all_dict(self):
        code, data = self._json("/status")
        self.assertEqual(code, 200)
        self.assertIsInstance(data, dict)

    def test_legacy_fields_present(self):
        code, body = self._get(
            "/download?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dlegacyv")
        self.assertEqual(code, 200)
        tid = json.loads(body.decode("utf-8"))["task_id"]
        try:
            _, data = self._json("/status?id=" + tid)
            for name in ("status", "percent", "speed", "eta", "url",
                         "title", "created_at", "updated_at"):
                self.assertIn(name, data, name)
        finally:
            drop_when_terminal(tid)

    def test_unknown_path_json(self):
        code, data = self._json("/nope")
        self.assertEqual(code, 404)
        self.assertEqual(data["error_code"], "not_found")


if __name__ == "__main__":
    unittest.main()
