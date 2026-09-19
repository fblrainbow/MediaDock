"""Stage-006 config / dependency / security live probe (no real network).

Starts a real `server.Handler` on 127.0.0.1:<ephemeral port> against a real
`config.json`, a real log file, a real download directory and a real SQLite
file, then proves the Stage-006 contract end to end:

  * `config.json` is loaded (`source=file`) and really drives the server
    (download dir, log file, database path, concurrency limit)
  * `/health` exposes `config` + `dependencies` diagnostics
  * a malformed config is reported in `/health.config.errors` while the
    server keeps serving and falls back to loopback
  * `Host` / `Origin` / body-size / URL hardening reject hostile requests
    (403 `forbidden_host`, 403 `forbidden_origin`, 413 `payload_too_large`,
    400 `invalid_url`) without breaking a normal download
  * log lines are level-filtered, redacted and length-limited on disk
  * downloads and cleanup stay inside the configured download directory
  * `python server.py --check-config` reports the configuration and returns
    0 for a usable setup / 1 when something is wrong

Process spawn is replaced by an instant in-process engine; everything else
(config file, log file, SQLite file, HTTP, handler) is real.

Usage (project venv):
  C:\\Users\\Administrator\\Envs\\mediadock\\Scripts\\python.exe tests\\probe_security.py
Writes tests\\probe_security_result.json.
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from http.server import ThreadingHTTPServer

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = tempfile.mkdtemp(prefix="mediadock-security-")
DOWNLOAD_DIR = os.path.join(TMP_DIR, "downloads")
LOG_PATH = os.path.join(TMP_DIR, "MediaDock-server.log")
DB_PATH = os.path.join(TMP_DIR, "tasks.db")
CONFIG_PATH = os.path.join(TMP_DIR, "config.json")
BAD_CONFIG_PATH = os.path.join(TMP_DIR, "bad-config.json")

# 必须在 import server 之前：模块导入时就会 bootstrap 一次
os.environ["MEDIADOCK_DB"] = ":memory:"
sys.path.insert(0, REPO_DIR)

import server as srv  # noqa: E402 - env must be set first
from core_engine import build_command  # noqa: E402
from core_files import discover_task_files, is_inside  # noqa: E402


class InstantEngine:
    """Engine stub: finishes a task immediately, never spawns a process."""

    def run(self, task_id, url, control=None):
        srv.manager.transition(task_id, "downloading")
        srv.manager.transition(task_id, "completed", percent=100.0)


def write_config(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def raw_request(port, path="/health", method="GET", host=None, origin=None,
                headers=None, body=b"", content_length=None, timeout=15):
    """Send a request with full control over Host/Origin/Content-Length."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=host is not None,
                        skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        if origin is not None:
            conn.putheader("Origin", origin)
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        if content_length is not None:
            conn.putheader("Content-Length", str(content_length))
        conn.endheaders()
        if body:
            conn.send(body)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def as_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def wait_status(task_id, statuses, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = srv.manager.get(task_id)
        if task is not None and task.status in statuses:
            return task.status
        time.sleep(0.02)
    task = srv.manager.get(task_id)
    return task.status if task else None


def check_config_cli(path):
    """Run `python server.py --check-config --config <path>` in a child."""
    completed = subprocess.run(
        [sys.executable, "server.py", "--check-config", "--config", path],
        cwd=REPO_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=120, text=True, encoding="utf-8", errors="replace")
    return completed.returncode, completed.stdout or ""


def main():
    checks = {}
    write_config(CONFIG_PATH, {
        "host": "127.0.0.1", "port": 8765,
        "download_dir": DOWNLOAD_DIR, "log_file": LOG_PATH,
        "log_level": "info", "db_path": DB_PATH, "max_active_tasks": 2,
        "purge_keep": 10,
        # deterministic dependency checks: existing executables
        "ytdlp_path": sys.executable, "ffmpeg_path": sys.executable})

    config = srv.reload_config(path=CONFIG_PATH)
    checks["config_source_file"] = (config.source == "file"
                                    and config.path == CONFIG_PATH)
    checks["config_no_errors"] = not config.errors

    srv.bootstrap(DB_PATH, config=config)
    srv.scheduler.set_engine_factory(lambda: InstantEngine())

    checks["download_dir_applied"] = (srv.DOWNLOAD_DIR == DOWNLOAD_DIR
                                      and os.path.isdir(DOWNLOAD_DIR))
    checks["concurrency_applied"] = srv.scheduler.active_limit == 2
    checks["db_path_applied"] = (srv.DB_PATH == DB_PATH
                                 and srv.storage is not None)
    deps = srv.dependencies_info()
    checks["deps_snapshot_ok"] = (
        deps.get("ok") is True
        and [c["name"] for c in deps["checks"]] == ["yt-dlp", "ffmpeg",
                                                   "download_dir",
                                                   "disk_space"]
        and bool(deps.get("checked_at")))

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        # 1) /health 暴露配置与依赖
        code, raw = raw_request(port, "/health")
        health = as_json(raw)
        checks["health_config_ok"] = (
            code == 200 and health["config"]["source"] == "file"
            and health["config"]["ok"] is True
            and health["config"]["values"]["download_dir"] == DOWNLOAD_DIR
            and health["config"]["values"]["max_active_tasks"] == 2)
        checks["health_deps_ok"] = (health["dependencies"]["ok"] is True)

        # 2) Host / Origin 守卫
        code, raw = raw_request(port, "/health", host="evil.com")
        checks["host_guard_403"] = (code == 403
                                    and as_json(raw)["error_code"]
                                    == "forbidden_host")
        code, _ = raw_request(port, "/health", host=f"127.0.0.1:{port}")
        checks["host_loopback_200"] = code == 200
        code, raw = raw_request(port, "/health", origin="https://evil.com")
        checks["origin_guard_403"] = (code == 403
                                      and as_json(raw)["error_code"]
                                      == "forbidden_origin")
        code, _ = raw_request(port, "/health",
                              origin="https://www.youtube.com")
        checks["origin_youtube_200"] = code == 200
        code, _ = raw_request(port, "/health")
        checks["origin_absent_200"] = code == 200

        # 3) 请求体上限
        limit = srv.CONFIG.request_max_bytes
        code, raw = raw_request(port, "/pause", method="POST",
                                headers={"Content-Type": "application/json"},
                                content_length=limit + 1)
        checks["body_limit_413"] = (code == 413
                                    and as_json(raw)["error_code"]
                                    == "payload_too_large")

        # 4) URL 校验（控制字符 / 超长 / 非 http）
        code, _ = raw_request(
            port, "/download?url="
            + urllib.parse.quote("https://example.com/a\tb", safe=""))
        checks["url_control_char_400"] = code == 400
        code, _ = raw_request(
            port, "/download?url="
            + urllib.parse.quote("https://example.com/" + "a" * 3000, safe=""))
        checks["url_overlong_400"] = code == 400
        code, _ = raw_request(
            port, "/download?url="
            + urllib.parse.quote("--exec=calc", safe=""))
        checks["url_flag_injection_400"] = code == 400

        # 5) 正常下载仍然可用（守卫不误伤，Stage-007 起用已接入的 YouTube URL）
        parsed = urllib.parse.urlparse(base)
        code, raw = raw_request(
            port, "/download?url="
            + urllib.parse.quote("https://www.youtube.com/watch?v=probeok",
                                 safe=""),
            host=f"127.0.0.1:{parsed.port}")
        task_id = as_json(raw).get("task_id", "")
        checks["download_200"] = code == 200 and bool(task_id)
        checks["download_completed"] = (
            wait_status(task_id, ("completed",)) == "completed")
        code, raw = raw_request(port, f"/status?id={task_id}")
        checks["status_shape_unchanged"] = (
            code == 200 and as_json(raw)["status"] == "completed"
            and as_json(raw)["percent"] == 100.0)
    finally:
        for tid in list(srv.scheduler.active_ids()) + \
                list(srv.scheduler.queued_ids()):
            try:
                srv.scheduler.cancel(tid, timeout=5)
            except Exception:  # noqa: BLE001 - probe cleanup only
                pass
        srv.scheduler.wait_idle(20)

    # 6) 日志：写入配置的文件、脱敏、级别过滤
    home = os.path.expanduser("~")
    srv.log("PROBE-INFO-MARKER")
    srv.log("probe url https://example.com/v?token=SECRET123&v=abc under "
            + os.path.join(home, "Videos", "clip.mp4"))
    srv._log_fp.flush()
    with open(LOG_PATH, "r", encoding="utf-8") as handle:
        log_text = handle.read()
    checks["log_file_written"] = "PROBE-INFO-MARKER" in log_text
    checks["log_redacted"] = ("SECRET123" not in log_text
                              and "token=***" in log_text
                              and "v=abc" in log_text)
    checks["log_path_shortened"] = home not in log_text

    srv.LOG_LEVEL = "warning"
    srv.log("PROBE-HIDDEN-MARKER")
    srv.LOG_LEVEL = "info"
    srv._log_fp.flush()
    with open(LOG_PATH, "r", encoding="utf-8") as handle:
        checks["log_level_filter"] = "PROBE-HIDDEN-MARKER" not in handle.read()

    # 7) 坏配置：不阻塞启动、错误可见、回退回环
    write_config(BAD_CONFIG_PATH, {
        "host": "0.0.0.0", "port": "abc", "download_dir": DOWNLOAD_DIR,
        "log_file": LOG_PATH, "db_path": DB_PATH,
        "ytdlp_path": sys.executable, "ffmpeg_path": sys.executable})
    bad = srv.reload_config(path=BAD_CONFIG_PATH)
    srv.bootstrap(DB_PATH, config=bad)
    srv.scheduler.set_engine_factory(lambda: InstantEngine())
    checks["bad_config_fell_back"] = (bad.host == "127.0.0.1"
                                      and len(bad.errors) >= 2)
    code, raw = raw_request(port, "/health")
    payload = as_json(raw)
    checks["bad_config_visible"] = (
        code == 200 and payload["status"] == "ok"
        and payload["config"]["ok"] is False
        and len(payload["config"]["errors"]) >= 2)
    code, raw = raw_request(
        port, "/download?url="
        + urllib.parse.quote("https://www.youtube.com/watch?v=afterbadcfg",
                             safe=""))
    fallback_id = as_json(raw).get("task_id", "")
    checks["service_after_bad_config"] = (
        code == 200 and wait_status(fallback_id, ("completed",)) == "completed")
    srv.manager.drop(fallback_id)

    # 8) 路径边界：下载目录以外的同名文件永不被清理
    boundary_url = "https://www.youtube.com/watch?v=BOUNDARYID"
    outside = os.path.join(TMP_DIR, "outside [BOUNDARYID].mp4")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("x")
    checks["boundary_detects_outside"] = is_inside(DOWNLOAD_DIR, outside) is False
    checks["boundary_ignores_outside"] = discover_task_files(
        DOWNLOAD_DIR, boundary_url, [outside], 0.0) == []

    # 9) argv 形状：URL 永远是最后一个参数，且从不使用 shell
    sample = "https://example.com/x?token=abc"
    command = build_command(srv.YT_DLP, srv.DOWNLOAD_DIR, sample, "")
    checks["argv_url_last"] = command[-1] == sample
    checks["argv_shape_unchanged"] = command[-3:] == ["-P", srv.DOWNLOAD_DIR,
                                                      sample]
    with open(os.path.join(REPO_DIR, "core_engine.py"), "r",
              encoding="utf-8") as handle:
        engine_source = handle.read()
    checks["argv_no_shell"] = "shell=True" not in engine_source

    # 10) --check-config CLI（真实子进程）
    code, out = check_config_cli(CONFIG_PATH)
    checks["cli_good_rc0"] = code == 0 and '"source": "file"' in out
    checks["cli_reports_dependencies"] = ('"dependencies"' in out
                                          and '"yt-dlp"' in out)
    code, _ = check_config_cli(BAD_CONFIG_PATH)
    checks["cli_bad_rc1"] = code == 1

    failed = [name for name, ok in checks.items() if not ok]
    result = {
        "config_file": CONFIG_PATH, "bad_config_file": BAD_CONFIG_PATH,
        "download_dir": DOWNLOAD_DIR, "log_file": LOG_PATH, "db": DB_PATH,
        "base": base, "bind": "127.0.0.1",
        "engine_stub": "instant engine (process spawn replaced)",
        "config_source": config.source,
        "config_errors": list(config.errors),
        "bad_config_errors": list(bad.errors),
        "dependencies": {c["name"]: c["status"] for c in deps["checks"]},
        "checks": checks, "failed_checks": failed,
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "probe_security_result.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    assert not failed, failed
    print("SECURITY PROBE OK checks=%d config=%s deps=%s"
          % (len(checks), config.source, result["dependencies"]))
    print("wrote " + path)

    srv.bootstrap(":memory:")
    httpd.shutdown()
    httpd.server_close()


if __name__ == "__main__":
    main()


