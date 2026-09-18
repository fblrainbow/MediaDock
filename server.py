import atexit
import json
import os
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from core_control import ControlError
from core_engine import DownloadEngine
from core_listing import build_task_list
from core_manager import TaskManager
from core_parse import MERGE_RE, PROGRESS_RE
from core_scheduler import MAX_ACTIVE_TASKS, Scheduler
from core_task import Task

# =========================
# 日志：同时写文件 + 保留控制台（pythonw 下控制台为空也不报错）
# =========================
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MediaDock-server.log")
_log_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
# 进程退出时关闭日志句柄，避免 unittest 报 ResourceWarning
atexit.register(lambda: _log_fp.close())


def log(*args):
    msg = " ".join(str(a) for a in args)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        _log_fp.write(line + "\n")
    except Exception:
        pass


# =========================
# 配置（Stage-002：值与 Stage-001 一致，来源收敛到 core_engine）
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# yt-dlp 查找：优先固定路径，其次 PATH 中的 yt-dlp / yt-dlp.exe
_CANDIDATES = [
    r"C:\Tools\yt-dlp\yt-dlp.exe",
    "yt-dlp.exe",
    "yt-dlp",
]


def resolve_ytdlp():
    from core_engine import resolve_ytdlp as _resolve
    return _resolve()


YT_DLP = resolve_ytdlp()

# =========================
# Task Manager (Stage-002: 唯一状态写入口)
# =========================
manager = TaskManager()
tasks = manager._tasks
tasks_lock = manager._lock

# =========================
# Scheduler (Stage-003: 活动槽位 + FIFO 等待队列的唯一决策点)
# =========================
def _make_engine():
    """每次运行构造一个引擎；yt-dlp 路径延迟读取，便于测试/探针替换。"""
    return DownloadEngine(manager, ytdlp=YT_DLP, download_dir=DOWNLOAD_DIR,
                          logger=log)


scheduler = Scheduler(manager, _make_engine, max_active=MAX_ACTIVE_TASKS,
                      logger=log, download_dir=DOWNLOAD_DIR)

# =========================
# 控制接口 (Stage-004)：POST /pause /resume /cancel /retry
# =========================
CONTROL_ROUTES = {
    "/pause": "pause",
    "/resume": "resume",
    "/cancel": "cancel",
    "/retry": "retry",
}

# task_id 只允许 uuid4()[:8] 这类安全字符，避免任何拼接/注入面
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def control_action(path, task_id):
    """Run a scheduler control action; returns (body, http_code)."""
    method_name = CONTROL_ROUTES.get(path)
    if method_name is None:
        return _error("not_found", "Not Found", 404)
    if not task_id:
        return _error("missing_task_id", "Missing task_id", 400)
    if not TASK_ID_RE.match(str(task_id)):
        return _error("invalid_task_id",
                      "task_id must be 1-64 chars of [A-Za-z0-9_-]", 400,
                      task_id)
    try:
        result = getattr(scheduler, method_name)(str(task_id))
        return result, 200
    except ControlError as exc:
        return _error(exc.code, exc.message, exc.http_status, exc.task_id)
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to HTTP
        log(f"control {method_name} failed for {task_id}: {exc}")
        return _error("control_failed", str(exc), 500, task_id)


def _get(task_id):
    t = manager.get(task_id)
    return t.to_dict() if t else None


def _all():
    return manager.all()


def _update(task_id, **fields):
    status = fields.pop("status", None)
    # 兼容旧直接调用：download_video(task_id, url) 先建 pending 再转态
    if manager.get(task_id) is None:
        url = fields.pop("url", "")
        manager.get_or_create(task_id, url)
        if not status:
            status = "downloading"
    if status is not None:
        try:
            updated = manager.transition(task_id, status, **fields)
        except Exception:
            # pending->downloading->completed/error 之外的一律拒绝写脏状态
            return
        _sync_legacy_view(task_id, updated.to_dict())
        return
    t = manager.get(task_id)
    if t is None:
        return
    if t.status != "downloading":
        return
    if "title" in fields and len(fields) == 1:
        manager.report_title(task_id, fields["title"])
    elif "percent" in fields:
        manager.report_progress(task_id, fields.get("percent", t.percent),
                                fields.get("speed", t.speed),
                                fields.get("eta", t.eta))
    elif set(fields) == {"speed"} and fields.get("speed") == "merging":
        manager.report_merging(task_id)
    _sync_legacy_view(task_id, manager.get(task_id).to_dict())


