# Stage-003：多任务调度与共享任务列表

> 依据：[plan-whole.md](plan-whole.md) `0.2`、[Stage-002.md](Stage-002.md)、[docs/stage002-migration.md](docs/stage002-migration.md)
>
> 本阶段在已冻结的 Task 模型、基础状态机和后端模块边界上，支持多个独立下载任务、并发限制、等待队列以及跨 YouTube 页面共享的任务列表。暂停、继续、取消、删除、重试、断点恢复和 SQLite 持久化不属于本阶段。

---

## 1. 阶段元数据

- 阶段编号：`Stage-003`
- 阶段名称：多任务调度与共享任务列表
- 总计划版本：`0.2`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-19
- 预计完成时间：2026-09-19
- 当前阻塞：无；跨页面/SPA/窄屏为浏览器手工验证，已由用户确认
- 前置阶段：`Stage-002` Task 核心模型与后端边界
- 后续阶段：`Stage-004` 暂停、继续、取消与断点

---

## 2. 阶段目标

把当前“每个页面只能跟踪一个 `currentTaskId`、每次请求直接启动一个线程”的行为，改为由后端统一调度的多任务模型：

```text
YouTube 页面 A/B/C
       |
       v
共享 Local API
       |
       v
TaskManager + Scheduler
   |              |
运行中任务 <= 3    FIFO 等待队列
       |
       v
DownloadEngine -> yt-dlp -> downloads/
       |
       v
全量任务列表 API
       |
       v
所有 YouTube 页面共享列表和状态
```

### 2.1 本阶段成功标准

- 每次下载仍创建一个独立 Task，Task ID、字段和基础状态机与 Stage-002 一致。
- 同时运行的下载任务最多为 3 个；超过容量的任务进入 `pending` 等待，不丢失、不伪装成已开始。
- 运行中的任务完成或失败后，调度器自动启动下一个等待任务。
- 所有页面通过同一个任务源看到相同任务集合、状态、进度和排序。
- Userscript 从单任务按钮升级为共享任务列表，同时保留当前下载入口和 API 兼容性。
- YouTube SPA 导航后列表和下载入口仍可用，补齐 Stage-001 遗留的 T015。
- 服务重启后不恢复旧任务或旧队列；任务持久化继续留给 Stage-005。

---

## 3. 范围边界

### 3.1 本阶段包含

- 在 Stage-002 TaskManager 之上增加 Scheduler/队列责任边界。
- 固定最大活动下载数为 `3`，并定义运行中、等待中和终态任务的计数规则。
- 创建任务时先登记，再由调度器决定立即启动或排队。
- 任务完成、失败和引擎启动失败后的自动补位。
- 保持内存任务和内存队列；记录队列在进程重启后丢失的限制。
- 扩展全量任务查询的排序、显示容量和稳定 DOM 标识契约。
- 修改 Userscript，显示共享任务列表和多个任务状态。
- 验证多个 YouTube 页面、SPA 导航和并发/排队行为。
- 为调度器、队列、API、前端渲染和端到端模拟链路补充测试。

### 3.2 本阶段明确不包含

- 暂停、继续、取消、删除、重试和断点续传；属于 Stage-004。
- 子进程暂停、终止、临时文件清理和恢复策略；属于 Stage-004。
- SQLite、任务历史、服务重启恢复、历史分页和数据迁移；属于 Stage-005。
- 配置文件、可配置并发数、依赖安装和日志完整脱敏；主要属于 Stage-006。
- 多平台 Adapter、动态格式选择和 `/formats`；属于 Stage-007/008。
- 音频模式、媒体处理任务和高级 FFmpeg 参数；属于 Stage-009。
- 浏览器扩展、桌面 UI 或新的前端框架。
- 改变默认 `height<=1080` MP4 格式策略。

---

## 4. 输入契约

### 4.1 Stage-002 已确认输入

- `Task` 是唯一字段来源，必须继续支持 `task_id`、`type`、`status`、`url`、`platform`、`title`、`percent`、`speed`、`eta`、`file_path`、错误字段及时间字段。
- 当前基础状态为 `pending`、`downloading`、`completed`、`error`。
- Stage-003 只能使用已有转换：

```text
pending -> downloading -> completed
pending -> downloading -> error
pending -> error
```

- `TaskManager` 提供创建、查询单个、查询全部、状态转换和进度回写；状态不能由 Scheduler 或 UI 直接修改字符串。
- `DownloadEngine.run(task_id, url)` 负责一次下载，Scheduler 负责调用它，不负责重写引擎内部逻辑。
- `server.py` 当前创建 Task 后立即启动线程，Stage-003 必须将启动决策集中到调度器。
- 当前 API 为 `GET /health`、`GET /download?url=...`、`GET /status` 和 `GET /status?id=...`。
- 当前 Userscript 使用单一 `currentTaskId`、单一轮询和固定按钮；这是本阶段的主要前端替换面。

