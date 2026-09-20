import atexit
import json
import os
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlparse, parse_qs

from core_config import (Config, config_public, load_config)
from core_control import ControlError
from core_deps import failed_checks, run_checks
from core_engine import DownloadEngine, resolve_ffmpeg
from core_formats import (DEFAULT_PRESET, ERROR_FORMAT_NOT_AVAILABLE,
                          ERROR_INVALID_FORMAT, FormatsProbe, audio_format_for,
                          format_id_present, preset_satisfied, resolve_preset,
                          selector_for, validate_format_id)
from core_instance import take_over_port
from core_listing import (build_history, build_task_list, normalize_limit,
                          HISTORY_DEFAULT_LIMIT, HISTORY_STATUSES)
from core_media import (AUDIO_TASK_TYPE, ERROR_INVALID_TARGET,
                        ERROR_SOURCE_NOT_FOUND, ERROR_SOURCE_OUTSIDE,
                        AudioProcessor, find_source_file, resolve_target,
                        targets_public)
from core_manager import TaskManager
from core_parse import MERGE_RE, PROGRESS_RE
from core_platform import detect_platform
from core_release import APP_VERSION, release_info
from core_scheduler import MAX_ACTIVE_TASKS, Scheduler
from core_security import (body_within_limit, check_host_header, check_origin,
                           clip, redact)
from core_store import (PURGE_KEEP_DEFAULT, TaskPersister, open_store,
                        resolve_db_path)
from core_task import Task

# =========================
# 配置 (Stage-006)：路径/端口/日志/数据库的唯一真值来源是 core_config.Config
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LEVEL_RANK = {"debug": 10, "info": 20, "warning": 30, "error": 40}

# 类型说明：下面这些全局量在模块导入末尾（apply_config + bootstrap）一定会被赋值，
# 但初始值是 None。用 cast 声明真实类型，避免 Pylance 把它们推断为 None，
# 从而在使用点误报 "xxx is not a known attribute of None"。
CONFIG: Config = cast(Config, None)
CONFIG_ERRORS = []
HOST = "127.0.0.1"
PORT = 8765
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
YT_DLP = ""
FFMPEG = ""
LOG_FILE = os.path.join(BASE_DIR, "MediaDock-server.log")
LOG_LEVEL = "info"
LOG_MAX_LINE = 4000
DEPENDENCIES = {}

# Stage-008: formats probe seam + last-probed payloads per normalized URL.
FORMATS_RUNNER = None
FORMATS_PROBE_FACTORY = None
FORMATS_CACHE = {}
FORMATS_CACHE_LIMIT = 20

# Stage-009: media (audio) conversion seams.
MEDIA_POPEN_FACTORY = None
MEDIA_PROCESSOR_FACTORY = None

_log_fp = None


def _open_log_file(path):
    """(Re)open the configured log file; keep the old handle on failure.

    A missing/unwritable log file must not stop the server, so failures are
    reported on stderr only (Stage-006.md 9.1).
    """
    global _log_fp, LOG_FILE
    target = str(path or "")
    try:
        handle = open(target, "a", encoding="utf-8", buffering=1)
    except OSError as exc:
        print(f"[MediaDock] cannot open log file {target}: {exc}", flush=True)
        LOG_FILE = target
        return False
    if _log_fp is not None:
        try:
            _log_fp.close()
        except Exception:  # noqa: BLE001 - reopening must not fail on this
            pass
    _log_fp = handle
    LOG_FILE = target
    return True


def _close_log_file():
    global _log_fp
    if _log_fp is not None:
        try:
            _log_fp.close()
        except Exception:  # noqa: BLE001 - process shutdown best effort
            pass


# 进程退出时关闭日志句柄，避免 unittest 报 ResourceWarning
atexit.register(_close_log_file)


def log(*args, level="info"):
    """Log with level filtering and redaction (Stage-006.md 5.5/任务006)."""
    rank = LEVEL_RANK.get(str(level).lower(), LEVEL_RANK["info"])
    if rank < LEVEL_RANK.get(LOG_LEVEL, LEVEL_RANK["info"]):
        return
    msg = " ".join(str(a) for a in args)
    stamp = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
    line = clip(stamp + redact(msg, LOG_MAX_LINE), LOG_MAX_LINE)
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001 - pythonw has no console
        pass
    if _log_fp is not None:
        try:
            _log_fp.write(line + "\n")
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


