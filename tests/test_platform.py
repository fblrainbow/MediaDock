"""Stage-007 platform Adapter tests (T701-T712).

Pure detection tests need no network and no process spawn; the HTTP cases use
the real `server.Handler` with `tests.helpers.InstantEngine` as the process
boundary, exactly like the Stage-004..006 API tests.
"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import core_platform
import server as srv
from core_platform import (DEFAULT_REGISTRY, PlatformAdapter, PlatformRegistry,
                           YouTubeAdapter, detect_platform, host_matches,
                           platform_names)
from tests.helpers import (InstantEngine, install_factory, restore_engine,
                           wait_terminal)

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

YOUTUBE_URLS = (
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
)

OTHER_PLATFORM_URLS = (
    "https://www.tiktok.com/@a/video/1",
    "https://x.com/a/status/1",
    "https://www.instagram.com/p/abc/",
    "https://www.facebook.com/watch/?v=1",
    "https://example.com/v.mp4",
    "http://127.0.0.1/video",
)


class SpyAdapter(PlatformAdapter):
    """Records whether the registry over-dispatched to it (T707)."""

    def __init__(self, name, hosts, ready=True, video_id=""):
        self.name = name
        self.hosts = tuple(hosts)
        self.ready = ready
        self.match_calls = 0
        self.info_calls = 0
        self._video_id = video_id

    def matches(self, url):
        self.match_calls += 1
        return PlatformAdapter.matches(self, url)

    def info(self, url):
        self.info_calls += 1
        return PlatformAdapter.info(self, url)

    def video_id(self, url):
        return self._video_id


class TestHostMatching(unittest.TestCase):
    """T701/T704: host coverage and video-id extraction."""

    def test_youtube_domains_match(self):
        for url in YOUTUBE_URLS:
            adapter, code, message = detect_platform(url)
            self.assertIsInstance(adapter, YouTubeAdapter, url)
            self.assertEqual((code, message), ("", ""), url)
            self.assertEqual(adapter.name, "youtube")

    def test_host_matches_only_real_subdomains(self):
        self.assertTrue(host_matches("youtube.com", ("youtube.com",)))
        self.assertTrue(host_matches("WWW.YouTube.COM.", ("youtube.com",)))
        self.assertTrue(host_matches("m.youtube.com", ("youtube.com",)))
        for host in ("notyoutube.com", "youtube.com.evil.net", "",
                     "youtube", None):
            self.assertFalse(host_matches(host, ("youtube.com",)), host)

    def test_video_id_extraction(self):
        adapter = YouTubeAdapter()
        cases = (("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
                 ("https://www.youtube.com/watch?list=x&v=abcdefghijk",
                  "abcdefghijk"),
                 ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
                 ("https://www.youtube.com/shorts/abcdefghijk", "abcdefghijk"),
                 ("https://www.youtube.com/feed/subscriptions", ""))
        for url, expected in cases:
            self.assertEqual(adapter.video_id(url), expected, url)

    def test_normalize_keeps_query_and_drops_fragment(self):
        adapter = YouTubeAdapter()
        url = "https://www.youtube.com/watch?v=abc123456&list=PL1#t=30"
        self.assertEqual(adapter.normalize(url),
                         "https://www.youtube.com/watch?v=abc123456&list=PL1")
        self.assertEqual(adapter.normalize("https://youtu.be/abc123456"),
                         "https://youtu.be/abc123456")

    def test_info_shape(self):
        info = YouTubeAdapter().info(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ#frag")
        self.assertEqual(info.to_dict(),
                         {"name": "youtube",
                          "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                          "video_id": "dQw4w9WgXcQ"})
        self.assertEqual(info.name, "youtube")
        self.assertEqual(info.video_id, "dQw4w9WgXcQ")


class TestDetection(unittest.TestCase):
    """T702/T703/T705/T706/T707/T711/T712."""

    def test_unsupported_platform_is_rejected(self):
        """T702: a valid http(s) URL with no Adapter never creates a Task."""
        for url in OTHER_PLATFORM_URLS:
            adapter, code, message = detect_platform(url)
            self.assertIsNone(adapter, url)
            self.assertEqual(code, "unsupported_platform", url)
            self.assertTrue(message, url)

    def test_invalid_url_is_reported_by_the_detector(self):
        """T703: Stage-006 validation stays the first gate."""
        for url in (None, "", "   ", "ftp://youtube.com/x",
                    "javascript:alert(1)",
                    "https://user:pw@youtube.com/watch?v=abc123456",
                    "https://www.youtube.com/a\tb"):
            adapter, code, message = detect_platform(url)
            self.assertIsNone(adapter, repr(url))
            self.assertEqual(code, "invalid_url", repr(url))
            self.assertTrue(message, repr(url))

    def test_not_ready_adapter(self):
        """T705: a recognised but unwired platform is never handled."""
        spy = SpyAdapter("tiktok", ("tiktok.com",), ready=False)
        registry = PlatformRegistry((spy,))
        adapter, code, message = registry.detect(
            "https://www.tiktok.com/@a/video/1")
        self.assertIsNone(adapter)
        self.assertEqual(code, "platform_not_ready")
        self.assertIn("tiktok", message)
        self.assertEqual(spy.info_calls, 0)

    def test_registration_order_wins(self):
        """T706: the first registered matching Adapter is selected."""
        first = SpyAdapter("first", ("youtube.com",), video_id="first-id")
        second = SpyAdapter("second", ("youtube.com",), video_id="second-id")
        registry = PlatformRegistry((first, second))
        adapter, code, _ = registry.detect(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertIs(adapter, first)
        self.assertEqual((code, first.info_calls), ("", 0))
        self.assertEqual(adapter.info(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ").video_id,
            "first-id")
        self.assertEqual(first.info_calls, 1)

        reversed_registry = PlatformRegistry((second, first))
        adapter, _, _ = reversed_registry.detect(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertIs(adapter, second)

    def test_no_over_dispatch_to_adapters(self):
        """T707: unmatched / unready URLs never reach `info()`."""
        spy = SpyAdapter("spy", ("tiktok.com",))
        registry = PlatformRegistry((YouTubeAdapter(), spy))
        adapter, code, _ = registry.detect("https://example.com/v")
        self.assertIsNone(adapter)
        self.assertEqual(code, "unsupported_platform")
        self.assertEqual(spy.info_calls, 0)
        consulted = spy.match_calls
        self.assertGreaterEqual(consulted, 1)
        adapter, _, _ = registry.detect(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertIsInstance(adapter, YouTubeAdapter)
        # the YouTube match short-circuits before the spy is even consulted
        self.assertEqual(spy.match_calls, consulted)
        self.assertEqual(spy.info_calls, 0)
        self.assertEqual(spy.info("https://www.tiktok.com/@a/video/1").name,
                         "spy")
        self.assertEqual(spy.info_calls, 1)

    def test_detection_has_no_side_effects(self):
        """T711: pure string judgement - no network, no store, no state."""
        before = len(srv.manager.ids())
        detect_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        detect_platform("https://example.com/v")
        detect_platform("not a url")
        self.assertEqual(len(srv.manager.ids()), before)
        with open(os.path.join(REPO_DIR, "core_platform.py"), "r",
                  encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("import socket", "urllib.request", "http.client",
                          "subprocess", "requests", "shell=True"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_runtime_registration_is_detected(self):
        """T712: adding a platform means registering an Adapter only."""
        registry = PlatformRegistry((YouTubeAdapter(),))
        url = "https://www.tiktok.com/@a/video/1"
        self.assertEqual(registry.detect(url)[1], "unsupported_platform")
        registry.register(SpyAdapter("tiktok", ("tiktok.com",),
                                     video_id="tiktok-id"))
        adapter, code, message = registry.detect(url)
        self.assertIsNotNone(adapter)
        self.assertEqual((code, message), ("", ""))
        self.assertEqual(adapter.info(url).name, "tiktok")

    def test_register_rejects_invalid_adapter(self):
        registry = PlatformRegistry(())
        with self.assertRaises(ValueError):
            registry.register(None)
        with self.assertRaises(ValueError):
            registry.register(PlatformAdapter())

    def test_default_registry_contract(self):
        self.assertEqual(platform_names(), ["youtube"])
        self.assertEqual(DEFAULT_REGISTRY.names(), ["youtube"])
        self.assertIsInstance(DEFAULT_REGISTRY.get("youtube"), YouTubeAdapter)
        self.assertIsNone(DEFAULT_REGISTRY.get("tiktok"))
        self.assertEqual(core_platform.ERROR_UNSUPPORTED_PLATFORM,
                         "unsupported_platform")


def raw_get(port, path, host=None, timeout=15):
    """One GET against the real handler; returns `(status, decoded body)`."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if host:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class PlatformApiBase(unittest.TestCase):
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
        srv.bootstrap(":memory:")

    def download(self, url, **kwargs):
        return raw_get(self.port, "/download?url=" + quote(url, safe=""),
                       **kwargs)

    def body(self, raw):
        return json.loads(raw)