### 4.2 保持不变的约束

- API 默认监听 `127.0.0.1:8765`。
- Userscript 不直接运行 yt-dlp 或 FFmpeg。
- 每个下载请求都是独立 Task。
- 后端负责路径、命令和外部进程控制。
- 调度器不能把等待任务标记为 `downloading`。
- 未完成任务优先显示，任务排序、容量、折叠和滚动遵守 `plan-whole.md`。
- 本阶段不承诺服务重启后恢复任务、队列或子进程。

---

## 5. 阶段契约

### 5.1 调度模型

默认调度策略：

- `MAX_ACTIVE_TASKS = 3`，本阶段固定为代码常量或构造参数，不做用户配置。
- `pending` 表示已登记但尚未占用活动槽位的任务。
- `downloading` 表示已占用一个活动槽位且 DownloadEngine 正在执行。
- `completed` 和 `error` 不占用活动槽位。
- 队列采用 FIFO；同一创建顺序下以 `created_at` 和稳定的内部序号保证顺序。
- 任务创建顺序和实际启动顺序可以不同于完成顺序；任务列表排序不得使用线程完成顺序替代计划中的排序规则。
- 创建任务必须先写入 Task，再由 Scheduler 原子地决定立即启动或放入队列，不能出现任务既未返回又无法查询的窗口。
- DownloadEngine 线程结束后必须通知 Scheduler；通知只能触发一次补位，不能重复启动同一个 Task。

### 5.2 调度状态不变量

| 条件 | 必须成立 |
| --- | --- |
| 活动任务计数 | `downloading` 任务数量不超过 3 |
| 等待队列 | 队列中的 Task 必须存在且状态为 `pending` |
| 活动集合 | 活动集合中的 Task 必须存在且状态为 `downloading` |
| 终态任务 | `completed`/`error` 不在队列或活动集合中 |
| 任务唯一性 | 一个 `task_id` 只能有一个排队记录和一个运行线程 |
| 补位 | 任一活动任务进入终态后，若队列非空且有槽位，启动队首任务 |
| 失败隔离 | 一个任务失败不能让其他任务伪造失败或停滞 |

### 5.3 任务列表 API 契约

Stage-003 优先保持现有 `GET /status` 的兼容入口，并在其全量返回中提供可排序的完整 Task 对象。若前端需要明确的列表语义，可新增 `GET /tasks`，但不得删除或改变现有 `/status` 和 `/status?id=...` 的响应兼容性。

推荐响应：

```json
{
  "tasks": [
    {
      "task_id": "a82f31c4",
      "status": "downloading",
      "percent": 42.5,
      "speed": "1.2MiB/s",
      "eta": "00:18",
      "title": "Example video",
      "created_at": "2026-09-19T12:00:00",
      "updated_at": "2026-09-19T12:00:05"
    }
  ],
  "active_count": 1,
  "active_limit": 3,
  "queued_count": 0
}
```

实际是否新增包装字段，必须先处理现有 `/status` 返回字典的兼容影响。保守方案是：继续让 `/status` 返回现有任务字典，新增 `/tasks` 返回上述列表摘要；Userscript 使用 `/tasks`，旧调用方继续使用 `/status`。

错误响应继续使用 Stage-002 的 `{ "error_code": "...", "message": "..." }`。

### 5.4 任务列表排序契约

排序必须由后端或前端的单一排序函数稳定执行，所有页面使用相同规则：

1. 未完成任务优先于已完成任务。
2. 未完成任务按 `percent` 从高到低排序。
3. `pending`、`error` 等没有有效实时进度的任务按 `0%` 排序；同百分比按 `created_at` 从新到旧排序。
4. 已完成任务按 `completed_at` 倒序排列；同一秒使用 `completion_order`。
5. `task_id` 作为最终稳定 tie-breaker，避免 DOM 行跳动到另一任务。

说明：`error` 属于未完成分组，但本阶段不提供重试或删除按钮；它必须可见且保持稳定。

### 5.5 前端显示契约

