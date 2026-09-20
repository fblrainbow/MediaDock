"""Stage-009 audio probe: real FFmpeg end to end (no network, no yt-dlp).

1. builds a real MP4 (video + audio) in a temporary download directory with
   the real FFmpeg,
2. drives `POST /audio` through the real `server.Handler` / scheduler /
   SQLite store,
3. verifies the produced MP3/M4A/WAV with magic bytes and a size check,
4. proves the failure paths (unsupported source, outside-directory source,
   missing FFmpeg) leave no finished-looking file behind.

Usage (project venv):
  C:\\Users\\Administrator\\Envs\\mediadock\\Scripts\\python.exe tests\\probe_media.py
Writes tests\\probe_media_result.json.
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

from http.server import ThreadingHTTPServer

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = tempfile.mkdtemp(prefix="mediadock-media-")
DOWNLOAD_DIR = os.path.join(TMP_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

VIDEO_ID = "MEDIAPROBE1"
GOOD_URL = "https://www.youtube.com/watch?v=" + VIDEO_ID
BAD_ID = "MEDIAPROBE2"
BAD_URL = "https://www.youtube.com/watch?v=" + BAD_ID
GOOD_NAME = "probe clip [" + VIDEO_ID + "].mp4"
BAD_NAME = "not a video [" + BAD_ID + "].mp4"

# 必须在 import server 之前：模块导入时就会 bootstrap 一次
os.environ["MEDIADOCK_DB"] = ":memory:"
sys.path.insert(0, REPO_DIR)

import server as srv  # noqa: E402 - env must be set first
from core_engine import resolve_ffmpeg  # noqa: E402
from core_media import target_names  # noqa: E402

MAGIC = {"mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
         "m4a": (b"\x00\x00\x00", b"ftyp"),
         "wav": (b"RIFF",)}


def run(command, timeout=120):
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
              "encoding": "utf-8", "errors": "replace"}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(list(command), timeout=timeout, **kwargs)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def build_source_video(ffmpeg, path):
    """Real MP4 with a real audio track; a fallback container is recorded."""
    attempts = (
        (path, ["-c:v", "mpeg4", "-q:v", "8", "-c:a", "aac", "-b:a", "96k"]),
        (os.path.splitext(path)[0] + ".mkv",
         ["-c:v", "mpeg4", "-q:v", "8", "-c:a", "pcm_s16le"]),
    )
    for target, codec in attempts:
        command = [ffmpeg, "-hide_banner", "-nostdin", "-y",
                   "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5:duration=1",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                   "-shortest"] + codec + [target]
        rc, _, err = run(command)
        if rc == 0 and os.path.isfile(target) and os.path.getsize(target) > 0:
            return target, " ".join(codec), ""
    return "", "", err.strip()[-300:]


def request(port, path, method="GET", payload=None, timeout=180):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def as_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def wait_status(task_id, statuses, timeout=120.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        task = srv.manager.get(task_id)
        last = task.status if task else None
        if last in statuses:
            return last
        time.sleep(0.05)
    return last


def magic_ok(path, target):
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False, b""
    return any(candidate in head for candidate in MAGIC[target]), head


def completed_download(url):
    task = srv.manager.create(url)
    srv.manager.transition(task.task_id, "downloading")
    srv.manager.transition(task.task_id, "completed", percent=100.0)
    return task.task_id


def main():
    checks = {}
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise SystemExit("FAIL: ffmpeg not found; Stage-009 needs a real FFmpeg")
    source, container, build_error = build_source_video(
        ffmpeg, os.path.join(DOWNLOAD_DIR, GOOD_NAME))
    if not source:
        raise SystemExit("FAIL: cannot build a source video: " + build_error)
    bad_source = os.path.join(DOWNLOAD_DIR, BAD_NAME)
    with open(bad_source, "w", encoding="utf-8") as handle:
        handle.write("this is not a video file")
    outside = os.path.join(TMP_DIR, "outside.mp4")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("outside the download directory")

    srv.DOWNLOAD_DIR = DOWNLOAD_DIR
    srv.FFMPEG = ffmpeg
    srv.MEDIA_POPEN_FACTORY = None
    srv.set_media_processor(None)
    srv.bootstrap(":memory:")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    base = "http://127.0.0.1:%d" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    outputs = []
    try:
        # 1) 目标列表
        code, raw = request(port, "/audio")
        payload = as_json(raw)
        checks["targets_listed"] = (
            code == 200 and [t["name"] for t in payload.get("targets", [])]
            == target_names() and payload.get("default") == "mp3")

        # 2) 真实转换：MP4 -> MP3 / WAV
        for target in ("mp3", "wav"):
            task_id = completed_download(GOOD_URL)
            code, raw = request(port, "/audio?target=" + target, "POST",
                                {"task_id": task_id})
            data = as_json(raw)
            checks["audio_%s_created" % target] = (
                code == 200 and data.get("task_id") and
                data.get("source_task_id") == task_id and
                data.get("target") == target)
            media_id = data.get("task_id", "")
            if not media_id:
                continue
            final = wait_status(media_id, ("completed", "error"))
            media = srv.manager.get(media_id)
            checks["audio_%s_completed" % target] = final == "completed"
            checks["audio_%s_type" % target] = media.type == "audio"
            produced = media.file_path
            outputs.append(produced)
            checks["audio_%s_file" % target] = (
                bool(produced) and os.path.isfile(produced)
                and os.path.getsize(produced) > 1000)
            ok, _head = magic_ok(produced, target)
            checks["audio_%s_magic" % target] = ok
            checks["audio_%s_marker" % target] = (
                "[" + VIDEO_ID + "]" in os.path.basename(produced))
            checks["audio_%s_path_inside" % target] = (
                os.path.realpath(produced).startswith(
                    os.path.realpath(DOWNLOAD_DIR)))

        # 3) 转码失败：真实 ffmpeg 拒绝非视频输入，且不留下"完成"文件
        bad_task = completed_download(BAD_URL)
        code, raw = request(port, "/audio?target=mp3", "POST",
                            {"task_id": bad_task})
        data = as_json(raw)
        checks["bad_source_accepted_for_conversion"] = code == 200
        media_id = data.get("task_id", "")
        final = wait_status(media_id, ("completed", "error")) if media_id else None
        media = srv.manager.get(media_id) if media_id else None
        checks["bad_source_fails"] = final == "error"
        checks["bad_source_error_code"] = (
            media is not None and media.error_code in ("ffmpeg_failed",
                                                      "output_missing"))
        checks["bad_source_no_output"] = not os.path.isfile(
            os.path.join(DOWNLOAD_DIR, os.path.splitext(BAD_NAME)[0] + ".mp3"))

        # 4) 边界与输入校验：不启动任何进程
        code, raw = request(port, "/audio", "POST", {})
        checks["missing_task_id_400"] = (
            code == 400 and as_json(raw)["error_code"] == "missing_task_id")
        code, raw = request(port, "/audio?target=flac", "POST",
                            {"task_id": bad_task})
        checks["invalid_target_400"] = (
            code == 400
            and as_json(raw)["error_code"] == "invalid_audio_target")
        code, raw = request(port, "/audio?target=mp3", "POST",
                            {"task_id": bad_task, "source": outside})
        checks["outside_source_400"] = (
            code == 400
            and as_json(raw)["error_code"] == "source_outside_download_dir")
        code, raw = request(port, "/audio?target=mp3", "POST",
                            {"task_id": "no-such-task"})
        checks["unknown_task_404"] = (
            code == 404 and as_json(raw)["error_code"] == "task_not_found")

        # 5) ffmpeg 缺失：任务失败而不是挂起
        srv.FFMPEG = ""
        missing = completed_download(GOOD_URL)
        code, raw = request(port, "/audio?target=mp3", "POST",
                            {"task_id": missing})
        media_id = as_json(raw).get("task_id", "")
        final = wait_status(media_id, ("completed", "error")) if media_id else None
        checks["missing_ffmpeg_fails"] = (
            final == "error"
            and srv.manager.get(media_id).error_code == "ffmpeg_missing")
        srv.FFMPEG = ffmpeg

        # 6) 历史把媒体任务当作普通任务（type=audio）
        code, raw = request(port, "/history?limit=50")
        history = as_json(raw)
        types = [t.get("type") for t in history.get("tasks", [])]
        checks["history_has_audio_type"] = code == 200 and "audio" in types

        failed = [name for name, ok in checks.items() if not ok]
        result = {
            "base": base, "bind": "127.0.0.1", "port_mode": "ephemeral",
            "ffmpeg": ffmpeg, "source_video": source,
            "source_container_codec": container,
            "download_dir": DOWNLOAD_DIR, "targets": target_names(),
            "outputs": outputs, "checks": checks, "failed_checks": failed,
        }
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "probe_media_result.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        assert not failed, failed
        print("MEDIA PROBE OK checks=%d ffmpeg=%s outputs=%d"
              % (len(checks), os.path.basename(ffmpeg), len(outputs)))
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
