"""Stage-007 platform Adapter live probe (no real network, no real download).

Starts a real `server.Handler` on 127.0.0.1:<ephemeral port> against a real
config/log/SQLite setup and proves the Stage-007 contract end to end:

  * the platform decision lives in exactly one module: `server.py` contains no
    platform literal and no second URL parser
  * `DEFAULT_REGISTRY` only knows YouTube; `platform_names()` reports it
  * every YouTube domain shape (www/m/music/youtu.be/nocookie/shorts) returns
    200 `{task_id}` and the Task carries `platform="youtube"`
  * a valid http(s) URL of another platform returns 400 `unsupported_platform`
    and creates no Task, no queue slot and no process
  * the Stage-006 error contract survives: `invalid_url` / `missing_url`
  * the Adapter extension point works at runtime (`register()` + `ready`)
  * detection is side-effect free: no Task, no event, no file is produced

Process spawn is replaced by an instant in-process engine; the HTTP handler,
the scheduler, the store and the real default registry are untouched.

Usage (project venv):
  C:\\\\Users\\\\Administrator\\\\Envs\\\\mediadock\\\\Scripts\\\\python.exe tests\\\\probe_platform.py
Writes tests\\\\probe_platform_result.json.
"""
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse

from http.server import ThreadingHTTPServer

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 必须在 import server 之前：模块导入时就会 bootstrap 一次
os.environ["MEDIADOCK_DB"] = ":memory:"
sys.path.insert(0, REPO_DIR)

import server as srv  # noqa: E402 - env must be set first
from core_platform import (DEFAULT_REGISTRY, PlatformAdapter,  # noqa: E402
                           PlatformRegistry, YouTubeAdapter,
                           detect_platform, platform_names)


class InstantEngine:
    """Engine stub: finishes a task immediately, never spawns a process."""

    def run(self, task_id, url, control=None):
        srv.manager.transition(task_id, "downloading")
        srv.manager.transition(task_id, "completed", percent=100.0)