# yt-dlp 查找：优先固定路径，其次 PATH 中的 yt-dlp / yt-dlp.exe
def resolve_ytdlp():
    from core_engine import resolve_ytdlp as _resolve
    return _resolve()


def effective_db_path(explicit=None):
    """`explicit` > `config.db_path` > `MEDIADOCK_DB` > `<仓库>/tasks.db`."""
    if explicit:
        return explicit
    if CONFIG is not None and CONFIG.db_path:
        return CONFIG.db_path
    return resolve_db_path(None)


def apply_config(config):
    """Point every module-level truth at `config` (Stage-006.md 5.6)."""
    global CONFIG, CONFIG_ERRORS, HOST, PORT, DOWNLOAD_DIR, YT_DLP, FFMPEG
    global LOG_LEVEL, LOG_MAX_LINE, DB_PATH
    CONFIG = config
    CONFIG_ERRORS = list(config.errors)
    HOST = config.host
    PORT = config.port
    DOWNLOAD_DIR = config.download_dir
    YT_DLP = config.ytdlp_path or resolve_ytdlp()
    FFMPEG = config.ffmpeg_path or resolve_ffmpeg()
    LOG_LEVEL = config.log_level
    LOG_MAX_LINE = config.log_line_max
    _open_log_file(config.log_file)
    DB_PATH = effective_db_path(None)
    return config


def reload_config(path=None, env=None):
    """Reload configuration from disk/env and apply it (no restart needed)."""
    return apply_config(load_config(path=path, env=env, logger=log))


def dependencies_info():
    """Startup dependency snapshot (created by `refresh_dependencies`)."""
    return DEPENDENCIES


def refresh_dependencies(deep=False):
    """Re-run yt-dlp/FFmpeg/dir/disk checks; never blocks the server."""
    global DEPENDENCIES
    DEPENDENCIES = run_checks(CONFIG, log, deep=deep)
    failed = failed_checks(DEPENDENCIES)
    if failed:
        log(f"dependency check failed: {', '.join(failed)}", level="warning")
    return DEPENDENCIES


CONFIG = load_config(logger=log)
apply_config(CONFIG)

# =========================
# Stage-005: 存储 + Task Manager + Scheduler 由 bootstrap() 统一装配
# =========================
INTERRUPTED_CODE = "interrupted"
INTERRUPTED_MESSAGE = ("service restarted before the task finished; "
                       "it was not resumed automatically")
RESTART_ERROR_STATUSES = ("downloading", "pending")

manager: TaskManager = cast(TaskManager, None)
scheduler: Scheduler = cast(Scheduler, None)
storage: Any = None
storage_reason = ""
tasks = {}
tasks_lock: Any = None
DB_PATH = effective_db_path(None)

# =========================
# Scheduler (Stage-003: 活动槽位 + FIFO 等待队列的唯一决策点)
# =========================
def _make_engine():
    """每次运行构造一个引擎；yt-dlp 路径延迟读取，便于测试/探针替换。"""
    return DownloadEngine(manager, ytdlp=YT_DLP, download_dir=DOWNLOAD_DIR,
                          logger=log, ffmpeg=FFMPEG)


def _apply_restart_matrix(store, task_manager):
    """Restart rules (Stage-005.md 5.3). Returns the interrupted task ids.

    `downloading` and `pending` cannot survive a restart: the process and the
    queue are gone. They become `error` + `error_code=interrupted` instead of
    pretending to still be running. `paused` stays paused; terminal records
    load unchanged.
    """
    restored = store.load_tasks()
    interrupted = []
    for task_id, raw in restored.items():
        data = dict(raw)
        status = str(data.get("status") or "")
        if status in RESTART_ERROR_STATUSES:
            data["status"] = "error"
            data["error_code"] = INTERRUPTED_CODE
            data["error_message"] = INTERRUPTED_MESSAGE
            data["completed_at"] = data.get("completed_at") or \
                datetime.now().isoformat(timespec="seconds")
            store.save_task(data)
            store.record_event(task_id, "restart_interrupted", status)
            interrupted.append(task_id)
            log(f"task {task_id} was {status} at shutdown -> error/interrupted")
        try:
            task_manager.load_task(data)
        except ValueError as exc:
            log(f"skipped invalid persisted task {task_id}: {exc}")
    return interrupted


