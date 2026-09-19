# Stage-005：持久化与任务历史

> 依据：[plan-whole.md](plan-whole.md) `0.2`、[Stage-004.md](Stage-004.md)、[docs/stage004-migration.md](docs/stage004-migration.md)
>
> 本阶段把内存任务表落到本地 SQLite，让任务记录在服务重启后可查询，并明确「重启时仍在运行」的任务的处理方式。进程自动恢复、跨平台 Adapter、格式选择和媒体处理不属于本阶段。

---

## 1. 阶段元数据

- 阶段编号：`Stage-005`
- 阶段名称：持久化与任务历史
- 总计划版本：`0.2`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-19
- 预计完成时间：2026-09-19
- 当前阻塞：无
- 前置阶段：`Stage-004` 暂停、继续、取消与断点（已完成）
- 后续阶段：`Stage-006` 配置、依赖与安全加固；`Stage-009`、`Stage-010` 同样依赖本阶段输出

---

## 2. 阶段目标

```text
服务启动
  |
  v
SQLite(tasks.db) --迁移--> schema_version
  |                         ^
  | load_tasks()            |  versioned migrations + 备份
  v
TaskManager(内存, 唯一写入口)
  |  persister 回写（create/transition/进度节流/drop）
  v
Task 记录持久化
  |
  v
GET /history · GET /events · POST /delete · /health(storage)
```

### 2.1 本阶段成功标准

- Task 记录写入本地 SQLite；服务重启后已完成、失败、取消任务仍可查询。
- 迁移带显式版本号，可重复执行；升级前自动生成备份文件，回滚步骤可执行。
- 重启时仍处于 `downloading`/`pending` 的任务被标记为 `error` + `error_code=interrupted`，**不伪装成已恢复**。
- 重启时处于 `paused` 的任务保持 `paused`（当时没有进程在跑），由用户手动继续。
- `completion_order` 与 `completed_at` 持久化，重启后任务列表排序与重启前一致。
- 数据库损坏、写入失败、迁移失败都不会让服务无法启动：降级为内存模式并在 `/health`、`/tasks` 中明确暴露。
- 新增 `/history`、`/events`、`POST /delete`，且不破坏既有 GET/POST 接口。
- 未实现的进程自动恢复、队列恢复和历史分页不写成已支持能力。

---

## 3. 范围边界

### 3.1 本阶段包含

- 新增 `core_store.py`：SQLite 连接、版本化迁移、备份/还原、Task 读写、事件表、删除与清理。
- `TaskManager` 增加持久化回写钩子、`load_task()`、`completion_seq` 恢复；进度写入按时间节流。
- 服务启动流程改为「先迁移 → 再加载 → 再提供 HTTP」，并把状态构建收敛到 `bootstrap()`。
- 重启状态矩阵与 `interrupted` 错误码（复用 `error` 状态 + `error_code`，不新增状态）。
- 新增历史查询、事件查询、终态记录删除和不变量测试。
- 数据库失败降级路径与对应错误码/健康信息。
- Userscript 增加终态记录的删除按钮，`@version` 升到 `5.0`。

### 3.2 本阶段明确不包含

- 子进程/队列的自动恢复（重启后不自动续传、不自动重排队）。
- 多平台 Adapter、`/formats`、音频模式与 Media Processor（Stage-007/008/009）。
- `config.json` 配置系统、依赖诊断与日志脱敏（Stage-006）；数据库路径本阶段只支持环境变量覆盖。
- 历史分页 UI、导出、统计报表和跨设备同步。
- 数据库加密、远程数据库和 WAL 调优。
- 把 `tasks.db` 纳入版本控制（`.gitignore` 排除）。

---

## 4. 输入契约

### 4.1 Stage-004 已确认输入