def request(port, path, method="GET", timeout=15):
    """Send a request with full control over the raw path."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        raw = response.read().decode("utf-8", "replace")
        return response.status, raw
    finally:
        connection.close()


def as_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def download(port, url):
    return request(port, "/download?url=" + urllib.parse.quote(url, safe=""))


def wait_status(task_id, statuses, timeout=15.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        task = srv.manager.get(task_id)
        last = task.status if task else None
        if last in statuses:
            return last
        time.sleep(0.02)
    return last


def main():
    checks = {}
    srv.bootstrap(":memory:")
    srv.scheduler.set_engine_factory(lambda: InstantEngine())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        # 1) 单一真值：server.py 不写死平台字符串
        with open(os.path.join(REPO_DIR, "server.py"), "r",
                  encoding="utf-8") as handle:
            server_source = handle.read()
        checks["server_has_no_platform_literal"] = "youtube" not in server_source
        checks["server_uses_detector"] = "detect_platform(" in server_source
        checks["registry_only_youtube"] = platform_names() == ["youtube"]
        checks["registry_default_is_youtube"] = isinstance(
            DEFAULT_REGISTRY.get("youtube"), YouTubeAdapter)

        # 2) YouTube 全域名形态 -> 200 + platform=youtube
        youtube_urls = (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        )
        ok_codes = True
        platform_ok = True
        for url in youtube_urls:
            code, raw = download(port, url)
            ok_codes = ok_codes and code == 200
            if code != 200:
                continue
            task = srv.manager.get(as_json(raw).get("task_id", ""))
            platform_ok = platform_ok and task is not None \
                and task.platform == "youtube"
        checks["youtube_domains_200"] = ok_codes
        checks["youtube_task_platform"] = platform_ok

        # 3) fragment 归一化：交给引擎的 URL 不带 fragment
        code, raw = download(
            port, "https://www.youtube.com/watch?v=dQw4w9WgXcQ#t=42")
        frag_id = as_json(raw).get("task_id", "")
        frag_task = srv.manager.get(frag_id) if frag_id else None
        checks["fragment_stripped"] = (
            code == 200 and frag_task is not None
            and frag_task.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        # 4) 未接入平台：400 unsupported_platform 且不产生 Task/槽位/进程
        before = len(srv.manager.ids())
        unsupported_codes = []
        for url in ("https://www.tiktok.com/@a/video/1",
                    "https://x.com/a/status/1",
                    "https://www.instagram.com/p/abc/",
                    "https://www.facebook.com/watch/?v=1",
                    "https://example.com/v.mp4"):
            code, raw = download(port, url)
            unsupported_codes.append(
                (code, as_json(raw).get("error_code", "")))
        checks["unsupported_platform_400"] = all(
            code == 400 and err == "unsupported_platform"
            for code, err in unsupported_codes)
        checks["unsupported_creates_no_task"] = len(srv.manager.ids()) == before
        checks["unsupported_no_slot"] = (
            srv.scheduler.active_count() == 0
            and srv.scheduler.queued_count() == 0)

        # 5) Stage-001..006 错误契约不变
        code, raw = request(port, "/download")
        checks["missing_url_400"] = (
            code == 400 and as_json(raw)["error_code"] == "missing_url")
        code, raw = download(port, "ftp://www.youtube.com/watch?v=abc123456")
        checks["invalid_url_400"] = (
            code == 400 and as_json(raw)["error_code"] == "invalid_url")
        code, raw = download(
            port, "https://user:pw@youtube.com/watch?v=abc123456")
        checks["userinfo_rejected_400"] = (
            code == 400 and as_json(raw)["error_code"] == "invalid_url")

        # 6) /status 形状不变（platform 字段可见，既有字段不变）
        code, raw = request(port, "/status?id=" + frag_id)
        payload = as_json(raw)
        checks["status_shape_unchanged"] = (
            code == 200 and payload.get("status") == "completed"
            and payload.get("platform") == "youtube"
            and payload.get("percent") == 100.0
            and payload.get("url") == frag_task.url)
        checks["task_reached_terminal"] = wait_status(
            frag_id, ("completed",)) == "completed"

        # 7) 检测层无副作用：不建 Task、不改任务表
        task_count = len(srv.manager.ids())
        detect_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        detect_platform("https://example.com/v")
        detect_platform("nonsense")
        checks["detect_has_no_side_effect"] = (
            len(srv.manager.ids()) == task_count == before)

        # 8) 扩展点：注册即可被发现，未就绪平台显式拒绝
        class StubAdapter(PlatformAdapter):
            name = "stub"
            hosts = ("stub.example",)

        class NotReadyAdapter(PlatformAdapter):
            name = "notready"
            hosts = ("notready.example",)
            ready = False

        registry = PlatformRegistry((YouTubeAdapter(),))
        first = registry.detect("https://stub.example/v")
        registry.register(StubAdapter())
        second = registry.detect("https://stub.example/v")
        third = PlatformRegistry((NotReadyAdapter(),)).detect(
            "https://notready.example/v")
        checks["extension_point_visible"] = (
            first[1] == "unsupported_platform"
            and second[0] is not None and second[1] == ""
            and registry.names() == ["youtube", "stub"])
        checks["not_ready_rejected"] = (
            third[0] is None and third[1] == "platform_not_ready")

        failed = [name for name, ok in checks.items() if not ok]
        result = {
            "base": base, "bind": "127.0.0.1", "port_mode": "ephemeral",
            "engine_stub": "instant engine (process spawn replaced)",
            "platforms": platform_names(),
            "youtube_domains": list(youtube_urls),
            "unsupported_results": [
                {"code": code, "error_code": err}
                for code, err in unsupported_codes],
            "checks": checks, "failed_checks": failed,
        }
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "probe_platform_result.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        assert not failed, failed
        print("PLATFORM PROBE OK checks=%d platforms=%s"
              % (len(checks), result["platforms"]))
        print("wrote " + path)
    finally:
        for task_id in list(srv.scheduler.active_ids()) + \
                list(srv.scheduler.queued_ids()):
            try:
                srv.scheduler.cancel(task_id, timeout=5)
            except Exception:  # noqa: BLE001 - probe cleanup only
                pass
        srv.scheduler.wait_idle(20)
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