def set_formats_probe(probe):
    """Test/harness seam: replace how the formats probe is built."""
    global FORMATS_PROBE_FACTORY
    FORMATS_PROBE_FACTORY = probe
    clear_formats_cache()


def formats_probe():
    """Current formats probe (Stage-008); `FORMATS_RUNNER` injects the runner."""
    if FORMATS_PROBE_FACTORY is not None:
        return FORMATS_PROBE_FACTORY()
    return FormatsProbe(YT_DLP, FFMPEG, runner=FORMATS_RUNNER, logger=log)


def clear_formats_cache():
    FORMATS_CACHE.clear()


def cache_formats(url, payload):
    """Remember the last `/formats` payload per URL (bounded FIFO)."""
    key = str(url or "")
    if not key or not isinstance(payload, dict):
        return
    FORMATS_CACHE.pop(key, None)
    FORMATS_CACHE[key] = payload
    while len(FORMATS_CACHE) > FORMATS_CACHE_LIMIT:
        FORMATS_CACHE.pop(next(iter(FORMATS_CACHE)), None)


def cached_formats(url):
    return FORMATS_CACHE.get(str(url or ""))


def resolve_format_choice(url, preset_raw, format_id_raw):
    """`(expression, audio_format, error_code, message)`; nothing unvalidated.

    A non-default preset or an explicit `format_id` must match something a
    previous `/formats` call really reported for this URL, so no arbitrary text
    can reach the yt-dlp argv (Stage-008.md 5.4). `audio_format` is non-empty
    only for the fixed `audio` preset, which extracts MP3 with FFmpeg
    (Stage-012).
    """
    preset, code, message = resolve_preset(preset_raw)
    if preset is None:
        return "", "", code, message
    format_id = str(format_id_raw or "").strip()
    cached = cached_formats(url)
    formats = list(cached.get("formats") or []) if cached else []
    if format_id:
        ok, message = validate_format_id(format_id)
        if not ok:
            return "", "", ERROR_INVALID_FORMAT, message
        if not format_id_present(format_id, formats):
            return "", "", ERROR_FORMAT_NOT_AVAILABLE, (
                "format_id is not available for this video; "
                "call /formats first")
        return selector_for(None, format_id), "", "", ""
    if preset.name == DEFAULT_PRESET:
        return selector_for(preset), "", "", ""
    if not cached:
        return "", "", ERROR_FORMAT_NOT_AVAILABLE, (
            "call /formats before choosing a preset")
    if not preset_satisfied(preset, formats):
        return "", "", ERROR_FORMAT_NOT_AVAILABLE, (
            f"preset {preset.name} is not available for this video")
    return (selector_for(preset), audio_format_for(preset), "", "")


def media_processor_factory():
    """How the scheduler builds the media path (test seam, Stage-009)."""
    if MEDIA_PROCESSOR_FACTORY is not None:
        return MEDIA_PROCESSOR_FACTORY()
    return AudioProcessor(manager, ffmpeg=FFMPEG, download_dir=DOWNLOAD_DIR,
                          popen_factory=MEDIA_POPEN_FACTORY, logger=log)


def task_engine():
    """One run: audio jobs go to the media processor, everything else to yt-dlp."""
    download = DownloadEngine(manager, ytdlp=YT_DLP, download_dir=DOWNLOAD_DIR,
                              ffmpeg=FFMPEG, logger=log)

    class _TaskEngine:
        def run(self, task_id, url, control=None):
            job = dict(getattr(control, "media_job", {}) or {})
            if job.get("kind") == "audio":
                return media_processor_factory().run(task_id, job, control)
            return download.run(task_id, url, control)

    return _TaskEngine()


def set_media_processor(factory):
    """Test/harness seam: replace how the media processor is built."""
    global MEDIA_PROCESSOR_FACTORY
    MEDIA_PROCESSOR_FACTORY = factory