def _sync_legacy_view(task_id, full):
    """让旧读码/调试仍能从 manager._tasks 看到 Stage-001 字段。"""
    with tasks_lock:
        legacy = tasks.get(task_id)
        if isinstance(legacy, Task):
            return
        if isinstance(legacy, dict):
            for key in ("status", "percent", "speed", "eta", "url", "title",
                        "created_at", "updated_at"):
                if key in full:
                    legacy[key] = full[key]


def download_video(task_id, url):
    """Stage-002 兼容入口：把已存在的 Task 交给调度器。

    Stage-002 中它是线程体（同步跑完一次下载）；Stage-003 起 HTTP 入口统一
    走 `scheduler.submit()`，这里保留为显式适配入口，同样受活动上限和
    FIFO 队列约束。返回 True 表示已进入活动集合，False 表示已排队。
    """
    manager.get_or_create(task_id, url)
    return scheduler.admit(task_id, url)


def _error(code, message, http_code, task_id=None):
    body = {"error_code": code, "message": message}
    if task_id is not None:
        body["task_id"] = task_id
    return body, http_code


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 接管 http.server 默认日志，走统一 log()
        log(f"HTTP {self.address_string()} {fmt % args}")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_GET(self):
        parsed = urlparse(self.path)
        # 健康检查 (plan.md #9)
        if parsed.path == "/health":
            self._json({"status": "ok"})
            return
        # 共享任务列表：全量任务（契约排序）+ 调度器计数 (Stage-003)
        if parsed.path == "/tasks":
            self._json(build_task_list(manager, scheduler))
            return
        # 查询下载进度：/status 全量 / /status?id=xxx 单任务 (plan.md #9)
        if parsed.path == "/status":
            params = parse_qs(parsed.query)
            tid = params.get("id", [None])[0]
            if tid:
                t = _get(tid)
                if t is None:
                    body, code = _error("task_not_found", "task not found",
                                        404, tid)
                    self._json(body, code=code)
                else:
                    self._json(t)
            else:
                self._json(_all())
            return
        # 开始下载
        if parsed.path == "/download":
            params = parse_qs(parsed.query)
            url = params.get("url", [None])[0]
            if not url:
                body, code = _error("missing_url", "Missing url", 400)
                self._json(body, code=code)
                return
            if not (url.startswith("http://") or url.startswith("https://")):
                body, code = _error("invalid_url",
                                    "URL must use http or https", 400)
                self._json(body, code=code)
                return
            # 创建 pending Task 后交给调度器：有空闲槽位就启动，否则 FIFO 排队
            created = scheduler.submit(url)
            self._json({"task_id": created["task_id"]})
            return
        # 其他路径
        body, code = _error("not_found", "Not Found", 404)
        self._json(body, code=code)

    def do_POST(self):
        """控制接口 (Stage-004)：JSON body {"task_id": "..."}。"""
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            body, code = _error("bad_request", "Expected a JSON object body", 400)
            self._json(body, code=code)
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            body, code = _error("bad_request", "Body is not valid JSON", 400)
            self._json(body, code=code)
            return
        if not isinstance(payload, dict):
            body, code = _error("bad_request", "Body must be a JSON object", 400)
            self._json(body, code=code)
            return
        body, code = control_action(parsed.path, payload.get("task_id"))
        self._json(body, code=code)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()


class MediaDockServer(ThreadingHTTPServer):
    """单实例本地服务。

    `HTTPServer` 默认 `allow_reuse_address = True`，实测在 Windows 上会让
    第二个进程也成功绑定同一 127.0.0.1:8765（socket 实验：持有一方设置
    SO_REUSEADDR 后，默认配置的 HTTPServer 能再次绑定成功）。两个进程各有
    一份内存任务表，前端会看到任务"时有时无"。这里关掉端口复用：端口被
    占用时以 rc=2 退出并写明确日志。
    """

    allow_reuse_address = False


def main():
    try:
        server = MediaDockServer(("127.0.0.1", 8765), Handler)
    except OSError as exc:
        log(f"ERROR: cannot bind 127.0.0.1:8765 ({exc})")
        log("另一个 MediaDock 实例可能已在运行；请先结束它再启动。")
        log("提示：Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
            "| Where-Object { $_.CommandLine -like '*server.py*' }")
        sys.exit(2)
    log("MediaDock server started")
    log("http://127.0.0.1:8765")
    log(f"downloads: {DOWNLOAD_DIR}")
    log(f"yt-dlp: {YT_DLP}")
    log(f"max active downloads: {scheduler.active_limit}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()