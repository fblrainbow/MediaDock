"""MediaDock single-instance takeover (Stage-011).

`MediaDockServer.allow_reuse_address = False` (Stage-003) means a second start
fails with `WinError 10048` instead of silently sharing the port. This module
turns that failure into a **targeted** restart:

1. find the process that owns the listening socket on `host:port`;
2. only accept it when it really is a MediaDock `server.py` (python/pythonw);
3. terminate that process tree, wait for the port to free up, then let the
   caller bind again.

Design rules:

* We never kill by name pattern — only the PID that actually holds **our**
  port, and only when its command line looks like a MediaDock server.
* Every external command goes through an injectable `runner`, so unit tests
  never touch real processes.
* Nothing here raises: a failed takeover degrades to "could not take over"
  and the caller keeps its original error path.

stdlib only (`base64`, `re`, `socket`, `subprocess`, `time`, `dataclasses`).
"""
from __future__ import annotations

import base64
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

DEFAULT_WAIT = 10.0
POLL_INTERVAL = 0.2
CMD_TIMEOUT = 20.0

# `server.py` as a whole path token: "E:\...\server.py", "./server.py", "server.py"
_SERVER_PY_RE = re.compile(r"(^|[\\/\s\"'])server\.py($|[\s\"'])",
                           re.IGNORECASE)
_PYTHON_NAMES = ("python.exe", "pythonw.exe", "python", "pythonw")

# One PowerShell round trip returns "pid|name|commandline" for the port owner.
# Tokens are plain text (not str.format fields) because the script itself uses
# PowerShell braces; `__PORT__`/`__HOST__` are replaced by `port_owner_script`.
_PORT_OWNER_PS = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$c = Get-NetTCPConnection -State Listen -LocalPort __PORT__ | "
    "Where-Object { $_.LocalAddress -eq '__HOST__' -or "
    "$_.LocalAddress -eq '0.0.0.0' -or $_.LocalAddress -eq '::' } | "
    "Select-Object -First 1;"
    "if (-not $c) { $c = Get-NetTCPConnection -State Listen "
    "-LocalPort __PORT__ | Select-Object -First 1 };"
    "if ($c) { $p = Get-CimInstance Win32_Process "
    "-Filter ('ProcessId=' + $c.OwningProcess);"
    "if ($p) { $p.ProcessId.ToString() + '|' + $p.Name + '|' + "
    "$p.CommandLine } }"
)


@dataclass(frozen=True)
class PortOwner:
    """Process currently listening on a port."""

    pid: int
    name: str
    command_line: str

    def is_mediadock(self) -> bool:
        """True only for a python/pythonw process running a `server.py`."""
        if self.pid <= 0:
            return False
        if str(self.name or "").strip().lower() not in _PYTHON_NAMES:
            return False
        return bool(_SERVER_PY_RE.search(self.command_line or ""))


def encode_powershell(script: str) -> str:
    """Base64(UTF-16LE) form for `powershell -EncodedCommand` (no quoting hell)."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def port_owner_script(host: str, port: int) -> str:
    """PowerShell that prints `pid|name|commandline` for the port owner."""
    return (_PORT_OWNER_PS
            .replace("__PORT__", str(int(port)))
            .replace("__HOST__", str(host or "127.0.0.1")))


def parse_port_owner(text: str) -> Optional[PortOwner]:
    """Parse the PowerShell output; `None` when the port is free/unreadable."""
    for line in str(text or "").splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0].strip())
        except ValueError:
            continue
        name = parts[1].strip()
        command_line = parts[2].strip() if len(parts) > 2 else ""
        return PortOwner(pid=pid, name=name, command_line=command_line)
    return None


def default_runner(args: Sequence[str]) -> Tuple[int, str, str]:
    """Run one command; returns `(returncode, stdout, stderr)`. Never raises."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(list(args), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=CMD_TIMEOUT, **kwargs)
    except Exception as exc:  # noqa: BLE001 - takeover must degrade, not crash
        return 1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def powershell_args(script: str) -> List[str]:
    """`powershell -NoProfile -NonInteractive -EncodedCommand <script>`."""
    return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
            encode_powershell(script)]


