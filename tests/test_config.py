"""Stage-006 configuration layer (T601-T608, T626). Temp files only."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import core_config
import server as srv


def write_config(directory, data, name="config.json"):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    return path


class ConfigBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mediadock-cfg-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def load(self, data=None, env=None, name="config.json"):
        """Load a config from a temp file (or defaults when `data` is None)."""
        path = write_config(self.dir, data or {}, name) if data is not None \
            else None
        return core_config.load_config(path=path, env=env or {})


class TestDefaults(ConfigBase):
    def test_no_file_uses_defaults(self):
        """T601: no config.json anywhere => built-in defaults, no errors."""
        with mock.patch.object(core_config, "BASE_DIR", self.dir):
            config = core_config.load_config(env={})
        self.assertEqual(config.source, "defaults")
        self.assertEqual(config.path, "")
        self.assertEqual(config.errors, ())
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8765)
        self.assertEqual(config.max_active_tasks, 3)
        self.assertEqual(config.purge_keep, 200)
        self.assertEqual(config.log_level, "info")
        self.assertFalse(config.allow_lan)
        # defaults are repo-relative and are not affected by the patch
        self.assertEqual(config.download_dir,
                         core_config.DEFAULTS["download_dir"])

    def test_default_config_file_is_repo_relative(self):
        self.assertEqual(core_config.DEFAULTS["log_file"],
                         os.path.join(core_config.BASE_DIR,
                                      "MediaDock-server.log"))


class TestFileLoading(ConfigBase):
    def test_valid_file_is_applied(self):
        """T602/T607: values are adopted and relative paths are resolved."""
        config = self.load({"port": 9001, "log_level": "debug",
                            "max_active_tasks": 5, "download_dir": "dl",
                            "log_file": "logs\\server.log",
                            "ytdlp_path": "bin\\yt-dlp.exe"})
        self.assertEqual(config.source, "file")
        self.assertTrue(config.path.endswith("config.json"))
        self.assertEqual(config.port, 9001)
        self.assertEqual(config.log_level, "debug")
        self.assertEqual(config.max_active_tasks, 5)
        self.assertEqual(config.download_dir,
                         os.path.join(core_config.BASE_DIR, "dl"))
        self.assertTrue(os.path.isabs(config.log_file))
        self.assertTrue(os.path.isabs(config.ytdlp_path))
        self.assertEqual(config.errors, ())

    def test_invalid_values_fall_back(self):
        """T603: bad types/ranges/enums never raise; they are reported."""
        config = self.load({"port": "abc", "max_active_tasks": 99,
                            "log_level": "loud", "allow_lan": "maybe",
                            "request_max_bytes": 5})
        self.assertEqual(config.port, 8765)
        self.assertEqual(config.max_active_tasks, 3)
        self.assertEqual(config.log_level, "info")
        self.assertEqual(config.request_max_bytes, 65536)
        self.assertFalse(config.allow_lan)
        self.assertEqual(len(config.errors), 5)
        self.assertTrue(all("(in config file)" in e for e in config.errors))

    def test_unknown_key_is_reported(self):
        """T604: a typo must not be silently ignored."""
        config = self.load({"donwload_dir": "x"})
        self.assertTrue(any("unknown_config_key" in e
                            for e in config.errors))
        self.assertNotIn("donwload_dir", config.values())

    def test_missing_explicit_file_is_reported(self):
        config = core_config.load_config(
            path=os.path.join(self.dir, "nope.json"), env={})
        self.assertEqual(config.source, "defaults")
        self.assertTrue(any("not found" in e for e in config.errors))

    def test_broken_json_is_reported(self):
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        config = core_config.load_config(path=path, env={})
        self.assertEqual(config.source, "defaults")
        self.assertTrue(config.errors)


class TestHostEnforcement(ConfigBase):
    def test_non_loopback_falls_back(self):
        """T605: no allow_lan => refuse and fall back to 127.0.0.1."""
        config = self.load({"host": "0.0.0.0"})
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.warnings, ())
        self.assertTrue(any("not a loopback" in e for e in config.errors))

    def test_non_loopback_with_allow_lan_warns(self):
        config = self.load({"host": "192.168.1.50", "allow_lan": True})
        self.assertEqual(config.host, "192.168.1.50")
        self.assertEqual(config.errors, ())
        self.assertTrue(any("local network" in w for w in config.warnings))

    def test_loopback_aliases_are_quiet(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            config = self.load({"host": host})
            self.assertEqual(config.host, host)
            self.assertEqual((config.errors, config.warnings), ((), ()))


class TestEnvOverrides(ConfigBase):
    def test_env_wins_over_file(self):
        """T606: env overrides the file and marks the source as `env`."""
        config = self.load({"port": 9001},
                           env={"MEDIADOCK_PORT": "9002",
                                "MEDIADOCK_LOG_LEVEL": "warning"})
        self.assertEqual(config.port, 9002)
        self.assertEqual(config.log_level, "warning")
        self.assertEqual(config.source, "env")

    def test_bad_env_value_is_reported_and_ignored(self):
        config = self.load(None, env={"MEDIADOCK_PORT": "http"})
        self.assertEqual(config.port, 8765)
        self.assertTrue(any("MEDIADOCK_PORT" in e for e in config.errors))

    def test_config_env_var_points_at_the_file(self):
        path = write_config(self.dir, {"port": 9100})
        config = core_config.load_config(
            env={core_config.CONFIG_ENV_VAR: path})
        self.assertEqual(config.port, 9100)
        self.assertEqual(config.path, path)


class TestPublicPayload(ConfigBase):
    def test_public_payload_shape_and_redaction(self):
        """T620 (shape) + T625 (paths shortened before leaving the process)."""
        home = os.path.expanduser("~")
        config = self.load({"download_dir": os.path.join(home, "MediaDockDL"),
                            "port": 9001})
        payload = core_config.config_public(config)
        self.assertEqual(sorted(payload),
                         ["errors", "ok", "path", "source", "values",
                          "warnings"])
        self.assertEqual(payload["source"], "file")
        self.assertTrue(payload["ok"])
        self.assertNotIn(home, json.dumps(payload))
        self.assertTrue(payload["values"]["download_dir"].startswith("~"))
        self.assertEqual(payload["values"]["port"], 9001)


class TestServerConfigWiring(unittest.TestCase):
    """T608/T626: the running server really uses the configured values."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mediadock-cfg-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def tearDown(self):
        # Restore the built-in defaults so later test modules still see the
        # frozen Stage-004 concurrency limit of 3.
        srv.bootstrap(":memory:", config=core_config.load_config(env={}))

    def test_bootstrap_applies_config(self):
        """T626: download dir, concurrency limit and log level are wired."""
        download_dir = os.path.join(self.dir, "dl")
        path = write_config(self.dir, {
            "download_dir": download_dir, "max_active_tasks": 1,
            "log_file": os.path.join(self.dir, "test.log"),
            "log_level": "warning", "purge_keep": 5})
        config = core_config.load_config(path=path, env={})
        srv.bootstrap(":memory:", config=config)
        self.assertEqual(srv.DOWNLOAD_DIR, download_dir)
        self.assertTrue(os.path.isdir(download_dir))
        self.assertEqual(srv.scheduler.active_limit, 1)
        self.assertEqual(srv.CONFIG.max_active_tasks, 1)
        self.assertEqual(srv.LOG_LEVEL, "warning")
        self.assertEqual(srv.LOG_FILE, os.path.join(self.dir, "test.log"))
        self.assertEqual(srv.storage_info()["db"], ":memory:")
        self.assertTrue(srv.DOWNLOAD_DIR.endswith("dl"))

    def test_bootstrap_restores_concurrency(self):
        srv.bootstrap(":memory:")
        self.assertEqual(srv.scheduler.active_limit, 3)

    def test_check_config_cli(self):
        """T608: exit code 0/1/2 and JSON output, without starting a server."""
        good = write_config(self.dir, {
            "ytdlp_path": sys.executable, "ffmpeg_path": sys.executable,
            "download_dir": self.dir,
            "log_file": os.path.join(self.dir, "check.log")}, name="good.json")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = srv.run_check_config(["--check-config", "--config", good])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["config"]["source"], "file")
        self.assertIn("dependencies", payload)
        self.assertTrue(payload["dependencies"]["checks"])

        bad = write_config(self.dir, {"port": "abc"}, name="bad.json")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srv.run_check_config(["--config", bad]), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srv.run_check_config(
                ["--config", os.path.join(self.dir, "missing.json")]), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srv.run_check_config(["--nope"]), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srv.run_check_config(["--config"]), 2)


if __name__ == "__main__":
    unittest.main()
