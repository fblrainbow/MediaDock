"""Stage-010 release gate: static checks + real runtime regression.

One command answers "is this build shippable?":

  python tests\\release_check.py     -> exit 0 = shippable, 1 = not shippable

Static half (no server): required files, version consistency between
`core_release.APP_VERSION` / `/health.version.app` / README / changelog, and
`config.example.json` parity with `core_config.DEFAULTS`.

Runtime half (real `server.Handler`, real SQLite, real HTTP on an ephemeral
port, temp download dir): core endpoints, error codes, control API, security
guards, full create -> complete -> history -> delete flow, restart semantics,
and two rollback drills (database backup/restore, degraded memory mode).

Design constraints (Stage-010.md 5.2/7.3):
  * never touches the repo database or `downloads/` (all temp paths),
  * offline: process spawn is replaced by a scripted process object,
  * only writes `tests/release_check_result.json`.

Usage (project venv):
  C:\\\\Users\\\\Administrator\\\\Envs\\\\mediadock\\\\Scripts\\\\python.exe tests\\\\release_check.py
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT_PATH = os.path.join(BASE_DIR, "tests", "release_check_result.json")

TMP_DIR = tempfile.mkdtemp(prefix="mediadock-release-")
DB_PATH = os.path.join(TMP_DIR, "tasks.db")
BACKUP_PATH = os.path.join(TMP_DIR, "tasks.db.backup")
CORRUPT_PATH = os.path.join(TMP_DIR, "corrupt.db")
DOWNLOAD_DIR = os.path.join(TMP_DIR, "downloads")
LOG_PATH = os.path.join(TMP_DIR, "release.log")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 必须在 import server 之前：模块导入时就会 bootstrap 一次
os.environ["MEDIADOCK_DB"] = DB_PATH
os.environ["MEDIADOCK_DOWNLOAD_DIR"] = DOWNLOAD_DIR
os.environ["MEDIADOCK_LOG_FILE"] = LOG_PATH
os.environ["MEDIADOCK_LOG_LEVEL"] = "info"

sys.path.insert(0, BASE_DIR)
import server as srv                                   # noqa: E402
from core_release import (APP_VERSION, API_VERSION,    # noqa: E402
                          USERSCRIPT_VERSION, format_report, static_checks)
from core_store import MIGRATIONS                      # noqa: E402

SCHEMA_VERSION = max(version for version, _ in MIGRATIONS)
VIDEO_URL = "https://www.youtube.com/watch?v=releasecheck1"

CHECKS = {}
DETAILS = {}


def check(name, ok, detail=""):
    """Record one check; `detail` is kept for the JSON evidence."""
    CHECKS[name] = bool(ok)
    if detail:
        DETAILS[name] = str(detail)
    return bool(ok)


# =========================
# HTTP helpers
# =========================
def http_json(base, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base + path, data=data, headers=headers,
        method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def raw_request(port, path="/health", method="GET", host=None, origin=None):
    """Request with full control over Host/Origin (same seam as probe_security)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.putrequest(method, path, skip_host=host is not None,
                        skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        if origin is not None:
            conn.putheader("Origin", origin)
        conn.endheaders()
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def error_code(raw):
    try:
        return json.loads(raw).get("error_code", "")
    except ValueError:
        return ""


def wait_status(task_id, statuses, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = srv.manager.get(task_id)
        if task is not None and task.status in statuses:
            return task.status
        time.sleep(0.02)
    task = srv.manager.get(task_id)
    return task.status if task else None


# =========================
# Scripted download boundary (no network, no real yt-dlp)
# =========================
class ScriptedProcess:
    def __init__(self, delay=0.02, slow=False):
        self.returncode = None
        self._delay = delay
        self._slow = slow
        self._terminated = False

    def terminate(self):
        self._terminated = True

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 1 if self._terminated else 0
        return self.returncode

    @property
    def stdout(self):
        yield "[youtube] release: Downloading webpage"
        for pct in (25.0, 100.0):
            if self._terminated:
                return
            yield "[download] %5.1f%% of ~ 1.00MiB at 1.00MiB/s ETA 00:01" % pct
            time.sleep(self._delay)
        if self._slow:
            while not self._terminated:
                time.sleep(0.05)


COMMANDS = []


def install_engines():
    """Re-install the scripted engine factory (bootstrap resets it)."""
    from core_engine import DownloadEngine

    def fast():
        def popen_factory(command, **kwargs):
            COMMANDS.append(list(command))
            return ScriptedProcess()
        return DownloadEngine(srv.manager, ytdlp="FAKE-YTDLP",
                              download_dir=srv.DOWNLOAD_DIR,
                              popen_factory=popen_factory, logger=srv.log)

    def slow():
        def popen_factory(command, **kwargs):
            COMMANDS.append(list(command))
            return ScriptedProcess(slow=True)
        return DownloadEngine(srv.manager, ytdlp="FAKE-YTDLP",
                              download_dir=srv.DOWNLOAD_DIR,
                              popen_factory=popen_factory, logger=srv.log)

    srv.scheduler.set_engine_factory(fast)
    return fast, slow


# =========================
# 1) static checks
# =========================
def static_section():
    for item in static_checks(BASE_DIR):
        check("static." + item["name"], item["ok"], item["detail"])


# =========================
# 2) CLI + userscript artifacts
# =========================
def cli_section():
    proc = subprocess.run([sys.executable, os.path.join(BASE_DIR, "server.py"),
                           "--check-config"],
                          cwd=BASE_DIR, capture_output=True, text=True,
                          timeout=120)
    check("cli.check_config_rc0", proc.returncode == 0,
          f"rc={proc.returncode}")
    # `--check-config` 也会把诊断日志写到 stdout，因此从第一个 "{" 开始解析
    text = proc.stdout or ""
    payload = {}
    start = text.find("{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
        except ValueError:
            payload = {}
    check("cli.check_config_payload",
          isinstance(payload, dict) and "config" in payload
          and "dependencies" in payload,
          "keys=" + ",".join(sorted(payload)))

    script = subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, "tests", "check_userscript.py")],
        cwd=BASE_DIR, capture_output=True, text=True, timeout=120)
    check("cli.userscript_structure", script.returncode == 0,
          (script.stdout or script.stderr or "").strip().splitlines()[-1:] and
          (script.stdout or script.stderr).strip().splitlines()[-1] or "")