- `TaskManager` 是唯一状态写入口；状态集合为 `pending`、`downloading`、`paused`、`completed`、`cancelled`、`error`。
- `Task` 字段冻结：`task_id`、`type`、`status`、`url`、`platform`、`title`、`percent`、`speed`、`eta`、`file_path`、`error_code`、`error_message`、`created_at`、`started_at`、`updated_at`、`completed_at`、`completion_order`。
- `completed_at` 在 `completed`/`error` 时写入；`completion_order` 只在 `completed` 时自增。
- `RETRY_RESET_FIELDS` 会清空 `percent`/`speed`/`eta`/`file_path`/`error_code`/`error_message`/`completed_at`/`completion_order`。
- 暂停/取消/重试的语义、错误码与文件策略已冻结（`docs/stage004-migration.md`）。
- 控制上下文、恢复队列和一次性运行记录都是内存结构，重启即丢。
- 数据库路径必须可覆盖，以便测试与探针使用临时文件而不是真实数据文件。

### 4.2 保持不变的约束

- API 默认监听 `127.0.0.1:8765`；错误响应统一为 `{error_code, message}`。
- 既有 `GET /health`、`/download`、`/status`、`/tasks` 与 4 个控制 POST 的响应形状与错误码不变。
- 并发上限 3、FIFO 排队、暂停/取消释放槽位、文件保留/删除策略不变。
- `Task` 字段不新增；`interrupted` 用 `error_code` 表达，不扩状态集合。
- 下载目录与文件名策略不变。
- 不承诺重启后恢复子进程，也不承诺恢复等待队列。

### 4.3 高影响决策门禁（plan-whole.md 第 12 节）

| 编号 | 决策 | 本阶段处理 |
| --- | --- | --- |
| D-012 | 服务重启后的运行中任务状态和是否自动恢复 | 采纳保守方案并确认：`downloading`/`pending` → `error` + `error_code=interrupted`；`paused` 保持 `paused`；不做自动恢复 |
| D-007 | 暂不引入 SQLite 到 MVP | 本阶段按计划引入 SQLite（仅标准库 `sqlite3`），不改 Task 字段与 API 形状 |
| D-017 | 完成/失败任务暂时保留，可手动删除 | 实现 `POST /delete`（仅终态） + `purge_terminal` 容量清理 |

---

## 5. 阶段契约

### 5.1 数据模型

`tasks` 表一列一个 Task 字段，列名与字段名一一对应，便于 `to_dict()` 直写直读：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | TEXT PRIMARY KEY | Task 唯一标识 |
| `type` | TEXT | 任务类型，当前固定 `download` |
| `status` | TEXT | `pending`/`downloading`/`paused`/`completed`/`cancelled`/`error` |
| `url` / `platform` / `title` | TEXT | 下载目标与展示字段 |
| `percent` | REAL | 最近一次进度 |
| `speed` / `eta` | TEXT | 展示字段 |
| `file_path` | TEXT | 输出文件路径（预留） |
| `error_code` / `error_message` | TEXT | 失败或中断原因 |
| `created_at` / `started_at` / `updated_at` / `completed_at` | TEXT | ISO 秒精度时间戳 |
| `completion_order` | INTEGER NULL | 完成序号，排序依据 |

`task_events` 表（状态事件流）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | 事件序号 |
| `task_id` | TEXT | 关联任务 |
| `kind` | TEXT | `created`/`transition`/`restart_interrupted`/`deleted`/`purged` |
| `detail` | TEXT | 例如 `pending->downloading` |
| `created_at` | TEXT | 事件时间 |

`schema_version` 表：单行，记录已应用的迁移版本。

### 5.2 迁移与备份

- `MIGRATIONS = [(1, [...sql...]), (2, [...sql...])]`，按版本号升序应用；`CREATE TABLE/INDEX IF NOT EXISTS` 保证幂等。
- 应用迁移在单个事务内完成；任一步失败则整体回滚，`schema_version` 保持旧值，服务以降级模式启动。
- 需要升级（`current < target`）且数据库文件已存在时，先复制为 `<db>.bak` 再迁移；备份失败则中止迁移。
- `restore_backup(db_path)` 把 `<db>.bak` 复制回 `db_path`（回滚步骤）；返回是否执行。
- 迁移可重复执行：第二次启动版本不变、无 schema 变更。

### 5.3 重启状态矩阵

