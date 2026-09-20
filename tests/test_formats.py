"""Stage-008 format model + Formats API tests (T801-T812).

Pure model tests need no process; the HTTP cases run the real
`server.Handler` with `tests.helpers.InstantEngine` as the download boundary
and an injected formats runner as the metadata boundary.
"""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any, Tuple
from urllib.parse import quote

import server as srv
from core_engine import FORMAT_EXPR, build_command
from core_formats import (DEFAULT_PRESET, ERROR_FORMATS_UNAVAILABLE,
                          ERROR_INVALID_FORMAT, FormatsProbe,
                          build_formats_payload, build_probe_command,
                          format_entry, parse_probe_output, preset_choices,
                          preset_names, preset_sizes, presets_public,
                          resolve_preset, selector_for, validate_format_id)
from tests.helpers import (CancelableEngine, InstantEngine, install_factory,
                           restore_engine)

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def raw_info():
    """Minimal but realistic `--dump-single-json` payload."""
    return {
        "id": "dQw4w9WgXcQ",
        "title": "Probe video",
        "uploader": "Probe channel",
        "duration": 213.0,
        "extractor_key": "Youtube",
        "formats": [
            {"format_id": "137", "ext": "mp4", "height": 1080, "width": 1920,
             "fps": 30, "vcodec": "avc1.640028", "acodec": "none",
             "filesize": 1234567, "format_note": "1080p"},
            {"format_id": "136", "ext": "mp4", "height": 720, "width": 1280,
             "fps": 30, "vcodec": "avc1.4d401f", "acodec": "none",
             "filesize_approx": 700000, "format_note": "720p"},
            {"format_id": "135", "ext": "mp4", "height": 480, "width": 854,
             "fps": 30, "vcodec": "avc1.4d401e", "acodec": "none",
             "format_note": "480p"},
            {"format_id": "140", "ext": "m4a", "height": None, "vcodec": "none",
             "acodec": "mp4a.40.2", "abr": 128.0, "filesize": 3200000,
             "format_note": "medium"},
            {"format_id": "18", "ext": "mp4", "height": 360, "width": 640,
             "fps": 30, "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
             "format_note": "360p"},
        ],
    }


def json_out(info=None):
    return json.dumps(info if info is not None else raw_info())


def fake_runner(payload=None, rc=0, out=None, err="", calls=None):
    """Build a `(rc, stdout, stderr)` runner recording the argv it saw."""
    def runner(command, timeout):
        if calls is not None:
            calls.append((list(command), timeout))
        if out is not None:
            return rc, out, err
        return rc, json_out(payload), err
    return runner


def raw_get(port, path, timeout=20):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class TestPresets(unittest.TestCase):
    """T801/T802/T804: preset table is fixed and the default is frozen."""

    def test_preset_table_is_ordered_and_stable(self):
        self.assertEqual(preset_names(),
                         ["best", "1080p", "720p", "480p", "audio"])
        self.assertEqual(DEFAULT_PRESET, "best")
        for item in presets_public():
            self.assertEqual(sorted(item),
                             ["audio_format", "kind", "label", "max_height",
                              "name", "selector", "size_bytes"])
        by_name = {item["name"]: item for item in presets_public()}
        # 只有 audio 预设请求转码成 MP3（Stage-012），其余一律不转码
        self.assertEqual(by_name["audio"]["audio_format"], "mp3")
        for name in ("best", "1080p", "720p", "480p"):
            self.assertEqual(by_name[name]["audio_format"], "", name)

    def test_default_preset_keeps_the_frozen_policy(self):
        preset, code, message = resolve_preset(None)
        self.assertEqual((code, message), ("", ""))
        self.assertEqual(selector_for(preset), FORMAT_EXPR)
        self.assertEqual(selector_for(), FORMAT_EXPR)
        for value in ("", "  ", None):
            preset, code, _ = resolve_preset(value)
            if preset is None:
                self.fail("empty value must resolve to the default preset")
            self.assertEqual((preset.name, code), ("best", ""))

    def test_unknown_preset_is_rejected(self):
        for value in ("4k", "1080P; rm -rf", "--exec=calc", "720", "best1080"):
            preset, code, message = resolve_preset(value)
            self.assertIsNone(preset, value)
            self.assertEqual(code, ERROR_INVALID_FORMAT, value)
            self.assertTrue(message, value)
        # whitespace and case are normalised; anything outside the table is not
        preset, code, _ = resolve_preset("  1080P  ")
        if preset is None:
            self.fail("normalised value must resolve")
        self.assertEqual((preset.name, code), ("1080p", ""))

    def test_selector_for_uses_constants_only(self):
        self.assertEqual(selector_for(resolve_preset("720p")[0]),
                         "bv*[height<=720]+ba/b[height<=720]")
        self.assertEqual(selector_for(None, "137"), "137+ba/137/b")

    def test_format_id_validation_blocks_injection(self):
        for value in ("", "  ", "--exec=calc", "-f", "a b", "a;b", "a/b",
                      "a$(x)", "a|b", "ba*", "137+140", "a" * 65, "a\nb"):
            ok, message = validate_format_id(value)
            self.assertFalse(ok, repr(value))
            self.assertTrue(message, repr(value))
        for value in ("137", "251-1", "sb0", "hls-2311", "140-dash"):
            ok, _ = validate_format_id(value)
            self.assertTrue(ok, value)


