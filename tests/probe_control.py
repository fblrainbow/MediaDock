"""Stage-004 control live-chain probe (stdlib only, no real network).

Runs a real `server.Handler` on 127.0.0.1:<ephemeral port> and drives the
real HTTP -> Scheduler -> DownloadEngine -> parse_line -> TaskManager chain
with a scripted process object instead of a real yt-dlp spawn (same stub
boundary as tests\\probe_multi.py, see Stage-003.md section 13).

What this probe proves with real HTTP and the real download directory:

  * POST /pause  -> status paused, process stopped, .part file KEPT
  * POST /resume -> downloading again, breakpoint file still there when the
                    second run starts (yt-dlp --continue reuse)
  * POST /cancel -> status cancelled, run temp + output files DELETED
  * POST /cancel on a queued task -> cancelled, never started
  * control only affects the target task

Real process-tree termination (parent + child) is covered separately by
tests\\test_control.py, and real spawn + nonzero exit by tests\\probe_chain.py.

Usage (project venv):
  C:\\\\Users\\\\Administrator\\\\Envs\\\\mediadock\\\\Scripts\\\\python.exe tests\\\\probe_control.py
Writes tests\\\\probe_control_result.json.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

# 探针不写真实数据库（Stage-005）
os.environ.setdefault("MEDIADOCK_DB", ":memory:")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv
from core_engine import DownloadEngine
from core_files import video_id_from_url

VIDEO_ID = "PROBECTRL01"
STEP_SECONDS = 0.4
PROGRESS = (5.0, 30.0, 60.0, 85.0, 95.0)


def url_for(video_id):
    return "https://www.youtube.com/watch?v=" + video_id


def output_for(video_id):
    return os.path.join(srv.DOWNLOAD_DIR, f"probe [{video_id}].mp4")


class ScriptedProcess:
    """Fake Popen that streams slowly, can be terminated, and marks progress."""

    def __init__(self, out_path, on_start, delay=STEP_SECONDS):
        self.returncode = None
        self._out = out_path
        self._on_start = on_start
        self._delay = delay
        self._terminated = False

    def terminate(self):
        self._terminated = True

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 1 if self._terminated else 0
        return self.returncode

    @property
    def stdout(self):
        self._on_start()
        yield "[download] Destination: " + self._out
        for pct in PROGRESS:
            if self._terminated:
                return
            yield "[download] %5.1f%% of ~ 5.00MiB at 1.00MiB/s ETA 00:05" % pct
            time.sleep(self._delay)


def http_json(base, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers,
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


def main():
    runs = {}
    submitted = []

    def make_factory():
        def popen_factory(command, **kwargs):
            url = command[-1]
            vid = video_id_from_url(url)
            out = output_for(vid)

            def on_start():
                history = runs.setdefault(vid, [])
                history.append({
                    "run": len(history) + 1,
                    "part_existed_before_start": os.path.exists(out + ".part"),
                })
                with open(out + ".part", "w", encoding="utf-8") as fh:
                    fh.write("partial payload")

            return ScriptedProcess(out, on_start)
        return DownloadEngine(srv.manager, ytdlp="FAKE-YTDLP",
                              download_dir=srv.DOWNLOAD_DIR,
                              popen_factory=popen_factory, logger=srv.log)

    old_factory = srv.scheduler._engine_factory
    srv.scheduler.set_engine_factory(make_factory)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    base = "http://127.0.0.1:%d" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def create(video_id):
        quoted = urllib.parse.quote(url_for(video_id), safe="")
        code, body = http_json(base, "/download?url=" + quoted)
        assert code == 200, (code, body)
        task_id = body["task_id"]
        submitted.append(task_id)
        return task_id

    def submit(video_id):
        task_id = create(video_id)
        assert wait_status(task_id, ("downloading",)) == "downloading", task_id
        return task_id

    def control(action, task_id):
        code, body = http_json(base, "/" + action, {"task_id": task_id})
        assert code == 200, (action, code, body)
        return body

    try:
        # 1) 暂停：进程停止，断点文件保留
        paused_task = submit(VIDEO_ID)
        body = control("pause", paused_task)
        assert body["status"] == "paused", body
        part = output_for(VIDEO_ID) + ".part"
        assert os.path.isfile(part), "pause must keep the .part file"
        assert srv.manager.get(paused_task).status == "paused"
        assert srv.scheduler.active_count() == 0, srv.scheduler.summary()

        # 2) 继续：第二次运行启动时断点文件仍在（yt-dlp --continue 复用）
        body = control("resume", paused_task)
        assert body["status"] == "downloading", body
        # 引擎线程与断言存在竞态（status 先变，stdout 迭代后才回调 on_start）：
        # 等第二次运行真正开始，而不是假定它已经发生
        deadline = time.monotonic() + 10.0
        while len(runs.get(VIDEO_ID, [])) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(runs.get(VIDEO_ID, [])) >= 2, runs
        assert runs[VIDEO_ID][1]["part_existed_before_start"] is True, runs

        # 3) 取消：状态 cancelled，断点与本次输出都被删除
        body = control("cancel", paused_task)
        assert body["status"] == "cancelled", body
        assert srv.manager.get(paused_task).status == "cancelled"
        assert not os.path.exists(part), "cancel must remove the .part file"
        assert not os.path.exists(output_for(VIDEO_ID)), "cancel must remove output"

        # 4) 控制隔离：暂停一个任务不影响其他任务
        first = submit("PROBECTRL02")
        second = submit("PROBECTRL03")
        control("pause", first)
        assert srv.manager.get(second).status == "downloading", "isolation broken"
        assert srv.manager.get(first).status == "paused"

        # 5) 队列取消：等待中的任务直接 cancelled，永不启动
        submit("PROBECTRL04")           # 暂停已释放槽位 -> active 2
        submit("PROBECTRL05")           # active 3
        waiting = create("PROBECTRL06")  # 无空闲槽位 -> FIFO 队列
        assert srv.scheduler.queued_count() >= 1, srv.scheduler.summary()
        body = control("cancel", waiting)
        assert body["status"] == "cancelled", body
        assert srv.manager.get(waiting).status == "cancelled"
        assert waiting not in srv.scheduler.queued_ids()

        result = {
            "base": base,
            "bind": "127.0.0.1",
            "engine_stub": "scripted process (spawn replaced)",
            "download_dir": srv.DOWNLOAD_DIR,
            "runs": runs,
            "paused_task": paused_task,
            "pause_kept_breakpoint": True,
            "resume_reused_breakpoint": runs[VIDEO_ID][1][
                "part_existed_before_start"],
            "cancel_cleaned_files": True,
            "isolation": {"paused": first, "untouched": second},
            "queued_cancel": {"queued_task": waiting},
            "scheduler": srv.scheduler.summary(),
        }
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "probe_control_result.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("CONTROL PROBE OK paused=%s resume_kept_breakpoint=%s "
              "cancel_cleaned=%s" % (paused_task, True, True))
        print("wrote " + path)
    finally:
        for task_id in list(srv.scheduler.active_ids()) + \
                list(srv.scheduler.queued_ids()):
            try:
                srv.scheduler.cancel(task_id, timeout=5)
            except Exception:  # noqa: BLE001 - probe cleanup only
                pass
        srv.scheduler.wait_idle(20)
        srv.scheduler.set_engine_factory(old_factory)
        for task_id in submitted:
            srv.manager.drop(task_id)
        for video_id in runs:
            for suffix in ("", ".part"):
                stray = output_for(video_id) + suffix
                if os.path.exists(stray):
                    try:
                        os.remove(stray)
                    except OSError:
                        pass
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()

