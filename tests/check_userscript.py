"""Structural sanity check for MediaDock.js (stdlib only, no Node.js needed).

Stage-003 introduced a shared task-list userscript but this Windows box has
no Node.js, so the userscript cannot be parsed or linted by a real JS tool.
This script covers what a hand-edit can realistically break:

  * brace/paren/bracket balance, ignoring comments and string literals
  * no template literals (backticks) sneaking in
  * the Stage-003/004 contract anchors still exist
  * control requests use POST + JSON and render from server state only

Usage (project venv):
  C:\\Users\\Administrator\\Envs\\mediadock\\Scripts\\python.exe tests\\check_userscript.py
Exit code 0 = structure OK, 1 = problem found.
"""
import re
import sys
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "MediaDock.js"
def strip_code(src):
    """Return source with comments and string bodies removed."""
    out = []
    i, n = 0, len(src)
    state = "code"
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block"
                i += 2
                continue
            if ch in "'\"":
                state = ch
                i += 1
                continue
            out.append(ch)
            i += 1
        elif state == "line":
            if ch == "\n":
                state = "code"
                out.append("\n")
            i += 1
        elif state == "block":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
            i += 1
        else:  # inside a string literal
            if ch == "\\":
                i += 2
                continue
            if ch == state:
                state = "code"
            i += 1
    if state not in ("code", "line"):
        raise SystemExit(f"FAIL: unterminated {state}")
    return "".join(out)


def main():
    src = P.read_text(encoding="utf-8")
    if "`" in src:
        raise SystemExit("FAIL: template literal backtick found")
    code = strip_code(src)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for idx, ch in enumerate(code):
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                raise SystemExit(f"FAIL: unbalanced {ch!r} at offset {idx}")
    if stack:
        raise SystemExit(f"FAIL: unclosed {stack}")

    required = [
        "@version      5.2",
        "'/tasks'",
        "'/download?url='",
        "'/formats?url='",
        "'/pause'",
        "'/resume'",
        "'/cancel'",
        "'/retry'",
        "'/delete'",
        "data-task-id",
        "data-control",
        "MAX_VISIBLE_ROWS = 20",
        "mediadock-completed-toggle",
        "mediadock-preset",
        "PRESET_OPTIONS",
        "DEFAULT_PRESET",
        "仅音频",
        "yt-navigate-finish",
        "正在提交",
        "已加入任务列表",
        "排队中",
        "处理中",
        "暂停",
        "继续",
        "取消",
        "重试",
        "删除",
        "已暂停",
        "已取消",
        "ERROR_HINTS",
        # Stage-010：版本一致性与服务端版本展示锚点
        "USERSCRIPT_VERSION = '5.2'",
        "HEALTH_PATH = '/health'",
        "versionWarning",
        "serverVersion",
    ]
    missing = [needle for needle in required if needle not in src]
    if missing:
        raise SystemExit(f"FAIL: missing anchors {missing}")

    # Stage-010：JS 声称的版本必须与 @version 头一致，避免发布期版本漂移
    header = re.search(r"^//\s*@version\s+(\S+)\s*$", src, re.M)
    declared = re.search(r"USERSCRIPT_VERSION = '([^']+)'", src)
    if not header or not declared:
        raise SystemExit("FAIL: userscript version anchors missing")
    if header.group(1) != declared.group(1):
        raise SystemExit(
            f"FAIL: @version {header.group(1)} != USERSCRIPT_VERSION "
            f"{declared.group(1)}")

    # 注释里可以提到"不再依赖 currentTaskId"；只检查真实代码
    no_comments = re.sub(r"//[^\n]*", "", src)
    forbidden = ["currentTaskId", "_update(", "srv."]
    present = [needle for needle in forbidden if needle in no_comments]
    if present:
        raise SystemExit(f"FAIL: forbidden leftovers in code {present}")

    # Stage-008：前端只发送 preset 名字，绝不拼接 yt-dlp 选择器
    selectors = ["bv*", "bestaudio", "+ba/", "height<="]
    leaked = [needle for needle in selectors if needle in no_comments]
    if leaked:
        raise SystemExit(f"FAIL: yt-dlp selector built in JS {leaked}")

    # Stage-004：控制请求必须走 POST + JSON，而不是 GET 查询串
    if "method: 'POST'" not in no_comments:
        raise SystemExit("FAIL: control requests must use POST")
    if "JSON.stringify({ task_id: taskId })" not in no_comments:
        raise SystemExit("FAIL: control body must be {task_id}")
    # 按钮可见性必须由服务端 status 推导，不能出现本地状态机
    if "function controlsFor(status)" not in no_comments:
        raise SystemExit("FAIL: controlsFor(status) must exist")

    print(f"JS STRUCTURE OK ({len(src.splitlines())} lines, "
          f"{len(code)} code chars, no backticks, anchors present)")
    return 0


if __name__ == "__main__":
    sys.exit(main())