| 持久化状态 | 重启后状态 | 处理 |
| --- | --- | --- |
| `downloading` | `error` | `error_code=interrupted`，`error_message` 说明未自动恢复；写 `restart_interrupted` 事件 |
| `pending` | `error` | 同上（队列是内存结构，重启不保留） |
| `paused` | `paused` | 当时无进程在跑，保留断点状态；由用户手动继续，不做自动续传 |
| `completed` / `error` / `cancelled` | 不变 | 作为历史记录保留 |

- 重启不会创建等待队列，也不会自动调用 `admit()`。
- `started_at`、`completed_at`、`completion_order` 原样恢复，保证列表排序与重启前一致。

### 5.4 API 契约

| 方法 | 路径 | 请求 | 成功响应 |
| --- | --- | --- | --- |
| GET | `/history?limit=20&status=completed` | 查询参数可选 | `{tasks, total, returned, limit, storage}` |
| GET | `/events?id=...&limit=50` | `id` 必填 | `{task_id, events, returned}` |
| POST | `/delete` | `{"task_id": "..."}` | `{task_id, deleted: true}` |

- `/history`：只返回终态任务（`completed`/`error`/`cancelled`），按 `completed_at` 倒序、`completion_order` 倒序、`task_id` 升序；`limit` 默认 20，范围 1..200。
- `/events`：按事件序号升序；任务行与事件都不存在时 404 `task_not_found`。
- `/delete`：仅终态任务可删；非终态返回 409 `not_deletable`；未知任务 404 `task_not_found`；删除同时清理事件行。
- `/health` 与 `/tasks` 增加 `storage` 字段：`{kind: "sqlite"|"memory", db, schema_version, degraded, reason}`。
- 新增错误码：`invalid_limit`(400)、`invalid_status`(400)、`not_deletable`(409)。
- 既有接口形状与错误码保持不变。

### 5.5 责任边界

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `core_store.Store` | SQLite 连接、迁移、备份/还原、Task 与事件读写、删除/清理 | 决定状态、控制进程 |
| `core_store.TaskPersister` | 接收 `TaskManager` 的写回调用，处理错误与日志 | 修改 Task 字段 |
| `TaskManager` | 唯一状态写入口；调用 persister；`load_task()` 恢复 | 打开数据库、执行 SQL |
| `core_listing` | `/tasks` 与 `/history` 的排序契约与负载 | 读写数据库 |
| `server.bootstrap()` | 组装 store + manager + scheduler，执行重启状态矩阵 | 直接执行 SQL |
| `Handler` | 路由、参数校验、错误码 | 直接访问 SQLite |

### 5.6 不变量

| 条件 | 必须成立 |
| --- | --- |
| 单一写入口 | 只有 `TaskManager` 改状态，persister 只读快照 |
| 降级可用 | 数据库不可用时服务仍可启动、下载/控制功能不受影响，只是不持久化 |
| 失败不致命 | 任何 SQLite 异常不得冒泡到 HTTP 层或杀死调度线程 |
| 无假恢复 | 重启后不存在「显示 downloading 但没有进程」的任务 |
| 排序一致 | 同一数据集在重启前后 `/tasks` 顺序一致 |
| 幂等迁移 | 重复启动不重复建表、不改变版本、不损坏数据 |
| 可回滚 | 迁移前后都有可用备份，`restore_backup` 可恢复旧数据 |
| 记录完整 | `completed_at`/`completion_order` 往返不失真 |

---

## 6. 具体任务

> 下列任务的完成标准已由第 10 节验收与第 12 节执行记录逐项覆盖，全部通过。

### 任务 001：SQLite Store 与版本化迁移

**涉及文件：** 新增 `core_store.py`、`tests/test_store.py`

**内容：** 实现 `Store`（连接、`initialize() -> version`、`version` 属性、`save_task`、`save_tasks`、`load_tasks`、`record_event`、`events`、`delete_task`、`purge_terminal`、`backup`、`restore_backup`、`close`），两段迁移，`open_store()` 降级入口。

**完成标准：**

- [ ] 首次初始化创建表/索引并写入版本 2。
- [ ] 重复 `initialize()` 版本不变、无报错。
- [ ] 迁移前生成 `<db>.bak`，`restore_backup()` 能恢复被破坏的数据。
- [ ] 迁移失败（注入坏 SQL）时版本保持旧值且不留半成品 schema。

