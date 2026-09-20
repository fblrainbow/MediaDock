# Stage-009：音频模式与 Media Processor

> 依据：[plan-whole.md](plan-whole.md) `0.2` §9 Stage-009、[Stage-008.md](Stage-008.md)、
> [docs/stage008-migration.md](docs/stage008-migration.md)
>
> 把「下载好的视频」变成音频成品（MP3/M4A/WAV），并让这次转换走与下载完全相同的
> Task 状态机、控制接口、并发上限、存储与历史。

---

## 1. 阶段元数据

- 阶段编号：`Stage-009`
- 阶段名称：音频模式与 Media Processor
- 总计划版本：`0.2`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-19
- 预计完成时间：2026-09-19
- 当前阻塞：无
- 前置阶段：`Stage-004`（控制/文件策略）、`Stage-005`（持久化）、`Stage-008`（音频预设）
- 后续阶段：`Stage-010` 发布、回归与长期扩展

---

## 2. 阶段目标

```text
POST /audio?target=mp3   body {"task_id": "<completed download task>"}
        |
        v
校验：task_id 形状 → 目标表 → 任务存在 → type=download → status=completed
        |
        v
core_media.find_source_file(download_dir, url)     (路径边界：只在下载目录内)
        |
        v
Scheduler.submit(url, platform, media_job=..., task_type="audio")
        |
        v
task_engine() -> AudioProcessor.run(task_id, media_job, control)
        |
        v
FFmpeg argv（常量编码参数 + 两个受控路径）-> downloading -> completed/error/cancelled
```

### 2.1 本阶段成功标准

- 视频可按固定目标生成音频文件；输出是真实可辨认的音频（MP3 有 ID3/帧同步，
  WAV 有 RIFF 头，M4A 有 `ftyp`），而不是空文件或错误扩展名。
- 原视频与输出音频的路径边界安全：两者都必须在 `download_dir` 内，
  显式 `source` 越界直接 400，且不启动任何进程。
- 失败不会留下「看起来已完成」的文件：只有 `returncode == 0` 且输出存在且非空
  才标记 `completed`；失败分支删除输出。
- 媒体处理任务与下载任务的状态/错误模型一致：同一状态集合、同一 `/tasks` 排序、
  同一 `/history` 与 `/events`、同一暂停/取消/重试语义。

---

## 3. 范围边界

### 3.1 本阶段包含

- 新增 `core_media.py`：目标表、路径解析、进度解析、磁盘检查、`AudioProcessor`。
- `server.py` 新增 `GET /audio`（目标列表）与 `POST /audio`（创建媒体任务）。
- `TaskManager.create(url, platform, task_type="download")` 与
  `Scheduler.submit/admit(media_job=..., task_type=...)`；
  `TaskControl.media_job`；`server.task_engine()` 分流。
- 测试：`tests/test_media.py`、`tests/probe_media.py`（真实 FFmpeg 端到端）。
- 文档：`docs/stage009-migration.md`。

### 3.2 本阶段明确不包含

- 音频裁剪、音量归一化、变速、字幕、缩略图、批量队列 UI。
- 修改 `Task` 字段、状态集合、API 错误格式、并发上限或排序规则。
- 新增第三方 Python 包（只用标准库 + 本机 FFmpeg 可执行文件）。
- 前端音频按钮（Userscript 仍是「下载 + 统一任务面板」；`/audio` 先作为
  已测契约交付，前端交互留给 Stage-010 的发布说明评估）。

---

## 4. 输入契约

### 4.1 前置阶段已确认输入

- `Task.type` 自 Stage-002 存在并持久化（`core_store.TASK_COLUMNS` 含 `type`）。
- `Task.file_path` 存在，`completed` 时可用于指向产物。
- `Scheduler` 负责槽位/FIFO/控制编排；`TaskControl` 承载运行期参数（Stage-008 已有 `format_expr`）。
- `core_files.is_inside()` / `discover_task_files()` / `video_id_from_url()` 是路径边界与文件归属的唯一真值。
- `core_engine.resolve_ffmpeg()` 给出 FFmpeg 位置；`--check-config` 已把它纳入依赖诊断。

### 4.2 保持不变的约束

- 无 `media_job` 的运行仍走 `DownloadEngine`，argv 与 Stage-008 完全一致。
- `Task` 字段与状态集合不变；`/status`、`/tasks`、`/history`、`/events` 形状不变。
- 不许任何 HTTP 输入进入 FFmpeg argv（只有目标名进入常量选择）。

---

## 5. 阶段契约

### 5.1 模块责任

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `core_media` | 目标表、路径解析、argv 构造、进度解析、一次转换 | HTTP、调度、Task 存储 |
| `server.Handler` | `/audio` 校验与错误码映射 | 拼接 FFmpeg 参数 |
| `core_scheduler` | 记录并投递 `media_job`，复用槽位与队列 | 执行转换 |
| `core_control` | 承载 `media_job`、进程句柄、暂停/取消意图 | 决定转换目标 |
| `core_engine` | 仍负责下载 argv | 音频转换 |

### 5.2 不变量

| 条件 | 必须成立 |
| --- | --- |
| 目标封闭 | `target` 必须命中 `TARGETS` 固定表；编码参数是常量 |
| 路径封闭 | 源与输出都在 `download_dir` 内；越界不启动进程 |
| 状态一致 | 媒体任务用与下载任务相同的状态集合与错误字段 |
| 无伪完成 | 只有 rc=0 且输出存在且非空才 `completed`；失败删除输出 |
| 兼容 | 无 `media_job` 的请求路径完全不变 |

### 5.3 HTTP 契约

| 场景 | 响应 |
| --- | --- |
| `GET /audio` | 200 `{targets, default, audio_type}` |
| `POST /audio` 成功 | 200 `{task_id, source_task_id, target, source}` |
| 缺/非法 `task_id` | 400 `missing_task_id` / `invalid_task_id` |
| 未知 `task_id` | 404 `task_not_found` |
| 源任务未完成 / 不是下载任务 | 409 `not_completed` / `not_a_download_task` |
| `target` 不在表内 | 400 `invalid_audio_target` |
| 源文件缺失 / 越界 | 404 `source_not_found` / 400 `source_outside_download_dir` |

新增错误码：`invalid_audio_target`(400)、`not_a_download_task`(409)、
`source_not_found`(404)、`source_outside_download_dir`(400)。
任务级错误码：`ffmpeg_missing`、`ffmpeg_failed`、`output_missing`、`insufficient_space`。
既有错误码不变。
