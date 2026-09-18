"""Stage-004 control-context tests (T406/T407 and T403 helpers). No network."""
import os
import subprocess
import sys
import threading
import time
import unittest

from core_control import (ControlRegistry, TaskControl, terminate_tree)


class CountingProcess:
    """Process double that records terminate() calls."""

    def __init__(self):
        self.returncode = None
        self.terminations = 0

    def terminate(self):
        self.terminations += 1
        self.returncode = 1

    def wait(self, timeout=None):
        return self.returncode


class TestTaskControl(unittest.TestCase):
    def test_pause_is_idempotent_and_stops_process_once(self):
        process = CountingProcess()
        control = TaskControl("t1")
        control.attach_process(process)
        self.assertTrue(control.request_pause())
        self.assertFalse(control.request_pause())
        self.assertEqual(process.terminations, 1)
        self.assertTrue(control.pause_requested())
        self.assertFalse(control.cancel_requested())

    def test_cancel_after_pause_is_still_recorded(self):
        process = CountingProcess()
        control = TaskControl("t2")
        control.attach_process(process)
        self.assertTrue(control.request_pause())
        self.assertTrue(control.request_cancel())
        self.assertFalse(control.request_cancel())
        # 取消优先于暂停，进程只会被终止一次
        self.assertEqual(process.terminations, 1)

    def test_request_without_process_does_not_raise(self):
        control = TaskControl("t3")
        self.assertTrue(control.request_pause())
        self.assertTrue(control.request_cancel())
        self.assertFalse(control.has_process())

    def test_attach_after_request_stops_immediately(self):
        control = TaskControl("t4")
        control.request_pause()
        process = CountingProcess()
        control.attach_process(process)
        self.assertEqual(process.terminations, 1)

    def test_clear_pause_allows_a_new_run(self):
        control = TaskControl("t5")
        control.request_pause()
        control.clear_pause()
        self.assertFalse(control.pause_requested())
        self.assertTrue(control.request_pause())

    def test_artifacts_are_unique_and_copied(self):
        control = TaskControl("t6")
        control.record_artifacts("a.mp4")
        control.record_artifacts(["a.mp4", "b.mp4"])
        control.record_artifacts(None)
        self.assertEqual(control.artifacts(), ["a.mp4", "b.mp4"])
        control.artifacts().append("c.mp4")
        self.assertEqual(control.artifacts(), ["a.mp4", "b.mp4"])

    def test_terminate_tree_is_safe_without_process(self):
        self.assertFalse(terminate_tree(None))
        done = CountingProcess()
        done.returncode = 0
        self.assertFalse(terminate_tree(done))


class TestControlRegistry(unittest.TestCase):
    def test_get_or_create_returns_same_object(self):
        registry = ControlRegistry()
        first = registry.get_or_create("x")
        second = registry.get_or_create("x")
        self.assertIs(first, second)
        self.assertEqual(registry.ids(), ["x"])

    def test_unregister(self):
        registry = ControlRegistry()
        registry.get_or_create("x")
        self.assertIsNotNone(registry.unregister("x"))
        self.assertIsNone(registry.get("x"))
        self.assertIsNone(registry.unregister("x"))

    def test_concurrent_register_and_unregister(self):
        registry = ControlRegistry()
        errors = []

        def worker(index):
            try:
                for _ in range(200):
                    registry.get_or_create(f"t{index % 5}")
                    registry.get("t1")
                    registry.unregister(f"t{index % 5}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


def _pid_alive(pid):
    if sys.platform == "win32":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False)
        return str(pid) in (completed.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@unittest.skipUnless(sys.platform == "win32", "Windows process-tree check")
class TestProcessTreeTermination(unittest.TestCase):
    """T407: killing yt-dlp must not orphan its FFmpeg child on Windows."""

    def test_parent_and_child_are_both_gone(self):
        parent_code = (
            "import subprocess, sys, time;"
            "child = subprocess.Popen([sys.executable, '-c',"
            " 'import time; time.sleep(120)']);"
            "print(child.pid, flush=True);"
            "time.sleep(120)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", parent_code],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            first_line = (process.stdout.readline() or "").strip()
            self.assertTrue(first_line.isdigit(),
                            f"child pid not reported: {first_line!r}")
            child_pid = int(first_line)
            self.assertGreater(child_pid, 0)
            self.assertTrue(_pid_alive(child_pid))
            self.assertTrue(terminate_tree(process))
            process.wait(timeout=15)
            self.assertIsNotNone(process.returncode)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _pid_alive(child_pid):
                time.sleep(0.1)
            self.assertFalse(_pid_alive(process.pid), "parent still alive")
            self.assertFalse(_pid_alive(child_pid), "orphan child still alive")
        finally:
            if process.poll() is None:
                process.kill()
            if process.stdout is not None:
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