class TestProbeModel(unittest.TestCase):
    """T805/T806/T807/T808: command shape, parsing and the stable payload."""

    def test_probe_command_shape(self):
        command = build_probe_command("yt-dlp.exe", URL, "ffmpeg.exe")
        self.assertEqual(command[-1], URL)
        self.assertIn("--dump-single-json", command)
        self.assertIn("--skip-download", command)
        self.assertNotIn("-f", command)
        self.assertIn("--ffmpeg-location", command)
        self.assertEqual(build_probe_command("yt-dlp.exe", URL)[0], "yt-dlp.exe")

    def test_parse_probe_output_handles_noise_and_garbage(self):
        info, code, message = parse_probe_output(json_out())
        self.assertEqual((code, message), ("", ""))
        if info is None:
            self.fail("valid JSON must parse into a payload")
        self.assertEqual(info["id"], "dQw4w9WgXcQ")
        info, code, _ = parse_probe_output(
            "WARNING: some notice\n" + json_out() + "\n")
        self.assertIsNotNone(info)
        self.assertEqual(code, "")
        for value in ("", "   ", "not json", "[1, 2]"):
            info, code, _ = parse_probe_output(value)
            self.assertIsNone(info, repr(value))
            self.assertEqual(code, ERROR_FORMATS_UNAVAILABLE, repr(value))

    def test_payload_model_is_stable(self):
        payload = build_formats_payload(raw_info(), URL, "youtube")
        self.assertEqual(sorted(payload),
                         ["count", "default_preset", "duration", "extractor",
                          "formats", "platform", "presets", "title", "total",
                          "uploader", "url", "video_id"])
        self.assertEqual((payload["count"], payload["total"]), (5, 5))
        self.assertEqual(payload["video_id"], "dQw4w9WgXcQ")
        by_id = {item["format_id"]: item for item in payload["formats"]}
        self.assertEqual(sorted(by_id["137"]),
                         ["abr", "acodec", "ext", "filesize", "format_id",
                          "fps", "height", "note", "presets", "tbr", "vcodec",
                          "width"])
        self.assertEqual(by_id["137"]["presets"], ["best", "1080p"])
        self.assertEqual(by_id["136"]["presets"][-1], "720p")
        self.assertEqual(by_id["140"]["presets"], ["audio"])
        # a 480p stream can serve every ceiling at or above its height
        self.assertEqual(by_id["135"]["presets"],
                         ["best", "1080p", "720p", "480p"])
        self.assertIn("best", by_id["18"]["presets"])
        # a stream above a ceiling never advertises that preset
        tall = format_entry({"format_id": "313", "height": 2160,
                             "vcodec": "vp9", "acodec": "none"})
        self.assertEqual(tall["presets"], [])

    def test_entry_survives_missing_fields(self):
        entry = format_entry({"format_id": "x"})
        self.assertEqual(entry["format_id"], "x")
        self.assertIsNone(entry["height"])
        self.assertEqual(entry["vcodec"], "none")
        self.assertEqual(entry["presets"], [])

    def test_payload_limit_keeps_total(self):
        payload = build_formats_payload(raw_info(), URL, "youtube", limit=2)
        self.assertEqual((payload["count"], payload["total"]), (2, 5))

    def test_probe_fetch_paths(self):
        probe = FormatsProbe("yt-dlp.exe", runner=fake_runner())
        payload, code, _ = probe.fetch(URL)
        if payload is None:
            self.fail("the fake runner must produce a payload")
        self.assertEqual((code, payload["platform"]), ("", "youtube"))

        probe = FormatsProbe("yt-dlp.exe",
                             runner=fake_runner(rc=1, err="ERROR: nope"))
        payload, code, message = probe.fetch(URL)
        self.assertIsNone(payload)
        self.assertEqual(code, ERROR_FORMATS_UNAVAILABLE)
        self.assertIn("nope", message)

        probe = FormatsProbe("yt-dlp.exe", runner=fake_runner(out="garbage"))
        payload, code, _ = probe.fetch(URL)
        self.assertIsNone(payload)
        self.assertEqual(code, ERROR_FORMATS_UNAVAILABLE)

        probe = FormatsProbe("", runner=fake_runner())
        payload, code, _ = probe.fetch(URL)
        self.assertIsNone(payload)
        self.assertEqual(code, ERROR_FORMATS_UNAVAILABLE)