- 所有 YouTube 页面从本地 API 轮询同一个任务列表，不能只渲染当前页面创建的任务。
- 下载按钮仍负责创建当前页面 URL 的 Task；创建成功后立即刷新共享列表。
- 任务列表每行使用 `data-task-id` 或等价稳定标识，状态刷新不能把控制或标题绑定到另一任务。
- 下载中的任务显示标题、百分比、速度和 ETA。
- `pending` 显示排队状态；`completed` 显示完成状态；`error` 显示失败状态。
- 本阶段不显示暂停、继续、取消、删除和重试按钮，避免暴露未实现行为。
- 默认最多直接展示 20 个任务；未完成任务超过 20 个时，面板内部滚动，不隐藏未完成任务。
- 已完成任务超过默认容量时折叠为“已完成 N 个”，展开仍使用面板内部滚动。
- 任务列表不得撑开页面遮挡 YouTube 主内容；按钮和列表在桌面、窄屏和 SPA 导航后保持可用。
- 页面导航不应清空共享列表，不应把旧页面任务误显示为当前页面唯一任务。

### 5.6 Scheduler 责任边界

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `Scheduler` | 活动槽位、FIFO 队列、启动线程、完成回调、补位 | 修改 Task 字段、解析 HTTP、渲染 UI |
| `TaskManager` | Task 存储、状态转换、快照和进度回写 | 决定并发数量、操作 DOM |
| `DownloadEngine` | 执行一个下载并报告结果 | 启动其他任务、管理队列 |
| `Handler` | 创建任务、查询列表、返回 JSON | 直接启动 DownloadEngine 或修改队列 |
| `MediaDock.js` | 创建请求、轮询列表、渲染共享任务面板、处理 SPA | 运行下载进程、推断后端队列状态 |

Scheduler 和 TaskManager 必须有清晰的锁顺序或单一调度锁，避免任务完成回调与新建任务请求互相死锁。

---

## 6. 具体任务

### 任务 001：实现多任务 Scheduler

**涉及文件：** 新增调度模块、`core_manager.py`、`server.py`、`tests/`

**内容：**

- 新增 Scheduler 类或等价后端边界，构造时接收 TaskManager、DownloadEngine 工厂和活动上限。
- 实现 `submit(url)`：创建 pending Task，活动槽位可用时启动，否则进入 FIFO 队列。
- 实现一次性 `start(task_id)` 和 `on_finished(task_id)` 回调。
- 在启动前执行 `pending -> downloading`，或让 DownloadEngine 保持该转换，但只能有一个责任方。
- DownloadEngine 返回或抛出异常时，确保 Task 进入 `completed` 或 `error`，并释放活动槽位。
- 在同一调度临界区完成释放和补位，避免超过并发上限。

**完成标准：**

- [x] 同时提交 4 个任务时只有 3 个进入 `downloading`，第 4 个保持 `pending`。
- [x] 任一活动任务完成或失败后，队首 pending 任务自动启动。
- [x] 同一 Task 不会启动两次。
- [x] 线程异常不会永久占用活动槽位。
- [x] Scheduler 不直接改写 Task 字段或绕过 TaskManager 状态机。

### 任务 002：统一创建入口和 API 调度行为

**涉及文件：** `server.py`、调度模块、`tests/test_apiv2.py` 或新的 API 测试

**内容：**

- 将 `/download` 从“创建后直接 `threading.Thread`”改为调用 Scheduler.submit。
- 保持合法请求返回 `task_id`，且返回后立即可以通过状态 API 查询。
- 定义队列满时的行为。本阶段推荐不拒绝请求，继续创建 pending Task；内存资源限制和请求限流留给后续安全阶段。
- 保持现有 URL 校验、错误码、CORS、监听地址和默认格式。
- 增加 Scheduler 运行摘要所需的 `active_count`、`active_limit`、`queued_count`，但不破坏旧查询结构。

**完成标准：**

- [x] `/download` 不再直接创建下载线程。
- [x] 连续请求得到独立 Task ID。
- [x] `/status` 和新任务列表接口能观察 pending/downloading/completed/error。
- [x] 服务在没有可用 yt-dlp 时，任务失败并继续调度后续任务。
- [x] Stage-002 API 回归仍全部通过。

### 任务 003：实现全量任务排序和列表摘要

**涉及文件：** 调度模块、TaskManager 或新的查询模块、`server.py`、`tests/`

**内容：**

- 实现一个单一的后端排序函数，使用计划规定的未完成优先、进度降序、完成时间倒序和稳定 tie-breaker。
- 保证 `completed_at` 和 `completion_order` 已写入的值用于完成任务排序。
- 为前端提供任务列表、队列数量、活动数量和活动上限。
- 明确返回完整字段还是摘要字段；任务控制所需的 `task_id` 必须始终存在。
- 不因列表排序删除或修改 Task 记录。

**完成标准：**

