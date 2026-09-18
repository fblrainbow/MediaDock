import json
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import os

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
# 配置
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
    for c in _CANDIDATES:
        if os.path.isabs(c):
            if os.path.isfile(c):
                return c
        else:
            # PATH 中查找
            for p in os.environ.get("PATH", "").split(os.pathsep):
                full = os.path.join(p.strip('"'), c)
                if os.path.isfile(full):
                    return full
    return _CANDIDATES[0]


YT_DLP = resolve_ytdlp()

# =========================
# Task Manager (MVP: 内存 dict + 锁)
# =========================
tasks = {}
tasks_lock = threading.Lock()


def _update(task_id, **fields):
    with tasks_lock:
        t = tasks.get(task_id)
        if t is None:
            return
        t.update(fields)
        t["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _get(task_id):
    with tasks_lock:
        t = tasks.get(task_id)
        return dict(t) if t else None


def _all():
    with tasks_lock:
        return {k: dict(v) for k, v in tasks.items()}

PROGRESS_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+.*?at\s+(.+?)\s+ETA\s+(.+)"
)
MERGE_RE = re.compile(r"\[Merger\]|Merging formats", re.IGNORECASE)


def download_video(task_id, url):
    with tasks_lock:
        tasks[task_id] = {
            "status": "downloading",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "url": url,
            "title": "",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    command = [
        YT_DLP,
        "-f",
        "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]",
        "--merge-output-format",
        "mp4",
        "--newline",
        "--no-playlist",
        "-P",
        DOWNLOAD_DIR,
        url,
    ]
    log(f"Task {task_id} start: {url}")
    log(f"yt-dlp: {YT_DLP}")
    try:
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        # 只在 Windows 上隐藏/新建控制台；POSIX 上不传该参数
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(command, **popen_kwargs)
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            log(f"[{task_id}] {line}")
            # 合并阶段：yt-dlp 不再输出 [download] 百分比，固定显示 99% 合并中
            if MERGE_RE.search(line):
                _update(task_id, percent=99.0, speed="merging", eta="")
                continue
            if "[download] Destination:" in line:
                continue
            match = PROGRESS_RE.search(line)
            if match:
                _update(
                    task_id,
                    percent=float(match.group(1)),
                    speed=match.group(2).strip(),
                    eta=match.group(3).strip(),
                )
            # 标题回填：[info] ...: Downloading video ... / [download] ... 已有标题时跳过
            elif line.startswith("[info]") and not _get(task_id).get("title"):
                m = re.search(r"\[info\]\s+(.+?):\s+Downloading", line)
                if m:
                    _update(task_id, title=m.group(1).strip())
        process.wait()
        if process.returncode == 0:
            _update(task_id, status="completed", percent=100.0, speed="", eta="")
            log(f"Task {task_id} completed")
        else:
            _update(task_id, status="error")
            log(f"Task {task_id} failed: returncode={process.returncode}")
    except FileNotFoundError:
        _update(task_id, status="error")
        log(f"Task {task_id} error: yt-dlp not found at {YT_DLP}")
    except Exception as e:
        _update(task_id, status="error")
        log(f"Task {task_id} error: {e}")
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
                    self._json({"error": "task not found"}, code=404)
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
                self.send_response(400)
                self.send_cors()
                self.end_headers()
                self.wfile.write(b"Missing url")
                return
            if not (url.startswith("http://") or url.startswith("https://")):
                self.send_response(400)
                self.send_cors()
                self.end_headers()
                self.wfile.write(b"Invalid url")
                return
            # 启动后台线程
            task_id = str(uuid.uuid4())[:8]
            thread = threading.Thread(
                target=download_video,
                args=(task_id, url),
                daemon=True,
            )
            thread.start()
            self._json({"task_id": task_id})
            return
        # 其他路径
        self.send_response(404)
        self.send_cors()
        self.end_headers()
        self.wfile.write(b"Not Found")

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