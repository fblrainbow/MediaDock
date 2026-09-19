# Stage-005 迁移说明：持久化与任务历史

> 日期：2026-09-19；任务记录从「进程内存」升级为「SQLite + 内存缓存」。
> 回滚点：Stage-004 已签字基线（128/128 单元测试 + 控制探针）。

## 新模块（纯标准库 `sqlite3`）

- `core_store.py`
  - `Store`：单连接 + RLock 封装。`initialize()` 按版本应用迁移（当前 v2），
    `backup()` 复制 `<db>.bak`，`save_task()`/`load_tasks()`/`count_tasks()`，
    `record_event()`/`events()`/`has_events()`，`delete_task(record=True)`、
    `purge_terminal(keep=200)`、`info()`。
  - `MIGRATIONS = [(1, ...), (2, ...)]`：v1 建 `tasks`/`task_events`/`schema_version`
    与索引，v2 增加 `idx_tasks_created_at`。语句全部 `IF NOT EXISTS`，因此幂等。
  - `TaskPersister`：交给 `TaskManager` 的适配器，吞掉并记录所有异常。
  - `open_store()`：打开 + 迁移；失败返回 `(None, reason)`，服务降级为内存模式。
  - `restore_backup()`：把 `<db>.bak` 复制回 `<db>`（回滚步骤）。
  - `resolve_db_path()`：`显式参数 > MEDIADOCK_DB > <仓库>/tasks.db`。
- 运行时数据库文件默认在仓库根目录（`tasks.db`），已在 `.gitignore` 排除。

## 数据模型

`tasks` 表：一列一个 Task 字段（`task_id` 主键 + 其余 16 列），`completion_order`
为 INTEGER NULL。`task_events` 表：`id/task_id/kind/detail/created_at`，
`kind ∈ {created, transition, restart_interrupted, deleted, purged 预留}`。
`schema_version` 表：单行版本号。

## 写回路径

| 触发 | 行为 |
| --- | --- |
| `TaskManager.create()` | 立即写回 + `created` 事件 |
| `TaskManager.transition()` | 立即写回 + `transition` 事件（`from->to`） |
| `report_progress()` / `report_merging()` | 按 `PROGRESS_PERSIST_SECONDS=1s` 节流写回 |
| `report_title()` | 立即写回 |
| `drop()` | 删除行 + 事件，并写 `deleted` 审计事件 |
| `purge_terminal()` | 只保留最新 N 条终态记录，删除时不写审计事件（避免事件表跟着膨胀） |

- 所有 persister 调用都包在 try/except 中：数据库故障不会影响下载、控制或 HTTP 响应。
- 状态仍然只由 `TaskManager` 写；store 只接收快照。

## 启动装配与重启矩阵

`server.bootstrap(db_path=None)`：关闭旧 store → 打开/迁移 → 加载任务 → 应用重启矩阵
→ 重建 `Scheduler` → 安装 persister → `purge_terminal(200)`。`main()` 与 `Handler`
共用同一批全局对象；测试/探针可以再次调用 `bootstrap()` 模拟重启。

| 关机时状态 | 重启后 | 处理 |
| --- | --- | --- |
| `downloading` | `error` | `error_code=interrupted`，`completed_at` 补写，`restart_interrupted` 事件 |
| `pending` | `error` | 同上（队列是内存结构，重启不保留） |
| `paused` | `paused` | 保持断点状态，不自动续传，由用户手动继续 |
| `completed`/`error`/`cancelled` | 不变 | 作为历史记录保留 |

- `completion_order` 恢复后同步推进内部序号，保证后续完成序号不冲突。
- 重启不创建等待队列、不自动 `admit()`：不存在「显示 downloading 但没有进程」的任务。

## API 变化

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/history?limit=20&status=completed` | 终态任务按 `completed_at`/`completion_order`/`task_id` 倒序；`limit` 1..200 |
| GET | `/events?id=...&limit=50` | 事件升序返回；任务不存在且无事件 → 404 |
| POST | `/delete` | 仅终态可删；非终态 409 `not_deletable` |
| GET | `/health` | 增加 `storage` |
| GET | `/tasks` | 增加 `storage` |

新增错误码：`invalid_limit`(400)、`invalid_status`(400)、`not_deletable`(409)。
`storage = {kind: "sqlite"|"memory", db, schema_version, degraded, reason}`。
既有 `/health`、`/download`、`/status`、`/tasks`、4 个控制 POST 形状与错误码不变。

## MediaDock.js v5.0

- `@version` 4.0 → 5.0；`CONTROL_PATHS` 增加 `delete: '/delete'`。
- 终态行按钮：`error`/`cancelled` → 重试 + 删除；`completed` → 删除。
- `ERROR_HINTS`：`interrupted` 显示为「服务重启中断」，仍保留服务端原始 `error_code`。
- 面板不展示存储状态（Stage-005 任务 006 标记为可选）：失败时容易误导，运行时状态
  通过 `/health` 查看。

## 测试与证据

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_store.py` | T501-T510、T522：迁移幂等、v1→v2 备份、迁移失败回滚、字段往返、upsert、写失败吞掉、事件顺序、备份/还原、删除、容量清理 |
| `tests/test_manager.py` | T511-T513：`create/transition/drop` 写回、进度节流、persister 异常不影响状态、`load_task` 与完成序号恢复 |
| `tests/test_restart.py` | T514-T518：完成/失败/取消记录跨重启、`downloading`/`pending`→`interrupted`、`paused` 保持、排序一致、损坏库降级后仍可下载 |
| `tests/test_history.py` | T519-T521：`/history` 排序与 limit 边界、`/events` 事件流与 404、`/delete` 三种结果、`storage` 暴露、GET 回归 |
| `tests/probe_persist.py` | 真实 HTTP + 真实 Scheduler/Engine + 真实 SQLite：17 项检查全部通过（`tests/probe_persist_result.json`） |
| `tests/probe_multi.py` / `probe_chain.py` / `probe_control.py` | Stage-003/004 回归，仍通过（探针已改为 `MEDIADOCK_DB=:memory:`，不写真实库） |
| `tests/check_userscript.py` | v5.0 结构与锚点（472 行） |

单元测试库被 `tests/__init__.py` 固定为 `:memory:`，因此测试不会创建或修改仓库里的
`tasks.db`；只有 `probe_persist.py` 使用临时目录里的真实文件库。

## 已知限制（交接 Stage-006/009/010）

- 重启不做进程/队列恢复，也不自动续传 `paused` 任务；`interrupted` 任务需用户手动重试。
- `/history` 只有 `limit`，没有分页游标、导出和统计报表。
- 事件表无独立清理策略，只随任务删除一起清理。
- 数据库为单文件、无加密、无 WAL 调优、无并发写压力测试。
- 数据库路径只能通过 `MEDIADOCK_DB` 覆盖，配置系统（Stage-006）尚未接管。
- `MediaDock-server.log` 仍无轮转（Stage-006）。

## 回滚

1. 代码回滚：把 `bootstrap()` 的 `db_path` 固定为 `":memory:"`（或删除 `core_store`
   调用点）即可回到 Stage-004 的纯内存行为。
2. 数据回滚：`restore_backup(path)` 或手工把 `tasks.db.bak` 复制回 `tasks.db`；
   也可以直接删除 `tasks.db` 让服务重新建库（历史记录会丢失，任务表为空）。
3. schema 已迁移到 v2 且不可逆；如需降级必须先用备份文件恢复再回滚代码。
4. 版本表 `schema_version` 可手工改写以强制重放迁移，但这属于应急手段，需先备份。