- [x] 混合 pending/downloading/error/completed 数据的排序与总计划示例一致。
- [x] 同一秒完成的任务排序稳定。
- [x] API 返回的活动数、排队数与实际调度器集合一致。
- [x] 多次查询不会因字典插入顺序改变结果。

### 任务 004：升级 Userscript 为共享任务列表

**涉及文件：** `MediaDock.js`、必要的测试夹具或浏览器验证记录

**内容：**

- 保留当前页面下载按钮及 URL 归一化逻辑。
- 将 `currentTaskId` 替换为任务列表数据和稳定的 `task_id` 行标识；当前页面只负责创建任务，不垄断全局状态。
- 增加共享任务面板的创建、更新、销毁和重建逻辑，避免重复注入 DOM。
- 轮询任务列表时更新所有任务行，不因排序变化把节点事件绑定到另一任务。
- 将 pending、downloading、completed、error 显示为明确状态；不显示未实现控制按钮。
- 在 `yt-navigate-finish` 后保留入口和共享列表，补齐 Stage-001 T015。
- 处理服务不可用、空列表、解析错误和列表刷新时的 UI 降级。

**完成标准：**

- [x] 单页面连续创建多个任务时，列表同时显示这些任务。
- [x] 两个 YouTube 页面都能看到同一任务集合和状态变化。
- [x] 任务完成顺序与列表排序契约一致。
- [x] SPA 导航后按钮和任务列表仍存在且不重复注入。
- [x] 窄屏页面不出现明显遮挡、溢出或任务行错绑。

### 任务 005：处理内存边界和生命周期

**涉及文件：** 调度模块、TaskManager、`tests/`、文档

**内容：**

- 记录服务重启会清空任务和等待队列，不实现恢复。
- 规定服务进程退出时的 daemon 线程行为，并在文档中说明未完成任务不会被恢复。
- 保持终态任务在内存中的可查询性，不在本阶段自动清理完成或失败任务。
- 明确任务列表持续增长的已知资源风险，容量和历史清理留给 Stage-005/006 的决策。
- 防止完成回调、HTTP 查询和新提交之间出现重复记录或负活动计数。

**完成标准：**

- [x] 重启后不声称恢复旧任务，但可创建新任务。
- [x] 活动数永不为负，队列数永不为负。
- [x] 终态任务仍可查询，且不占用活动槽位。
- [x] 调度器关闭或异常路径有可诊断日志。

### 任务 006：回归测试、浏览器验证和文档

**涉及文件：** `tests/`、`docs/`、`Stage-003.md`、必要时 `plan-whole.md`

**内容：**

- 增加 Scheduler 单元测试、假引擎测试、API 多任务测试、排序测试和前端验证记录。
- 使用假 DownloadEngine 控制任务完成时机，避免并发测试依赖真实 YouTube 网络。
- 使用真实 YouTube 下载做至少一次多任务回归；网络不可用时必须标为阻塞或未执行，不可用模拟结果替代。
- 验证两个浏览器页或两个 YouTube 页面实例共享状态；至少记录 T015 SPA 导航结果。
- 更新阶段执行记录、实际输出、差异和对 Stage-004 的影响。

**完成标准：**

- [x] Stage-002 全量测试继续通过。
- [x] 多任务并发、排队、补位、失败隔离和排序测试通过。
- [x] API 和 Userscript 验证结果已记录。
- [x] 未把暂停、取消、删除、重试或持久化写成已实现能力。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| T301 | 单任务提交 | 单元/API | 创建独立 pending Task 并可查询 |
| T302 | 三任务并发 | 单元 | 3 个任务同时运行，不超过上限 |
| T303 | 第四任务排队 | 单元/API | 第 4 个为 pending，进入 FIFO 队列 |
| T304 | 完成后自动补位 | 单元 | 队首任务启动，活动数仍不超过 3 |
| T305 | 失败后自动补位 | 单元 | 失败任务为 error，后续任务正常启动 |
| T306 | 重复完成回调 | 单元 | 不重复释放槽位或启动任务 |
| T307 | 引擎启动异常 | 单元 | 当前任务 error，队列继续运行 |
| T308 | 多线程提交 | 并发 | Task ID 唯一，队列/活动集合无重复 |
| T309 | 任务排序 | 单元 | 未完成优先、进度降序、完成倒序且稳定 |
| T310 | 列表摘要 | API | active/queued 数量与调度器一致 |
| T311 | 多任务 `/download` | API | 连续请求全部返回独立 task_id |
| T312 | `/status` 兼容 | API | 旧全量/单任务查询继续可用 |
| T313 | 服务重启边界 | 回归 | 旧任务不恢复，新任务可创建 |
| T314 | Stage-002 全量回归 | 回归 | 原有 42/42 及新增核心测试通过 |
| T315 | Userscript 多任务 | 手工/浏览器 | 一个页面可显示多个任务 |
| T316 | 跨页面共享 | 手工/浏览器 | 两个页面看到同一任务集合 |
| T317 | SPA 导航 | 手工/浏览器 | T015 关闭，按钮和列表不重复且可用 |
| T318 | 窄屏布局 | 手工/浏览器 | 列表不溢出、不遮挡主要内容 |
| T319 | 真实多任务下载 | 集成 | 允许测试时完成或产生明确可诊断错误 |