# =========================
# 3) runtime regression
# =========================
def runtime_section(port, base, fast_factory, slow_factory):
    # --- 健康检查与版本 ---
    code, health = http_json(base, "/health")
    check("health.200", code == 200 and health.get("status") == "ok",
          f"code={code}")
    version = health.get("version") or {}
    check("health.version_app", version.get("app") == APP_VERSION,
          f"app={version.get('app')} expected={APP_VERSION}")
    check("health.version_api", version.get("api") == API_VERSION,
          f"api={version.get('api')}")
    check("health.version_userscript",
          version.get("userscript") == USERSCRIPT_VERSION,
          f"userscript={version.get('userscript')}")
    check("health.version_schema", version.get("schema") == SCHEMA_VERSION,
          f"schema={version.get('schema')} expected={SCHEMA_VERSION}")
    check("health.version_matches_storage",
          version.get("schema") == (health.get("storage") or {}).get("schema_version"),
          f"version={version.get('schema')} "
          f"storage={(health.get('storage') or {}).get('schema_version')}")
    check("health.sections",
          all(key in health for key in ("status", "version", "storage",
                                        "config", "dependencies")),
          "keys=" + ",".join(sorted(health)))

    # --- 列表 / 历史 / 音频目标 ---
    code, tasks = http_json(base, "/tasks")
    check("tasks.shape",
          code == 200 and all(key in tasks for key in
                              ("tasks", "active_count", "queued_count",
                               "active_limit", "storage")),
          f"code={code} keys=" + ",".join(sorted(tasks)))
    check("tasks.limit_3", tasks.get("active_limit") == 3,
          f"active_limit={tasks.get('active_limit')}")

    code, history = http_json(base, "/history?limit=10")
    check("history.shape",
          code == 200 and "tasks" in history and "total" in history,
          f"code={code}")

    code, audio = http_json(base, "/audio")
    names = [t.get("name") for t in (audio.get("targets") or [])]
    check("audio.targets",
          code == 200 and names == ["mp3", "m4a", "wav"]
          and audio.get("default") == "mp3",
          f"code={code} targets={names}")

    # --- 错误码回归 ---
    cases = [
        ("missing_url", "/download", 400, "missing_url"),
        ("invalid_url", "/download?url=not-a-url", 400, "invalid_url"),
        ("unsupported_platform",
         "/download?url=" + urllib.parse.quote("https://vimeo.com/12345", safe=""),
         400, "unsupported_platform"),
        ("invalid_format",
         "/download?url=" + urllib.parse.quote(VIDEO_URL, safe="") + "&preset=bogus",
         400, "invalid_format"),
        ("format_not_available",
         "/download?url=" + urllib.parse.quote(VIDEO_URL, safe="") + "&preset=720p",
         400, "format_not_available"),
        ("not_found", "/nope", 404, "not_found"),
        ("formats_missing_url", "/formats", 400, "missing_url"),
        ("history_invalid_limit", "/history?limit=0", 400, "invalid_limit"),
        ("history_invalid_status", "/history?status=bogus", 400, "invalid_status"),
        ("events_missing_id", "/events", 400, "missing_task_id"),
    ]
    for name, path, expect_code, expect_error in cases:
        code, body = http_json(base, path)
        check(f"error.{name}",
              code == expect_code and body.get("error_code") == expect_error,
              f"code={code} error_code={body.get('error_code')}")

    # --- 控制接口 ---
    code, body = http_json(base, "/pause", {})
    check("control.missing_task_id",
          code == 400 and body.get("error_code") == "missing_task_id",
          f"code={code} error_code={body.get('error_code')}")
    code, body = http_json(base, "/pause", {"task_id": "bad id!"})
    check("control.invalid_task_id",
          code == 400 and body.get("error_code") == "invalid_task_id",
          f"code={code}")
    code, body = http_json(base, "/resume", {"task_id": "nosuchtask"})
    check("control.unknown_404",
          code == 404 and body.get("error_code") == "task_not_found",
          f"code={code} error_code={body.get('error_code')}")

    # --- 安全守卫 ---
    code, raw = raw_request(port, "/health", host="evil.com")
    check("guard.host_403",
          code == 403 and error_code(raw) == "forbidden_host", f"code={code}")
    code, raw = raw_request(port, "/health", origin="https://evil.com")
    check("guard.origin_403",
          code == 403 and error_code(raw) == "forbidden_origin", f"code={code}")
    code, _ = raw_request(port, "/health", host=f"127.0.0.1:{port}")
    check("guard.loopback_host_200", code == 200, f"code={code}")
    code, _ = raw_request(port, "/health")
    check("guard.absent_origin_200", code == 200, f"code={code}")

    # --- 全链路：创建 -> 完成 -> 历史 -> 删除 ---
    code, created = http_json(
        base, "/download?url=" + urllib.parse.quote(VIDEO_URL, safe=""))
    check("flow.create", code == 200 and bool(created.get("task_id")),
          f"code={code}")
    task_id = created.get("task_id")
    check("flow.completed", wait_status(task_id, ("completed",)) == "completed",
          "status=" + str(wait_status(task_id, ("completed",), timeout=0.1)))
    check("flow.argv_real",
          any(str(cmd[-1]).startswith("https://www.youtube.com/watch")
              for cmd in COMMANDS),
          f"commands={len(COMMANDS)}")

    code, status = http_json(base, "/status?id=" + task_id)
    check("flow.status_single",
          code == 200 and status.get("task_id") == task_id
          and status.get("status") == "completed", f"code={code}")

    code, history = http_json(base, "/history?limit=50&status=completed")
    check("flow.in_history",
          code == 200 and task_id in [t["task_id"] for t in history["tasks"]],
          f"code={code} total={history.get('total')}")

    code, events = http_json(base, "/events?id=" + task_id)
    check("flow.events",
          code == 200 and events.get("returned", 0) > 0,
          f"code={code} returned={events.get('returned')}")

    # 音频契约：已完成的下载任务但没有源文件 -> 404 source_not_found
    code, body = http_json(base, "/audio", {"task_id": task_id, "target": "mp3"})
    check("audio.source_not_found",
          code == 404 and body.get("error_code") == "source_not_found",
          f"code={code} error_code={body.get('error_code')}")
    code, body = http_json(base, "/audio?target=flac", {"task_id": task_id})
    check("audio.invalid_target",
          code == 400 and body.get("error_code") == "invalid_audio_target",
          f"code={code} error_code={body.get('error_code')}")
    code, body = http_json(base, "/audio", {"task_id": "nosuchtask"})
    check("audio.task_not_found",
          code == 404 and body.get("error_code") == "task_not_found",
          f"code={code}")

    # 非终态任务不可删除 -> 409 not_deletable
    srv.scheduler.set_engine_factory(slow_factory)
    code, running = http_json(
        base, "/download?url=https://www.youtube.com/watch?v=releasecheck2")
    running_id = running.get("task_id")
    wait_status(running_id, ("downloading",))
    code, body = http_json(base, "/delete", {"task_id": running_id})
    check("delete.not_deletable",
          code == 409 and body.get("error_code") == "not_deletable",
          f"code={code} error_code={body.get('error_code')}")
    http_json(base, "/cancel", {"task_id": running_id})
    wait_status(running_id, ("cancelled",))

    # 终态删除 -> 200，随后查询 404
    code, body = http_json(base, "/delete", {"task_id": task_id})
    check("delete.terminal_ok",
          code == 200 and body.get("task_id") == task_id, f"code={code}")
    code, body = http_json(base, "/status?id=" + task_id)
    check("delete.then_404",
          code == 404 and body.get("error_code") == "task_not_found",
          f"code={code}")
    code, body = http_json(base, "/delete", {"task_id": task_id})
    check("delete.repeat_404", code == 404, f"code={code}")

    # 恢复快速引擎：后面的重启/回滚演练要能跑完，不能卡在 slow 进程上
    srv.scheduler.set_engine_factory(fast_factory)
    return running_id


