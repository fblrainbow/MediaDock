"""Stage-011 single-instance takeover tests (T1101-T1108).

Every external command and every "is the port free?" probe is injected, so no
process is ever killed and no port is ever bound by these tests.
"""
import base64
import unittest

import core_instance as ci


def owner_line(pid=4242, name="pythonw.exe",
               cmd='"C:\\Python313\\pythonw.exe" server.py'):
    return "%d|%s|%s" % (pid, name, cmd)


class FakeRunner:
    """Records commands and replays a scripted result per command prefix."""

    def __init__(self, ps_output="", ps_code=0, taskkill_code=0):
        self.ps_output = ps_output
        self.ps_code = ps_code
        self.taskkill_code = taskkill_code
        self.calls = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args and args[0] == "taskkill":
            return self.taskkill_code, "", ""
        return self.ps_code, self.ps_output, ""

    @property
    def killed_pids(self):
        return [c[2] for c in self.calls if c and c[0] == "taskkill"]


class TestPortOwnerParsing(unittest.TestCase):
    """T1101: PowerShell output -> PortOwner, tolerating junk."""

    def test_parses_normal_line(self):
        owner = ci.parse_port_owner(owner_line())
        self.assertEqual(owner.pid, 4242)
        self.assertEqual(owner.name, "pythonw.exe")
        self.assertIn("server.py", owner.command_line)

    def test_last_line_wins_and_junk_is_skipped(self):
        text = "noise without pipes\n\n" + owner_line(pid=7)
        owner = ci.parse_port_owner(text)
        self.assertEqual(owner.pid, 7)

    def test_missing_command_line_is_allowed(self):
        owner = ci.parse_port_owner("99|python.exe")
        self.assertEqual(owner.pid, 99)
        self.assertEqual(owner.command_line, "")

    def test_empty_or_invalid_input(self):
        for text in ("", "   ", "not-a-pid|python.exe", "|python.exe"):
            self.assertIsNone(ci.parse_port_owner(text), repr(text))


class TestMediadockDetection(unittest.TestCase):
    """T1102: only a python process running `server.py` may be stopped."""

    def test_accepts_python_variants(self):
        cases = [
            ("python.exe", '"C:\\Python313\\python.exe" server.py'),
            ("pythonw.exe", 'pythonw.exe E:\\GitHub\\MediaDock\\server.py'),
            ("python.exe", 'python.exe .\\server.py --restart'),
        ]
        for name, cmd in cases:
            owner = ci.PortOwner(pid=1, name=name, command_line=cmd)
            self.assertTrue(owner.is_mediadock(), cmd)

    def test_rejects_other_processes(self):
        cases = [
            ("chrome.exe", "chrome.exe server.py"),
            ("python.exe", 'python.exe other_tool.py'),
            ("python.exe", 'python.exe server.pyx'),
            ("python.exe", 'python.exe myserver.py'),
            ("python.exe", ""),
        ]
        for name, cmd in cases:
            owner = ci.PortOwner(pid=1, name=name, command_line=cmd)
            self.assertFalse(owner.is_mediadock(), cmd)

    def test_rejects_invalid_pid(self):
        owner = ci.PortOwner(pid=0, name="python.exe", command_line="server.py")
        self.assertFalse(owner.is_mediadock())


class TestScriptBuilding(unittest.TestCase):
    """T1103: the PowerShell script carries the port/host and encodes cleanly."""

    def test_tokens_are_replaced(self):
        script = ci.port_owner_script("127.0.0.1", 9123)
        self.assertIn("-LocalPort 9123", script)
        self.assertIn("'127.0.0.1'", script)
        self.assertNotIn("__PORT__", script)
        self.assertNotIn("__HOST__", script)

    def test_powershell_args_use_encoded_command(self):
        args = ci.powershell_args("Write-Output 'hi'")
        self.assertEqual(args[:4], ["powershell", "-NoProfile", "-NonInteractive",
                                    "-EncodedCommand"])
        decoded = base64.b64decode(args[4]).decode("utf-16-le")
        self.assertEqual(decoded, "Write-Output 'hi'")

    def test_find_port_owner_uses_the_runner(self):
        runner = FakeRunner(ps_output=owner_line(pid=555))
        owner = ci.find_port_owner("127.0.0.1", 8765, runner=runner)
        self.assertEqual(owner.pid, 555)

    def test_find_port_owner_returns_none_on_failure(self):
        runner = FakeRunner(ps_output=owner_line(), ps_code=1)
        self.assertIsNone(ci.find_port_owner("127.0.0.1", 8765, runner=runner))


