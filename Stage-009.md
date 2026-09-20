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

---

## 6. 具体任务

### 任务 001：媒体模型、目标表与 FFmpeg argv（`core_media.py`、`tests/test_media.py`）

- [x] 固定目标表 `mp3`（默认）/`m4a`/`wav`，编码参数是常量，`target` 是唯一可从
      HTTP 传入的转换参数。
- [x] `build_ffmpeg_command()` 是 argv 唯一构造点：`shell=False`，源与输出各是一个参数。
- [x] `find_source_file()` 只接受下载目录内的文件；显式 `source` 越界直接拒绝。
- [x] `output_path_for()` 与原文件同目录同主名 + 目标扩展名，保留 `[<video id>]` 标记。

### 任务 002：转换执行与失败清理（`AudioProcessor`）

- [x] `run(task_id, media_job, control)` 永不抛异常，失败一律落 `error` + 错误码。
- [x] 只有 `returncode == 0` 且输出存在且非空才 `completed`；失败分支删除本次输出。
- [x] 转换前做磁盘空间检查（`has_free_space`，保留量 64MiB）。
- [x] 暂停保留半成品、取消删除本次输出，与 Stage-004 的文件策略一致。

### 任务 003：HTTP 接入与调度穿透

- [x] `server.py` 新增 `GET /audio`（只列目标表）与 `POST /audio`（创建媒体任务）。
- [x] `TaskManager.create(..., task_type=)`、`Scheduler.submit/admit(media_job=, task_type=)`、
      `TaskControl.media_job`、`server.task_engine()` 分流（按 `media_job["kind"]`）。
- [x] 媒体任务复用同一状态机、并发槽位、FIFO 队列、控制接口、`/tasks` 排序与 `/history`。
- [x] 不新增 Task 字段、不改存储 schema（仍 v2）。

### 任务 004：探针、证据与文档

- [x] `tests/probe_media.py`：真实 FFmpeg 端到端（伪造/真实损坏输入、路径越界、缺 FFmpeg）。
- [x] `tests/probe_media_result.json` 记录 25 项检查结果。
- [x] `docs/stage009-migration.md` 记录新模块、目标表、HTTP 契约、失败与清理策略、回滚。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| T901 | 目标表顺序与字段 | 单元 | `mp3, m4a, wav`；默认 `mp3` |
| T902 | `resolve_target` 归一化 | 单元 | 空值→默认；大小写/空白归一化；表外→`invalid_audio_target` |
| T903 | FFmpeg argv 形状 | 单元 | 常量编码参数、`-progress pipe:1`、无 shell、路径各为一个参数 |
| T904 | 输出路径策略 | 单元 | 同目录同主名 + 目标扩展名，保留 `[video id]` |
| T905 | 源文件路径边界 | 单元 | 越界 `source` 返回 `source_outside_download_dir`，不启动进程 |
| T906 | 时长与进度解析 | 单元 | `Duration:` 与 `out_time_ms`/`progress=` 正确解析 |
| T907 | 成功路径 | 单元 | `rc=0` 且输出非空 → `completed`，`file_path` 写入输出 |
| T908 | 三种目标扩展名 | 单元 | `.mp3`/`.m4a`/`.wav` 各自生成对应文件 |
| T909 | 失败不留伪成品 | 单元 | `rc≠0` 或输出缺失 → `error`，输出文件被删除 |
| T910 | 缺 FFmpeg / 源缺失 / 取消 / 暂停 | 单元 | 对应错误码；取消与暂停不产生 `completed` |
| T911 | `/audio` 目标列表 | API | 200 `{targets, default, audio_type}` |
| T912 | 源任务校验 | API | 未完成 409 `not_completed`；非下载任务 409 `not_a_download_task`；未知 404 |
| T913 | 转换链路与目标变体 | API | 200 创建 `type="audio"` 任务；三种目标均可用 |
| T914 | 真实 FFmpeg 端到端 | 探针 | `probe_media_result.json` 25 项全部通过，产出 2 个可识别音频文件 |

