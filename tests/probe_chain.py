"""Stage-001 live-chain probe (stdlib only, no new deps).

Starts a real server.Handler on 127.0.0.1:<ephemeral port> and creates
one task whose yt-dlp is a fake executable that exits nonzero. This
validates the observable failure path with a real process spawn:

  GET /download?url=https://... -> 200 {task_id}
  GET /status?id=...            -> error (error_code=exit_code)
  GET /status                   -> contains the task

Known limitation (recorded in Stage-003.md as a difference): the fake
tool has to be a `.bat` on Windows, and cmd.exe re-parses the frozen
`-f bv*[height<=1080]...` argument as a redirection, so the fake never
prints a progress line. Progress parsing on the live chain is therefore
covered by tests\\probe_multi.py instead; this probe only proves the
spawn -> nonzero exit -> Task error chain.

Usage (project venv):
  C:\\Users\\Administrator\\Envs\\mediadock\\Scripts\\python.exe tests\\probe_chain.py
Writes tests\\probe_chain_result.json on success.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


def make_fake_ytdlp():
    tmp = tempfile.mkdtemp(prefix="mediadock-fake-ytdlp-")
    if os.name == "nt":
        # .bat 内容实际不会执行：命令行里 -f 表达式含 "<"，cmd.exe 会把它
        # 当重定向，进程直接以 rc=1 结束——正是本探针要验证的失败链路。
        # 真正的进度解析链路由 tests\probe_multi.py 覆盖。
        path = os.path.join(tmp, "yt-dlp.bat")
        # newline="" 防止 Python 把已写入的 \r\n 再翻译成 \r\r\n
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("@echo [download]   1.0% of ~ 1.00MiB "
                    "at 1.00KiB/s ETA 00:01\r\n")
            f.write("@echo simulated failure output\r\n")
            f.write("@exit /b 1\r\n")
    else:
        path = os.path.join(tmp, "yt-dlp")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho '[download] 1.0% of ~ 1.00MiB "
                    "at 1.00KiB/s ETA 00:01'\necho simulated failure\nexit 1\n")
        os.chmod(path, 0o755)
    return path


def http_get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    fake = make_fake_ytdlp()
    old = srv.YT_DLP
    srv.YT_DLP = fake
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        url = urllib.parse.quote("https://www.youtube.com/watch?v=BASELINE-PROBE",
                                 safe="")
        code, body = http_get(base, f"/download?url={url}")
        assert code == 200, (code, body)
        task_id = json.loads(body)["task_id"]
        assert task_id, body
        final = None
        for _ in range(100):
            time.sleep(0.1)
            c2, b2 = http_get(base, "/status?id=" + task_id)
            assert c2 == 200, (c2, b2)
            final = json.loads(b2)
            if final.get("status") in ("completed", "error"):
                break
        assert final is not None, "no status observed"
        assert final.get("status") == "error", final
        assert final.get("error_code") == "exit_code", final
        c3, b3 = http_get(base, "/status")
        assert c3 == 200, (c3, b3)
        assert task_id in json.loads(b3), b3[:500]
        out = {"base": base, "task_id": task_id, "final": final,
               "fake_ytdlp": fake, "port_mode": "ephemeral",
               "bind": "127.0.0.1"}
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "probe_chain_result.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"CHAIN PROBE OK task_id={task_id} status=error")
        print(f"wrote {p}")
    finally:
        srv.YT_DLP = old
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
