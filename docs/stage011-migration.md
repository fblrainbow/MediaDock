# Stage-011 迁移说明：单实例启动接管

> 日期：2026-09-20；应用版本 **1.0.0 → 1.0.1**，用户脚本仍 **5.2**，存储 schema 仍 **2**。
> 回滚基线：Stage-010 / `1.0.0`。

---

## 问题

关闭端口复用是 Stage-003 的既定决策（避免两个进程各持一份任务表），代价是：

```text
(mediadock) E:\GitHub\MediaDock>python server.py
[2026-09-20 12:49:59] ERROR: cannot bind 127.0.0.1:8765 ([WinError 10048] ...)
[2026-09-20 12:49:59] 另一个 MediaDock 实例可能已在运行；请先结束它再启动。
```

用户需要手动找到并结束旧实例（例如 `pythonw.exe server.py`，pid 3184）才能启动。

---

## 新模块 `core_instance.py`（纯标准库 + 系统命令）

| 函数 | 作用 |
| --- | --- |
| `PortOwner(pid, name, command_line)` | 端口占用者；`is_mediadock()` 做身份判定 |
| `encode_powershell()` / `powershell_args()` | 用 `-EncodedCommand`（Base64/UTF-16LE）传脚本，避开引号转义 |
| `port_owner_script()` | 一次 PowerShell 往返取回 `pid\|name\|commandline` |
| `parse_port_owner()` | 容错解析；垃圾输入返回 `None` |
| `find_port_owner()` | 查询端口占用者（`runner` 可注入） |
| `kill_process_tree()` | `taskkill /PID <pid> /T /F`（结束进程树） |
| `port_is_free()` / `wait_port_free()` | 试用绑定判断端口是否可用；轮询等待释放 |
| `take_over_port()` | 编排整个接管，返回 `free`/`killed`/`foreign`/`unknown`/`failed` |

身份判定（必须同时满足，否则绝不结束）：

1. 该进程**就是**监听 `host:port` 的那个 PID；
2. 进程名 ∈ `python.exe` / `pythonw.exe`；
3. 命令行把 `server.py` 作为完整路径 token 运行（正则允许绝对路径、`./server.py` 等）。

---

## `server.py` 的改动

```python
def bind_server():            # 返回 (server, error)
def free_port_for_start():    # 调用 take_over_port 并输出结论日志
def main(argv=None):
    ...
    server, error = bind_server()
    if server is None and not restarted:
        ok, _ = free_port_for_start(False)      # 仅在冲突时接管
        if ok:
            server, error = bind_server()       # 重试一次
    if server is None:
        log("ERROR: cannot bind ...")
        log("...可用 `python server.py --restart` 先结束它再启动。")
        sys.exit(2)
```

| 入口 | 1.0.0 | 1.0.1 |
| --- | --- | --- |
| `server.py`（端口空闲） | 启动 | 启动（零副作用） |
| `server.py`（被旧实例占用） | 退出码 2 | 自动接管后启动 |
| `server.py`（被其他程序占用） | 退出码 2 | 退出码 2 + 打印占用者 PID/进程名/命令行 |
| `server.py --restart` | 未知选项 | 启动前主动接管 |
| `server.py --check-config` | 诊断 | 诊断（不触发接管） |
| `server.py <未知选项>` | 被忽略 | `unknown option: X` + 退出码 2 |

---

## 证据

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 接管决策单元测试 | `python -m unittest tests.test_instance` | `Ran 20 tests ... OK` |
| 真实接管端到端 | `python tests\probe_restart.py` | `RESTART PROBE OK checks=14 passed=14` |
| 全量回归 | `python -m unittest discover -s tests -t .` | `ran=355 fail=0 err=0` |
| 全部探针 | 9 个 `tests/probe_*.py` | 全部 rc=0 |
| 发布门禁 | `python tests\release_check.py` | `checks=60 passed=60 failed=0` |

`tests/probe_restart.py` 的检查覆盖：首次启动服务可用 → `--restart` 启动第二个实例 →
旧实例已退出 → 端口所有者换人 → `/health.version` 有值 → `/tasks` 可用 →
第二个实例被结束后，`--restart` 在空闲端口上仍能正常启动。

---

## 注意事项与已知边界

- **接管会中断旧实例正在运行的任务**：它们在下一次启动时变为
  `error` + `error_code=interrupted`（Stage-005 语义不变），需要在页面点「重试」。
- **不接管非 MediaDock 进程**：端口被别的程序占用时只报告，不杀。
- **依赖系统 PowerShell**：`Get-NetTCPConnection` / `Get-CimInstance` 不可用时返回
  `unknown`，不猜测、不杀进程，退化为退出码 2。
- **探针不会被用户实例干扰**：`probe_restart.py` 使用临时端口/数据库/下载目录，
  只结束自己启动的进程；用户正在使用的 8765 实例不受影响。
- **venv 的 `python.exe` 是 shim**：`Popen.pid` 不是服务 PID（真实服务是它的子进程），
  因此判定一律以「端口所有者的 PID」为准；`taskkill /T` 保证连子进程一起结束。

---

## 回滚

1. 代码：删除 `core_instance.py` 与 `main()` 中的 `--restart`/接管分支
   → 恢复「绑定失败即退出码 2」（Stage-003 行为）。
2. 测试：删除 `tests/test_instance.py`、`tests/probe_restart.py` 与
   `tests/probe_restart_result.json`。
3. 版本：`core_release.APP_VERSION` 回退 `1.0.0`，`docs/release-notes.md` 去掉 1.0.1 条目
   （发布门禁会校验版本一致性）。
4. 数据：无 schema 变更、无迁移，无需数据回滚。
