import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from core_engine import DownloadEngine
from core_manager import TaskManager
from core_parse import MERGE_RE, PROGRESS_RE
from core_task import Task

# =========================
# 日志：同时写文件 + 保留控制台（pythonw 下控制台为空也不报错）
# =========================
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MediaDock-server.log")
_log_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)


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
    created = manager.get_or_create(task_id, url)
    engine = DownloadEngine(manager, ytdlp=YT_DLP,
                            download_dir=DOWNLOAD_DIR, logger=log)
    # 保持旧线程入口签名；pending->downloading 由引擎统一转换
    engine.run(task_id, url)


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
            # 先创建 pending Task，再启动线程（避免查询竞态）
            created = manager.create(url)
            task_id = created.task_id
            thread = threading.Thread(
                target=download_video,
                args=(task_id, url),
                daemon=True,
            )
            thread.start()
            self._json({"task_id": task_id})
            return
        # 其他路径
        body, code = _error("not_found", "Not Found", 404)
        self._json(body, code=code)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    log("MediaDock server started")
    log("http://127.0.0.1:8765")
    log(f"downloads: {DOWNLOAD_DIR}")
    log(f"yt-dlp: {YT_DLP}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()