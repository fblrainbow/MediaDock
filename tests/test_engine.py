"""Stage-002 parser/engine tests (T207-T211)."""
import unittest

from core_engine import DownloadEngine, FORMAT_EXPR, build_command
from core_manager import TaskManager
from core_parse import parse_line


class FakeProcess:
    def __init__(self, lines, returncode=0):
        self._lines = lines
        self.returncode = returncode

    @property
    def stdout(self):
        return iter(self._lines)

    def wait(self):
        return self.returncode


def make_manager():
    return TaskManager()


class TestParser(unittest.TestCase):
    def test_progress(self):
        e = parse_line("[download]  8.8% of ~ 50.00MiB at 22.68KiB/s ETA 47:53")
        self.assertEqual(e.kind, "progress")
        self.assertAlmostEqual(e.percent, 8.8)

    def test_merging(self):
        e = parse_line('[Merger] Merging formats into "o.mp4"')
        self.assertEqual(e.kind, "merging")

    def test_destination_ignored(self):
        e = parse_line("[download] Destination: downloads\\x.mp4")
        self.assertEqual(e.kind, "ignored")

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
        self.assertEqual(m.get(tid).status, "completed")
        self.assertEqual(m.get(tid).percent, 100.0)

    def test_failure(self):
        m, tid, res = self._run(["some output"], 1)
        self.assertEqual(m.get(tid).status, "error")
        self.assertEqual(m.get(tid).error_code, "exit_code")

    def test_missing_binary(self):
        def boom(*a, **k):
            raise FileNotFoundError("nope")
        m, tid, res = self._run([], factory=boom)
        self.assertEqual(m.get(tid).status, "error")
        self.assertEqual(m.get(tid).error_code, "ytdlp_missing")


if __name__ == "__main__":
    unittest.main()
