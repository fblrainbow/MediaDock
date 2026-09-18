# Stage-002 迁移说明：Task 核心模型与后端边界

> 日期：2026-09-19；`server.py` 已从单体实现改为组装入口。

## 新模块
- `core_task.py`：Task 字段/默认值/ID/时间/序列化；`legacy_view()` 保留 Stage-001 字段子集。
- `core_manager.py`：TaskManager（创建/查询/状态机/进度回写/并发锁）；终态 `completed`/`error` 不可再转。
- `core_parse.py`：纯解析 `parse_line()` → progress/merging/title/ignored 事件。
- `core_engine.py`：`build_command()`（格式策略冻结）+ `DownloadEngine.run()`（pending→downloading→completed/error）。

## server.py 职责
- 只做：参数校验 → `manager.create()` → 起线程 `download_video()` → JSON 响应。
- 状态写入唯一入口：TaskManager；Handler 不再直接写任务字典。
- 错误统一：`{error_code, message}`（+ task 相关时带 `task_id`）；HTTP 状态码保持 Stage-001。
- 直接调用兼容：`download_video(task_id, url)` 先 `get_or_create()` 再 `engine.run()`。
- 兼容导出：`tasks`/`tasks_lock` 指向 manager 内部；`PROGRESS_RE`/`MERGE_RE`/`resolve_ytdlp` 由 core 模块重导出语义。

## Stage-003 可用边界
- `manager.create(url)`、`manager.get(id)`、`manager.all()`、`manager.transition(...)`、
  `report_progress/report_merging/report_title`。
- 不得重定义 Task 字段或绕过状态机加队列/控制；持久化替换时必须兼容本阶段序列化结构。

## 回滚
- 回滚点：Stage-001 已签字基线（`docs/stage001-baseline.md` + 15 项旧测试）。
- 若 API/真实下载回归失败，恢复到本次迁移前 `server.py`，保留 core 模块不动；不修改 Stage-001 快照。