# =========================
# 4) restart + rollback drills
# =========================
def restart_section(base):
    completed = srv.scheduler.submit(VIDEO_URL)["task_id"]
    srv.scheduler.wait_idle(20)
    before = srv.manager.get(completed).to_dict()
    check("restart.completed_before", before["status"] == "completed",
          f"status={before['status']}")

    live = srv.manager.create("https://www.youtube.com/watch?v=releasecheck3")
    srv.manager.transition(live.task_id, "downloading", percent=42.0)
    paused = srv.manager.create("https://www.youtube.com/watch?v=releasecheck4")
    srv.manager.transition(paused.task_id, "downloading")
    srv.manager.transition(paused.task_id, "paused")

    # 备份：回滚演练与"数据回滚"文档路径依赖它
    srv.storage.close()
    shutil.copy2(DB_PATH, BACKUP_PATH)
    srv.bootstrap(DB_PATH)
    _, slow_factory = install_engines()
    check("restart.backup_created", os.path.isfile(BACKUP_PATH)
          and os.path.getsize(BACKUP_PATH) > 0, BACKUP_PATH)

    restored_completed = srv.manager.get(completed)
    check("restart.completed_survives",
          restored_completed is not None
          and restored_completed.status == "completed"
          and restored_completed.completed_at == before["completed_at"],
          f"status={getattr(restored_completed, 'status', None)}")

    interrupted = srv.manager.get(live.task_id)
    check("restart.downloading_interrupted",
          interrupted is not None and interrupted.status == "error"
          and interrupted.error_code == "interrupted",
          f"status={getattr(interrupted, 'status', None)} "
          f"error_code={getattr(interrupted, 'error_code', None)}")

    still_paused = srv.manager.get(paused.task_id)
    check("restart.paused_stays_paused",
          still_paused is not None and still_paused.status == "paused",
          f"status={getattr(still_paused, 'status', None)}")

    code, health = http_json(base, "/health")
    storage = health.get("storage") or {}
    check("restart.storage_ok",
          code == 200 and storage.get("kind") == "sqlite"
          and storage.get("degraded") is False
          and storage.get("schema_version") == SCHEMA_VERSION,
          json.dumps(storage, ensure_ascii=False))

    # 回滚演练 A：用备份恢复数据库后历史仍然可见
    srv.storage.close()
    shutil.copy2(BACKUP_PATH, DB_PATH)
    srv.bootstrap(DB_PATH)
    _, slow_factory = install_engines()
    code, history = http_json(base, "/history?limit=50")
    check("rollback.db_restore_history",
          code == 200 and completed in [t["task_id"] for t in history["tasks"]],
          f"code={code} total={history.get('total')}")

    # 回滚演练 B：数据库不可用 -> 降级内存模式仍可服务
    with open(CORRUPT_PATH, "wb") as handle:
        handle.write(b"corrupted on purpose by release_check")
    srv.storage.close()
    srv.bootstrap(CORRUPT_PATH)
    _, slow_factory = install_engines()
    info = srv.storage_info()
    check("rollback.degraded_memory",
          srv.storage is None and info["kind"] == "memory"
          and info["degraded"] is True, json.dumps(info, ensure_ascii=False))
    code, health = http_json(base, "/health")
    version = health.get("version") or {}
    check("rollback.degraded_version",
          code == 200 and version.get("schema") == 0,
          f"code={code} version={json.dumps(version, ensure_ascii=False)}")
    code, created = http_json(
        base, "/download?url=https://www.youtube.com/watch?v=releasecheck5")
    check("rollback.degraded_accepts_task",
          code == 200 and bool(created.get("task_id")), f"code={code}")
    if created.get("task_id"):
        wait_status(created["task_id"], ("completed",))

    # 恢复到正常数据库，释放脚本化引擎
    srv.storage.close() if srv.storage is not None else None
    srv.bootstrap(DB_PATH)
    install_engines()


