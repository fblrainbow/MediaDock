"""Structural sanity check for MediaDock.js (stdlib only, no Node.js needed).

Stage-003 introduced a shared task-list userscript but this Windows box has
no Node.js, so the userscript cannot be parsed or linted by a real JS tool.
This script covers what a hand-edit can realistically break:

  * brace/paren/bracket balance, ignoring comments and string literals
  * no template literals (backticks) sneaking in
  * the Stage-003 contract anchors still exist
  * no leftover single-task state or unimplemented control verbs

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
        "@version      3.0",
        "'/tasks'",
        "'/download?url='",
        "data-task-id",
        "MAX_VISIBLE_ROWS = 20",
        "mediadock-completed-toggle",
        "yt-navigate-finish",
        "正在提交",
        "已加入任务列表",
        "排队中",
        "合并中",
    ]
    missing = [needle for needle in required if needle not in src]
    if missing:
        raise SystemExit(f"FAIL: missing anchors {missing}")

    # 注释里可以提到"不再依赖 currentTaskId"；只检查真实代码
    no_comments = re.sub(r"//[^\n]*", "", src)
    forbidden = ["currentTaskId", "_update(", "srv."]
    present = [needle for needle in forbidden if needle in no_comments]
    if present:
        raise SystemExit(f"FAIL: forbidden leftovers in code {present}")
    control = ["取消", "暂停", "重试", "删除"]
    bad_control = [word for word in control if word in no_comments]
    if bad_control:
        raise SystemExit(f"FAIL: unimplemented control verbs {bad_control}")

    print(f"JS STRUCTURE OK ({len(src.splitlines())} lines, "
          f"{len(code)} code chars, no backticks, anchors present)")
    return 0


if __name__ == "__main__":
    sys.exit(main())