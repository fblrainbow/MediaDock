# Stage-004 迁移说明：暂停、继续、取消与断点

> 日期：2026-09-19；任务从「只能开始/结束」升级为「可在运行中被真实控制」。
> 回滚点：Stage-003 已签字基线（69/69 单元测试 + `docs/stage003-migration.md`）。

## 新模块（纯 stdlib）

- `core_control.py`
  - `TaskControl`：单任务控制上下文（暂停标志、取消标志、进程句柄、本次运行
    产物清单）。`request_pause()` / `request_cancel()` 幂等；`attach_process()`
    在已有控制意图时立即终止进程；`clear_pause()` 供「继续」开启新一轮运行。
  - `terminate_tree(process)`：Windows 用 `taskkill /F /T /PID` 结束整棵进程树
    （yt-dlp 会派生 FFmpeg，只杀父进程会留下孤儿进程并锁住输出文件）；其他平台
    回退 `terminate()` → `kill()`。已结束的进程不报错。
  - `ControlRegistry`：`task_id -> TaskControl`，线程安全，由 Scheduler 持有。
  - `ControlError(code, message, http_status, task_id)`：控制类业务错误的统一载体。
- `core_files.py`
  - `video_id_from_url()`：`watch?v=` / `shorts/` / `youtu.be/`。
  - `is_inside(root, path)`：`realpath` + `commonpath` 的真实路径边界校验。
  - `started_epoch()`：把 `started_at` 转 epoch，容忍空值/非法值。
  - `discover_task_files()` / `cleanup_task_files()`：合并「yt-dlp 输出记录的
    目标路径」与「目录扫描 `[video_id]` 文件」，并按 mtime 过滤本次运行的文件。
    `cleanup_task_files(since_epoch<=0)` 一律拒绝删除（无法区分旧成果）。

## 状态机（Stage-004.md 5.1）

| 当前状态 | 允许的目标状态 |
| --- | --- |
| `pending` | `downloading`、`cancelled`、`error` |
| `downloading` | `downloading`、`paused`、`completed`、`cancelled`、`error` |
| `paused` | `downloading`、`cancelled`、`error` |
| `completed` | 无（仍是终态） |
| `error` | `pending`、`downloading` |
| `cancelled` | `pending`、`downloading` |

- `Task` 字段未新增；`TASK_STATUSES`、`TERMINAL_STATUSES`、`SETTLED_STATUSES`、
  `NO_LIVE_PROGRESS_STATUSES` 在 `core_task.py` 中定义。
- `core_manager.RETRY_RESET_FIELDS` 定义重试要清空的字段，Scheduler 重试时使用。
- `_check_transition()` 现在也拒绝未知状态字符串。

## 责任边界

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| `TaskControl` | 控制意图、进程句柄、产物清单、进程树终止 | 状态转换、HTTP |
| `ControlRegistry` | 控制上下文注册/注销 | 队列与槽位 |
| `core_files` | 路径边界、文件发现与删除 | 状态与队列 |
| `TaskManager` | 唯一状态机与字段写入口 | 进程控制、文件删除 |
| `Scheduler` | 槽位、FIFO 队列、控制编排、补位、一次性记录重置 | 直接写 Task 字段 |
| `DownloadEngine` | 一次下载；按控制意图落到 `paused`/`cancelled`/终态 | 管理队列 |
| `Handler` | POST 路由、JSON 校验、错误码 | 直接操作进程或队列 |

## 引擎行为

- `DownloadEngine.run(task_id, url, control=None)`：第三个参数可选，旧两参调用
  仍然可用（内部自建控制上下文）。
- 输出读取循环每次迭代先检查控制标志；命中就终止进程树并跳出。
- 收尾优先级：**取消 > 暂停 > returncode**，绝不把被终止的进程判为成功。
- `core_parse` 新增路径事件：
  `[download] Destination:` 与 `has already been downloaded` → `destination`；
  `[Merger] Merging formats into "..."` → `merged`（同时上报 99% 合并进度）。
- `_finish_paused()` 只改状态；`_finish_cancelled()` 改状态后调用 `cleanup()`。
- `cleanup()` 在 `started_at` 为空时跳过删除，避免误删同一视频的旧成果。

## Scheduler 控制编排

| 方法 | 行为 |
| --- | --- |
| `pause(task_id)` | 只接受 `downloading` 且存在控制上下文的任务；请求终止并等待 `paused`；释放槽位并补位 |
| `resume(task_id)` | 只接受 `paused`；`clear_pause()` + `_reset_marks()` + `admit()`；无空闲槽位时保持 `paused` 并进入队列（`queued=True`） |
| `cancel(task_id)` | `downloading` 请求取消；`pending`（队列）与 `paused` 直接转 `cancelled`；都执行文件清理；完成后注销控制上下文 |
| `retry(task_id)` | 只接受 `error`/`cancelled`；注销控制上下文、重置一次性记录、`pending` + 清字段、重新 `admit()` |
| `wait_for_status()` | 有界轮询（`CONTROL_TIMEOUT=10s`），超时返回最后观察到的状态 |

- `_settle()` 把 `paused`/`cancelled` 视为可接受收尾状态，不再误标
  `scheduler_incomplete`。