class TestDownloadPlatform(PlatformApiBase):
    """T708/T709/T710: the HTTP contract of the detection layer."""

    def test_youtube_url_creates_youtube_task(self):
        """T708: the normal flow is unchanged and carries the platform."""
        code, raw = self.download("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(code, 200, raw)
        data = self.body(raw)
        self.assertEqual(sorted(data), ["task_id"])
        task_id = data["task_id"]
        task = srv.manager.get(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.platform, "youtube")
        # the fragment-free URL is what the engine receives
        self.assertEqual(task.url, "https://youtu.be/dQw4w9WgXcQ")
        self.assertTrue(wait_terminal(task_id))
        self.assertEqual(srv.manager.get(task_id).status, "completed")

    def test_fragment_is_stripped_before_submission(self):
        code, raw = self.download(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ#t=42")
        self.assertEqual(code, 200, raw)
        task = srv.manager.get(self.body(raw)["task_id"])
        self.assertEqual(task.url,
                         "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_unsupported_platform_is_400_and_creates_no_task(self):
        """T709: no Task, no queue slot, no process for other platforms."""
        before = len(srv.manager.ids())
        for url in ("https://www.tiktok.com/@a/video/1",
                    "https://x.com/a/status/1",
                    "https://example.com/v.mp4",
                    "https://www.facebook.com/watch/?v=1"):
            with self.subTest(url=url):
                code, raw = self.download(url)
                self.assertEqual(code, 400, raw)
                data = self.body(raw)
                self.assertEqual(data["error_code"], "unsupported_platform")
                self.assertTrue(data["message"])
                self.assertNotIn("task_id", data)
        self.assertEqual(len(srv.manager.ids()), before)
        self.assertEqual(srv.scheduler.active_count(), 0)
        self.assertEqual(srv.scheduler.queued_count(), 0)

    def test_legacy_error_contract_is_unchanged(self):
        """T710: `missing_url` and `invalid_url` keep their Stage-001..006 shape."""
        code, raw = raw_get(self.port, "/download")
        self.assertEqual((code, self.body(raw)["error_code"]),
                         (400, "missing_url"))
        code, raw = self.download("ftp://youtube.com/watch?v=abc123456")
        self.assertEqual((code, self.body(raw)["error_code"]),
                         (400, "invalid_url"))
        code, raw = self.download("--exec=calc")
        self.assertEqual((code, self.body(raw)["error_code"]),
                         (400, "invalid_url"))
        code, raw = self.download("https://user:pw@youtube.com/v")
        self.assertEqual((code, self.body(raw)["error_code"]),
                         (400, "invalid_url"))


if __name__ == "__main__":
    unittest.main()
