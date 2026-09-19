"""Stage-006 HTTP hardening + logging (T614, T620-T625, T627). Real HTTP."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import core_config
import server as srv
from tests.helpers import (InstantEngine, install_factory, restore_engine,
                           wait_terminal)


def raw_request(port, path="/health", method="GET", host=None, origin=None,
                headers=None, body=b"", content_length=None, timeout=15):
    """Send a request with full control over Host/Origin/Content-Length."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=host is not None,
                        skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        if origin is not None:
            conn.putheader("Origin", origin)
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        if content_length is not None:
            conn.putheader("Content-Length", str(content_length))
        conn.endheaders()
        if body:
            conn.send(body)
        response = conn.getresponse()
        raw = response.read().decode("utf-8", "replace")
        return response.status, raw
    finally:
        conn.close()


def json_body(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


class SecApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mediadock-sec-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        srv.bootstrap(":memory:", config=core_config.load_config(env={}))
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
        srv.bootstrap(":memory:", config=core_config.load_config(env={}))

    def get(self, path, **kwargs):
        return raw_request(self.port, path, **kwargs)


class TestHostGuard(SecApiBase):
    def test_foreign_host_is_rejected(self):
        """T622: only loopback Host values reach the API."""
        for host in ("evil.com", "evil.com:8765", "0.0.0.0:8765",
                     "192.168.1.10"):
            code, raw = self.get("/health", host=host)
            self.assertEqual(code, 403, host)
            self.assertEqual(json_body(raw)["error_code"], "forbidden_host")

    def test_loopback_hosts_are_accepted(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}",
                     f"[::1]:{self.port}", "127.0.0.1"):
            code, raw = self.get("/health", host=host)
            self.assertEqual(code, 200, f"{host}: {raw}")

    def test_guard_covers_post_and_options(self):
        code, _ = raw_request(self.port, "/pause", method="POST",
                              host="evil.com", body=b"{}",
                              headers={"Content-Type": "application/json"},
                              content_length=2)
        self.assertEqual(code, 403)
        code, _ = raw_request(self.port, "/pause", method="OPTIONS",
                              host="evil.com")
        self.assertEqual(code, 403)


class TestOriginGuard(SecApiBase):
    def test_absent_origin_passes(self):
        code, _ = self.get("/health")
        self.assertEqual(code, 200)

    def test_youtube_origins_pass(self):
        """T623: the userscript's own origin keeps working."""
        for origin in ("https://www.youtube.com", "https://youtu.be",
                       "https://m.youtube.com"):
            code, raw = self.get("/health", origin=origin)
            self.assertEqual(code, 200, f"{origin}: {raw}")

    def test_foreign_origin_is_rejected(self):
        for origin in ("https://evil.com", "null", "*",
                       "chrome-extension://abcdef"):
            code, raw = self.get("/health", origin=origin)
            self.assertEqual(code, 403, origin)
            self.assertEqual(json_body(raw)["error_code"], "forbidden_origin")

    def test_post_from_youtube_origin_works(self):
        code, raw = raw_request(
            self.port, "/pause", method="POST",
            origin="https://www.youtube.com",
            headers={"Content-Type": "application/json"}, body=b"{}",
            content_length=2)
        self.assertEqual(code, 400)
        self.assertEqual(json_body(raw)["error_code"], "missing_task_id")


class TestBodyLimit(SecApiBase):
    def test_oversized_body_is_rejected(self):
        """T614: Content-Length above `request_max_bytes` => 413."""
        limit = srv.CONFIG.request_max_bytes
        code, raw = raw_request(
            self.port, "/pause", method="POST",
            headers={"Content-Type": "application/json"},
            content_length=limit + 1)
        self.assertEqual(code, 413)
        body = json_body(raw)
        self.assertEqual(body["error_code"], "payload_too_large")
        self.assertIn(str(limit), body["message"])

    def test_body_at_the_limit_is_still_validated(self):
        payload = b"{}"
        code, raw = raw_request(
            self.port, "/pause", method="POST",
            headers={"Content-Type": "application/json"}, body=payload,
            content_length=len(payload))
        self.assertEqual(code, 400)
        self.assertEqual(json_body(raw)["error_code"], "missing_task_id")


