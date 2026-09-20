"""Stage-002 parser/engine tests (T207-T211) + Stage-004 control tests."""
import os
import tempfile
import threading
import time
import unittest
from typing import cast

from core_control import TaskControl
from core_engine import DownloadEngine, FORMAT_EXPR, build_command
from core_manager import TaskManager
from core_parse import parse_line, parse_size


class FakeProcess:
    def __init__(self, lines, returncode=0):
        self._lines = lines
        self.returncode = returncode

    @property
    def stdout(self):
        return iter(self._lines)

    def wait(self):
        return self.returncode


class SlowProcess(FakeProcess):
    """Process double that streams slowly and can be stopped.

    It deliberately has no `pid` attribute, so `terminate_tree` uses
    `terminate()` instead of `taskkill` (never touch a real process here).
    """

    def __init__(self, lines, returncode=0, delay=0.02):
        super().__init__(lines, returncode)
        self.returncode = None
        self._final = returncode
        self._delay = delay
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.returncode = 1 if self.terminated else self._final
        return self.returncode

    @property
    def stdout(self):
        for line in self._lines:
            if self.terminated:
                return
            yield line
            time.sleep(self._delay)


def make_manager():
    return TaskManager()


class TestParser(unittest.TestCase):
    def test_progress(self):
        e = parse_line("[download]  8.8% of ~ 50.00MiB at 22.68KiB/s ETA 47:53")
        self.assertEqual(e.kind, "progress")
        percent = e.percent
        self.assertIsNotNone(percent)
        self.assertAlmostEqual(float(percent or 0.0), 8.8)

    def test_merging(self):
        e = parse_line("[Merger] Merging formats into \"o.mp4\"")
        self.assertEqual((e.kind, e.path), ("merged", "o.mp4"))

    def test_merging_without_path_is_progress_only(self):
        e = parse_line("[Merger] Something else happened")
        self.assertEqual(e.kind, "merging")

    def test_destination_records_path(self):
        e = parse_line("[download] Destination: downloads\\x.mp4")
        self.assertEqual((e.kind, e.path), ("destination", "downloads\\x.mp4"))

    def test_already_downloaded_records_path(self):
        e = parse_line("[download] downloads\\x.mp4 has already been downloaded")
        self.assertEqual(e.kind, "destination")
        self.assertIn("x.mp4", e.path)

    def test_merge_path_recorded(self):
        e = parse_line('[Merger] Merging formats into "downloads\\o.mp4"')
        self.assertEqual((e.kind, e.path), ("merged", "downloads\\o.mp4"))

    def test_extract_audio_records_the_mp3(self):
        """Stage-012: `-x` reports the final file, and it must be recorded."""
        e = parse_line("[ExtractAudio] Destination: downloads\\song [abc].mp3")
        self.assertEqual(e.kind, "merged")
        self.assertTrue(e.path.endswith("song [abc].mp3"), e.path)

    def test_progress_line_records_total_size(self):
        """Stage-013: the `of ~ 50.00MiB` token drives the row's size."""
        e = parse_line("[download]  8.8% of ~ 50.00MiB at 22.68KiB/s ETA 47:53")
        self.assertEqual(e.size, "50.00MiB")
        # 现有分组契约不变（test_baseline 依赖 group(2) 是速度）
        e2 = parse_line("[download]   1.4% of ~ 368.62MiB at 1.55MiB/s "
                        "ETA 01:31 (frag 4/364)")
        self.assertEqual(e2.size, "368.62MiB")
        self.assertEqual(e2.speed, "1.55MiB/s")

    def test_progress_line_without_size(self):
        e = parse_line("[download] 100% of 1MiB at 1MiB/s ETA 00:00")
        self.assertEqual(e.kind, "progress")
        self.assertIn(e.size, ("", "1MiB"))

    def test_parse_size_units(self):
        self.assertEqual(parse_size("1MiB"), 1024 * 1024)
        self.assertEqual(parse_size("1.5MiB"), int(1.5 * 1024 * 1024))
        self.assertEqual(parse_size("2KiB"), 2048)
        self.assertEqual(parse_size("1MB"), 1000 * 1000)
        self.assertEqual(parse_size("1.5GiB"), int(1.5 * 1024 ** 3))
        self.assertEqual(parse_size("500B"), 500)
        for bad in ("", "abc", "12", "MiB"):
            self.assertEqual(parse_size(bad), 0, bad)

    def test_title(self):
        e = parse_line("[info] Some Video: Downloading video")
        self.assertEqual((e.kind, e.title), ("title", "Some Video"))

    def test_unknown_ignored(self):
        self.assertEqual(parse_line("[info] hello").kind, "ignored")
        self.assertEqual(parse_line("").kind, "ignored")


