# Stage-003 迁移说明：多任务调度与共享任务列表

> 日期：2026-09-19；`/download` 不再直接起线程，任务入口统一经过 Scheduler。

## 新模块
- `core_scheduler.py`：`Scheduler`（活动槽位上限 3、FIFO 等待队列、一次性
  `_started`/`_finished` 记录、完成回调补位、`wait_idle()` 回归辅助）。
  `submit(url)` 先 `manager.create()` 再原子决定「立即启动 / 入队」。
- `core_listing.py`：`sort_tasks()`（唯一排序契约）+ `build_task_list()`（`/tasks`
  负载 = 有序任务 + 调度器计数）。纯函数，不改任务记录。

## 责任边界
| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| `Scheduler` | 活动槽位、FIFO 队列、启动线程、补位、一次性回调 | 改写 Task 字段、解析 HTTP、渲染 UI |
| `TaskManager` | Task 存储、状态转换、进度回写 | 决定并发数量 |
| `DownloadEngine` | 一次下载，且是 `pending->downloading` 的唯一起点 | 管理队列 |
| `server.py` | 校验、组装、JSON 响应 | 决定是否立即启动 |

## server.py 变化
- `GET /download`：`scheduler.submit(url)` 替代 `manager.create()` + `threading.Thread`。
- `GET /tasks`（新增）：`build_task_list(manager, scheduler)` → 
  `{tasks, active_count, active_limit, queued_count}`。
- `GET /status`、`GET /status?id=`：形状与错误码不变（Stage-002 兼容）。
- `download_video(task_id, url)`：保留为显式兼容入口，内部改走
  `scheduler.admit()`（返回 True=已启动 / False=已排队），不再自行构造引擎。
- 移除 `import threading`（线程只由 Scheduler 创建）；`atexit` 关闭日志句柄
  （消除 unittest 的 ResourceWarning）。
- 启动日志增加 `max active downloads: 3`。

## MediaDock.js v3.0
- 删除单一 `currentTaskId`，改为轮询 `GET /tasks`（单一计时器，1s）。
- 面板摘要：`active/limit · 排队 n`；每行 `data-task-id` 作为 DOM 身份，
  排序变化不会把事件绑到别的任务。
- 状态文本：`🕒 排队中` / `⏳ …% ` / `合并中` / `✅ 完成` / `❌ 失败(error_code)`。
- 布局：默认 20 行可见（超出面板内滚动，不隐藏未完成任务）；已完成折叠
  （展开上限 100，超出明确提示）；窄屏宽度自适应。
- SPA：`yt-navigate-finish` + 3s 保活只重建入口，不清空共享列表（关闭 T015）。
- 未出现任何暂停/继续/取消/删除/重试按钮或文案。

## 测试与证据
| 文件 | 覆盖 |
| --- | --- |
| `tests/test_scheduler.py` | T301–T308：并发上限、FIFO、补位（成功/失败）、重复回调、引擎异常、静默引擎不伪造成功、12 线程并发提交 |
| `tests/test_listing.py` | T309/T310–T312：排序契约、计数一致、`/tasks` 形状、多任务 ID 唯一、`/status` 兼容 |
| `tests/helpers.py` | 可替换引擎工厂（单元测试不启动真实 yt-dlp、不依赖网络） |
| `tests/probe_multi.py` | 真实 HTTP + 真实 Scheduler/Engine 循环 + 脚本化进程：3 并发 / 2 排队 / 失败隔离 / 排序 / 命令策略 |
| `tests/probe_chain.py` | 真实进程 spawn + 非零退出 → `error_code=exit_code` |

## Stage-004 可用边界
- `scheduler.active_ids()` / `queued_ids()` 是暂停/取消的调度基础。
- 控制操作必须扩状态机后再提供 API，不能把 `status` 改成字符串来模拟暂停。
- 等待任务与运行中任务的取消行为需分别定义。

## 已知差异（见 Stage-003.md 第 13 节）
- `probe_multi.py` 用脚本化进程对象替代真实 spawn（Windows `cmd.exe` 会把
  `-f bv*[height<=1080]…` 里的 `<` 当重定向，`.bat` 假 yt-dlp 无法输出进度行）。
  真实 spawn 由 `probe_chain.py` 单独覆盖。
- `core_scheduler` 增加防御性 `scheduler_error` / `scheduler_incomplete`：
  引擎异常或未落到终态时，槽位绝不泄漏，也绝不伪造成功。

## 单实例边界
- `server.py` 的 `MediaDockServer` 关闭端口复用（`allow_reuse_address = False`）。
- 实测：默认 `HTTPServer` 在 Windows 上可以重复绑定已被占用的 `127.0.0.1:8765`
  （socket 实验对已监听端口返回 `BOUND (shared!)`），导致两个进程各持一份内存
  任务表，前端看到任务「时有时无」。关闭后第二个实例以 rc=2 退出并写
  `ERROR: cannot bind 127.0.0.1:8765`。
- 注意：`Scripts\python.exe` 是 venv 启动器，会再拉起子解释器，因此任务管理器里
  出现两个 `python.exe` 属正常现象，不代表双实例；以 `Get-NetTCPConnection
  -LocalPort 8765 -State Listen` 的 OwningProcess 为准。

## 已知限制（交接 Stage-005/006）
- 任务表与等待队列无容量上限；终态任务不会自动清理。
- 服务重启即丢失任务与队列，UI 与文档均不声称恢复。
- `MediaDock-server.log` 无轮转，本阶段结束时已达 1.1 MB。

## 回滚
- 回滚点：Stage-002 已签字基线（42/42 + `docs/stage002-migration.md`）。
- 回滚方式：把 `/download` 恢复为 Stage-002 的 `manager.create()` + 线程入口，
  关掉 `/tasks` 与 JS 列表入口；`core_scheduler.py`/`core_listing.py` 保留供修复。