### 7.2 建议命令

```powershell
python -m py_compile .\server.py .\core_task.py .\core_manager.py .\core_parse.py .\core_engine.py .\core_scheduler.py .\core_listing.py
python -m unittest discover -s tests -v
python tests\probe_multi.py
python tests\probe_chain.py
python tests\check_userscript.py
```

新增调度模块后，将其加入 `py_compile` 命令；前端浏览器验证需要记录浏览器、页面数量、测试 URL 类型、轮询结果和截图或操作记录。
`tests\check_userscript.py` 是本机无 Node.js 时的 `MediaDock.js` 结构校验（括号平衡、无模板字符串、契约锚点齐全、无未实现控制文案）。

### 7.3 测试原则

- 调度器单元测试使用可控假引擎，测试不依赖网络速度和真实下载时长。
- 并发上限必须通过活动集合或引擎调用记录验证，不能只看 UI 文本。
- 队列补位测试必须覆盖成功和失败两条终态路径。
- API 测试必须同时检查旧接口兼容和新增列表摘要。
- 前端测试必须验证稳定 `task_id` 绑定，而不是只验证页面上出现了若干文字。
- 真实下载只能作为补充证据，不能替代确定性的单元和集成测试。

---

## 8. 阶段产物

- Scheduler/Task 调度实现，固定最大活动任务数为 3。
- FIFO 等待队列和活动任务集合。
- 任务完成/失败自动补位逻辑。
- 兼容的任务列表 API 或查询扩展。
- `MediaDock.js` 共享任务列表和多任务状态渲染。
- 多任务、排序、并发、失败隔离和 API 回归测试。
- T015 SPA 导航验证记录。
- Stage-003 迁移说明、执行记录、差异和 Stage-004 输入契约。

---

## 9. 风险、依赖与回滚

### 9.1 风险

| 风险 | 影响 | 缓解措施 |
| --- | --- | --- |
| Scheduler 与 DownloadEngine 双重管理状态 | 任务卡在 pending 或重复转换 | 明确由一方负责 pending->downloading，统一通过 TaskManager |
| 完成回调重复或丢失 | 活动槽位泄漏、队列停滞 | 为每个 Task 保存一次性运行记录，测试重复回调 |
| 并发竞态超过 3 个 | 资源耗尽、行为不确定 | 单一调度锁和活动集合断言，压力测试多线程提交 |
| `/status` 响应形状改变 | 旧调用方失效 | 保留旧接口；优先新增 `/tasks`，执行 API 回归 |
| 前端列表排序导致任务错绑 | 用户操作错误 | `task_id` 作为 DOM key，节点更新不依赖数组索引 |
| 页面过多导致轮询放大 | 本地服务请求增加 | 统一轮询间隔，前端实例只更新自身 DOM；后续可评估共享广播 |
| 内存队列无限增长 | 长时间运行资源增加 | 记录风险；限流、清理和持久化留给 Stage-005/006 |
| 服务重启丢失队列 | 用户误以为任务仍会继续 | UI/文档明确本阶段不恢复，重启回归测试覆盖 |
| YouTube SPA DOM 改变 | 按钮或列表消失 | 保留导航事件和定时重建，记录浏览器验证结果 |

### 9.2 外部依赖

- Stage-002 的 TaskManager、DownloadEngine、标准库测试基线。
- Windows Python 3、yt-dlp、FFmpeg 和可写 `downloads/`。
- 浏览器、Tampermonkey 和至少两个 YouTube 页面实例用于共享验证。
- 允许测试的 YouTube URL；不可用时使用假引擎完成局部验证并记录真实链路阻塞。

### 9.3 回滚策略

