"""Stage-010 release tests (R1001-R1009).

Static half: version truth, userscript/changelog parsing, required files and
`config.example.json` parity — no server involved.

Runtime half: `/health.version` served by the real `server.Handler`.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server as srv
from core_config import DEFAULTS
from core_release import (APP_VERSION, API_VERSION, REQUIRED_DOCS,
                          USERSCRIPT_VERSION, changelog_latest,
                          changelog_versions, config_key_report, failed_names,
                          format_report, is_valid_version, latest_changelog_version,
                          missing_docs, parse_userscript_version, read_text,
                          release_info, static_checks, userscript_version,
                          version_tuple)
from core_store import MIGRATIONS
from tests.helpers import InstantEngine, install_factory, restore_engine

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 目标 schema 版本 = 最高一条迁移；发布检查用它断言 /health.version.schema
SCHEMA_VERSION = max(version for version, _ in MIGRATIONS)


def http_json(base, path):
    request = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


class TestVersionModel(unittest.TestCase):
    """R1001: `X.Y.Z` shape and ordering."""

    def test_valid_versions(self):
        for text in ("0.0.1", "1.0.0", "10.20.30"):
            self.assertTrue(is_valid_version(text), text)

    def test_invalid_versions(self):
        for text in ("", None, "1", "1.0", "v1.0.0", "1.0.0.0", "1.0.0-beta",
                     "abc"):
            self.assertFalse(is_valid_version(text), repr(text))

    def test_surrounding_whitespace_is_ignored(self):
        # 配置文件/环境变量里的版本字符串可能带空白，读取时统一去掉
        self.assertTrue(is_valid_version(" 1.0.0 "))

    def test_version_tuple_ordering(self):
        self.assertEqual(version_tuple("1.2.3"), (1, 2, 3))
        self.assertEqual(version_tuple("bad"), ())
        self.assertGreater(version_tuple("1.10.0"), version_tuple("1.9.9"))

    def test_app_version_is_valid(self):
        self.assertTrue(is_valid_version(APP_VERSION), APP_VERSION)


class TestUserscriptVersion(unittest.TestCase):
    """R1002: `@version` parsing and release alignment."""

    def test_parses_header(self):
        source = "// ==UserScript==\n// @version      9.9\n// ==/UserScript==\n"
        self.assertEqual(parse_userscript_version(source), "9.9")

    def test_missing_header(self):
        self.assertEqual(parse_userscript_version("// @name x\n"), "")
        self.assertEqual(parse_userscript_version(""), "")

    def test_repo_userscript_matches_release(self):
        path = os.path.join(BASE_DIR, "MediaDock.js")
        self.assertEqual(userscript_version(path), USERSCRIPT_VERSION)


class TestChangelog(unittest.TestCase):
    """R1003: changelog parsing."""

    def test_bracketed_and_plain_headings(self):
        text = "# 变更日志\n\n## [2.0.0] - 2026-10-01\n\n## 1.0.0\n"
        self.assertEqual(changelog_versions(text), ["2.0.0", "1.0.0"])
        self.assertEqual(latest_changelog_version(text), "2.0.0")

    def test_empty_changelog(self):
        self.assertEqual(latest_changelog_version(""), "")
        self.assertEqual(changelog_versions("# 变更日志\n"), [])

    def test_repo_changelog_matches_app_version(self):
        path = os.path.join(BASE_DIR, "docs", "release-notes.md")
        self.assertEqual(changelog_latest(path), APP_VERSION)


class TestRequiredFiles(unittest.TestCase):
    """R1004: required release artifacts exist."""

    def test_repo_has_every_required_file(self):
        self.assertEqual(missing_docs(BASE_DIR), [])

    def test_empty_directory_reports_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(missing_docs(tmp), sorted(REQUIRED_DOCS))

    def test_partial_directory_reports_only_the_missing_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as fh:
                fh.write("# x\n")
            missing = missing_docs(tmp)
            self.assertNotIn("README.md", missing)
            self.assertIn("docs/install.md", missing)

    def test_read_text_is_safe(self):
        self.assertEqual(read_text(os.path.join(BASE_DIR, "nope.md")), "")


class TestConfigParity(unittest.TestCase):
    """R1005: `config.example.json` keys == `core_config.DEFAULTS` keys."""

    def test_repo_example_matches_defaults(self):
        path = os.path.join(BASE_DIR, "config.example.json")
        report = config_key_report(path, DEFAULTS)
        self.assertTrue(report["ok"], report["reason"])
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["extra"], [])

    def test_missing_key_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.example.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"host": "127.0.0.1"}, fh)
            report = config_key_report(path, DEFAULTS)
            self.assertFalse(report["ok"])
            self.assertIn("port", report["missing"])
            self.assertTrue(report["reason"])

    def test_unknown_key_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.example.json")
            payload = {key: 1 for key in DEFAULTS}
            payload["typo_key"] = 1
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            report = config_key_report(path, DEFAULTS)
            self.assertFalse(report["ok"])
            self.assertEqual(report["extra"], ["typo_key"])

    def test_unreadable_file_is_reported(self):
        report = config_key_report(os.path.join(BASE_DIR, "nope.json"), DEFAULTS)
        self.assertFalse(report["ok"])
        self.assertTrue(report["reason"])


class TestReleaseInfo(unittest.TestCase):
    """R1006: `/health.version` payload shape."""

    def test_payload_fields(self):
        info = release_info(2)
        self.assertEqual(sorted(info), ["api", "app", "schema", "userscript"])
        self.assertEqual(info["app"], APP_VERSION)
        self.assertEqual(info["api"], API_VERSION)
        self.assertEqual(info["userscript"], USERSCRIPT_VERSION)
        self.assertEqual(info["schema"], 2)

    def test_schema_defaults_to_zero(self):
        for value in (None, "", "bad", 0):
            self.assertEqual(release_info(value)["schema"], 0, repr(value))

    def test_report_helpers(self):
        checks = [{"name": "a", "ok": True, "detail": ""},
                  {"name": "b", "ok": False, "detail": "boom"}]
        self.assertEqual(failed_names(checks), ["b"])
        report = format_report(checks)
        self.assertIn("[OK  ] a", report)
        self.assertIn("[FAIL] b - boom", report)


class TestStaticChecks(unittest.TestCase):
    """R1007: the static release check passes on the repo and fails safely."""

    def test_repo_passes(self):
        checks = static_checks(BASE_DIR, DEFAULTS)
        self.assertEqual(failed_names(checks), [],
                         format_report(checks))
        names = [item["name"] for item in checks]
        self.assertEqual(names, ["required_files", "app_version_shape",
                                 "userscript_version", "changelog_version",
                                 "readme_version", "config_example_keys"])

    def test_empty_root_fails_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = static_checks(tmp, DEFAULTS)
            self.assertIn("required_files", failed_names(checks))
            self.assertIn("userscript_version", failed_names(checks))
            self.assertIn("changelog_version", failed_names(checks))
            self.assertIn("readme_version", failed_names(checks))
            # 版本号形状不依赖仓库内容，因此仍然通过
            self.assertNotIn("app_version_shape", failed_names(checks))


class TestHealthVersionApi(unittest.TestCase):
    """R1008/R1009: `/health.version` over real HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="mediadock-release-")
        cls.db = os.path.join(cls.tmp, "tasks.db")
        srv.bootstrap(cls.db)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        srv.bootstrap(":memory:")

    def test_health_reports_version(self):
        code, body = http_json(self.base, "/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], {
            "app": APP_VERSION, "api": API_VERSION,
            "userscript": USERSCRIPT_VERSION,
            "schema": SCHEMA_VERSION})
        # 既有字段仍然存在，且 schema 与存储快照一致
        self.assertIn("storage", body)
        self.assertIn("config", body)
        self.assertIn("dependencies", body)
        self.assertEqual(body["version"]["schema"],
                         body["storage"]["schema_version"])

    def test_memory_mode_reports_schema_zero(self):
        storage, reason = srv.storage, srv.storage_reason
        srv.storage = None
        srv.storage_reason = "probe: storage disabled"
        try:
            code, body = http_json(self.base, "/health")
        finally:
            srv.storage, srv.storage_reason = storage, reason
        self.assertEqual(code, 200)
        self.assertEqual(body["version"]["schema"], 0)
        self.assertEqual(body["storage"]["kind"], "memory")
        self.assertEqual(body["storage"]["schema_version"], 0)

    def test_engine_stub_still_completes_a_task(self):
        """Sanity: the release stage changed nothing in the download path."""
        factory = install_factory(lambda: InstantEngine())
        try:
            task_id = srv.scheduler.submit(
                "https://www.youtube.com/watch?v=release1")["task_id"]
            self.assertTrue(srv.scheduler.wait_idle(10))
            self.assertEqual(srv.manager.get(task_id).status, "completed")
        finally:
            restore_engine(factory)
            srv.manager.drop(task_id)


if __name__ == "__main__":
    unittest.main()