class TestCommand(unittest.TestCase):
    def test_build_command_policy(self):
        cmd = build_command("YT", "DIR", "URL")
        self.assertEqual(cmd[0], "YT")
        self.assertIn(FORMAT_EXPR, cmd)
        self.assertIn("bv*[height<=1080][ext=mp4]", FORMAT_EXPR)
        self.assertEqual(cmd[-3:], ["-P", "DIR", "URL"])
        for flag in ("--merge-output-format", "mp4", "--newline",
                     "--no-playlist"):
            self.assertIn(flag, cmd)

    def test_audio_format_replaces_merge(self):
        """Stage-012: `audio_format` extracts audio instead of merging MP4."""
        cmd = build_command("YT", "DIR", "URL", "FF", "bestaudio/best",
                            "mp3")
        self.assertEqual(cmd[cmd.index("-f") + 1], "bestaudio/best")
        self.assertEqual(cmd[cmd.index("--audio-format") + 1], "mp3")
        self.assertIn("--extract-audio", cmd)
        self.assertNotIn("--merge-output-format", cmd)
        self.assertEqual(cmd[-3:], ["-P", "DIR", "URL"])
        self.assertEqual(cmd[cmd.index("--ffmpeg-location") + 1], "FF")

    def test_empty_audio_format_keeps_the_frozen_policy(self):
        for value in ("", "   "):
            cmd = build_command("YT", "DIR", "URL", "", FORMAT_EXPR, value)
            self.assertIn("--merge-output-format", cmd, repr(value))
            self.assertNotIn("--extract-audio", cmd, repr(value))
        # None（字段没设过）也走默认策略
        cmd = build_command("YT", "DIR", "URL", "", FORMAT_EXPR,
                            cast(str, None))
        self.assertIn("--merge-output-format", cmd)
        self.assertNotIn("--extract-audio", cmd)


def get_task(manager, task_id):
    """`manager.get()` narrowed for tests: fail loudly instead of a bare None."""
    task = manager.get(task_id)
    assert task is not None, f"task {task_id} disappeared"
    return task


class TestEngine(unittest.TestCase):
    def _run(self, lines, returncode=0, factory=None):
        m = make_manager()
        t = m.create("https://example.com/v")
        eng = DownloadEngine(m, ytdlp="FAKE-YTDLP", download_dir="DIR",
                             popen_factory=factory or
                             (lambda *a, **k: FakeProcess(lines, returncode)),
                             logger=lambda *a: None)
        res = eng.run(t.task_id, "https://example.com/v")
        return m, t.task_id, res

    def test_success(self):
        lines = ["[download]  8.8% of ~ 1MiB at 1KiB/s ETA 00:01",
                 '[Merger] Merging formats into "o.mp4"']
        m, tid, res = self._run(lines, 0)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(get_task(m, tid).status, "completed")
        self.assertEqual(get_task(m, tid).percent, 100.0)

    def test_failure(self):
        m, tid, res = self._run(["some output"], 1)
        self.assertEqual(get_task(m, tid).status, "error")
        self.assertEqual(get_task(m, tid).error_code, "exit_code")

    def test_missing_binary(self):
        def boom(*a, **k):
            raise FileNotFoundError("nope")
        m, tid, res = self._run([], factory=boom)
        self.assertEqual(get_task(m, tid).status, "error")
        self.assertEqual(get_task(m, tid).error_code, "ytdlp_missing")