1. 开始前保存 Stage-002 42/42 基线和 `docs/stage002-migration.md`。
2. 先完成后端 Scheduler 和 API 回归，再切换 Userscript，避免前后端同时失去可运行入口。
3. 保留现有 `/status`、`/status?id=...` 和 `/download` 兼容路径，必要时可暂时关闭新列表入口而不回滚 Task 模型。
4. 若调度回归失败，恢复 `server.py` 直接调用 Stage-002 的兼容入口，保留新模块和测试供修复，不修改 Stage-002 记录。
5. 若前端列表验证失败，恢复单按钮显示作为临时入口，但不得继续声称已支持多任务共享 UI。
6. 任何改变 Task 字段、状态或现有 API 的方案，都必须先更新总计划和本阶段契约，再重新验收。

---

## 10. 阶段验收标准

### 10.1 调度功能验收

- [x] 每个请求创建独立 Task，Task ID 不重复。
- [x] 活动下载数始终不超过 3。
- [x] 第 4 个及之后任务进入 pending FIFO 队列。
- [x] 活动任务完成或失败后，等待任务自动补位。
- [x] 一个任务的失败不会阻塞后续任务，也不会修改其他任务状态。
- [x] 重复回调、线程异常和启动失败不会泄漏活动槽位。

### 10.2 API 验收

- [x] `GET /download?url=...` 继续返回独立 `task_id`。
- [x] `GET /status` 和 `GET /status?id=...` 保持 Stage-002 兼容。
- [x] 任务列表可返回完整 Task 或明确的列表摘要。
- [x] active、queued 和 limit 数值与调度器实际集合一致。
- [x] 错误响应继续使用 `{error_code,message}`，监听地址仍为 `127.0.0.1:8765`。

### 10.3 前端验收

- [x] 一个 YouTube 页面可创建并同时显示多个任务。
- [x] 两个 YouTube 页面能看到同一个后端任务集合。
- [x] 任务行按稳定 `task_id` 更新，排序变化不造成错绑。
- [x] pending、downloading、completed、error 状态显示明确。
- [x] SPA 导航后按钮和任务面板保持可用，T015 完成。
- [x] 默认 20 项、未完成优先、完成折叠和内部滚动规则符合总计划。
- [x] 窄屏下无明显溢出或遮挡。

### 10.4 质量与边界验收

- [x] Stage-002 全量回归和 Stage-003 新测试通过。
- [x] 新增 Python 文件通过语法检查。
- [x] 服务重启后不声称恢复旧任务或队列。
- [x] 未新增暂停、继续、取消、删除、重试或持久化能力。
- [x] 未改变默认下载格式、FFmpeg 合并策略和本地监听安全边界。
- [x] 执行记录完整包含真实网络、浏览器和模拟测试的差异。

### 10.5 进入 Stage-004 的条件

- [x] Scheduler 的活动集合、FIFO 队列和补位行为已冻结。
- [x] TaskManager 仍是唯一状态写入口。
- [x] 前端任务列表只依赖稳定 `task_id`，没有将 UI 索引当作任务身份。
- [x] 后续控制 API 的按钮和状态扩展位置已记录，但尚未伪装为可用。
- [x] 服务重启、内存增长和未完成任务清理限制已记录并交接给 Stage-005/006。
- [x] 阶段完成影响检查已完成。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-002 的影响

- Task 字段、基础状态机和 TaskManager 责任保持不变。
- Scheduler 只能调用 TaskManager 的创建、转换和回写接口，不能写入 `_tasks` 或直接修改 Task。
- DownloadEngine 的单任务执行边界保持不变；本阶段只增加调用编排。
- 如果为了排队需要增加字段，必须说明字段用途、序列化兼容性和对 Stage-002 测试的影响。

### 11.2 对 Stage-004 的输出

- Stage-004 使用 Scheduler 的活动集合和队列作为暂停、继续、取消的调度基础。
- Stage-004 必须扩展状态机后再提供控制 API；不能通过把 `status` 改成字符串来模拟暂停。
- 取消或暂停等待任务与运行中任务的行为需要分别定义，本阶段只保留 pending，不实现控制。
- 控制操作必须以 `task_id` 为目标，不得依赖前端列表索引。

### 11.3 对 Stage-005 的输出

- 当前 TaskStore 和队列是内存实现，服务重启后任务和队列丢失。
- 持久化必须能保存 Task 字段、状态、创建/完成排序字段，并明确恢复时如何处理曾经处于 downloading 的任务。
- 队列顺序必须有可持久化依据，不能仅依赖进程内线程对象。
- 实测限制：任务表与队列无容量上限，`MediaDock-server.log` 也无轮转（本阶段结束时已达 1.1 MB）。多任务场景下日志与内存增长都会加速，资源上限、历史清理和日志轮转留给 Stage-005/006 决定。

### 11.4 对后续阶段的不变约束