### 7.2 执行命令

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_media.py core_scheduler.py core_control.py core_manager.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_media.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\check_userscript.py
```

### 7.3 测试原则

- FFmpeg 进程边界可注入（`popen_factory` / `server.MEDIA_POPEN_FACTORY`），
  单元与 API 测试不依赖本机 FFmpeg。
- 探针使用**真实 FFmpeg**，并对「损坏输入」验证失败路径不留下伪成品。
- 路径边界用例必须断言「没有启动进程」，而不是只断言错误码。
- 媒体任务与下载任务共用同一套状态/排序断言，不做特例。

---

## 8. 阶段产物

- `core_media.py`（+362）：目标表、`resolve_target`、`build_ffmpeg_command`、
  `output_path_for`、`find_source_file`、`parse_media_line`、`has_free_space`、
  `AudioProcessor`、4 个 HTTP 错误码与 4 个任务级错误码。
- `server.py`（+101）：`GET /audio`、`POST /audio`、`audio_action()`、
  `audio_targets_info()`、`task_engine()` 分流、`MEDIA_POPEN_FACTORY` /
  `MEDIA_PROCESSOR_FACTORY` 测试缝。
- `core_scheduler.py`（+25）：`submit/admit(media_job=, task_type=)` 与发送期记录。
- `core_control.py`（+7）：`media_job`。
- `core_manager.py`（+6）：`create(..., task_type=)`。
- `tests/test_media.py`（+443，20 用例）、`tests/probe_media.py`（+274）、
  `tests/probe_media_result.json`（+46，25 项检查）。
- `docs/stage009-migration.md`（+109）、`Stage-009.md`。
- 提交：`545d781 Stage-009 completed`（17 文件，+1541/−51）。

---

## 9. 风险与回滚

### 9.1 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| HTTP 文本进入 FFmpeg argv | 命令注入 | 目标名必须命中固定表；编码参数是常量；路径经 `core_files.is_inside` 校验 |
| 源文件越界读/写 | 读到下载目录外文件 | 显式 `source` 越界 400，且不启动进程；输出路径由源路径派生 |
| 失败留下「看起来完成」的文件 | 用户误以为转换成功 | 成功只认 `rc=0` + 输出存在且非空；失败分支删除输出 |
| 磁盘写满 | 半成品与误导状态 | 转换前 `has_free_space`（保留 64MiB）→ `insufficient_space` |
| FFmpeg 缺失/版本差异 | 任务失败 | `ffmpeg_missing` 错误码 + `--check-config` 已纳入 FFmpeg 诊断 |
| 音频任务抢占下载槽位 | 下载变慢 | 与下载共用同一并发上限与 FIFO 队列（有意为之，保证一致性） |

### 9.2 外部依赖

- 仅标准库（`os`、`subprocess`、`re`、`shutil`）+ 本机 FFmpeg 可执行文件。
- 无新增第三方 Python 包，`requirements.txt` 未变。

### 9.3 回滚策略

1. 删除 `/audio` 路由与 `task_engine()` 的 `audio` 分支，移除 `core_media.py`
   → 行为回到 Stage-008（`/audio` 404）。
2. `Scheduler.submit/admit` 的 `media_job` 默认 `None`；`TaskManager.create()` 的
   `task_type` 默认 `"download"`，旧调用不变。
3. 无数据回滚：无 schema 变更、无迁移；已产生的音频文件只是普通下载文件。

---

## 10. 阶段验收标准

### 10.1 功能验收

- [x] 视频可按选择格式生成目标音频，输出是真实可辨认的音频（MP3 帧同步、WAV RIFF 头、
      M4A `ftyp`），不是空文件或错误扩展名。
- [x] 原视频与输出音频的路径边界安全：两者都在 `download_dir` 内；显式 `source`
      越界直接 400 且不启动任何进程。
- [x] 失败不会留下「看起来已完成」的文件：只有 `rc=0` 且输出存在且非空才 `completed`。
- [x] 媒体处理任务与下载任务的状态、错误模型、排序、历史与事件流一致。

### 10.2 测试与质量验收

- [x] `tests/test_media.py` 20 用例通过（T901-T913）。
- [x] 全量单元测试通过：`Ran 308 tests ... OK`（Stage-008 基线 288 + 本次 20，无回归）。
- [x] `tests/probe_media.py` OK：`checks=25`，真实 FFmpeg 产出 2 个音频文件。
- [x] 既有 7 个探针全部通过（chain/control/multi/persist/security/platform/formats）。
- [x] `py_compile` 通过；`tests/check_userscript.py` 通过（本阶段未改用户脚本）。
- [x] 无新增第三方依赖。

### 10.3 进入 Stage-010 的条件

- [x] 媒体任务已具备进度、状态与历史记录，可由 `/tasks` 与 `/history` 查询。
- [x] 已知限制（前端无音频按钮、输出与源同目录、无裁剪/归一化）已记录，
      交由 Stage-010 写入 `docs/known-limitations.md`。
- [x] `config.example.json`、`/health`、错误码契约未变化，发布阶段可直接回归。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-001/002 的影响

- Task 字段、状态机、`/download`/`/status` 形状不变；`type` 字段沿用 Stage-002 定义。
- `TaskManager.create()` 新增可选 `task_type`，默认值保证旧调用零变化。

### 11.2 对 Stage-003/004/005/006/007/008 的影响

- 并发上限、FIFO 队列、控制接口、文件清理策略、存储 schema、配置层、安全守卫、
  平台检测与格式选择全部不变；无 `media_job` 的运行路径与 Stage-008 完全一致。

### 11.3 对 Stage-010 的输出

- 发布阶段需把「视频转音频」写入变更日志与能力表，并把下列限制写入文档：
  前端不触发音频转换、输出与源文件同目录、只支持三种固定目标。
- 发布检查需覆盖 `/audio` 两条路由的形状与错误码。

### 11.4 不变约束

- 不修改 Task 字段、状态集合、API 错误格式与文件策略。
- 不允许任何 HTTP 输入进入 FFmpeg argv。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-19 | 阶段准备 | Cline | 已完成 | 核对 Stage-008 输出（`Task.type`、`TaskControl` 运行期参数、`core_files` 路径边界），冻结目标表与错误码 | Stage-008 基线 288/288 单测 + 7 个探针 | 无 |
| 2026-09-19 | 任务 001 | Cline | 已完成 | 新增 `core_media.py`：目标表、`resolve_target`、`build_ffmpeg_command`、`output_path_for`、`find_source_file`、进度解析 | T901-T906 通过 | 无 |
| 2026-09-19 | 任务 002 | Cline | 已完成 | `AudioProcessor`：成功只认 `rc=0`+输出非空，失败删除输出，磁盘预检，暂停/取消语义 | T907-T910 通过 | 无 |
| 2026-09-19 | 任务 003 | Cline | 已完成 | `/audio` 两条路由；`task_type`/`media_job` 穿透 Scheduler 与 Control；`task_engine()` 分流 | T911-T913 通过；无 `media_job` 路径 argv 不变 | 无 |
| 2026-09-19 | 任务 004 | Cline | 已完成 | `probe_media.py` 真实 FFmpeg 端到端；`docs/stage009-migration.md`；计划状态更新 | `checks=25` 全部通过，产出 2 个音频文件 | 无 |
| 2026-09-20 | 验收与提交 | Cline | 已完成 | 复跑全量回归并提交 `545d781 Stage-009 completed` | 308/308 单测 + 8 探针 + JS 结构检查 | 无 |

---

## 13. 实际输出与计划差异

- 原计划输出：媒体处理 Task 或统一 Task type、Media Processor API、MP3/M4A/WAV、
  失败场景验证、进度与历史。
- 实际输出：全部实现，另加两点证据化产物：
  1. `tests/probe_media.py` 用**真实 FFmpeg** 端到端验证（25 项检查，含损坏输入与越界路径）。
  2. 明确的 4 个 HTTP 错误码与 4 个任务级错误码清单，便于发布阶段回归。
- 差异：
  1. **不新增 Task 字段、不改 schema**：媒体任务用 `type="audio"` 表达（该字段自
     Stage-002 就存在），转换参数走发送期记录 `Scheduler._jobs` + `TaskControl.media_job`，
     与 Stage-008 的 `format_expr` 同一手法。
  2. **目标名是唯一可从 HTTP 传入的转换参数**：编码参数是服务端常量，比计划更严格。
  3. **输出与源文件同目录同主名**：保证输出仍在 `download_dir` 内，并让 Stage-004 的
     取消清理规则继续生效。
  4. **前端不提供音频按钮**：`/audio` 先作为已测 API 契约交付，交互留给 Stage-010
     评估；Stage-010 复核后仍保持 API-only，并写入已知限制。
- 差异影响：均为安全收紧与范围澄清，不改变 Task 字段、状态机、存储 schema 与既有 API 形状。
- 处理决定：全部接受并写入 `docs/stage009-migration.md`；计划中 Stage-009 完成时间
  记为 `2026-09-19`（实现）并于 `2026-09-20` 完成验收提交。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：允许进入 Stage-010
- 对 Stage-001..008 影响：无回改；Task 字段、状态机、并发/文件策略、存储 schema、
  配置层、安全守卫、平台检测与格式选择均未变化；既有 288 个单测与 7 个探针全部回归通过
  （迁移后全量 308/308）
- 对 Stage-010 影响：发布说明需写明「音频转换是 API 能力、前端暂无按钮」「输出与源文件
  同目录」与三种固定目标；发布检查需覆盖 `/audio`
- 是否更新 `plan-whole.md`：是，Stage-009 完成时间 `2026-09-19`，变更记录 C-008
- 审查人：用户
- 日期：2026-09-20