### 任务 002：TaskManager 持久化回写与加载

**涉及文件：** `core_manager.py`、`tests/test_manager.py`

**内容：** 增加 `persister` 参数、`_persist_save`/`_persist_delete`、`load_task()`、`completion_seq` 恢复、进度写入节流（默认 1 秒）。

**完成标准：**

- [ ] `create`/`transition`/`drop` 分别触发写回；进度在节流窗口内不重复写。
- [ ] persister 抛异常不影响内存状态，也不冒泡。
- [ ] `load_task()` 能还原全部字段与 `completion_order`，并推进内部序号。
- [ ] 未配置 persister 时行为与 Stage-004 完全一致（全部回归通过）。

### 任务 003：启动装配与重启状态矩阵

**涉及文件：** `server.py`、`tests/test_restart.py`

**内容：** 新增 `bootstrap(db_path=None)`：打开 store → 迁移 → 加载 → 应用重启矩阵 → 重建 manager/scheduler → 安装 persister、`purge_terminal`。`main()` 调用 `bootstrap()`；测试可通过 `bootstrap(tmp_db)` 模拟重启。

**完成标准：**

- [ ] 重启后 `completed`/`error`/`cancelled` 记录仍可查询。
- [ ] 曾处于 `downloading`/`pending` 的任务变为 `error` + `error_code=interrupted`。
- [ ] `paused` 保持 `paused`，且 `/tasks` 中不占用活动槽位。
- [ ] 重启后 `/tasks` 顺序与重启前一致。
- [ ] 数据库损坏时服务启动成功且 `storage.degraded=true`。

### 任务 004：历史与事件查询、删除接口

**涉及文件：** `core_listing.py`、`server.py`、`tests/test_history.py`

**内容：** `sort_history()`/`build_history()`、`GET /history`、`GET /events`、`POST /delete`、`storage` 健康信息、`limit`/`status` 校验。

**完成标准：**

- [ ] `/history` 排序、`limit` 边界与 `total/returned` 正确。
- [ ] `/events` 返回状态事件流，未知任务 404。
- [ ] `/delete` 仅删终态；非终态 409，未知 404，删除后 `/status?id=` 404。
- [ ] 既有 GET/POST 接口回归通过。

### 任务 005：降级与失败路径

**涉及文件：** `core_store.py`、`server.py`、`tests/test_store.py`、`tests/test_restart.py`

**内容：** 损坏文件、只读/不可写、迁移失败三种情况的降级测试；`/health` 暴露 `degraded/reason`；写入失败只记录日志。

**完成标准：**

- [ ] 损坏数据库 → 服务可启动、下载/控制可用、`storage.degraded=true`。
- [ ] 只读数据库 → 写入失败被吞掉并记日志，任务在内存中仍正常流转。
- [ ] 迁移失败 → 版本不变、服务降级、旧数据仍可读。

### 任务 006：Userscript 历史与删除

**涉及文件：** `MediaDock.js`、`tests/check_userscript.py`

**内容：** `@version` 升到 `5.0`；终态行增加删除按钮（`POST /delete`）；`interrupted` 失败原因按 `error_code` 展示；面板摘要显示存储状态（可选，失败时不误导）。

**完成标准：**

- [ ] 结构校验脚本通过（新增 `/delete`、`删除` 锚点）。
- [ ] 删除按钮只出现在终态行，点击后刷新列表。
- [ ] 重启后恢复的任务正常显示，不出现 `downloading` 假状态。

### 任务 007：回归、验证与文档

**涉及文件：** `tests/`、`docs/stage005-migration.md`、`Stage-005.md`、`plan-whole.md`、`.gitignore`

**内容：** 新增 `tests/test_store.py`、`tests/test_restart.py`、`tests/test_history.py`、`tests/probe_persist.py`；`tests/__init__.py` 固定测试数据库为 `:memory:`；`.gitignore` 排除 `*.db`/`*.db.bak`；更新文档与计划状态。

**完成标准：**

