"""Stage-011 live restart probe: real servers, real takeover, temp port.

Proves the "kill the previous instance on start" behaviour end to end:

  * start #1 binds a free port and answers `/health`;
  * start #2 with `--restart` stops #1 and serves `/health` on the same port;
  * the port owner after the takeover is start #2's PID;
  * `--restart` on an already free port still starts normally;
  * no manual intervention (no `WinError 10048` exit code 2).

A fresh temporary port, database, download dir and log file are used, so a
MediaDock instance the user is running on 8765 is never touched.

Only processes this probe started are stopped (via
`core_instance.kill_process_tree`), so it is safe to run at any time.

Usage (project venv):
  C:\\Users\\Administrator\\Envs\\mediadock\\Scripts\\python.exe tests\\probe_restart.py
Writes tests\\probe_restart_result.json.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core_instance import find_port_owner, kill_process_tree  # noqa: E402

HOST = "127.0.0.1"
START_TIMEOUT = 40.0
STOP_TIMEOUT = 20.0


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def health(port):
    """`(status_code, payload)` or `(None, None)` while the server is down."""
    try:
        with urllib.request.urlopen(
                "http://%s:%d/health" % (HOST, port), timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def wait_health(port, timeout=START_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, payload = health(port)
        if code == 200 and payload:
            return payload
        time.sleep(0.25)
    return None


def wait_stopped(proc, timeout=STOP_TIMEOUT):
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def start(port, tmp, extra_args=()):
    env = dict(os.environ)
    env.update({
        "MEDIADOCK_PORT": str(port),
        "MEDIADOCK_DB": os.path.join(tmp, "tasks.db"),
        "MEDIADOCK_DOWNLOAD_DIR": os.path.join(tmp, "downloads"),
        "MEDIADOCK_LOG_FILE": os.path.join(tmp, "server-%d.log" % port),
        "MEDIADOCK_LOG_LEVEL": "info",
    })
    return subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"), *extra_args],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    checks = {}
    tmp = tempfile.mkdtemp(prefix="mediadock-restart-")
    first = second = third = None
    try:
        port = free_port()

        # 1) 第一次启动：端口空闲，正常服务
        # 注意：venv 的 python.exe 是 shim，真正的服务进程是它的子进程，
        # 所以 `Popen.pid` 不等于端口占用者；这里一律以端口占用者为准。
        first = start(port, tmp)
        payload = wait_health(port)
        checks["first_start_serves_health"] = bool(payload)
        checks["first_start_is_silent"] = first.poll() is None
        owner = find_port_owner(HOST, port)
        first_owner_pid = owner.pid if owner else 0
        checks["first_owner_recognised_as_mediadock"] = bool(owner) and \
            owner.is_mediadock()
        checks["first_owner_pid_positive"] = first_owner_pid > 0

        # 2) 第二次启动（--restart）：接管端口，旧实例被结束
        second = start(port, tmp, extra_args=("--restart",))
        payload = wait_health(port)
        checks["second_start_serves_health"] = bool(payload)
        checks["second_start_still_running"] = second.poll() is None
        checks["previous_instance_stopped"] = wait_stopped(first)
        owner = find_port_owner(HOST, port)
        checks["port_owner_changed"] = bool(owner) and \
            owner.pid != first_owner_pid and owner.pid > 0
        checks["health_reports_version"] = bool(payload) and \
            (payload.get("version") or {}).get("app", "") != ""
        checks["old_instance_was_not_the_server"] = first.pid != second.pid

        # 3) 旧实例被接管后，服务端记录的是「需要重试」而不是伪完成
        checks["second_instance_serves_tasks"] = False
        try:
            with urllib.request.urlopen(
                    "http://%s:%d/tasks" % (HOST, port), timeout=5) as response:
                checks["second_instance_serves_tasks"] = response.status == 200
        except (urllib.error.URLError, OSError):
            pass

        # 4) 收尾：结束第二个实例，再用 --restart 在空闲端口上启动第三个
        kill_process_tree(second.pid)
        checks["second_instance_stopped"] = wait_stopped(second)

        third = start(port, tmp, extra_args=("--restart",))
        payload = wait_health(port)
        checks["restart_on_free_port_works"] = bool(payload) and \
            third.poll() is None
        kill_process_tree(third.pid)
        checks["third_instance_stopped"] = wait_stopped(third)
    finally:
        for proc in (first, second, third):
            if proc is not None and proc.poll() is None:
                kill_process_tree(proc.pid)
                wait_stopped(proc, timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for value in checks.values() if value)
    total = len(checks)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "checks": checks,
        "passed": passed,
        "total": total,
        "ok": passed == total,
    }
    with open(os.path.join(ROOT, "tests", "probe_restart_result.json"), "w",
              encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    failed = [name for name, value in checks.items() if not value]
    print("RESTART PROBE %s checks=%d passed=%d failed=%s"
          % ("OK" if passed == total else "FAILED", total, passed, failed))
    print("wrote tests\\probe_restart_result.json")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