class TestDownloadValidation(SecApiBase):
    def test_control_characters_are_rejected(self):
        """T609/T630: no weird URL reaches the yt-dlp argv."""
        url = quote("https://example.com/a\tb", safe="")
        code, raw = self.get(f"/download?url={url}")
        self.assertEqual(code, 400)
        self.assertEqual(json_body(raw)["error_code"], "invalid_url")

    def test_overlong_url_is_rejected(self):
        url = quote("https://example.com/" + "a" * 3000, safe="")
        code, raw = self.get(f"/download?url={url}")
        self.assertEqual(code, 400)
        self.assertEqual(json_body(raw)["error_code"], "invalid_url")

    def test_script_scheme_is_rejected(self):
        url = quote("javascript:alert(1)", safe="")
        code, raw = self.get(f"/download?url={url}")
        self.assertEqual(code, 400)
        self.assertEqual(json_body(raw)["error_code"], "invalid_url")

    def test_valid_url_still_downloads(self):
        """T627: hardening must not break the normal flow."""
        url = quote("https://example.com/ok", safe="")
        code, raw = self.get(f"/download?url={url}")
        self.assertEqual(code, 200, raw)
        task_id = json_body(raw)["task_id"]
        self.assertTrue(wait_terminal(task_id))
        self.assertEqual(srv.manager.get(task_id).status, "completed")
        self.assertTrue(srv.manager.drop(task_id))


class TestHealthDiagnostics(SecApiBase):
    def test_health_reports_config_and_dependencies(self):
        """T620: config + dependency diagnostics are visible over HTTP."""
        code, raw = self.get("/health")
        self.assertEqual(code, 200)
        data = json_body(raw)
        self.assertEqual(data["status"], "ok")
        self.assertIn("storage", data)
        self.assertEqual(data["config"]["source"], "defaults")
        self.assertEqual(sorted(data["config"]),
                         ["errors", "ok", "path", "source", "values",
                          "warnings"])
        self.assertEqual(data["config"]["values"]["port"], 8765)
        names = [c["name"] for c in data["dependencies"]["checks"]]
        self.assertEqual(names, ["yt-dlp", "ffmpeg", "download_dir",
                                 "disk_space"])
        self.assertNotIn(os.path.expanduser("~"), raw)

    def test_config_errors_are_visible_and_service_still_works(self):
        """T621: a broken config is reported but does not stop the server."""
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"host": "0.0.0.0", "port": "abc",
                       "download_dir": self.dir}, handle)
        config = core_config.load_config(path=path, env={})
        srv.bootstrap(":memory:", config=config)
        self.assertEqual(srv.CONFIG.host, "127.0.0.1")
        code, raw = self.get("/health")
        self.assertEqual(code, 200)
        errors = json_body(raw)["config"]["errors"]
        self.assertTrue(any("not a loopback" in e for e in errors))
        self.assertTrue(any("port" in e for e in errors))
        self.assertFalse(json_body(raw)["config"]["ok"])


class TestLogging(SecApiBase):
    """T624/T625: level filtering, redaction and the configured log file."""

    def _reload(self, level):
        log_path = os.path.join(self.dir, "MediaDock.log")
        cfg_path = os.path.join(self.dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump({"log_file": log_path, "log_level": level,
                       "download_dir": self.dir}, handle)
        srv.reload_config(path=cfg_path)
        self.assertEqual(srv.LOG_FILE, log_path)
        return log_path

    def _read_log(self, path):
        srv._log_fp.flush()
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_level_filtering(self):
        log_path = self._reload("warning")
        srv.log("INFO-LEVEL-MARKER")
        srv.log("DEBUG-LEVEL-MARKER", level="debug")
        srv.log("WARN-LEVEL-MARKER", level="warning")
        text = self._read_log(log_path)
        self.assertNotIn("INFO-LEVEL-MARKER", text)
        self.assertNotIn("DEBUG-LEVEL-MARKER", text)
        self.assertIn("WARN-LEVEL-MARKER", text)

    def test_debug_level_keeps_info(self):
        log_path = self._reload("debug")
        srv.log("INFO-AFTER-DEBUG")
        self.assertIn("INFO-AFTER-DEBUG", self._read_log(log_path))

    def test_redaction_reaches_the_file(self):
        log_path = self._reload("info")
        home = os.path.expanduser("~")
        srv.log("probe url https://example.com/v?token=SECRET123&v=abc "
                f"under {os.path.join(home, 'Videos', 'clip.mp4')}")
        text = self._read_log(log_path)
        self.assertNotIn("SECRET123", text)
        self.assertIn("token=***", text)
        self.assertIn("v=abc", text)
        self.assertNotIn(home, text)
        self.assertIn("clip.mp4", text)

    def test_long_lines_are_clipped(self):
        log_path = self._reload("info")
        srv.log("LONG-MARKER-" + "x" * 9000)
        text = self._read_log(log_path)
        longest = max(len(line) for line in text.splitlines())
        self.assertLessEqual(longest, srv.CONFIG.log_line_max)


if __name__ == "__main__":
    unittest.main()