class TestPresetSizes(unittest.TestCase):
    """T1301-T1305: per-preset size estimates from the probe payload."""

    def _formats(self):
        return [format_entry(entry) for entry in raw_info()["formats"]]

    def test_video_preset_is_video_plus_audio(self):
        formats = self._formats()
        sizes = preset_sizes(formats)
        video = next(f for f in formats if f["height"] == 1080)
        audio = next(f for f in formats if f["vcodec"] == "none")
        self.assertEqual(sizes["1080p"], video["filesize"] + audio["filesize"])
        # 720p 的封顶不会选到 1080p 那个格式
        small = next(f for f in formats if f["height"] == 720)
        self.assertEqual(sizes["720p"], small["filesize"] + audio["filesize"])
        self.assertGreater(sizes["1080p"], sizes["720p"])

    def test_audio_preset_uses_audio_only(self):
        formats = self._formats()
        audio = next(f for f in formats if f["vcodec"] == "none")
        self.assertEqual(preset_sizes(formats)["audio"], audio["filesize"])

    def test_missing_component_means_unknown(self):
        only_video = [format_entry(raw_info()["formats"][0])]
        sizes = preset_sizes(only_video)
        self.assertEqual(sizes["1080p"], 0, "没有音频件时不能只报视频大小")
        self.assertEqual(sizes["audio"], 0)

    def test_empty_input_is_safe(self):
        self.assertEqual(preset_sizes([]),
                         {"best": 0, "1080p": 0, "720p": 0, "480p": 0,
                          "audio": 0})
        for item in preset_choices([]):
            self.assertEqual(item["size_bytes"], 0)

    def test_bitrate_fallback_uses_duration(self):
        entry = {"format_id": "140", "vcodec": "none", "acodec": "mp4a",
                 "abr": 128.0}
        sized = preset_sizes([format_entry(entry)], duration=100.0)
        self.assertEqual(sized["audio"], int(128.0 * 1000 / 8 * 100.0))


class FormatsApiBase(unittest.TestCase):
    """Real HTTP handler; metadata boundary is the injected runner."""

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
        self._old_runner = srv.FORMATS_RUNNER
        self._old_probe = srv.FORMATS_PROBE_FACTORY
        srv.FORMATS_RUNNER = fake_runner()

    def tearDown(self):
        for task_id in list(srv.scheduler.active_ids()) + \
                list(srv.scheduler.queued_ids()):
            try:
                srv.scheduler.cancel(task_id, timeout=5)
            except Exception:  # noqa: BLE001 - test cleanup only
                pass
        srv.scheduler.wait_idle(10)
        srv.FORMATS_RUNNER = self._old_runner
        srv.FORMATS_PROBE_FACTORY = self._old_probe
        restore_engine(self._old_factory)
        srv.bootstrap(":memory:")

    def json_get(self, path) -> Tuple[int, Any]:
        code, raw = raw_get(self.port, path)
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, {"raw": raw}

    def url(self, extra=""):
        return "/download?url=" + quote(URL, safe="") + extra

    def formats_url(self):
        return "/formats?url=" + quote(URL, safe="")