- Userscript 不直接运行 yt-dlp 或 FFmpeg。
- 不默认监听 `0.0.0.0`。
- 不改变默认 MP4 格式策略，除非单独记录影响并重新验收。
- 多任务列表显示规则由总计划统一定义，其他页面不得各自实现另一套排序或容量规则。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-19 | 阶段准备 | Cline | 已完成 | 核对 Stage-002 迁移说明、TaskManager/Scheduler 接口边界和前端单任务入口 | Stage-002 基线 42/42 通过 | 无 |
| 2026-09-19 | 任务 001 | Cline | 已完成 | 新增 `core_scheduler.py`：活动槽位上限 3、FIFO 队列、一次性启动/完成记录、补位、异常兜底 | `tests/test_scheduler.py` T301–T308 全通过 | 无 |
| 2026-09-19 | 任务 002 | Cline | 已完成 | `/download` 改走 `scheduler.submit()`；`download_video()` 降级为兼容入口；移除 `threading` 直接调用 | `test_listing.py` API 用例 + `probe_multi.py` 通过 | 无 |
| 2026-09-19 | 任务 003 | Cline | 已完成 | 新增 `core_listing.py`（唯一排序契约）+ `GET /tasks` 摘要接口 | 排序/计数用例全通过，多次查询结果稳定 | 无 |
| 2026-09-19 | 任务 004 | Cline | 已完成 | `MediaDock.js` 升到 v3.0：共享 `/tasks` 轮询、`data-task-id` 行身份、状态文案、20 行容量、完成折叠、SPA 保活 | 结构校验脚本通过（363 行、无模板字符串、锚点齐全、无未实现控制文案）；用户浏览器确认多任务与共享列表符合预期 | 无 |
| 2026-09-19 | 任务 005 | Cline | 已完成 | 记录重启丢队列、终态任务保留可查、槽位计数永不为负、异常路径日志 | 重启回归 + 计数断言通过 | 列表容量/历史清理留给 Stage-005/006 |
| 2026-09-19 | 任务 006 | Cline | 已完成 | 新增 3 个测试文件与 1 个探针；单元测试改为假引擎，不再启动真实 yt-dlp | `py_compile` rc=0；`unittest` 69/69 OK（1.8s，0 warning）；两个探针 rc=0 | 见第 13 节差异 |
| 2026-09-19 | 回归修复 | Cline | 已完成 | 修 `probe_chain.py` 的 `\r\r\n` 换行错误；`server.py` 用 `atexit` 关闭日志句柄；`drop_when_terminal()` 避免删除运行中任务 | 探针证据刷新，ResourceWarning 归零 | 无 |
| 2026-09-19 | 服务重启规范 | Cline | 已完成 | 每次验证前杀旧进程、用 venv 重启 `server.py`，再测 `/health` `/tasks` | 8765 正常，`/tasks` 计数与调度器一致 | 无 |
| 2026-09-19 | 真实链路验证 | 用户 | 已完成 | 浏览器点击下载真实 YouTube 视频，走新的 Scheduler 路径 | `GET /tasks` 返回该任务 `status=completed`、`percent=100.0`，且 `active_count=0`、`queued_count=0`（槽位已释放）；用户确认多任务与共享列表符合预期 | 无自动化脚本，属用户确认 |
| 2026-09-19 | 单实例加固 | Cline | 已完成 | 发现 `HTTPServer` 默认 `allow_reuse_address=True` 允许第二个进程绑定 8765；新增 `MediaDockServer(allow_reuse_address=False)`，绑定失败时 rc=2 并输出中文提示与排查命令 | socket 实验：默认配置对已监听端口 `BOUND (shared!)`；`allow_reuse_address=False` 被拒（WinError 10048）。第二次启动实测 rc=2 且日志出现 `cannot bind 127.0.0.1:8765` | 无 |
| 2026-09-19 | 最终验收 | Cline | 已完成 | 清理全部临时脚本后重跑全量验证，并用 venv 重启服务确认最终运行态 | `unittest` 69/69 OK（1.883s）；`probe_multi` OK（3 并发/2 排队/1 失败隔离）；`probe_chain` OK（rc=1→exit_code）；`check_userscript` OK（363 行）；8765 单一监听、`/health` 200、`/tasks` 空表 limit=3、错误码 missing_url/invalid_url/task_not_found/not_found 全部正确 | 重启后旧任务清空属预期 |

---

## 13. 实际输出与计划差异

开发完成后填写：

