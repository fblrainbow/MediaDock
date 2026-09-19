"""Stage-006 dependency diagnostics (T615-T619). No real yt-dlp/FFmpeg."""
import os
import sys
import tempfile
import unittest
from unittest import mock

import core_deps
from core_config import Config, load_config


def config_for(directory, **overrides):
    """A `Config` with the given download dir plus optional overrides."""
    data = {"download_dir": directory}
    data.update({k: v for k, v in overrides.items()
                 if k in Config.__dataclass_fields__})
    return Config(**data)


class TestExecutableChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mediadock-deps-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_detected_executable_is_ok(self):
        """T615: an existing executable is reported as `ok`."""
        result = core_deps.check_executable(
            "yt-dlp", sys.executable, core_deps.resolve_ytdlp, "missing")
        self.assertEqual(result["status"], core_deps.OK)
        self.assertEqual(result["path"], sys.executable)
        self.assertEqual(result["name"], "yt-dlp")

    def test_configured_directory_is_accepted(self):
        result = core_deps.check_executable(
            "ffmpeg", self.dir, core_deps.resolve_ffmpeg, "missing")
        self.assertEqual(result["status"], core_deps.OK)
        self.assertIn("directory", result["message"])

    def test_missing_configured_path_is_an_error(self):
        missing = os.path.join(self.dir, "nope.exe")
        result = core_deps.check_executable(
            "yt-dlp", missing, lambda: "", "missing")
        self.assertEqual(result["status"], core_deps.ERROR)
        self.assertIn("does not exist", result["message"])

    def test_missing_binary_is_reported_not_raised(self):
        result = core_deps.check_executable(
            "yt-dlp", "", lambda: "", "yt-dlp was not found")
        self.assertEqual(result["status"], core_deps.MISSING)
        self.assertIn("not found", result["message"])

    def test_ffmpeg_missing_is_only_a_warning(self):
        """T616: FFmpeg absence must not fail the dependency snapshot."""
        with mock.patch.object(core_deps, "resolve_ffmpeg", lambda: ""):
            result = core_deps.check_ffmpeg(config_for(self.dir))
        self.assertEqual(result["status"], core_deps.WARN)

    def test_ffmpeg_detected_is_ok(self):
        with mock.patch.object(core_deps, "resolve_ffmpeg",
                               lambda: sys.executable):
            result = core_deps.check_ffmpeg(config_for(self.dir))
        self.assertEqual(result["status"], core_deps.OK)


class TestDirectoryAndDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mediadock-deps-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_download_dir_is_created(self):
        """T617: a missing download dir is created and proven writable."""
        target = os.path.join(self.dir, "downloads", "nested")
        result = core_deps.check_download_dir(config_for(target))
        self.assertEqual(result["status"], core_deps.OK)
        self.assertTrue(os.path.isdir(target))

    def test_file_blocking_the_path_is_an_error(self):
        blocker = os.path.join(self.dir, "blocked")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        result = core_deps.check_download_dir(config_for(blocker))
        self.assertEqual(result["status"], core_deps.ERROR)
        self.assertTrue(result["message"])

    def test_empty_download_dir_is_an_error(self):
        result = core_deps.check_download_dir(config_for(""))
        self.assertEqual(result["status"], core_deps.ERROR)

    def test_disk_space_thresholds(self):
        """T618: real usage plus an injected threshold."""
        self.assertEqual(core_deps.check_disk_space(self.dir)["status"],
                         core_deps.OK)
        low = core_deps.check_disk_space(self.dir, min_free_mb=10 ** 9)
        self.assertEqual(low["status"], core_deps.WARN)
        self.assertIn("free", low["message"])

    def test_disk_space_missing_path(self):
        result = core_deps.check_disk_space(os.path.join(self.dir, "nope"))
        self.assertEqual(result["status"], core_deps.ERROR)


class TestSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mediadock-deps-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _config(self):
        return config_for(self.dir, ytdlp_path=sys.executable,
                          ffmpeg_path=sys.executable)

    def test_deep_false_never_spawns_a_process(self):
        """T619: the startup snapshot must not run subprocesses."""
        with mock.patch.object(core_deps.subprocess, "run") as runner:
            snapshot = core_deps.run_checks(self._config(), deep=False)
        runner.assert_not_called()
        self.assertTrue(snapshot["ok"])
        self.assertEqual([c["name"] for c in snapshot["checks"]],
                         ["yt-dlp", "ffmpeg", "download_dir", "disk_space"])
        self.assertTrue(snapshot["checked_at"])

    def test_probe_version_uses_the_real_executable(self):
        result = core_deps.probe_version(sys.executable, "python")
        self.assertEqual(result["status"], core_deps.OK)
        self.assertIn("Python", result["message"])

    def test_deep_true_adds_version_checks(self):
        snapshot = core_deps.run_checks(self._config(), deep=True)
        self.assertEqual(len(snapshot["checks"]), 6)

    def test_failed_checks_lists_hard_errors_only(self):
        config = config_for(self.dir,
                            ytdlp_path=os.path.join(self.dir, "no.exe"))
        snapshot = core_deps.run_checks(config)
        self.assertIn("yt-dlp", core_deps.failed_checks(snapshot))
        self.assertFalse(snapshot["ok"])

    def test_load_config_defaults_are_checkable(self):
        config = load_config(env={})
        snapshot = core_deps.run_checks(config)
        self.assertIn("checks", snapshot)
        self.assertIsInstance(snapshot["ok"], bool)


if __name__ == "__main__":
    unittest.main()
