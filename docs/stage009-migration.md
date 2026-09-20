# Stage-009 迁移说明：音频模式与 Media Processor

> 日期：2026-09-19；「只下载视频」升级为「下载后可把成品转成音频」。
> 媒体处理复用同一 Task 状态机、控制接口、存储与历史，不引入第二套任务模型。
> 回滚点：Stage-008 已签字基线（288/288 单元测试 + 7 个探针 + JS 结构检查）。

## 新模块（纯标准库 + 本机 FFmpeg）

- `core_media.py`
  - `TARGETS` / `TARGET_BY_NAME` / `target_names()` / `targets_public()`：
    固定顺序 `mp3, m4a, wav`；`DEFAULT_TARGET = "mp3"`。
  - `AudioTarget(name, label, extension, codec_args)`。
  - `resolve_target(value) -> (target, error_code, message)`：空值 → `mp3`；
    名字大小写/空白归一化；表外值 → `invalid_audio_target`。
  - `build_ffmpeg_command(ffmpeg, source, destination, target)`：唯一生成 FFmpeg
    argv 的地方；`-hide_banner -nostdin -y -i <src> -vn <codec> -progress pipe:1
    -nostats <dst>`，无 shell。
  - `output_path_for(source, target)`：同目录同主名 + 目标扩展名
    （因此输出仍带 `[<video id>]` 标记，Stage-004 的取消清理规则继续生效）。
  - `find_source_file(download_dir, url, explicit)`：显式路径必须位于下载目录内，
    否则取该视频 id 最新的非临时文件。
  - `parse_duration_seconds()` / `parse_media_line()` / `MediaEvent`：
    解析 FFmpeg 合并输出（`Duration:` + `-progress pipe:1` 的
    `out_time_ms` / `progress=`）。
  - `has_free_space(directory, needed, reserve=64MiB)`：转换前磁盘检查。
  - `AudioProcessor(manager, ffmpeg, download_dir, popen_factory, logger)`：
    `run(task_id, media_job, control)`，永不抛异常。
  - 错误码：`invalid_audio_target`(400)、`source_not_found`(404)、
    `source_outside_download_dir`(400)、`ffmpeg_missing`、
    `ffmpeg_failed`、`output_missing`、`insufficient_space`。

## 音频目标表

| 目标 | 扩展名 | FFmpeg 编码参数 |
| --- | --- | --- |
| `mp3`（默认） | `.mp3` | `-c:a libmp3lame -q:a 2` |
| `m4a` | `.m4a` | `-c:a aac -b:a 192k` |
| `wav` | `.wav` | `-c:a pcm_s16le` |

- 目标名是唯一允许从 HTTP 传入的转换参数；编码参数是常量。
- 源文件与输出文件都必须在 `download_dir` 内（`core_files.is_inside` 校验）。

## HTTP 契约

```text
GET /audio                      -> 200 {"targets":[...], "default":"mp3", "audio_type":"audio"}
POST /audio?target=mp3          -> body {"task_id": "<已完成的下载任务>"}
                                -> 200 {"task_id","source_task_id","target","source"}
```

| 场景 | 结果 |
| --- | --- |
| `GET /audio` | 200，只列固定目标表，不执行任何转换 |
| `POST /audio` 缺 `task_id` | 400 `missing_task_id` |
| `task_id` 形状非法 | 400 `invalid_task_id` |
| 未知 `task_id` | 404 `task_not_found` |
| 源任务不是下载任务（`type != download`） | 409 `not_a_download_task` |
| 源任务不是 `completed` | 409 `not_completed` |
| `target` 不在表内 | 400 `invalid_audio_target` |
| 找不到可转换的文件 | 404 `source_not_found` |
| `source` 指向下载目录之外 | 400 `source_outside_download_dir` |
| 合法请求 | 200，创建一个 `type="audio"` 的新 Task |

`target` 可以放在查询串（`?target=m4a`）或 JSON body 里；查询串优先。这样
Userscript 可以复用 Stage-004 的「POST + `{task_id}`」控制通道。

## 媒体任务如何运行

- 新 Task 用 `TaskManager.create(url, platform, task_type="audio")` 创建：
  `type` 字段自 Stage-002 就存在，**没有 schema 变更**。
- URL 沿用源下载任务的视频 URL，因此 `video_id_from_url()` 仍能定位本次产生的
  音频/临时文件，取消清理（D-009）行为与下载任务一致。
- 调度器新增发送期记录 `_jobs[task_id]`（`{"kind":"audio","source":..,"target":..}`），
  由 `TaskControl.media_job` 交给运行体；与 Stage-008 的 `format_expr` 同一手法。
- `server.task_engine()` 按 `control.media_job["kind"]` 分流：
  `audio` → `AudioProcessor`，其他 → `DownloadEngine`。因此并发上限、FIFO 队列、
  暂停/继续/取消/重试、进度上报、`/tasks` 排序与 `/history` 完全复用。
- 状态流与下载任务一致：`pending → downloading → completed/error/cancelled/paused`；
  进度用 `percent`，完成后写入 `file_path`（Stage-002 起就存在的字段）。

## 失败与清理策略

| 场景 | 行为 |
| --- | --- |
| 源文件缺失/在目录外 | 创建任务前 400/404，不启动进程 |
| FFmpeg 未配置 | Task `error/ffmpeg_missing`，不留下任何输出 |
| FFmpeg 返回非 0 | Task `error/ffmpeg_failed`，**删除**本次输出文件 |
| FFmpeg 返回 0 但没有输出 | Task `error/output_missing` |
| 磁盘空间不足 | Task `error/insufficient_space`（转换前检查） |
| 暂停 | `paused`，保留半成品（重试复用） |
| 取消 | `cancelled`，删除本次输出文件 |

「失败不会留下误标记为完成的文件」由「成功分支只认 `returncode == 0` 且输出存在且
非空」+「失败分支删除输出」共同保证。

## 兼容性

- `Task` 字段、状态集合、排序、并发上限、控制 API、存储 schema、配置层、
  平台检测、格式选择与文件边界全部不变。
- `/audio` 是新路由；既有路由行为不变。`POST /audio` 的 409/404 语义与
  `/pause`、`/delete` 保持一致。
- 既有用例无需改动：默认下载路径不经过任何新代码。

## 回滚

1. 代码：删除 `/audio` 路由、`task_engine()` 的 `audio` 分支与 `core_media.py`；
   `Scheduler.submit/admit` 的 `media_job` 默认 `None`，行为回到 Stage-008。
2. `TaskManager.create()` 的 `task_type` 默认值保证旧调用不变。
3. 数据：无 schema 变化、无迁移、无备份需求；已产生的音频文件只是普通下载文件。