def audio_targets_info():
    """`GET /audio` discovery payload (Stage-009)."""
    from core_media import DEFAULT_TARGET
    return {"targets": targets_public(), "default": DEFAULT_TARGET,
            "audio_type": AUDIO_TASK_TYPE}


def audio_action(source_task_id, target_raw, source_raw=""):
    """`POST /audio`: validate, then create one media Task.

    Returns `(body, http_code)`. The HTTP input can only pick a target name
    and point at a file that already lives inside the download directory.
    """
    if not source_task_id:
        return _error("missing_task_id", "Missing task_id", 400)
    if not TASK_ID_RE.match(str(source_task_id)):
        return _error("invalid_task_id", "task_id must be 1-64 chars of "
                      "[A-Za-z0-9_-]", 400, str(source_task_id))
    target, code, message = resolve_target(target_raw)
    if target is None:
        return _error(code or ERROR_INVALID_TARGET, message, 400)
    source_task = manager.get(str(source_task_id))
    if source_task is None:
        return _error("task_not_found", "task not found", 404,
                      str(source_task_id))
    if source_task.type != "download":
        return _error("not_a_download_task",
                      "only a download task can be converted", 409,
                      source_task.task_id)
    if source_task.status != "completed":
        return _error("not_completed",
                      f"task is {source_task.status}; wait for completed", 409,
                      source_task.task_id)
    path, code, message = find_source_file(DOWNLOAD_DIR, source_task.url,
                                           source_raw)
    if path == "":
        if code == ERROR_SOURCE_OUTSIDE:
            return _error(code, message, 400, source_task.task_id)
        if code == ERROR_SOURCE_NOT_FOUND:
            return _error(code, message, 404, source_task.task_id)
        return _error(code or ERROR_SOURCE_NOT_FOUND, message, 400,
                      source_task.task_id)
    job = {"kind": "audio", "source": path, "target": target.name,
           "source_task_id": source_task.task_id}
    created = scheduler.submit(source_task.url,
                               platform=source_task.platform,
                               media_job=job, task_type=AUDIO_TASK_TYPE)
    return {"task_id": created["task_id"],
            "source_task_id": source_task.task_id,
            "target": target.name, "source": path}, 200


def storage_info():
    """Payload for /health and /tasks (Stage-005.md 5.4)."""
    if storage is None:
        return {"kind": "memory", "db": DB_PATH, "schema_version": 0,
                "degraded": bool(storage_reason), "reason": storage_reason}
    info = storage.info()
    if storage_reason:
        info["reason"] = info.get("reason") or storage_reason
    return info


def bootstrap(db_path=None, config=None):
    """(Re)build storage + manager + scheduler; safe to call again (tests).

    Precedence for the database file (Stage-006.md 5.1): explicit `db_path`
    > `Config.db_path` > `MEDIADOCK_DB` > `<repo>/tasks.db`.
    """
    global manager, scheduler, tasks, tasks_lock, storage, storage_reason
    global DB_PATH
    if config is not None:
        apply_config(config)
    clear_formats_cache()
    if storage is not None:
        try:
            storage.close()
        except Exception:  # noqa: BLE001 - reopening must not fail on this
            pass
    DB_PATH = effective_db_path(db_path)
    store, reason = open_store(DB_PATH, log)
    storage = store
    storage_reason = reason
    persister = TaskPersister(store, log) if store is not None else None
    manager = TaskManager(persister=persister)
    interrupted = []
    if store is not None:
        interrupted = _apply_restart_matrix(store, manager)
        purged = store.purge_terminal(CONFIG.purge_keep or PURGE_KEEP_DEFAULT)
        log(f"storage ready: {len(manager.ids())} task(s) loaded, "
            f"{len(interrupted)} marked interrupted, {purged} purged")
    else:
        log("storage disabled: running from memory only")
    scheduler = Scheduler(manager, _make_engine,
                          max_active=CONFIG.max_active_tasks,
                          logger=log, download_dir=DOWNLOAD_DIR)
    # Stage-009：一次运行按 control.media_job 路由到 yt-dlp 或 FFmpeg
    scheduler.set_engine_factory(task_engine)
    tasks = manager._tasks
    tasks_lock = manager._lock
    refresh_dependencies()
    return manager, scheduler