# =========================
# main
# =========================
def main():
    started = time.monotonic()
    static_section()
    cli_section()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    base = "http://127.0.0.1:%d" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    fast_factory, slow_factory = install_engines()

    try:
        runtime_section(port, base, fast_factory, slow_factory)
        restart_section(base)
    finally:
        try:
            srv.scheduler.wait_idle(20)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass
        httpd.shutdown()
        httpd.server_close()

    passed = sum(1 for ok in CHECKS.values() if ok)
    failed = sorted(name for name, ok in CHECKS.items() if not ok)
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "release": {"app": APP_VERSION, "api": API_VERSION,
                    "userscript": USERSCRIPT_VERSION,
                    "schema": SCHEMA_VERSION},
        "ok": not failed,
        "total": len(CHECKS),
        "passed": passed,
        "failed_count": len(failed),
        "failed": failed,
        "checks": CHECKS,
        "details": DETAILS,
        "duration_seconds": round(time.monotonic() - started, 2),
        "temp_dir": TMP_DIR,
    }
    with open(RESULT_PATH, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    lines = [format_report([{"name": name, "ok": ok,
                             "detail": DETAILS.get(name, "")}
                            for name, ok in sorted(CHECKS.items())])]
    lines.append("")
    lines.append("RELEASE CHECK %s checks=%d passed=%d failed=%d (%.2fs)"
                 % ("OK" if report["ok"] else "FAILED", report["total"],
                    passed, len(failed), report["duration_seconds"]))
    if failed:
        lines.append("failed: " + ", ".join(failed))
    lines.append("wrote " + RESULT_PATH)
    print("\n".join(lines))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