def find_port_owner(host: str, port: int,
                    runner: Optional[Callable] = None) -> Optional[PortOwner]:
    """Listening process on `host:port`, or `None` (free port / no tooling)."""
    run = runner or default_runner
    code, out, _ = run(powershell_args(port_owner_script(host, port)))
    if code != 0:
        return None
    return parse_port_owner(out)


def kill_process_tree(pid: int, runner: Optional[Callable] = None) -> bool:
    """`taskkill /PID <pid> /T /F`; False when taskkill reports a failure."""
    run = runner or default_runner
    code, _, _ = run(["taskkill", "/PID", str(int(pid)), "/T", "/F"])
    return code == 0


def port_is_free(host: str, port: int) -> bool:
    """True when we can bind `host:port` right now (release it immediately)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host or "127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except OSError:
            pass


def wait_port_free(host: str, port: int, timeout: float = DEFAULT_WAIT,
                   sleeper: Callable[[float], None] = time.sleep,
                   probe: Optional[Callable[[str, int], bool]] = None) -> bool:
    """Poll until the port is bindable; False on timeout."""
    check = probe or port_is_free
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if check(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        sleeper(POLL_INTERVAL)


def take_over_port(host: str, port: int, logger: Optional[Callable] = None,
                   runner: Optional[Callable] = None,
                   timeout: float = DEFAULT_WAIT,
                   wait: Optional[Callable] = None,
                   probe: Optional[Callable[[str, int], bool]] = None) -> dict:
    """Free `host:port` from a previous MediaDock instance.

    Returns `{"status", "pid", "name", "command_line", "detail"}` where status
    is one of `free` (nothing to do), `killed`, `foreign` (someone else owns
    it), `unknown` (no tooling) or `failed`.
    """
    log = logger or (lambda *a, **k: None)
    is_free = probe or port_is_free
    result = {"status": "free", "pid": 0, "name": "", "command_line": "",
              "detail": ""}

    if is_free(host, port):
        log(f"port {host}:{port} is free")
        return result

    owner = find_port_owner(host, port, runner=runner)
    if owner is None:
        result["status"] = "unknown"
        result["detail"] = "could not identify the process holding the port"
        log(f"port {host}:{port} is busy but the owner is unknown", level="warning")
        return result

    result["pid"] = owner.pid
    result["name"] = owner.name
    result["command_line"] = owner.command_line

    if not owner.is_mediadock():
        result["status"] = "foreign"
        result["detail"] = (f"pid {owner.pid} ({owner.name}) is not a "
                            f"MediaDock server")
        log(f"port {host}:{port} is held by pid {owner.pid} ({owner.name}); "
            f"not a MediaDock server, refusing to kill it", level="warning")
        return result

    log(f"taking over port {host}:{port}: stopping previous MediaDock "
        f"instance pid {owner.pid}")
    if not kill_process_tree(owner.pid, runner=runner):
        result["status"] = "failed"
        result["detail"] = f"taskkill failed for pid {owner.pid}"
        log(f"could not stop pid {owner.pid}", level="warning")
        return result

    waiter = wait or (lambda h, p, t: wait_port_free(h, p, timeout=t))
    if not waiter(host, port, timeout):
        result["status"] = "failed"
        result["detail"] = f"port {host}:{port} still busy after killing pid {owner.pid}"
        log(f"port {host}:{port} is still busy after killing pid {owner.pid}",
            level="warning")
        return result

    result["status"] = "killed"
    result["detail"] = f"stopped previous instance pid {owner.pid}"
    log(f"previous instance pid {owner.pid} stopped; port {host}:{port} is free")
    return result
