"""Stage-009 audio conversion tests (T901-T913).

The FFmpeg boundary is injected (`popen_factory`), so no real transcode runs in
unit tests; the fake child process also creates its output file so the success
path can be asserted end to end.
"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server as srv
from core_control import TaskControl
from core_manager import TaskManager
from core_media import (AUDIO_TASK_TYPE, DEFAULT_TARGET,
                        ERROR_FFMPEG_MISSING, ERROR_INVALID_TARGET,
                        ERROR_OUTPUT_MISSING, ERROR_SOURCE_NOT_FOUND,
                        ERROR_SOURCE_OUTSIDE, AudioProcessor,
                        build_ffmpeg_command, find_source_file, has_free_space,
                        output_path_for, parse_duration_seconds,
                        parse_media_line, resolve_target, target_names,
                        targets_public)

VIDEO_ID = "VIDID12345"
VIDEO_URL = "https://www.youtube.com/watch?v=" + VIDEO_ID
SOURCE_NAME = "probe [" + VIDEO_ID + "].mp4"


class ScriptedFfmpeg:
    """Stand-in child process that also writes its output file."""

    def __init__(self, command, returncode=0, create_output=True,
                 extra_lines=()):
        self.command = list(command)
        self.returncode = returncode
        self.pid = 0
        destination = str(self.command[-1])
        if create_output and returncode == 0:
            with open(destination, "w", encoding="utf-8") as handle:
                handle.write("fake media payload")
        self.stdout = iter([
            "ffmpeg version 6.0",
            "  Duration: 00:00:30.00, start: 0.000000, bitrate: 1000 kb/s",
            "Stream #0:0: Video: h264, yuv420p, 64x64",
            "out_time_ms=15000000",
            "progress=continue",
            "out_time_ms=30000000",
            "progress=end",
        ] + list(extra_lines))

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        return None

    def terminate(self):
        return None


def make_source(directory, name=SOURCE_NAME, payload="video bytes"):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return path


class TestMediaModel(unittest.TestCase):
    """T901-T906: targets, argv, path boundary and progress parsing."""

    def test_target_table_is_stable(self):
        self.assertEqual(target_names(), ["mp3", "m4a", "wav"])
        self.assertEqual(DEFAULT_TARGET, "mp3")
        for item in targets_public():
            self.assertEqual(sorted(item), ["extension", "label", "name"])
        self.assertEqual([t["extension"] for t in targets_public()],
                         [".mp3", ".m4a", ".wav"])

    def test_resolve_target(self):
        target, code, message = resolve_target(None)
        self.assertEqual((target.name, code, message), ("mp3", "", ""))
        target, code, _ = resolve_target("  WAV ")
        self.assertEqual((target.name, code), ("wav", ""))
        for value in ("flac", "--exec=calc", "ogg", "wavv"):
            target, code, message = resolve_target(value)
            self.assertIsNone(target, value)
            self.assertEqual(code, ERROR_INVALID_TARGET, value)
            self.assertTrue(message, value)

    def test_ffmpeg_command_shape(self):
        target, _, _ = resolve_target("mp3")
        command = build_ffmpeg_command("ffmpeg.exe", "src.mp4", "dst.mp3",
                                       target)
        self.assertEqual(command[0], "ffmpeg.exe")
        self.assertIn("-nostdin", command)
        self.assertIn("-y", command)
        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-i") + 1], "src.mp4")
        self.assertEqual(command[-1], "dst.mp3")
        self.assertIn("libmp3lame", command)
        self.assertEqual(command[command.index("-progress") + 1], "pipe:1")

    def test_output_path_keeps_the_video_marker(self):
        target, _, _ = resolve_target("m4a")
        produced = output_path_for(os.path.join("d", SOURCE_NAME), target)
        self.assertEqual(os.path.basename(produced),
                         "probe [" + VIDEO_ID + "].m4a")

    def test_find_source_file_boundary(self):
        with tempfile.TemporaryDirectory(prefix="mediadock-media-") as root:
            parent = os.path.dirname(root)
            outside = make_source(parent, "outside.mp4")
            try:
                path, code, _ = find_source_file(root, VIDEO_URL, outside)
                self.assertEqual(path, "")
                self.assertEqual(code, ERROR_SOURCE_OUTSIDE)
            finally:
                os.remove(outside)
            path, code, _ = find_source_file(root, VIDEO_URL)
            self.assertEqual((path, code), ("", ERROR_SOURCE_NOT_FOUND))
            first = make_source(root)
            path, code, _ = find_source_file(root, VIDEO_URL)
            self.assertEqual((path, code), (os.path.realpath(first), ""))
            # temp files are never a source
            make_source(root, SOURCE_NAME + ".part")
            path, _, _ = find_source_file(root, VIDEO_URL)
            self.assertEqual(path, os.path.realpath(first))
            # an explicit path inside the directory is allowed
            path, code, _ = find_source_file(root, "https://x/y", first)
            self.assertEqual((path, code), (os.path.realpath(first), ""))
            self.assertEqual(find_source_file(
                root, "https://x/y", os.path.join(root, "missing.mp4"))[1],
                ERROR_SOURCE_NOT_FOUND)

    def test_duration_and_progress_parsing(self):
        self.assertAlmostEqual(parse_duration_seconds("00:00:30.00"), 30.0)
        self.assertAlmostEqual(parse_duration_seconds("01:02:03.50"), 3723.5)
        self.assertEqual(parse_duration_seconds("nope"), 0.0)
        event = parse_media_line(
            "  Duration: 00:00:30.00, start: 0.000000, bitrate: 1000 kb/s")
        self.assertEqual((event.kind, event.seconds), ("duration", 30.0))
        event = parse_media_line("out_time_ms=15000000", 30.0)
        self.assertEqual(event.kind, "progress")
        self.assertAlmostEqual(event.percent, 50.0)
        self.assertEqual(parse_media_line("out_time_ms=90000000", 30.0).percent,
                         99.9)
        self.assertIsNone(parse_media_line("out_time_ms=1", 0.0).percent)
        self.assertEqual(parse_media_line("progress=end").kind, "done")
        self.assertEqual(parse_media_line("progress=continue").kind, "info")
        self.assertEqual(parse_media_line("Error while opening decoder").kind,
                         "error")
        self.assertEqual(parse_media_line("frame=1").kind, "info")
        self.assertTrue(has_free_space(tempfile.gettempdir(), 1024))
        self.assertFalse(has_free_space(tempfile.gettempdir(), 1 << 62))


class TestAudioProcessor(unittest.TestCase):
    """T907-T910: success, failure, missing pieces and no phantom output."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mediadock-proc-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.runs = []
        self.manager = TaskManager()
        self.source = make_source(self.dir)
        self.task = self.manager.create(VIDEO_URL)
        self.manager.transition(self.task.task_id, "downloading")

    def make_processor(self, ffmpeg="ffmpeg.exe", returncode=0,
                       create_output=True, extra_lines=()):
        def factory(command, **kwargs):
            process = ScriptedFfmpeg(command, returncode=returncode,
                                     create_output=create_output,
                                     extra_lines=extra_lines)
            self.runs.append(process)
            return process

        return AudioProcessor(self.manager, ffmpeg=ffmpeg,
                              download_dir=self.dir, popen_factory=factory,
                              logger=lambda *a: None)

    def job(self, target="mp3"):
        return {"kind": "audio", "source": self.source, "target": target}

    def test_success_writes_output_and_completes(self):
        result = self.make_processor().run(self.task.task_id, self.job())
        task = self.manager.get(self.task.task_id)
        expected = os.path.join(self.dir, "probe [" + VIDEO_ID + "].mp3")
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.percent, 100.0)
        self.assertEqual(task.file_path, expected)
        self.assertEqual(result["file_path"], expected)
        self.assertTrue(os.path.isfile(expected))
        self.assertEqual(self.runs[0].command[-1], expected)
        self.assertEqual(self.runs[0].command[
            self.runs[0].command.index("-i") + 1], self.source)

    def test_each_target_extension(self):
        for target, extension in (("mp3", ".mp3"), ("m4a", ".m4a"),
                                  ("wav", ".wav")):
            task = self.manager.create(VIDEO_URL)
            self.manager.transition(task.task_id, "downloading")
            self.make_processor().run(task.task_id, self.job(target))
            stored = self.manager.get(task.task_id)
            self.assertEqual(stored.status, "completed", target)
            self.assertTrue(stored.file_path.endswith(extension), target)

    def test_failure_leaves_no_output_file(self):
        processor = self.make_processor(returncode=1, create_output=False,
                                        extra_lines=("Error opening output",
                                                     ))
        result = processor.run(self.task.task_id, self.job())
        task = self.manager.get(self.task.task_id)
        self.assertEqual(task.status, "error")
        self.assertEqual(task.error_code, "ffmpeg_failed")
        self.assertIn("Error opening output", task.error_message)
        self.assertEqual(result["error_code"], "ffmpeg_failed")
        self.assertFalse(os.path.isfile(
            os.path.join(self.dir, "probe [" + VIDEO_ID + "].mp3")))

    def test_partial_output_is_removed_on_failure(self):
        processor = self.make_processor(returncode=2)
        partial = os.path.join(self.dir, "probe [" + VIDEO_ID + "].mp3")
        with open(partial, "w", encoding="utf-8") as handle:
            handle.write("half done")
        processor.run(self.task.task_id, self.job())
        self.assertEqual(self.manager.get(self.task.task_id).status, "error")
        self.assertFalse(os.path.isfile(partial))

    def test_success_without_output_is_an_error(self):
        processor = self.make_processor(create_output=False)
        result = processor.run(self.task.task_id, self.job())
        self.assertEqual(result["error_code"], ERROR_OUTPUT_MISSING)
        self.assertEqual(self.manager.get(self.task.task_id).error_code,
                         ERROR_OUTPUT_MISSING)

    def test_missing_ffmpeg_and_missing_source(self):
        result = self.make_processor(ffmpeg="").run(self.task.task_id,
                                                    self.job())
        self.assertEqual(result["error_code"], ERROR_FFMPEG_MISSING)
        task = self.manager.create(VIDEO_URL)
        self.manager.transition(task.task_id, "downloading")
        result = self.make_processor().run(task.task_id,
                                           {"kind": "audio", "target": "mp3"})
        self.assertEqual(result["error_code"], ERROR_SOURCE_NOT_FOUND)

    def test_invalid_target_never_spawns(self):
        result = self.make_processor().run(self.task.task_id,
                                           self.job("flac"))
        self.assertEqual(result["error_code"], ERROR_INVALID_TARGET)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.manager.get(self.task.task_id).error_code,
                         ERROR_INVALID_TARGET)

    def test_cancel_before_start(self):
        control = TaskControl(self.task.task_id, lambda *a: None)
        control.request_cancel()
        result = self.make_processor().run(self.task.task_id, self.job(),
                                           control)
        self.assertEqual(result["error_code"], "cancelled")
        self.assertEqual(self.manager.get(self.task.task_id).status,
                         "cancelled")

    def test_pause_before_start(self):
        control = TaskControl(self.task.task_id, lambda *a: None)
        control.request_pause()
        result = self.make_processor().run(self.task.task_id, self.job(),
                                           control)
        self.assertEqual(result["error_code"], "paused")
        self.assertEqual(self.manager.get(self.task.task_id).status, "paused")