- 原计划输出：Scheduler、最多 3 个并发下载、FIFO 等待队列、任务列表 API/UI、多任务测试和 T015 验证。
- 实际输出：全部实现。`core_scheduler.py`（`Scheduler`，上限 3 + FIFO + 一次性补位）、`core_listing.py`（排序契约 + `/tasks` 摘要）、`server.py` 组装化、`MediaDock.js v3.0` 共享列表、`tests/test_scheduler.py` / `test_listing.py` / `helpers.py` / `probe_multi.py`，以及 `docs/stage003-migration.md`。
- 差异：
  1. **`probe_multi.py` 用脚本化进程对象替代真实进程 spawn。** 真实 spawn 无法与假 yt-dlp 共存：Windows `cmd.exe` 会把冻结的 `-f bv*[height<=1080]…` 里的 `<` 当成重定向，`.bat` 假工具因此拿不到参数、也不会输出进度行。该探针改为替换 `popen_factory`，其余链路（HTTP → Scheduler → DownloadEngine.run → parse_line → TaskManager）全部真实；真实 spawn + 非零退出仍由 `probe_chain.py` 覆盖，两者互补。
  2. **`probe_chain.py` 无法验证真实进度解析链。** 原因同上，已在文件头与 `docs/stage003-migration.md` 明确标注，进度解析由 `probe_multi.py` 与单元测试覆盖。
  3. **新增两个防御性错误码** `scheduler_error`（引擎抛异常）和 `scheduler_incomplete`（引擎结束但任务未落终态）。属原计划「线程异常不会永久占用活动槽位」的直接实现，不改 Task 字段。
  4. **`download_video(task_id, url)` 语义变化**：从「同步跑完一次下载」改为「交给 `scheduler.admit()`」，返回 True/False。保留函数名与参数以便回滚，但不再是线程体。
  5. **`MediaDock.js` 版本从 2.1 升到 3.0**，删除 `currentTaskId`。旧单任务显示逻辑被共享列表取代，冒号分隔的旧状态文案保留语义但前缀改为图标。
  6. **单元测试不再启动真实 yt-dlp**：`tests/helpers.py` 提供可替换引擎工厂。原 Stage-002 的 `test_apiv2.test_legacy_fields_present` 会真实调用 yt-dlp（依赖网络），现改为假引擎，符合本阶段 7.3「测试不依赖网络」。
  7. **`server.py` 增加 `atexit` 关闭日志句柄**，消除 unittest 的 `ResourceWarning`；另修复 `probe_chain.py` 的 `\r\r\n` 换行错误（Stage-001 遗留）。
  8. **`server.py` 新增 `MediaDockServer`（`allow_reuse_address=False`）**，超出原计划范围：排查「任务列表时有时无」时发现默认配置允许第二个进程绑定同一 `127.0.0.1:8765`，两个进程各持一份内存任务表。socket 实验证实默认配置能重复绑定，关闭复用以 rc=2 明确失败。该改动只影响「重复启动」这一异常路径，正常启动行为不变。
- 差异影响：差异 1/2 只是证据覆盖方式改变，不影响产品行为；差异 3/4 涉及 API 调用语义但仅影响内部兼容入口；差异 5 是前端 UI 重构，`@version` 变更会触发 Tampermonkey 更新提示；差异 6 让测试更快（4.5s → 1.8s）且离线可跑；差异 7 为资源与证据卫生；差异 8 只让重复启动由「静默共享端口」变成「明确失败」，属正确性修复。
- 处理决定：全部接受并已写入 `docs/stage003-migration.md` 与第 12 节执行记录。差异 1/2 不允许用模拟结果替代真实下载验证，因此真实 YouTube 多任务回归仍由用户浏览器完成并已确认；`T319` 保持「用户确认通过」而非自动化通过。另记录一项排查偏差：先前依据「两个 `python.exe` 进程」判断存在双实例，实际该现象多为 venv 启动器 stub + 子解释器（单一逻辑实例）；双实例风险由 socket 实验单独证实，与进程计数无关。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：允许进入 Stage-004
- 对 Stage-002 影响：Task 字段、状态机和 TaskManager 唯一写入口均未改变；`download_video()` 由线程体变为调度适配入口，`test_apiv2.py` 改用假引擎（不再依赖网络）
- 对 Stage-004 影响：`active_ids()`/`queued_ids()` 可作为暂停/取消基础；控制操作必须先扩状态机，不得用字符串改 `status` 模拟
- 对 Stage-005 影响：任务与队列仍为内存实现，重启即丢；持久化需保存状态与完成排序字段，并定义恢复时如何处理曾处于 `downloading` 的任务
- 是否更新 `plan-whole.md`：是，Stage-003 完成时间 `2026-09-19`（版本仍 `0.2`）
- 审查人：用户
- 日期：2026-09-19