class TestFormatsApi(FormatsApiBase):
    """T809: the `/formats` contract (+ T1306-T1308 sizes, Stage-013)."""

    def test_formats_payload_carries_preset_sizes(self):
        """T1306: the dropdown reads `presets[].size_bytes`."""
        code, data = self.json_get(self.formats_url())
        self.assertEqual(code, 200, data)
        by_name = {item["name"]: item for item in data["presets"]}
        self.assertEqual(by_name["audio"]["size_bytes"], 3200000)
        self.assertEqual(by_name["1080p"]["size_bytes"], 1234567 + 3200000)
        self.assertEqual(by_name["720p"]["size_bytes"], 700000 + 3200000)
        self.assertGreater(by_name["1080p"]["size_bytes"],
                           by_name["720p"]["size_bytes"])
        for item in data["presets"]:
            self.assertIsInstance(item["size_bytes"], int, item["name"])

    def test_tasks_carry_live_size_bytes(self):
        """T1307: while downloading the row shows yt-dlp's total size."""
        factory = install_factory(lambda: CancelableEngine(hold_seconds=8.0))
        try:
            code, created = self.json_get(self.url())
            self.assertEqual(code, 200, created)
            task_id = created["task_id"]
            control = None
            deadline = time.monotonic() + 5
            while control is None and time.monotonic() < deadline:
                control = srv.scheduler.control_for(task_id)
                if control is None:
                    time.sleep(0.02)
            if control is None:
                self.fail("running task must expose a control")
            control.total_size = 4242
            code, data = self.json_get("/tasks")
            self.assertEqual(code, 200, data)
            item = next(t for t in data["tasks"] if t["task_id"] == task_id)
            self.assertEqual(item["status"], "downloading")
            self.assertEqual(item["size_bytes"], 4242)
        finally:
            restore_engine(factory)

    def test_completed_size_uses_the_file_on_disk(self):
        """T1308: finished rows report the real file size (0 when no file)."""
        from core_files import task_file_size
        code, created = self.json_get(self.url())
        self.assertEqual(code, 200, created)
        self.assertTrue(srv.scheduler.wait_idle(10))
        code, data = self.json_get("/tasks")
        item = next(t for t in data["tasks"]
                    if t["task_id"] == created["task_id"])
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["size_bytes"],
                         task_file_size(srv.DOWNLOAD_DIR, item["url"]))

    def test_formats_payload_shape(self):
        code, data = self.json_get(self.formats_url())
        self.assertEqual(code, 200, data)
        self.assertEqual(data["platform"], "youtube")
        self.assertEqual(data["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(data["default_preset"], "best")
        self.assertEqual([p["name"] for p in data["presets"]],
                         ["best", "1080p", "720p", "480p", "audio"])
        self.assertEqual(data["count"], 5)
        self.assertEqual(data["total"], 5)
        self.assertEqual({f["format_id"] for f in data["formats"]},
                         {"137", "136", "135", "140", "18"})

    def test_formats_gates_before_probing(self):
        calls = []
        srv.FORMATS_RUNNER = fake_runner(calls=calls)
        code, data = self.json_get("/formats")
        self.assertEqual((code, data["error_code"]), (400, "missing_url"))
        code, data = self.json_get("/formats?url=" + quote("ftp://x/y", safe=""))
        self.assertEqual((code, data["error_code"]), (400, "invalid_url"))
        code, data = self.json_get(
            "/formats?url=" + quote("https://www.tiktok.com/@a/video/1",
                                    safe=""))
        self.assertEqual((code, data["error_code"]),
                         (400, "unsupported_platform"))
        self.assertEqual(calls, [], "yt-dlp must not run for rejected input")

    def test_formats_probe_failure_is_502_without_cache(self):
        srv.FORMATS_RUNNER = fake_runner(rc=1, err="ERROR: unavailable")
        code, data = self.json_get(self.formats_url())
        self.assertEqual(code, 502, data)
        self.assertEqual(data["error_code"], "formats_unavailable")
        self.assertIsNone(srv.cached_formats(URL))

    def test_formats_probe_uses_normalized_url(self):
        calls = []
        srv.FORMATS_RUNNER = fake_runner(calls=calls)
        code, _ = self.json_get("/formats?url=" + quote(URL + "#t=30", safe=""))
        self.assertEqual(code, 200)
        self.assertEqual(calls[0][0][-1], URL)
        self.assertIsNotNone(srv.cached_formats(URL))


class TestFormatSelection(FormatsApiBase):
    """T810/T811/T812: `/download` never builds argv from raw user text."""

    def test_default_download_keeps_the_frozen_policy(self):
        code, data = self.json_get(self.url())
        self.assertEqual(code, 200, data)
        self.assertEqual(srv.scheduler.format_for(data["task_id"]), FORMAT_EXPR)

    def test_preset_without_formats_is_rejected(self):
        code, data = self.json_get(self.url("&preset=720p"))
        self.assertEqual(code, 400, data)
        self.assertEqual(data["error_code"], "format_not_available")
        self.assertNotIn("task_id", data)

    def test_unknown_preset_is_rejected(self):
        for value in ("4k", "--exec=calc", "best; rm"):
            code, data = self.json_get(
                self.url("&preset=" + quote(value, safe="")))
            self.assertEqual(code, 400, value)
            self.assertEqual(data["error_code"], "invalid_format", value)

    def test_preset_after_formats_is_applied(self):
        self.assertEqual(self.json_get(self.formats_url())[0], 200)
        for preset, selector in (
                ("720p", "bv*[height<=720]+ba/b[height<=720]"),
                ("480p", "bv*[height<=480]+ba/b[height<=480]"),
                ("audio", "bestaudio/best"),
                ("best", FORMAT_EXPR)):
            code, data = self.json_get(self.url("&preset=" + preset))
            self.assertEqual(code, 200, (preset, data))
            self.assertEqual(srv.scheduler.format_for(data["task_id"]),
                             selector, preset)

    def test_audio_preset_extracts_mp3(self):
        """Stage-012: 仅音频 must produce a real .mp3, not the native stream."""
        self.assertEqual(self.json_get(self.formats_url())[0], 200)
        code, data = self.json_get(self.url("&preset=audio"))
        self.assertEqual(code, 200, data)
        task_id = data["task_id"]
        self.assertEqual(srv.scheduler.audio_format_for(task_id), "mp3")
        command = build_command("yt-dlp.exe", "downloads", URL, "",
                                srv.scheduler.format_for(task_id),
                                srv.scheduler.audio_format_for(task_id))
        self.assertIn("--extract-audio", command)
        self.assertEqual(command[command.index("--audio-format") + 1], "mp3")
        self.assertNotIn("--merge-output-format", command)

    def test_video_presets_never_extract_audio(self):
        self.assertEqual(self.json_get(self.formats_url())[0], 200)
        for preset in ("best", "720p"):
            code, data = self.json_get(self.url("&preset=" + preset))
            self.assertEqual(code, 200, (preset, data))
            self.assertEqual(srv.scheduler.audio_format_for(data["task_id"]),
                             "", preset)

    def test_format_id_does_not_force_transcoding(self):
        self.assertEqual(self.json_get(self.formats_url())[0], 200)
        code, data = self.json_get(self.url("&preset=audio&format_id=137"))
        self.assertEqual(code, 200, data)
        self.assertEqual(srv.scheduler.audio_format_for(data["task_id"]), "")

    def test_format_id_must_come_from_formats(self):
        code, data = self.json_get(self.url("&format_id=137"))
        self.assertEqual((code, data["error_code"]),
                         (400, "format_not_available"))
        for value in ("--exec=calc", "-f", "a b", "a;b"):
            code, data = self.json_get(
                self.url("&format_id=" + quote(value, safe="")))
            self.assertEqual((code, data["error_code"]),
                             (400, "invalid_format"), value)

    def test_format_id_reaches_argv_only_after_validation(self):
        self.assertEqual(self.json_get(self.formats_url())[0], 200)
        code, data = self.json_get(
            self.url("&format_id=" + quote("137", safe="")))
        self.assertEqual(code, 200, data)
        expression = srv.scheduler.format_for(data["task_id"])
        self.assertEqual(expression, "137+ba/137/b")
        command = build_command("yt-dlp.exe", "downloads", URL, "", expression)
        self.assertEqual(command[command.index("-f") + 1], "137+ba/137/b")
        self.assertEqual(command[-1], URL)

    def test_format_id_not_in_probe_is_rejected(self):
        self.assertEqual(self.json_get(self.formats_url())[0], 200)
        code, data = self.json_get(self.url("&format_id=999"))
        self.assertEqual((code, data["error_code"]),
                         (400, "format_not_available"))


if __name__ == "__main__":
    unittest.main()