- [ ] Stage-002/003/004 全量回归通过。
- [ ] 探针证据写入 `tests/probe_persist_result.json`。
- [ ] `py_compile` 无错误，无新增第三方依赖（`sqlite3` 为标准库）。
- [ ] 未把进程恢复、分页 UI、数据库加密写成已实现。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| T501 | 首次建库与迁移 | 单元 | 表/索引存在，`schema_version=2` |
| T502 | 重复初始化 | 单元 | 版本不变，无异常 |
| T503 | Task 往返 | 单元 | 15 个字段与 `completion_order` 全部一致 |
| T504 | upsert | 单元 | 同 id 更新而非插入新行 |
| T505 | 事件顺序 | 单元 | 按 id 升序返回 |
| T506 | 迁移前备份 | 单元 | `<db>.bak` 存在且可读 |
| T507 | 还原备份 | 单元 | 破坏数据后可恢复到备份内容 |
| T508 | 迁移失败回滚 | 单元 | 版本保持旧值，无半成品 schema |
| T509 | 损坏数据库降级 | 集成 | `open_store` 返回降级，`degraded=true` |
| T510 | 只读/写入失败 | 单元 | 写入失败被吞掉，内存状态正常 |
| T511 | create 触发写回 | 单元 | persister 收到 1 次 save |
| T512 | 进度节流 | 单元 | 窗口内多次上报只写 1 次 |
| T513 | persister 抛异常 | 单元 | 不冒泡、状态照常变更 |
| T514 | 重启后历史可查 | 集成 | 终态任务仍在 `/tasks` 与 `/history` |
| T515 | downloading → interrupted | 集成 | `error` + `error_code=interrupted` |
| T516 | pending → interrupted | 集成 | 同上，且不占队列 |
| T517 | paused 重启保持 | 集成 | 状态不变且不改文件 |
| T518 | 重启排序一致 | 集成 | `/tasks` 顺序前后一致 |
| T519 | `/history` 排序与 limit | API | 倒序正确，`limit` 1..200，非法值 400 |
| T520 | `/events` 事件流 | API | 升序返回，未知任务 404 |
| T521 | `/delete` 语义 | API | 终态 200，非终态 409，未知 404 |
| T522 | 容量清理 | 单元 | `purge_terminal(keep=n)` 只留最新 n 条 |
| T523 | Userscript 结构 | 静态 | 校验脚本 rc=0 |
| T524 | 真实重启链路 | 探针 | 跨「重启」验证记录与状态 |