bootstrap()

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


# Stage-005：终态记录可由用户手动删除（D-017），运行中的任务不允许删
DELETABLE_STATUSES = ("completed", "error", "cancelled")


def delete_action(task_id):
    """POST /delete：删除一条终态任务记录（内存 + 数据库）。"""
    if not task_id:
        return _error("missing_task_id", "Missing task_id", 400)
    if not TASK_ID_RE.match(str(task_id)):
        return _error("invalid_task_id",
                      "task_id must be 1-64 chars of [A-Za-z0-9_-]", 400,
                      task_id)
    task = manager.get(str(task_id))
    if task is None:
        return _error("task_not_found", "task not found", 404, task_id)
    if task.status not in DELETABLE_STATUSES:
        return _error("not_deletable",
                      f"task {task_id} is {task.status}; only finished tasks "
                      f"can be deleted", 409, task_id)
    manager.drop(str(task_id))
    log(f"deleted task {task_id} ({task.status})")
    return {"task_id": str(task_id), "deleted": True}, 200


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
        updated = manager.report_title(task_id, fields["title"])
    elif "percent" in fields:
        updated = manager.report_progress(task_id,
                                          fields.get("percent", t.percent),
                                          fields.get("speed", t.speed),
                                          fields.get("eta", t.eta))
    elif set(fields) == {"speed"} and fields.get("speed") == "merging":
        updated = manager.report_merging(task_id)
    else:
        updated = t
    # report_* 返回更新后的 Task；返回 None 表示任务已消失，沿用本地快照
    _sync_legacy_view(task_id, (updated if updated is not None else t).to_dict())


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
    def log_message(self, format, *args):
        # 接管 http.server 默认日志，走统一 log()
        # 形参名必须与基类 BaseHTTPRequestHandler.log_message 一致
        log(f"HTTP {self.address_string()} {format % args}")

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

    def _guard(self):
        """Host/Origin gate (Stage-006.md 5.5). False => response already sent."""
        ok, detail = check_host_header(self.headers.get("Host"), PORT)
        if not ok:
            log(f"request rejected: {detail}", level="warning")
            body, code = _error("forbidden_host", detail, 403)
            self._json(body, code=code)
            return False
        ok, detail = check_origin(self.headers.get("Origin"))
        if not ok:
            log(f"request rejected: {detail}", level="warning")
            body, code = _error("forbidden_origin", detail, 403)
            self._json(body, code=code)
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        parsed = urlparse(self.path)
        # 健康检查 (plan.md #9 / Stage-005 存储 / Stage-006 配置与依赖 /
        # Stage-010 版本)：version 为增量字段，其余字段形状不变
        if parsed.path == "/health":
            snapshot = storage_info()
            self._json({"status": "ok",
                        "version": release_info(snapshot.get("schema_version")),
                        "storage": snapshot,
                        "config": config_public(CONFIG),
                        "dependencies": dependencies_info()})
            return
        # 共享任务列表：全量任务（契约排序）+ 调度器计数 (Stage-003)
        if parsed.path == "/tasks":
            payload = build_task_list(manager, scheduler)
            payload["storage"] = storage_info()
            self._json(payload)
            return
        # 历史查询 (Stage-005)：仅终态任务，按完成时间倒序
        if parsed.path == "/history":
            params = parse_qs(parsed.query)
            limit, err = normalize_limit(params.get("limit", [None])[0])
            if err:
                body, code = _error("invalid_limit", err, 400)
                self._json(body, code=code)
                return
            # 走到这里 err 必为 None，limit 一定是有效整数（仅用于让类型检查收窄）
            limit = cast(int, limit)
            status = params.get("status", [None])[0]
            if status and status not in HISTORY_STATUSES:
                body, code = _error(
                    "invalid_status",
                    "status must be one of " + ", ".join(HISTORY_STATUSES), 400)
                self._json(body, code=code)
                return
            self._json(build_history(manager, limit=limit, status=status,
                                     storage=storage_info()))
            return
        # 状态事件流 (Stage-005)：未知任务且无事件 -> 404
        if parsed.path == "/events":
            params = parse_qs(parsed.query)
            tid = params.get("id", [None])[0]
            if not tid:
                body, code = _error("missing_task_id", "Missing id", 400)
                self._json(body, code=code)
                return
            if not TASK_ID_RE.match(str(tid)):
                body, code = _error("invalid_task_id",
                                    "task_id must be 1-64 chars of "
                                    "[A-Za-z0-9_-]", 400, tid)
                self._json(body, code=code)
                return
            limit, err = normalize_limit(params.get("limit", [None])[0],
                                         default=50)
            if err:
                body, code = _error("invalid_limit", err, 400)
                self._json(body, code=code)
                return
            limit = cast(int, limit)
            events = storage.events(str(tid), limit) if storage is not None else []
            if not events and manager.get(str(tid)) is None:
                body, code = _error("task_not_found", "task not found", 404, tid)
                self._json(body, code=code)
                return
            self._json({"task_id": str(tid), "events": events,
                        "returned": len(events)})
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
        # 格式查询 (Stage-008)：先过平台检测，只对已接入平台查询 yt-dlp
        if parsed.path == "/formats":
            params = parse_qs(parsed.query)
            url = params.get("url", [None])[0]
            if not url:
                body, code = _error("missing_url", "Missing url", 400)
                self._json(body, code=code)
                return
            adapter, code_name, message = detect_platform(url)
            if adapter is None:
                body, code = _error(code_name, message,
                                    400 if code_name != "formats_unavailable"
                                    else 502)
                self._json(body, code=code)
                return
            info = adapter.info(url)
            payload, code_name, message = formats_probe().fetch(
                info.url, info.name, info.video_id)
            if payload is None:
                body, code = _error(code_name, message, 502)
                self._json(body, code=code)
                return
            cache_formats(info.url, payload)
            self._json(payload)
            return
        # 开始下载
        if parsed.path == "/download":
            params = parse_qs(parsed.query)
            url = params.get("url", [None])[0]
            if not url:
                body, code = _error("missing_url", "Missing url", 400)
                self._json(body, code=code)
                return
            # 平台检测 (Stage-007)：URL 校验 + Adapter 匹配只有一处真值
            adapter, code_name, message = detect_platform(url)
            if adapter is None:
                body, code = _error(code_name, message, 400)
                self._json(body, code=code)
                return
            info = adapter.info(url)
            # 格式选择 (Stage-008)：preset / format_id 必须来自固定表或 /formats 结果
            expression, audio_format, code_name, message = resolve_format_choice(
                info.url, params.get("preset", [None])[0],
                params.get("format_id", [None])[0])
            if code_name:
                body, code = _error(code_name, message, 400)
                self._json(body, code=code)
                return
            # 归一化 URL 后创建 pending Task：有空闲槽位就启动，否则 FIFO 排队
            created = scheduler.submit(info.url, platform=info.name,
                                       format_expr=expression,
                                       audio_format=audio_format)
            self._json({"task_id": created["task_id"]})
            return
        # 音频目标列表 (Stage-009)：只列出固定表，不执行任何转换
        if parsed.path == "/audio":
            self._json(audio_targets_info())
            return
        # 其他路径
        body, code = _error("not_found", "Not Found", 404)
        self._json(body, code=code)

    def do_POST(self):
        """控制接口 (Stage-004)：JSON body {"task_id": "..."}。"""
        if not self._guard():
            return
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if not body_within_limit(length, CONFIG.request_max_bytes):
            body, code = _error(
                "payload_too_large",
                f"body must be at most {CONFIG.request_max_bytes} bytes", 413)
            self._json(body, code=code)
            return
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
        if parsed.path == "/delete":
            body, code = delete_action(payload.get("task_id"))
        elif parsed.path == "/audio":
            params = parse_qs(parsed.query)
            body, code = audio_action(
                payload.get("task_id"),
                params.get("target", [None])[0] or payload.get("target"),
                payload.get("source") or "")
        else:
            body, code = control_action(parsed.path, payload.get("task_id"))
        self._json(body, code=code)

    def do_OPTIONS(self):
        if not self._guard():
            return
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