def wait_status(manager, task_id, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.get(task_id)
        if task is not None and task.status == status:
            return True
        time.sleep(0.01)
    return False


class TestEngineControl(unittest.TestCase):
    """T408/T409/T415-at-engine-level: pause keeps, cancel deletes."""

    def _setup(self, tmp, lines, returncode=0, delay=0.05):
        m = TaskManager()
        task = m.create("https://www.youtube.com/watch?v=abcdefghijk")
        process = SlowProcess(lines, returncode=returncode, delay=delay)
        engine = DownloadEngine(m, ytdlp="FAKE-YTDLP", download_dir=tmp,
                                popen_factory=lambda *a, **k: process,
                                logger=lambda *a: None)
        return m, task, engine, process

    def _run_with_control(self, engine, task, control, timeout=10.0):
        outcome = {}

        def worker():
            outcome["result"] = engine.run(task.task_id, task.url, control)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(wait_status(engine.manager, task.task_id,
                                    "downloading", 5.0),
                        "engine never started")
        return thread, outcome

    def test_pause_marks_paused_and_keeps_files(self):
        tmp = tempfile.mkdtemp(prefix="mediadock-engine-")
        target = os.path.join(tmp, "video [abcdefghijk].mp4")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("partial data")
        lines = ["[download] Destination: " + target,
                 "[download]   5.0% of ~ 1MiB at 1KiB/s ETA 00:01",
                 "[download]  40.0% of ~ 1MiB at 1KiB/s ETA 00:01"]
        m, task, engine, process = self._setup(tmp, lines)
        control = TaskControl(task.task_id)
        thread, outcome = self._run_with_control(engine, task, control)
        self.assertTrue(control.request_pause())
        thread.join(10)
        stored = get_task(m, task.task_id)
        self.assertEqual(stored.status, "paused")
        self.assertEqual(stored.error_code, "")
        self.assertEqual(stored.error_message, "")
        self.assertTrue(os.path.isfile(target), "breakpoint file was deleted")
        self.assertEqual(control.artifacts(), [target])
        self.assertEqual(outcome["result"].error_code, "paused")
        # 幂等：重复暂停不产生第二次转换
        self.assertFalse(control.request_pause())
        self.assertEqual(get_task(m, task.task_id).status, "paused")

    def test_cancel_marks_cancelled_and_deletes_run_files(self):
        tmp = tempfile.mkdtemp(prefix="mediadock-engine-")
        target = os.path.join(tmp, "video [abcdefghijk].mp4")
        part = os.path.join(tmp, "video [abcdefghijk].mp4.part")
        for path in (target, part):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("partial data")
        lines = ["[download] Destination: " + target,
                 "[download]   5.0% of ~ 1MiB at 1KiB/s ETA 00:01",
                 "[download]  40.0% of ~ 1MiB at 1KiB/s ETA 00:01"]
        m, task, engine, process = self._setup(tmp, lines)
        control = TaskControl(task.task_id)
        thread, outcome = self._run_with_control(engine, task, control)
        self.assertTrue(control.request_cancel())
        thread.join(10)
        stored = get_task(m, task.task_id)
        self.assertEqual(stored.status, "cancelled")
        self.assertFalse(os.path.exists(target), "run output was not removed")
        self.assertFalse(os.path.exists(part), "temp file was not removed")
        self.assertEqual(outcome["result"].error_code, "cancelled")

    def test_cleanup_skips_without_started_at(self):
        tmp = tempfile.mkdtemp(prefix="mediadock-engine-")
        target = os.path.join(tmp, "video [abcdefghijk].mp4")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("never started")
        m, task, engine, process = self._setup(tmp, [])
        control = TaskControl(task.task_id)
        control.record_artifacts(target)
        # started_at 为空：无法区分本次与旧成果，取消必须跳过清理
        self.assertEqual(engine.cleanup(task.task_id, control), [])
        self.assertTrue(os.path.exists(target))

    def test_cleanup_keeps_output_older_than_started_at(self):
        tmp = tempfile.mkdtemp(prefix="mediadock-engine-")
        old = os.path.join(tmp, "video [abcdefghijk].mp4")
        with open(old, "w", encoding="utf-8") as fh:
            fh.write("previous download")
        past = time.time() - 3600
        os.utime(old, (past, past))
        m, task, engine, process = self._setup(tmp, [])
        m.transition(task.task_id, "downloading")
        control = TaskControl(task.task_id)
        control.record_artifacts(old)
        self.assertEqual(engine.cleanup(task.task_id, control), [])
        self.assertTrue(os.path.exists(old), "old output must be kept")


if __name__ == "__main__":
    unittest.main()