def raw_request(port, path, method="GET", payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_status(task_id, statuses, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = srv.manager.get(task_id)
        if task is None or task.status in statuses:
            return task.status if task else None
        time.sleep(0.02)
    return srv.manager.get(task_id).status


class MediaApiBase(unittest.TestCase):
    """T911-T913: the `/audio` HTTP contract on the real handler."""

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
        self.tmp = tempfile.TemporaryDirectory(prefix="mediadock-media-api-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.runs = []
        self._old_dir = srv.DOWNLOAD_DIR
        self._old_popen = srv.MEDIA_POPEN_FACTORY
        self._old_processor = srv.MEDIA_PROCESSOR_FACTORY
        srv.DOWNLOAD_DIR = self.dir
        srv.MEDIA_POPEN_FACTORY = self._factory
        srv.set_media_processor(None)
        srv.bootstrap(":memory:")

    def tearDown(self):
        for task_id in list(srv.scheduler.active_ids()) + \
                list(srv.scheduler.queued_ids()):
            try:
                srv.scheduler.cancel(task_id, timeout=5)
            except Exception:  # noqa: BLE001 - test cleanup only
                pass
        srv.scheduler.wait_idle(10)
        srv.DOWNLOAD_DIR = self._old_dir
        srv.MEDIA_POPEN_FACTORY = self._old_popen
        srv.MEDIA_PROCESSOR_FACTORY = self._old_processor
        srv.bootstrap(":memory:")

    def _factory(self, command, **kwargs):
        process = ScriptedFfmpeg(command)
        self.runs.append(process)
        return process

    def _json(self, path, method="GET", payload=None):
        code, raw = raw_request(self.port, path, method=method, payload=payload)
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, {"raw": raw}

    def completed_download(self, url=VIDEO_URL):
        task = srv.manager.create(url)
        srv.manager.transition(task.task_id, "downloading")
        srv.manager.transition(task.task_id, "completed", percent=100.0)
        return task.task_id

    def test_audio_discovery_lists_targets(self):
        code, data = self._json("/audio")
        self.assertEqual(code, 200, data)
        self.assertEqual(sorted(data), ["audio_type", "default", "targets"])
        self.assertEqual([t["name"] for t in data["targets"]],
                         ["mp3", "m4a", "wav"])
        self.assertEqual(data["default"], "mp3")
        self.assertEqual(data["audio_type"], AUDIO_TASK_TYPE)

    def test_audio_requires_a_completed_download(self):
        code, data = self._json("/audio", "POST", {})
        self.assertEqual((code, data["error_code"]), (400, "missing_task_id"))
        code, data = self._json("/audio", "POST", {"task_id": "no such id"})
        self.assertEqual((code, data["error_code"]), (400, "invalid_task_id"))
        code, data = self._json("/audio", "POST", {"task_id": "unknown123"})
        self.assertEqual((code, data["error_code"]), (404, "task_not_found"))
        pending = srv.manager.create(VIDEO_URL)
        code, data = self._json("/audio", "POST", {"task_id": pending.task_id})
        self.assertEqual((code, data["error_code"]), (409, "not_completed"))
        code, data = self._json("/audio?target=flac", "POST",
                                {"task_id": pending.task_id})
        self.assertEqual((code, data["error_code"]),
                         (400, ERROR_INVALID_TARGET))
        self.assertEqual(self.runs, [])

    def test_audio_missing_source_file(self):
        task_id = self.completed_download()
        code, data = self._json("/audio", "POST", {"task_id": task_id})
        self.assertEqual((code, data["error_code"]),
                         (404, ERROR_SOURCE_NOT_FOUND))

    def test_audio_conversion_success(self):
        make_source(self.dir)
        task_id = self.completed_download()
        code, data = self._json("/audio?target=mp3", "POST",
                                {"task_id": task_id})
        self.assertEqual(code, 200, data)
        self.assertEqual(sorted(data),
                         ["source", "source_task_id", "target", "task_id"])
        self.assertEqual(data["source_task_id"], task_id)
        self.assertEqual(data["target"], "mp3")
        media_id = data["task_id"]
        self.assertNotEqual(media_id, task_id)
        self.assertEqual(wait_status(media_id, ("completed", "error")),
                         "completed")
        media = srv.manager.get(media_id)
        self.assertEqual(media.type, AUDIO_TASK_TYPE)
        self.assertEqual(media.platform, srv.manager.get(task_id).platform)
        self.assertEqual(media.file_path,
                         os.path.join(os.path.realpath(self.dir),
                                      "probe [" + VIDEO_ID + "].mp3"))
        self.assertTrue(os.path.isfile(media.file_path))
        self.assertEqual(len(self.runs), 1)
        command = self.runs[0].command
        self.assertEqual(command[0], srv.FFMPEG)
        self.assertEqual(command[command.index("-i") + 1], data["source"])
        self.assertEqual(command[-1], media.file_path)
        # history and events see the media task exactly like a download task
        code, history = self._json("/history?status=completed")
        self.assertEqual(code, 200)
        self.assertIn(media_id, [t["task_id"] for t in history["tasks"]])
        code, events = self._json("/events?id=" + media_id)
        self.assertEqual(code, 200)
        self.assertIn("created", [e["kind"] for e in events["events"]])

    def test_audio_target_variants(self):
        make_source(self.dir)
        for target, extension in (("m4a", ".m4a"), ("wav", ".wav")):
            task_id = self.completed_download()
            code, data = self._json("/audio?target=" + target, "POST",
                                    {"task_id": task_id})
            self.assertEqual(code, 200, (target, data))
            self.assertEqual(wait_status(data["task_id"], ("completed",)),
                             "completed")
            self.assertTrue(srv.manager.get(
                data["task_id"]).file_path.endswith(extension), target)


if __name__ == "__main__":
    unittest.main()