### 7.2 建议命令

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_task.py core_manager.py core_parse.py core_engine.py core_scheduler.py core_listing.py core_control.py core_files.py core_store.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_persist.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_multi.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_chain.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_control.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\check_userscript.py
```

### 7.3 测试原则

- 单元测试使用 `:memory:` 或 `tempfile` 数据库，绝不写真实 `tasks.db`（`tests/__init__.py` 固定 `MEDIADOCK_DB=:memory:`）。
- 不依赖网络、不启动真实 yt-dlp；重启矩阵用假引擎构造状态。
- 数据库失败必须通过真实文件系统条件触发（损坏文件、只读目录），不 mock `sqlite3`。
- 探针使用临时文件数据库，重启通过再次 `bootstrap()` 模拟，并在同一进程内验证。

---

## 8. 阶段产物

- `core_store.py`：`Store`、`TaskPersister`、`MIGRATIONS`、`open_store()`、`restore_backup()`。
- `TaskManager` 持久化回写、`load_task()`、`completion_seq` 恢复、进度节流。
- `server.bootstrap()` 与重启状态矩阵；`main()` 启动前完成迁移与恢复。
- `GET /history`、`GET /events`、`POST /delete`、`/health`+`/tasks` 的 `storage` 信息。
- `MediaDock.js v5.0` 终态删除按钮。
- `tests/test_store.py`、`tests/test_restart.py`、`tests/test_history.py`、`tests/probe_persist.py`、`docs/stage005-migration.md`。
- `.gitignore` 排除运行时数据库。

---

## 9. 风险、依赖与回滚

### 9.1 风险

| 风险 | 影响 | 缓解措施 |
| --- | --- | --- |
| 数据库损坏导致服务无法启动 | 用户完全无法下载 | `open_store` 失败即降级为内存模式并在 `/health` 暴露 |
| 迁移过程中断电/中断 | schema 半成品、数据不可读 | 迁移在事务内执行；升级前生成 `.bak` |
| 写回阻塞下载线程 | 下载变慢或卡死 | 写回按状态变更 + 进度节流；异常一律吞掉并记日志 |
| 重启后假 `downloading` | UI 显示在下载但无进程 | 重启矩阵强制转 `error` + `interrupted` |
| 排序在重启后变化 | 用户看到列表跳变 | 持久化 `completed_at`/`completion_order` 并恢复序号 |
| 记录无限增长 | 数据库膨胀 | `purge_terminal(keep=200)` 在启动时清理 |
| 数据库文件被提交到仓库 | 泄漏本地路径与标题 | `.gitignore` 排除 `*.db`/`*.db.bak` |
| 误删仍在运行的任务记录 | 任务失去跟踪 | `/delete` 只接受终态，非终态 409 |

### 9.2 外部依赖

- 标准库 `sqlite3`（Python 3.13 内置，无新增第三方依赖）。
- 可写目录（默认仓库根目录，可用 `MEDIADOCK_DB` 覆盖）。
- 磁盘剩余空间 ≥ 数据库大小的两倍（备份）。

### 9.3 回滚策略

1. 保留 Stage-004 已签字基线（128/128 单元测试 + 控制探针）。
2. 代码回滚：把 `bootstrap(db_path=None)` 传 `None` 即可回到纯内存模式；状态与 API 契约不变。
3. 数据回滚：把 `<db>.bak` 复制回 `tasks.db`（`restore_backup()`），或删除 `tasks.db` 让服务重新建库。
4. 迁移版本不可逆；如需降级 schema，必须先还原备份再回滚代码。

---

## 10. 阶段验收标准

### 10.1 持久化验收

- [x] 重启后 `completed`/`error`/`cancelled` 记录仍可查询（`/tasks`、`/history`、`/status?id=`）。
- [x] `downloading`/`pending` 任务重启后为 `error` + `error_code=interrupted`。
- [x] `paused` 任务重启后仍为 `paused`，可继续控制（不自动续传）。
- [x] 重启前后 `/tasks` 顺序一致（终态记录排序一致）。
- [x] 迁移可重复执行，版本号明确（v2）。

### 10.2 失败与降级验收

- [x] 数据库损坏时服务仍可启动并使用，`/health` 报告 `degraded=true` 与原因。
- [x] 写入失败不影响内存状态与 HTTP 响应。
- [x] 迁移失败时版本不变、旧数据可读、旧备份可用。
- [x] 备份与还原步骤可执行且被测试覆盖（`backup()`/`restore_backup()`）。

### 10.3 API 验收

- [x] `/history`、`/events`、`POST /delete` 行为与 5.4 一致（含 5.4 之外记录的差异项）。
- [x] 400/404/409 错误码正确且响应为 JSON。
- [x] 既有 GET/POST 接口回归通过。

### 10.4 质量与边界验收

- [x] 全量单元测试通过（172/172），无新增第三方依赖。
- [x] 未实现能力（自动恢复、分页 UI、数据库加密、WAL 调优）未被写成已支持。
- [x] 运行时数据库未被纳入版本控制（`.gitignore` 排除 `*.db*`）。
- [x] 文档记录备份/回滚步骤与已知限制。

### 10.5 进入 Stage-006 的条件

- [x] 存储层与重启语义冻结，`interrupted` 语义可作为后续阶段前提。
- [x] 数据库路径可通过环境变量覆盖，供 Stage-006 配置系统接管。
- [x] 遗留限制（无自动恢复、无分页、日志无轮转、无来源校验）已交接。
- [x] 阶段完成影响检查已完成。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-004 的影响

- `TaskManager` 构造函数新增可选 `persister`，默认行为不变。
- `drop()` 增加可选写回；取消/重试语义与文件策略不变。
- 控制 API 与错误码不变。

### 11.2 对 Stage-002/003 的影响

- `Task` 字段与状态机不变，`interrupted` 通过 `error_code` 表达。
- `/tasks` 增加 `storage` 字段（新增键，不删除旧键）；排序契约不变。
- 并发上限、FIFO 队列、补位逻辑不变。

### 11.3 对 Stage-006 输出

- 数据库路径参数化（`MEDIADOCK_DB`）可直接接配置系统。
- `open_store()` 的降级原因字段可作为 `config.json` 诊断输出。
- 数据库文件属于运行时数据，配置系统需要给出备份与清理说明。

### 11.4 对 Stage-009/010 输出

- 媒体处理任务可以复用 `tasks` 表与事件流（`type` 字段已预留）。
- 发布流程需要包含数据库迁移与备份步骤，Stage-010 直接引用本阶段 9.3。

### 11.5 不变约束

- Userscript 不直接访问 SQLite。
- 不默认监听 `0.0.0.0`。
- 不改变默认 MP4 格式策略与路径边界规则。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-19 | 阶段准备 | Cline | 已完成 | 核对 Stage-004 输出、D-012/D-017 与 `Task` 字段契约 | Stage-004 基线 128/128 | 无 |
| 2026-09-19 | 任务 001 | Cline | 已完成 | 新增 `core_store.py`：`Store`、两段迁移、备份/还原、Task 与事件读写、删除/容量清理 | `test_store.py` 通过；v1→v2 升级写 `.bak`，坏迁移回滚后版本保持 1 | 无 |
| 2026-09-19 | 任务 002 | Cline | 已完成 | `TaskManager` 增加 persister 回写、`load_task()`、`completion_seq()`、进度节流（1s） | `test_manager.py` 新增 5 例通过：写回次数、节流、异常隔离、序号恢复 | 无 |
| 2026-09-19 | 任务 003 | Cline | 已完成 | `server.bootstrap()`：关旧库 → 迁移 → 加载 → 重启矩阵 → 重建 Scheduler；`main()` 输出 storage 日志 | `test_restart.py` 通过：终态保留、`downloading`/`pending`→`interrupted`、`paused` 保持、排序一致 | 无 |
| 2026-09-19 | 任务 004 | Cline | 已完成 | `core_listing` 增加 `sort_history`/`normalize_limit`/`build_history`；`server.py` 增加 `/history`、`/events`、`POST /delete`、`storage` 信息 | `test_history.py` 通过：排序、limit 边界、404/409/400、GET 回归 | 无 |
| 2026-09-19 | 任务 005 | Cline | 已完成 | 降级路径：损坏库 `open_store` 返回 None + `degraded/reason`；写失败标记降级但不抛异常 | 探针 `corrupt_degrades_to_memory`/`download_works_after_degrade` = true | 无 |
| 2026-09-19 | 任务 006 | Cline | 已完成 | `MediaDock.js v5.0`：`delete` 控制路径、终态删除按钮、`ERROR_HINTS`；更新结构校验锚点 | `check_userscript.py` rc=0（472 行） | 浏览器手工验证随 Stage-004 的 T421 一并确认 |
| 2026-09-19 | 任务 007 | Cline | 已完成 | 新增 `test_store.py`/`test_restart.py`/`test_history.py`/`probe_persist.py`；`tests/__init__.py` 固定测试库 `:memory:`；三个旧探针改为不写真实库；`.gitignore` 排除 `*.db*`；`docs/stage005-migration.md` | `unittest discover` 172/172 OK（11.5s）；`py_compile` rc=0；5 个探针全部 OK | 无 |
| 2026-09-19 | 回归修复 | Cline | 已完成 | 修正 3 项失败：`/delete` 成为真实路由导致旧「未知路径」用例失效、删除后仍留审计事件、`restore_backup(":memory:")` 返回值断言；并修正 `load_task` 缺少 `task_id` 的校验位置 | 172/172 通过 | 无 |
| 2026-09-19 | 探针修复 | Cline | 已完成 | `probe_persist.py` 两处问题：`bootstrap()` 会重建 scheduler 导致假引擎工厂丢失（首次运行误触真实 yt-dlp）、全量排序断言过严；改为重启后重装工厂 + 只比较「已完成子集」顺序 | `probe_persist_result.json`：17 项检查全部 true，`failed_checks: []` | 无 |
| 2026-09-19 | 最终验收 | Cline | 已完成 | 全量单元测试 + 5 个探针 + 结构校验 + `py_compile` | 172/172 OK；PERSIST/MULTI/CHAIN/CONTROL/JS 全部 OK | 无 |

---

## 13. 实际输出与计划差异

- 原计划输出：SQLite 持久化、迁移与备份、重启状态矩阵、历史/事件/删除 API、降级路径与测试。
- 实际输出：全部实现，摘要如下。
  - `core_store.py`（`Store`/`TaskPersister`/`MIGRATIONS`/`open_store`/`restore_backup`）。
  - `TaskManager` 写回钩子 + `load_task()` + `completion_seq()` + 1s 进度节流。
  - `server.bootstrap()` 启动装配与重启状态矩阵；`main()` 打印 storage 状态。
  - `GET /history`、`GET /events`、`POST /delete`；`/health`、`/tasks` 增加 `storage`。
  - `MediaDock.js v5.0` 终态删除按钮；`tests/test_store.py`、`test_restart.py`、`test_history.py`、`probe_persist.py`、`docs/stage005-migration.md`。
- 差异：
  1. **`/events` 的 404 语义放宽**：任务行已删除但仍有事件（审计）时返回 200 而非 404；只有两者都不存在才 404。原计划只写了「未知任务 404」。
  2. **新增 `invalid_status` 错误码**（`/history?status=...` 非法值），原计划只列了 `invalid_limit`。
  3. **`purge_terminal` 不写审计事件**，且删除时连带清理事件行；显式 `POST /delete` 才写 `deleted` 审计事件。原因是容量清理如果也写事件会让事件表随清理一起增长。
  4. **`delete_task(record=True)` 增加参数**（内部接口），用于区分用户删除与容量清理。
  5. **测试数据库固定为 `:memory:`**（`tests/__init__.py` 设置 `MEDIADOCK_DB`），并把 `probe_multi/chain/control` 也改为 `:memory:`，避免探针在仓库根目录生成 `tasks.db`。`probe_persist.py` 使用临时目录里的真实文件库。
  6. **`bootstrap()` 会关闭旧 store 并重建 Scheduler**：这是「模拟重启」的实现方式，代价是调用方必须重新安装引擎工厂（`probe_persist.py` 首次运行因此误触真实 yt-dlp，已修复并在执行记录中留档）。
  7. **`/history` 只比较已完成子集排序**：`downloading`/`pending` 重启后会变成 `error`，全量顺序必然变化，故排序一致性只对终态记录断言。
  8. **UI 不展示 storage 状态**：Stage-005 任务 006 标注为可选，选择不展示以避免误导；运行时状态通过 `/health` 查询。
- 差异影响：1/2 是错误码/接口细节的澄清；3/4 是防止数据库膨胀的必要取舍；5/6 是测试与探针的工程约束；7/8 是断言与展示口径的澄清，均不改变 Task 字段与既有 API 形状。
- 处理决定：全部接受并写入 `docs/stage005-migration.md`；`plan-whole.md` 增加变更记录 C-004 并把 D-012 标为已确认。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：允许进入 Stage-006
- 对 Stage-004 影响：`TaskManager` 增加可选 persister（默认行为不变）；控制 API、文件策略与状态机未变；Stage-004 的 128 个测试全部回归通过
- 对 Stage-006/009/010 影响：数据库路径已参数化（`MEDIADOCK_DB`），可在 Stage-006 被 `config.json` 接管；`open_store` 的降级原因可直接作为诊断输出；Stage-009 的媒体处理任务可复用 `tasks`/`task_events`（`type` 字段已预留）；Stage-010 的发布流程需包含迁移与备份步骤（见本阶段 9.3）
- 是否更新 `plan-whole.md`：是，当前状态改为「Stage-005 已完成，Stage-006 未开始」，Stage-005 完成时间 `2026-09-19`，D-012 转为「已确认」，新增变更记录 C-004（版本仍 `0.2`）
- 审查人：用户
- 日期：2026-09-19