- 队列不变量更新为「队列中的任务状态只能是 `pending` 或 `paused`」。
- 控制上下文在任务取消/重试时注销，其余情况保留（暂停→继续需要产物清单）。
  已知限制：终态任务的控制上下文会保留到进程退出，容量清理留给 Stage-005。

## API

| 方法 | 路径 | 请求体 | 成功响应 |
| --- | --- | --- | --- |
| POST | `/pause` | `{"task_id": "..."}` | `200 {task_id, status:"paused", changed}` |
| POST | `/resume` | `{"task_id": "..."}` | `200 {task_id, status:"downloading"|"paused", changed[, queued]}` |
| POST | `/cancel` | `{"task_id": "..."}` | `200 {task_id, status:"cancelled", changed}` |
| POST | `/retry` | `{"task_id": "..."}` | `200 {task_id, status:"downloading"|"pending", changed}` |

错误码：`bad_request`(400)、`missing_task_id`(400)、`invalid_task_id`(400)、
`task_not_found`(404)、`not_pausable`/`not_resumable`/`not_cancellable`/
`not_retryable`/`control_timeout`(409)、`control_failed`(500)、`not_found`(404)。

- `task_id` 只接受 `[A-Za-z0-9_-]{1,64}`，拒绝任何路径/注入字符。
- `OPTIONS` 允许方法改为 `GET, POST, OPTIONS`。
- GET 接口形状与错误码保持不变（Stage-002/003 兼容）。

## MediaDock.js v4.0

- `@version` 3.0 → 4.0；新增 `CONTROL_PATHS` 常量（`/pause` `/resume` `/cancel` `/retry`）。
- 每行按服务端状态渲染控制按钮：
  `downloading` → 暂停/取消；`paused` → 继续/取消；`pending` → 取消；
  `error`/`cancelled` → 重试；`completed` → 无。
- 控制请求走 `POST` + JSON body `{task_id}`，成功后立即刷新 `/tasks`；
  失败把 `error_code` 显示在面板摘要，不在前端推断状态。
- 状态文案新增 `⏸ 已暂停`、`🚫 已取消`；`data-task-id` 仍是行身份。

## 文件策略（D-009）

| 场景 | `.part`/`.ytdl`/中间文件 | 本次输出 |
| --- | --- | --- |
| 暂停 | 保留 | 保留 |
| 继续 | 保留（yt-dlp `--continue` 复用） | 保留 |
| 取消 | 删除 | 删除（仅本任务 id 且 mtime 不早于 `started_at`） |
| 完成 / 重试 | 保留 | 保留 |

## 测试与证据

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_manager.py` | T401-T405：`paused`/`cancelled`/重试状态机、终态拒绝、未知状态拒绝 |
| `tests/test_control.py` | T406/T407：控制意图幂等、注册表并发、真实父子进程树终止（Windows） |
| `tests/test_files.py` | T410/T411：`video_id`、路径边界、mtime 窗口、越界拒绝、幂等清理 |
| `tests/test_engine.py` | T408/T409：暂停保留 `.part`、取消删除本次文件、无 `started_at` 不清理 |
| `tests/test_control_api.py` | T412-T419：真实 HTTP 控制成功/失败路径、补位、隔离、GET 回归 |
| `tests/test_listing.py` | T422：`paused`/`cancelled` 按 0% 参与未完成排序 |
| `tests/probe_control.py` | 真实 HTTP + 真实 Scheduler/Engine 链路：暂停保留断点、继续复用断点、取消清理、队列取消、控制隔离 |
| `tests/check_userscript.py` | 结构 + 控制锚点 + POST/JSON 断言（rc=0） |
| `tests/probe_multi.py` / `probe_chain.py` | Stage-003 回归，仍通过 |

真实进程终止证据来自 `tests/test_control.py`（真实 `python -c` 父进程 + 子进程 +
`taskkill /F /T`）；`probe_control.py` 与 `probe_multi.py` 一样用脚本化进程对象
替代真实 spawn，理由见 Stage-003.md 第 13 节（Windows `cmd.exe` 会把 `-f` 表达式
里的 `<` 当成重定向，`.bat` 假 yt-dlp 无法输出进度行）。

## 已知限制（交接 Stage-005/006）

- 终态任务记录不删除；控制上下文按任务保留，无容量上限（D-017 的记录删除与
  历史清理属于 Stage-005）。
- 服务重启不恢复任务、暂停状态、队列或子进程。
- `MediaDock-server.log` 无轮转（Stage-006）。
- POST 接口未做来源校验/CSRF（仅监听 `127.0.0.1` + `task_id` 白名单字符），
  完整来源校验属于 Stage-006。

## 回滚

1. 保留 Stage-003 基线：`git checkout <stage-003-commit> -- server.py MediaDock.js`。
2. 关闭 4 个控制接口（`do_POST` 直接返回 `not_found`）并回退 `MediaDock.js` 到
   3.0，即可恢复「只能开始/结束」的 Stage-003 行为；`paused`/`cancelled` 状态定义
   与 `core_control.py`/`core_files.py` 可保留供修复。
3. 状态机回滚只需把 `core_manager._ALLOWED` 恢复为 Stage-003 的四个状态；
   `Task` 字段从未新增，因此不涉及数据迁移。
