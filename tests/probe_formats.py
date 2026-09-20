"""Stage-008 formats probe: real HTTP + real argv evidence (no network).

Two boundaries are covered with the strongest evidence available offline:

  * `/formats` runs a **real child process**: `srv.YT_DLP` points at a fake
    yt-dlp that records its own argv and prints a JSON payload, so the probe
    proves `--dump-single-json`, the "URL is the last argument" rule and the
    parse -> stable payload chain without touching the network.
  * the download argv is captured at the spawn boundary (`popen_factory`,
    the same seam `tests/probe_control.py` uses): `-f` must be exactly the
    selector the server resolved, never raw HTTP text.

The real `MediaDock` scheduler, TaskManager, SQLite store and handler are
used; only the process spawn and the payload are stubbed.

Usage (project venv):
  C:\\Users\\Administrator\\Envs\\mediadock\\Scripts\\python.exe tests\\probe_formats.py
Writes tests\\probe_formats_result.json.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse

from http.server import ThreadingHTTPServer

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = tempfile.mkdtemp(prefix="mediadock-formats-")
ARGV_PATH = os.path.join(TMP_DIR, "argv.txt")

PAYLOAD = (
    '{"id":"dQw4w9WgXcQ","title":"Probe video","uploader":"Probe channel",'
    '"duration":213.0,"extractor_key":"Youtube","formats":['
    '{"format_id":"137","ext":"mp4","height":1080,"width":1920,"fps":30,'
    '"vcodec":"avc1.640028","acodec":"none","filesize":1234567,'
    '"format_note":"1080p"},'
    '{"format_id":"136","ext":"mp4","height":720,"width":1280,"fps":30,'
    '"vcodec":"avc1.4d401f","acodec":"none","format_note":"720p"},'
    '{"format_id":"140","ext":"m4a","vcodec":"none","acodec":"mp4a.40.2",'
    '"abr":128.0,"format_note":"medium"}]}'
)

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
SELECTOR_720 = "bv*[height<=720]+ba/b[height<=720]"
SELECTOR_AUDIO = "bestaudio/best"
AUDIO_FORMAT = "mp3"

# 必须在 import server 之前：模块导入时就会 bootstrap 一次
os.environ["MEDIADOCK_DB"] = ":memory:"
sys.path.insert(0, REPO_DIR)

import server as srv  # noqa: E402 - env must be set first
from core_engine import FORMAT_EXPR, DownloadEngine  # noqa: E402
from core_formats import preset_names  # noqa: E402


def make_fake_ytdlp(directory, name, payload, rc=0, record=True):
    """A fake yt-dlp that records its argv and prints one JSON line."""
    if os.name == "nt":
        path = os.path.join(directory, name + ".bat")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("@echo off\r\n")
            if record:
                handle.write('echo %%* > "%s"\r\n' % ARGV_PATH)
            handle.write("echo %s\r\n" % payload)
            handle.write("@exit /b %d\r\n" % rc)
    else:
        path = os.path.join(directory, name + ".sh")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
            if record:
                handle.write('echo "$@" > "%s"\n' % ARGV_PATH)
            handle.write("echo '%s'\n" % payload)
            handle.write("exit %d\n" % rc)
        os.chmod(path, 0o755)
    return path


def recorded_argv():
    if not os.path.isfile(ARGV_PATH):
        return []
    with open(ARGV_PATH, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read().strip().split()


class RecordingProcess:
    """Scripted child process: records the argv it was spawned with."""

    def __init__(self, command, lines):
        self.command = list(command)
        self.returncode = 0
        self.stdout = iter(lines)
        self.pid = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        return None

    def terminate(self):
        return None


def request(port, path, timeout=60):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def quote_url(url):
    return urllib.parse.quote(url, safe="")


def as_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def main():
    checks = {}
    runs = []

    def popen_factory(command, **kwargs):
        process = RecordingProcess(command, [
            "[download]   5.0% of ~ 1.00MiB at 1.00KiB/s ETA 00:02",
            "[download] 100.0% of ~ 1.00MiB at 1.00KiB/s ETA 00:00",
            '[Merger] Merging formats into "probe [dQw4w9WgXcQ].mp4"',
        ])
        runs.append(process)
        return process

    fake_ok = make_fake_ytdlp(TMP_DIR, "yt-dlp-ok", PAYLOAD)
    fake_fail = make_fake_ytdlp(TMP_DIR, "yt-dlp-fail",
                                "ERROR: video unavailable", rc=1, record=False)

    srv.bootstrap(":memory:")
    srv.YT_DLP = fake_ok
    srv.FORMATS_RUNNER = None
    srv.clear_formats_cache()
    srv.scheduler.set_engine_factory(
        lambda: DownloadEngine(srv.manager, ytdlp="FAKE-YTDLP",
                               download_dir=srv.DOWNLOAD_DIR,
                               popen_factory=popen_factory, logger=srv.log))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    base = "http://127.0.0.1:%d" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # 1) 平台闸门先于 yt-dlp：非法/未接入输入不启动任何进程
        code, raw = request(port, "/formats")
        checks["missing_url_400"] = (
            code == 400 and as_json(raw)["error_code"] == "missing_url")
        code, raw = request(port, "/formats?url=" + quote_url(
            "https://www.tiktok.com/@a/video/1"))
        checks["unsupported_platform_400"] = (
            code == 400 and as_json(raw)["error_code"] == "unsupported_platform")
        checks["no_process_before_gate"] = not os.path.isfile(ARGV_PATH)

        # 2) 未查询过 /formats 时，非默认预设必须被拒绝（不能猜格式）
        code, raw = request(port, "/download?url=" + quote_url(URL)
                            + "&preset=720p")
        checks["preset_requires_formats"] = (
            code == 400
            and as_json(raw)["error_code"] == "format_not_available")
        code, raw = request(port, "/download?url=" + quote_url(URL)
                            + "&preset=" + quote_url("4k"))
        checks["unknown_preset_400"] = (
            code == 400 and as_json(raw)["error_code"] == "invalid_format")
        checks["rejected_preset_created_no_task"] = not runs

        # 3) /formats：真实子进程 + 稳定字段
        code, raw = request(port, "/formats?url=" + quote_url(URL))
        payload = as_json(raw)
        checks["formats_200"] = code == 200
        checks["formats_fields"] = (
            sorted(payload) == ["count", "default_preset", "duration",
                                "extractor", "formats", "platform", "presets",
                                "title", "total", "uploader", "url",
                                "video_id"])
        checks["formats_content"] = (
            payload.get("platform") == "youtube"
            and payload.get("video_id") == "dQw4w9WgXcQ"
            and payload.get("default_preset") == "best"
            and [p["name"] for p in payload.get("presets", [])]
            == preset_names()
            and {f["format_id"] for f in payload.get("formats", [])}
            == {"137", "136", "140"})
        checks["formats_preset_tags"] = (
            payload["formats"][0]["presets"] == ["best", "1080p"]
            and payload["formats"][2]["presets"] == ["audio"])

        # 4) 真实 argv：--dump-single-json、URL 在最后、无 -f
        argv = recorded_argv()
        checks["probe_argv_real_subprocess"] = bool(argv)
        checks["probe_argv_dump_json"] = "--dump-single-json" in argv
        checks["probe_argv_url_last"] = bool(argv) and argv[-1] == URL
        checks["probe_argv_has_no_format"] = "-f" not in argv
        checks["probe_argv_no_shell_chars"] = not any(
            token in ("|", "&", ">", "<") for token in argv)

        # 5) 显式 format_id：必须来自 /formats，且能真正到达 argv
        code, raw = request(port, "/download?url=" + quote_url(URL)
                            + "&format_id=137")
        first_id = as_json(raw).get("task_id", "")
        checks["format_id_from_probe_200"] = code == 200 and bool(first_id)
        checks["format_id_expression"] = (
            srv.scheduler.format_for(first_id) == "137+ba/137/b")
        deadline = time.monotonic() + 20
        while srv.scheduler.active_count() and time.monotonic() < deadline:
            time.sleep(0.02)
        checks["download_argv_real"] = bool(runs)
        if runs:
            command = runs[0].command
            checks["download_argv_format"] = (
                command[command.index("-f") + 1] == "137+ba/137/b")
            checks["download_argv_url_last"] = command[-1] == URL
            checks["download_argv_merge_mp4"] = (
                "--merge-output-format" in command
                and command[command.index("--merge-output-format") + 1] == "mp4")

        # 6) 预设选择走同一条 argv 路径
        code, raw = request(port, "/download?url=" + quote_url(URL)
                            + "&preset=720p")
        second_id = as_json(raw).get("task_id", "")
        checks["preset_after_formats_200"] = code == 200 and bool(second_id)
        checks["preset_expression"] = bool(second_id) and \
            srv.scheduler.format_for(second_id) == SELECTOR_720
        deadline = time.monotonic() + 20
        while srv.scheduler.active_count() and time.monotonic() < deadline:
            time.sleep(0.02)
        if len(runs) >= 2:
            command = runs[1].command
            checks["preset_argv_format"] = (
                command[command.index("-f") + 1] == SELECTOR_720)

        # 6b) 仅音频必须产出真实 MP3（Stage-012）：argv 走 -x --audio-format mp3
        code, raw = request(port, "/download?url=" + quote_url(URL)
                            + "&preset=audio")
        audio_id = as_json(raw).get("task_id", "")
        checks["audio_preset_200"] = code == 200 and bool(audio_id)
        checks["audio_preset_expression"] = bool(audio_id) and \
            srv.scheduler.format_for(audio_id) == SELECTOR_AUDIO
        checks["audio_preset_format_recorded"] = bool(audio_id) and \
            srv.scheduler.audio_format_for(audio_id) == AUDIO_FORMAT
        deadline = time.monotonic() + 20
        while srv.scheduler.active_count() and time.monotonic() < deadline:
            time.sleep(0.02)
        if len(runs) >= 3:
            command = runs[2].command
            checks["audio_argv_extract"] = "--extract-audio" in command
            checks["audio_argv_format_mp3"] = (
                "--audio-format" in command
                and command[command.index("--audio-format") + 1] == AUDIO_FORMAT)
            checks["audio_argv_quality"] = (
                "--audio-quality" in command
                and command[command.index("--audio-quality") + 1] == "0")
            checks["audio_argv_no_merge"] = \
                "--merge-output-format" not in command
            checks["audio_argv_url_last"] = command[-1] == URL

        # 7) 明确错误：未知 id、注入形状、探测失败
        code, raw = request(port, "/download?url=" + quote_url(URL)
                            + "&format_id=999")
        checks["unknown_format_id_400"] = (
            code == 400
            and as_json(raw)["error_code"] == "format_not_available")
        code, raw = request(port, "/download?url=" + quote_url(URL)
                            + "&format_id=" + quote_url("--exec=calc"))
        checks["injection_format_id_400"] = (
            code == 400 and as_json(raw)["error_code"] == "invalid_format")
        srv.clear_formats_cache()
        srv.YT_DLP = fake_fail
        code, raw = request(port, "/formats?url=" + quote_url(URL))
        checks["probe_failure_502"] = (
            code == 502
            and as_json(raw)["error_code"] == "formats_unavailable")
        checks["failure_not_cached"] = srv.cached_formats(URL) is None

        # 8) 默认路径不变：无 preset/format_id 时仍是冻结策略
        srv.YT_DLP = fake_ok
        code, raw = request(port, "/download?url=" + quote_url(URL))
        third_id = as_json(raw).get("task_id", "")
        checks["default_download_200"] = code == 200 and bool(third_id)
        checks["default_expression"] = bool(third_id) and \
            srv.scheduler.format_for(third_id) == FORMAT_EXPR

        failed = [name for name, ok in checks.items() if not ok]
        result = {
            "base": base, "bind": "127.0.0.1", "port_mode": "ephemeral",
            "fake_ytdlp": fake_ok, "argv_file": ARGV_PATH,
            "probe_argv": argv, "download_argvs": [r.command for r in runs],
            "presets": preset_names(),
            "checks": checks, "failed_checks": failed,
        }
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "probe_formats_result.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        assert not failed, failed
        print("FORMATS PROBE OK checks=%d presets=%s"
              % (len(checks), result["presets"]))
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
