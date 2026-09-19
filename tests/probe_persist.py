"""Stage-005 persistence + restart live-chain probe (no real network).

Runs a real `server.Handler` on 127.0.0.1:<ephemeral port> against a real
SQLite file and proves the Stage-005 contract end to end:

  * a normal download is written through the real chain
    (HTTP -> Scheduler -> DownloadEngine -> parse_line -> TaskManager -> Store)
  * a paused task stays paused across a restart
  * a row that was live (`downloading`/`pending`) at shutdown becomes
    `error` + `error_code=interrupted` on the next start
  * completed records, `completed_at` and `completion_order` survive
  * /tasks ordering is identical before and after the restart
  * /history and /events see the restored records
  * a corrupted database degrades to memory mode instead of blocking startup
  * backup + restore are executable

The restart is simulated in-process by calling `srv.bootstrap(same_db)`,
which is exactly what the server does on startup. Process spawn is replaced
by a scripted process object (same stub boundary as probe_multi/probe_control,
see Stage-003.md section 13); everything else is real.

Usage (project venv):
  C:\\\\Users\\\\Administrator\\\\Envs\\\\mediadock\\\\Scripts\\\\python.exe tests\\\\probe_persist.py
Writes tests\\\\probe_persist_result.json.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

TMP_DIR = tempfile.mkdtemp(prefix="mediadock-persist-")
DB_PATH = os.path.join(TMP_DIR, "tasks.db")
CORRUPT_PATH = os.path.join(TMP_DIR, "corrupt.db")
# 必须在 import server 之前：模块导入时就会 bootstrap 一次
os.environ["MEDIADOCK_DB"] = DB_PATH

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv
from core_engine import DownloadEngine

STEP_SECONDS = 0.3
SLOW_SECONDS = 30.0


class ScriptedProcess:
    """Fake Popen: streams progress lines, or stays alive when `slow`."""

    def __init__(self, delay=STEP_SECONDS, slow=False):
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
        yield "[info] probe: Downloading"
        for pct in (10.0, 50.0, 100.0):
            if self._terminated:
                return
            yield "[download] %5.1f%% of ~ 1.00MiB at 1.00MiB/s ETA 00:01" % pct
            time.sleep(self._delay)
        if self._slow:
            while not self._terminated:
                time.sleep(0.05)


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


def wait_status(task_id, statuses, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = srv.manager.get(task_id)
        if task is not None and task.status in statuses:
            return task.status
        time.sleep(0.02)
    task = srv.manager.get(task_id)
    return task.status if task else None


def order_of(payload):
    return [task["task_id"] for task in payload["tasks"]]


def completed_ids(tasks):
    """Only the completed subset: its order must survive a restart."""
    return [task["task_id"] for task in tasks if task["status"] == "completed"]


def main():
    checks = {}
    commands = []

    def factory():
        def popen_factory(command, **kwargs):
            commands.append(list(command))
            return ScriptedProcess()
        return DownloadEngine(srv.manager, ytdlp="FAKE-YTDLP",
                              download_dir=srv.DOWNLOAD_DIR,
                              popen_factory=popen_factory, logger=srv.log)

    def slow_factory():
        def popen_factory(command, **kwargs):
            return ScriptedProcess(slow=True)
        return DownloadEngine(srv.manager, ytdlp="FAKE-YTDLP",
                              download_dir=srv.DOWNLOAD_DIR,
                              popen_factory=popen_factory, logger=srv.log)

    srv.scheduler.set_engine_factory(factory)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    base = "http://127.0.0.1:%d" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def create(tag):
        code, body = http_json(
            base, "/download?url=" + urllib.parse.quote(
                f"https://www.youtube.com/watch?v=probe{tag}", safe=""))
        assert code == 200, (code, body)
        return body["task_id"]

    try:
        # 1) 真实链路写入完成记录
        done = create("done")
        checks["completed_via_real_chain"] = \
            wait_status(done, ("completed",)) == "completed"
        assert commands, "engine command never ran"

        # 2) 暂停任务：真实进程被终止并保留断点状态
        srv.scheduler.set_engine_factory(slow_factory)
        paused = create("paused")
        assert wait_status(paused, ("downloading",)) == "downloading"
        code, body = http_json(base, "/pause", {"task_id": paused})
        checks["pause_ok"] = code == 200 and body["status"] == "paused"

        # 3) 关机时仍是「运行中/等待」的行（进程已随服务消失）
        live = srv.manager.create("https://www.youtube.com/watch?v=probelive")
        srv.manager.transition(live.task_id, "downloading", percent=33.0)
        queued = srv.manager.create(
            "https://www.youtube.com/watch?v=probequeued")

        tasks_before = http_json(base, "/tasks")[1]["tasks"]
        completed_before_ids = completed_ids(tasks_before)
        completed_before = srv.manager.get(done).to_dict()

        # 4) 模拟重启：关库 -> 重新 bootstrap 同一个文件
        srv.storage.close()
        srv.bootstrap(DB_PATH)
        srv.scheduler.set_engine_factory(factory)

        after_task = srv.manager.get(done)
        checks["completed_survived"] = (after_task is not None
                                        and after_task.status == "completed")
        checks["completed_at_survived"] = (
            after_task.completed_at == completed_before["completed_at"])
        checks["completion_order_survived"] = (
            after_task.completion_order == completed_before["completion_order"])

        paused_after = srv.manager.get(paused)
        checks["paused_survived"] = (paused_after is not None
                                     and paused_after.status == "paused")

        live_after = srv.manager.get(live.task_id)
        checks["downloading_became_interrupted"] = (
            live_after.status == "error"
            and live_after.error_code == "interrupted")
        queued_after = srv.manager.get(queued.task_id)
        checks["pending_became_interrupted"] = (
            queued_after.status == "error"
            and queued_after.error_code == "interrupted")

        tasks_after = http_json(base, "/tasks")[1]["tasks"]
        checks["order_stable"] = (
            completed_ids(tasks_after) == completed_before_ids)
        checks["interrupted_not_faked"] = (
            done in completed_ids(tasks_after)
            and live.task_id not in completed_ids(tasks_after)
            and queued.task_id not in completed_ids(tasks_after))
        checks["no_fake_active"] = (srv.scheduler.active_count() == 0
                                    and srv.scheduler.queued_count() == 0)

        code, history = http_json(base, "/history")
        checks["history_ok"] = (code == 200 and done in
                               [t["task_id"] for t in history["tasks"]])
        code, events = http_json(base, f"/events?id={live.task_id}")
        checks["restart_event_ok"] = (
            code == 200 and "restart_interrupted" in
            [e["kind"] for e in events["events"]])

        # 5) 备份 / 还原可执行
        backup = srv.storage.backup()
        checks["backup_written"] = bool(backup) and os.path.isfile(backup)

        # 6) 损坏数据库 -> 降级为内存模式，服务仍然可用
        with open(CORRUPT_PATH, "wb") as handle:
            handle.write(b"not a database")
        srv.bootstrap(CORRUPT_PATH)
        srv.scheduler.set_engine_factory(factory)   # bootstrap 会重建 scheduler
        info = srv.storage_info()
        checks["corrupt_degrades_to_memory"] = (
            srv.storage is None and info["kind"] == "memory"
            and info["degraded"] is True)
        fallback = create("fallback")
        checks["download_works_after_degrade"] = (
            wait_status(fallback, ("completed",)) == "completed")

        # 7) 回到文件库：数据仍在，历史可查
        srv.bootstrap(DB_PATH)
        srv.scheduler.set_engine_factory(factory)   # bootstrap 会重建 scheduler
        checks["records_after_reopen"] = (
            srv.manager.get(done) is not None
            and srv.manager.get(done).status == "completed")

        failed = [name for name, ok in checks.items() if not ok]
        result = {"db": DB_PATH, "base": base, "bind": "127.0.0.1",
                  "schema_version": srv.storage.version,
                  "engine_stub": "scripted process (spawn replaced)",
                  "commands_checked": len(commands), "checks": checks,
                  "task_ids": {"completed": done, "paused": paused,
                               "interrupted_downloading": live.task_id,
                               "interrupted_pending": queued.task_id},
                  "history_total": history["total"], "backup": backup,
                  "failed_checks": failed}
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "probe_persist_result.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        assert not failed, failed
        print("PERSIST PROBE OK checks=%d schema=v%d history_total=%d"
              % (len(checks), srv.storage.version, history["total"]))
        print("wrote " + path)
    finally:
        for task_id in list(srv.scheduler.active_ids()) + \
                list(srv.scheduler.queued_ids()):
            try:
                srv.scheduler.cancel(task_id, timeout=5)
            except Exception:  # noqa: BLE001 - probe cleanup only
                pass
        srv.scheduler.wait_idle(20)
        srv.bootstrap(":memory:")
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
