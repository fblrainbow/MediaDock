"""Stage-003 multi-task live-chain probe (stdlib only, no real network).

Runs a real server.Handler on 127.0.0.1:<ephemeral port> and submits 5
tasks over HTTP, sampling GET /tasks to prove:

  * active_count never exceeds the fixed limit 3
  * surplus tasks are observable as pending (FIFO queue in use)
  * slots are refilled after completion and after failure
  * every task reaches a terminal status (no leaked slot, no stalled queue)
  * the /tasks ordering contract holds (unfinished before completed)
  * the frozen yt-dlp command (format policy) is still what the engine runs

Stub boundary (recorded in Stage-003.md as a difference): the OS process
spawn is replaced by a scripted process object, because a `.bat` fake
yt-dlp cannot be used while the `-f` expression contains `<` - cmd.exe
re-parses that argument as redirection. Real spawn + nonzero exit is
covered separately by tests\\probe_chain.py. Everything else here is real:
HTTP handler, Scheduler, DownloadEngine.run, parse_line, TaskManager.

Usage (project venv):
  C:\\Users\\Administrator\\Envs\\mediadock\\Scripts\\python.exe tests\\probe_multi.py
Writes tests\\probe_multi_result.json.
"""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv
from core_engine import FORMAT_EXPR, DownloadEngine

TASK_COUNT = 5
FAIL_INDEX = 1          # 第 2 个任务故意失败，验证失败隔离与补位
STEP_SECONDS = 0.6      # 每个进度行之间的间隔，制造可观测的并发窗口


class ScriptedProcess:
    """Fake Popen yielding progress lines over time, then a return code."""

    def __init__(self, delay, fail):
        self.returncode = None
        self._delay = delay
        self._fail = fail

    @property
    def stdout(self):
        for pct in (5.0, 55.0, 90.0):
            yield "[download] %5.1f%% of ~ 1.00MiB at 1.00KiB/s ETA 00:02" % pct
            time.sleep(self._delay)
        yield "[download] 100.0% of ~ 1.00MiB at 1.00KiB/s ETA 00:00"

    def wait(self):
        self.returncode = 1 if self._fail else 0
        return self.returncode


def http_get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace"))


def make_engine_factory(seen_commands):
    """Build engines whose process spawn is scripted instead of real."""

    def factory():
        def popen_factory(command, **kwargs):
            seen_commands.append(list(command))
            url = command[-1]
            fail = url.endswith("PROBE%d" % FAIL_INDEX)
            return ScriptedProcess(STEP_SECONDS, fail)
        return DownloadEngine(srv.manager, ytdlp="FAKE-YTDLP",
                              download_dir=srv.DOWNLOAD_DIR,
                              popen_factory=popen_factory, logger=srv.log)
    return factory


def main():
    seen_commands = []
    old_factory = srv.scheduler._engine_factory
    srv.scheduler.set_engine_factory(make_engine_factory(seen_commands))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    base = "http://127.0.0.1:%d" % port
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    submitted = []
    max_active_seen = 0
    max_queued_seen = 0
    pending_samples = 0
    order_ok = True
    final = {}
    try:
        for i in range(TASK_COUNT):
            url = urllib.parse.quote(
                "https://www.youtube.com/watch?v=PROBE%d" % i, safe="")
            code, body = http_get(base, "/download?url=" + url)
            assert code == 200, (code, body)
            submitted.append(body["task_id"])
        assert len(set(submitted)) == TASK_COUNT, submitted

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            code, payload = http_get(base, "/tasks")
            assert code == 200, (code, payload)
            assert payload["active_limit"] == 3, payload
            max_active_seen = max(max_active_seen, payload["active_count"])
            max_queued_seen = max(max_queued_seen, payload["queued_count"])
            seen_completed = False
            for task in payload["tasks"]:
                if task["status"] == "completed":
                    seen_completed = True
                elif seen_completed:
                    order_ok = False
                if task["status"] == "pending":
                    pending_samples += 1
            final = {t["task_id"]: t["status"] for t in payload["tasks"]}
            if len(final) >= TASK_COUNT and all(
                    v in ("completed", "error") for v in final.values()):
                break
            time.sleep(0.05)

        assert max_active_seen <= 3, max_active_seen
        assert max_active_seen >= 1, "no task ever became active"
        assert max_queued_seen >= 1, "queue was never used"
        assert pending_samples >= 1, "no task was ever observed as pending"
        assert order_ok, "unfinished/completed ordering violated"
        for i, tid in enumerate(submitted):
            expected = "error" if i == FAIL_INDEX else "completed"
            assert final.get(tid) == expected, (tid, final.get(tid), expected)
        assert srv.scheduler.active_count() == 0, srv.scheduler.summary()
        assert srv.scheduler.queued_count() == 0, srv.scheduler.summary()
        # 冻结的 yt-dlp 命令策略必须原样穿过调度链路
        assert len(seen_commands) == TASK_COUNT, len(seen_commands)
        assert all(FORMAT_EXPR in cmd for cmd in seen_commands), seen_commands
        assert all("--merge-output-format" in cmd for cmd in seen_commands)

        out = {"base": base, "bind": "127.0.0.1", "port_mode": "ephemeral",
               "engine_stub": "scripted process (spawn replaced)",
               "task_count": TASK_COUNT, "fail_index": FAIL_INDEX,
               "task_ids": submitted, "final_status": final,
               "max_active_seen": max_active_seen,
               "max_queued_seen": max_queued_seen,
               "pending_samples": pending_samples,
               "ordering_ok": order_ok,
               "commands_checked": len(seen_commands),
               "format_expr": FORMAT_EXPR,
               "scheduler_summary": srv.scheduler.summary()}
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "probe_multi_result.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("MULTI PROBE OK tasks=%d max_active=%d max_queued=%d "
              "pending_samples=%d" % (TASK_COUNT, max_active_seen,
                                      max_queued_seen, pending_samples))
        print("wrote " + path)
    finally:
        srv.scheduler.set_engine_factory(old_factory)
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()