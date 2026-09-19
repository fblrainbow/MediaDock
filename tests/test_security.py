"""Stage-006 security primitives (T609-T614, T630). No network, no server."""
import os
import unittest

from core_security import (MAX_PATH_LENGTH, MAX_URL_LENGTH, body_within_limit,
                           check_host_header, check_origin, redact,
                           sanitize_path, sanitize_url, validate_url)


class TestValidateUrl(unittest.TestCase):
    def test_accepts_http_and_https(self):
        for url in ("https://www.youtube.com/watch?v=abcdefghijk",
                    "http://example.com/v.mp4"):
            ok, code, message = validate_url(url)
            self.assertTrue(ok, message)
            self.assertEqual((code, message), ("", ""))

    def test_rejects_empty_and_non_string(self):
        for value in (None, "", "   ", 42):
            ok, code, _ = validate_url(value)
            self.assertFalse(ok)
            self.assertEqual(code, "invalid_url")

    def test_rejects_other_schemes(self):
        for url in ("ftp://example.com/x", "javascript:alert(1)",
                    "file:///c:/windows/win.ini", "-f", "data:text/html,x"):
            ok, code, _ = validate_url(url)
            self.assertFalse(ok, url)
            self.assertEqual(code, "invalid_url")

    def test_rejects_overlong_url(self):
        ok, _, message = validate_url("https://example.com/"
                                      + "a" * MAX_URL_LENGTH)
        self.assertFalse(ok)
        self.assertIn("2048", message)

    def test_rejects_control_characters_and_spaces(self):
        for url in ("https://example.com/a\tb", "https://example.com/a\x00b",
                    "https://example.com/a b", 'https://example.com/a"b',
                    "https://example.com/a|b"):
            ok, _, _ = validate_url(url)
            self.assertFalse(ok, repr(url))

    def test_rejects_missing_host_and_credentials(self):
        for url in ("http://", "https:///path",
                    "https://user:pw@example.com/v"):
            ok, _, _ = validate_url(url)
            self.assertFalse(ok, url)

    def test_blocks_flag_like_input(self):
        """T630: a value starting with '-' can never reach argv."""
        ok, _, _ = validate_url("--exec=calc")
        self.assertFalse(ok)
        ok, _, _ = validate_url("-P C:\\Windows")
        self.assertFalse(ok)


class TestHostHeader(unittest.TestCase):
    def test_loopback_names_pass(self):
        for host in ("127.0.0.1", "127.0.0.1:8765", "localhost",
                     "localhost:1234", "LOCALHOST:80", "[::1]:8765",
                     "127.0.0.1."):
            ok, message = check_host_header(host)
            self.assertTrue(ok, f"{host}: {message}")

    def test_non_loopback_and_malformed_fail(self):
        for host in ("", None, "evil.com", "evil.com:8765", "0.0.0.0:80",
                     "192.168.1.10", "localhost:abc", "127.0.0.1:99999",
                     "127.0.0.1:80/x", "::1:80"):
            ok, _ = check_host_header(host)
            self.assertFalse(ok, str(host))


class TestOrigin(unittest.TestCase):
    def test_absent_origin_passes(self):
        for value in (None, "", "   "):
            ok, message = check_origin(value)
            self.assertTrue(ok)
            self.assertIn("absent", message)

    def test_youtube_origins_pass(self):
        for origin in ("https://www.youtube.com", "http://youtube.com",
                       "https://m.youtube.com", "https://youtu.be",
                       "https://www.youtube-nocookie.com:443"):
            ok, message = check_origin(origin)
            self.assertTrue(ok, f"{origin}: {message}")

    def test_other_origins_fail(self):
        for origin in ("https://evil.com", "http://youtube.com.evil.com",
                       "null", "*", "file:///c:/x.html",
                       "https://youtube.co", "chrome-extension://abcdef"):
            ok, _ = check_origin(origin)
            self.assertFalse(ok, origin)


class TestSanitizeUrl(unittest.TestCase):
    def test_keeps_ordinary_parameters(self):
        cleaned = sanitize_url("https://www.youtube.com/watch?v=abcdefghijk")
        self.assertIn("watch", cleaned)
        self.assertIn("v=abcdefghijk", cleaned)

    def test_masks_sensitive_values(self):
        cleaned = sanitize_url(
            "https://example.com/v?token=SECRET&sig=abc&apikey=k&v=ok")
        self.assertIn("token=***", cleaned)
        self.assertIn("sig=***", cleaned)
        self.assertIn("apikey=***", cleaned)
        self.assertIn("v=ok", cleaned)
        self.assertNotIn("SECRET", cleaned)

    def test_masks_userinfo_password(self):
        cleaned = sanitize_url("https://user:hunter2@example.com/v?x=1")
        self.assertNotIn("hunter2", cleaned)
        self.assertIn("user:***@example.com", cleaned)

    def test_tolerates_non_url_text(self):
        self.assertEqual(sanitize_url(""), "")
        self.assertEqual(sanitize_url("not a url"), "not a url")


class TestRedact(unittest.TestCase):
    def test_shortens_home_directory(self):
        home = os.path.expanduser("~")
        text = f"Task started with {os.path.join(home, 'Videos', 'a.mp4')}"
        cleaned = redact(text, home=home)
        self.assertNotIn(home, cleaned)
        self.assertIn("~", cleaned)
        self.assertIn("a.mp4", cleaned)

    def test_masks_urls_in_sentences(self):
        line = "Task x start: https://example.com/v?token=SECRET&v=abc done"
        cleaned = redact(line)
        self.assertNotIn("SECRET", cleaned)
        self.assertIn("token=***", cleaned)
        self.assertIn("v=abc", cleaned)

    def test_limits_line_length(self):
        cleaned = redact("x" * 5000, max_len=200)
        self.assertEqual(len(cleaned), 200)
        self.assertTrue(cleaned.endswith("..."))

    def test_keeps_ordinary_text(self):
        self.assertEqual(redact("storage ready: 3 task(s)"),
                         "storage ready: 3 task(s)")

    def test_path_limit(self):
        long_path = "C:" + "\\d" * 400
        self.assertLessEqual(len(sanitize_path(long_path)), MAX_PATH_LENGTH)


class TestBodyLimit(unittest.TestCase):
    def test_within_and_over(self):
        self.assertTrue(body_within_limit(0, 1024))
        self.assertTrue(body_within_limit(1024, 1024))
        self.assertFalse(body_within_limit(1025, 1024))

    def test_bad_input_is_treated_as_empty(self):
        self.assertTrue(body_within_limit(None, 1024))
        self.assertTrue(body_within_limit("abc", 1024))

    def test_disabled_limit(self):
        self.assertTrue(body_within_limit(10 ** 9, 0))


if __name__ == "__main__":
    unittest.main()