class TestTakeOver(unittest.TestCase):
    """T1104-T1108: takeover decisions, never killing unexpected processes."""

    def test_free_port_does_nothing(self):
        runner = FakeRunner(ps_output=owner_line())
        result = ci.take_over_port("127.0.0.1", 8765, runner=runner,
                                   probe=lambda h, p: True)
        self.assertEqual(result["status"], "free")
        self.assertEqual(runner.calls, [], "must not inspect a free port")

    def test_unknown_owner_is_reported_not_killed(self):
        runner = FakeRunner(ps_output="junk")
        result = ci.take_over_port("127.0.0.1", 8765, runner=runner,
                                   probe=lambda h, p: False)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(runner.killed_pids, [])

    def test_foreign_owner_is_reported_not_killed(self):
        runner = FakeRunner(ps_output=owner_line(name="chrome.exe",
                                                cmd="chrome.exe server.py"))
        result = ci.take_over_port("127.0.0.1", 8765, runner=runner,
                                   probe=lambda h, p: False)
        self.assertEqual(result["status"], "foreign")
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(runner.killed_pids, [])

    def test_mediadock_owner_is_killed_then_port_waited(self):
        runner = FakeRunner(ps_output=owner_line(pid=777))
        waits = []

        def wait(host, port, timeout):
            waits.append((host, port, timeout))
            return True

        result = ci.take_over_port("127.0.0.1", 8765, runner=runner,
                                   probe=lambda h, p: False, wait=wait,
                                   timeout=3.0)
        self.assertEqual(result["status"], "killed")
        self.assertEqual(result["pid"], 777)
        self.assertEqual(runner.killed_pids, ["777"])
        self.assertEqual(waits, [("127.0.0.1", 8765, 3.0)])

    def test_taskkill_failure_is_reported(self):
        runner = FakeRunner(ps_output=owner_line(pid=888), taskkill_code=1)
        result = ci.take_over_port("127.0.0.1", 8765, runner=runner,
                                   probe=lambda h, p: False,
                                   wait=lambda h, p, t: True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("taskkill", result["detail"])

    def test_port_still_busy_after_kill_is_reported(self):
        runner = FakeRunner(ps_output=owner_line(pid=999))
        result = ci.take_over_port("127.0.0.1", 8765, runner=runner,
                                   probe=lambda h, p: False,
                                   wait=lambda h, p, t: False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("still busy", result["detail"])

    def test_wait_port_free_polls_until_ready(self):
        calls = []

        def probe(host, port):
            calls.append(1)
            return len(calls) >= 3

        self.assertTrue(ci.wait_port_free("127.0.0.1", 8765, timeout=5.0,
                                          sleeper=lambda s: None, probe=probe))
        self.assertEqual(len(calls), 3)

    def test_wait_port_free_times_out(self):
        self.assertFalse(ci.wait_port_free("127.0.0.1", 8765, timeout=0.0,
                                           sleeper=lambda s: None,
                                           probe=lambda h, p: False))

    def test_port_is_free_detects_a_bound_socket(self):
        import socket
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        host, port = holder.getsockname()[:2]
        try:
            self.assertFalse(ci.port_is_free(host, port))
        finally:
            holder.close()
        self.assertTrue(ci.port_is_free(host, port))


if __name__ == "__main__":
    unittest.main()