def run_check_config(argv):
    """`--check-config` CLI: print config + dependency diagnostics, no server.

    Exit code 0 = configuration valid and dependencies usable, 1 = a problem
    was found, 2 = bad command line.
    """
    args = list(argv)
    path = None
    deep = False
    while args:
        arg = args.pop(0)
        if arg == "--check-config":
            continue
        if arg == "--config":
            if not args:
                print("--config requires a file path")
                return 2
            path = args.pop(0)
        elif arg == "--probe":
            deep = True
        else:
            print(f"unknown option: {arg}")
            return 2
    config = load_config(path=path, logger=log)
    snapshot = run_checks(config, log, deep=deep)
    payload = {"config": config_public(config), "dependencies": snapshot}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if config.ok() and snapshot["ok"] else 1


def bind_server():
    """Bind the HTTP server; returns `(server_or_None, error_text)`.

    `MediaDockServer.allow_reuse_address = False` (Stage-003) means a second
    instance fails with `WinError 10048` instead of silently sharing the port.
    """
    try:
        return MediaDockServer((HOST, PORT), Handler), ""
    except OSError as exc:
        return None, str(exc)


def free_port_for_start(restarted):
    """Make `HOST:PORT` available for this start (Stage-011).

    `True` when the caller may bind. Only a process that really holds our port
    **and** looks like a MediaDock `server.py` is stopped; anything else is
    reported and left alone.
    """
    takeover = take_over_port(HOST, PORT, log)
    status = takeover["status"]
    if status in ("free", "killed"):
        if status == "killed":
            log(f"previous instance pid {takeover['pid']} stopped "
                f"({takeover['detail']})")
        return True, takeover
    if status == "foreign":
        log(f"端口 {HOST}:{PORT} 被 pid {takeover['pid']} "
            f"({takeover['name']}) 占用，它不是 MediaDock 服务，已放弃接管。",
            level="warning")
        log(f"该进程命令行：{takeover['command_line']}", level="warning")
    elif status == "unknown":
        log(f"端口 {HOST}:{PORT} 被占用，但无法识别占用者。", level="warning")
    else:
        log(f"接管 {HOST}:{PORT} 失败：{takeover['detail']}", level="warning")
    if restarted:
        log("提示：可手动确认后结束占用进程，或改配置里的 port 再启动。",
            level="warning")
    return False, takeover


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check-config" in argv:
        sys.exit(run_check_config(argv))
    unknown = [arg for arg in argv if arg not in ("--restart",)]
    if unknown:
        print(f"unknown option: {unknown[0]}")
        sys.exit(2)
    restarted = "--restart" in argv

    # --restart：先结束旧实例再启动（显式要求，不等待端口冲突）
    if restarted:
        ok, _ = free_port_for_start(True)
        if not ok:
            sys.exit(2)

    server, error = bind_server()
    if server is None and not restarted:
        # 端口被占用：只接管「确实持有本端口且是 MediaDock server.py」的进程
        ok, _ = free_port_for_start(False)
        if ok:
            server, error = bind_server()
    if server is None:
        log(f"ERROR: cannot bind {HOST}:{PORT} ({error})", level="error")
        log("另一个 MediaDock 实例可能已在运行；可用 "
            "`python server.py --restart` 先结束它再启动。")
        sys.exit(2)
    log("MediaDock server started")
    log(f"version: {APP_VERSION}")
    log(f"http://{HOST}:{PORT}")
    log(f"config: source={CONFIG.source} "
        f"path={CONFIG.path or '(built-in defaults)'} "
        f"errors={len(CONFIG.errors)} warnings={len(CONFIG.warnings)}")
    log(f"downloads: {DOWNLOAD_DIR}")
    log(f"yt-dlp: {YT_DLP}")
    log(f"ffmpeg: {FFMPEG or '(not found - merging may fail)'}")
    log(f"max active downloads: {scheduler.active_limit}")
    info = storage_info()
    log(f"storage: {info['kind']} ({info.get('db')}) "
        f"schema=v{info.get('schema_version')} degraded={info.get('degraded')}")
    deps = dependencies_info()
    summary = ", ".join(f"{c['name']}={c['status']}"
                        for c in deps.get("checks", []))
    log(f"dependencies: ok={deps.get('ok')} [{summary}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()