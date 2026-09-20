# Stage-012：仅音频预设产出 MP3

> 依据：[plan-whole.md](plan-whole.md) §12 `D-019`、[Stage-008.md](Stage-008.md)（预设表与 argv）、
> [Stage-009.md](Stage-009.md)（`/audio` 媒体处理）
>
> 用户反馈：选「仅音频」下载得到的是 `.webm`（Opus），不是 MP3。

---

## 1. 阶段元数据

- 阶段编号：`Stage-012`
- 阶段名称：仅音频预设产出 MP3
- 总计划版本：`0.5`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-20
- 完成时间：2026-09-20
- 当前阻塞：无
- 前置阶段：`Stage-008`（预设表/选择器单一真值）、`Stage-009`（FFmpeg 音频能力）、
  `Stage-010`（发布门禁与文档体系）
- 后续阶段：无

---

## 2. 阶段目标

```text
GET /download?url=...&preset=audio
        |
        v
core_formats.audio_format_for(preset)  -> "mp3"   （只有 audio 预设非空）
        |
        v
Scheduler.submit(..., audio_format="mp3")  ->  TaskControl.audio_format
        |
        v
core_engine.build_command(..., audio_format="mp3")
        |
        +--> argv: -f bestaudio/best -x --audio-format mp3 --audio-quality 0 ...
        |
        v
yt-dlp 下载最佳音频流 -> FFmpeg 提取 -> <title> [<video id>].mp3
        |
        v
core_parse 识别 `[ExtractAudio] Destination:` -> 记录最终产物 -> completed
```

其他预设（`best`/`1080p`/`720p`/`480p`）的 argv **完全不变**（仍 `--merge-output-format mp4`）。

### 2.1 本阶段成功标准

- 选「仅音频」得到的文件是真实 `.mp3`（不是 `.webm`/`.m4a`/`.opus`）。
- 默认路径零变化：不带 `preset` 或选视频预设时，argv 与 Stage-001 冻结策略逐字一致。
- 用户文本仍无法进入 argv：容器名 `mp3` 是 `core_formats` 的常量，来自固定预设表。
- 最终产物被正确记录（取消时能连 `.mp3` 一起清理）。
- Task 字段、状态机、存储 schema、API 错误码、排序与文件策略全部不变。

---

## 3. 范围边界

### 3.1 本阶段包含

- `core_formats`：`Preset.audio_format`、`AUDIO_PRESET_FORMAT = "mp3"`、
  `audio_format_for()`；`audio` 预设标签改为 `Audio only (MP3)`。
- `core_engine.build_command(..., audio_format="")`：非空时用
  `--extract-audio --audio-format <container> --audio-quality 0` 取代 `--merge-output-format mp4`。
- `core_parse`：识别 `[ExtractAudio] Destination: <path>`，作为「最终产物」事件。
- `core_control.TaskControl.audio_format`、`core_scheduler` 发送期记录与
  `audio_format_for(task_id)`；`server.resolve_format_choice()` 返回容器名并透传。
- `MediaDock.js`：预设标签 `仅音频 (MP3)`；FFmpeg 阶段文案「合并中」→「处理中」
  （视频合并与音频提取共用同一状态文案）。
- 测试：`tests/test_formats.py`、`tests/test_engine.py`、`tests/probe_formats.py`。

### 3.2 本阶段明确不包含

- 前端选择 m4a/wav 或码率（要别的容器仍用 `POST /audio`）。
- 改动 `core_media.AudioProcessor` / `POST /audio`（Stage-009 能力保持不变）。
- 新增 Task 字段或 schema（容器名仍走发送期记录，不落库）。
- 让「仅音频」跳过 `/formats` 探测（非默认预设仍需先探测，行为不变）。
- 修改视频预设的合并策略与默认 1080p MP4 策略。

---

## 4. 输入契约

### 4.1 前置阶段已确认输入

- `Stage-008`：`PRESETS` 是唯一预设真值；`selector_for()` 只由常量生成 `-f`；
  非默认预设必须先 `/formats` 且通过 `preset_satisfied()`。
- `Stage-009`：FFmpeg 是音频处理执行者；`ffmpeg_missing`/`ffmpeg_failed` 等错误码已存在；
  依赖诊断已覆盖 FFmpeg。
- `Stage-010`：发布门禁校验版本与文档一致性；`docs/known-limitations.md` 记录边界。
- `Stage-011`：`--restart`/接管不影响任务语义（本阶段不涉及）。

### 4.2 保持不变的约束

- 默认与视频预设 argv 逐字不变（探针 `default_expression` 继续断言）。
- Task 字段、状态集合、存储 schema（v2）、API 形状与错误码不变。
- HTTP 输入不得进入 argv：容器名只能取自固定预设表。

---

## 5. 阶段契约

### 5.1 模块责任

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `core_formats` | 声明 `audio` 预设需要转成哪个容器 | 拼装 argv |
| `core_engine` | 按 `audio_format` 选择「提取音频」或「合并 MP4」argv | 决定容器 |
| `core_parse` | 把 `[ExtractAudio]` 行解析为最终产物事件 | 决定容器 |
| `core_scheduler` / `core_control` | 发送期记录并传递给引擎 | 解析或校验容器 |
| `server` | 用 `resolution_format_choice` 把预设映射为 `(expr, container)` | 拼装 argv |
| `MediaDock.js` | 展示 `仅音频 (MP3)` 与「处理中」 | 生成任何 yt-dlp 参数 |

### 5.2 不变量

| 条件 | 必须成立 |
| --- | --- |
| 容器封闭 | 容器名只能来自 `core_formats` 常量（`mp3`） |
| 默认不变 | `audio_format` 为空时 argv 与 Stage-001 冻结策略逐字一致 |
| 二选一 | argv 中不会同时出现 `--extract-audio` 与 `--merge-output-format` |
| 无 schema 变更 | 容器名不写 Task、不落库；重启后重试回落默认（既有已知限制） |
| 产物可清理 | 最终 `.mp3` 走 `[ExtractAudio]` 事件进入 artifacts，取消时一并删除 |

### 5.3 接口契约

| 入口 | 变化 |
| --- | --- |
| `GET /download?...&preset=audio` | argv 变为提取 MP3；响应形状不变（`{"task_id": ...}`） |
| `GET /download`（无 preset / 视频预设） | 完全不变 |
| `GET /formats` | `presets[].audio_format` 为新增字段（`audio` 为 `"mp3"`，其余为 `""`） |
| `POST /audio` | 不变 |

新增错误码：无。

---

## 6. 具体任务

### 任务 001：预设与容器真值（`core_formats.py`）

- [x] `Preset` 增加 `audio_format: str = ""`，`to_dict()` 输出该字段。
- [x] `AUDIO_PRESET_FORMAT = "mp3"`；`audio` 预设标签 `Audio only (MP3)`。
- [x] `audio_format_for(preset, format_id)`：显式 `format_id` 时不转码。

### 任务 002：argv 与解析（`core_engine.py`、`core_parse.py`）

- [x] `build_command(..., audio_format="")`：非空走 `-x --audio-format <c> --audio-quality 0`，
      不空走 `--merge-output-format mp4`（互斥）。
- [x] 引擎记录 `audio: extract to mp3` 日志。
- [x] `EXTRACT_PATH_RE` 识别 `[ExtractAudio] Destination:` → `merged` 事件（最终产物）。

### 任务 003：穿透与前端（`core_control`/`core_scheduler`/`server`/`MediaDock.js`）

- [x] `TaskControl.audio_format`；`Scheduler._audio` + `audio_format_for()`；
      `submit/admit(..., audio_format=)`；`_run` 写入 control。
- [x] `resolve_format_choice()` 返回 `(expression, audio_format, code, message)`。
- [x] 前端标签 `仅音频 (MP3)`、「合并中」→「处理中」。

### 任务 004：测试、证据与文档

- [x] `tests/test_formats.py`（预设表字段、audio 预设记录 mp3、视频预设不转码、
      `format_id` 不隐式转码）；`tests/test_engine.py`（argv 三分支 + `[ExtractAudio]` 解析）。
- [x] `tests/probe_formats.py` 增加 audio 预设真实 argv 断言（30 → 38 项检查）。
- [x] 版本 `1.0.1 → 1.0.2`、`docs/stage012-migration.md`、release-notes/known-limitations/
      userscript/README 与 `plan-whole.md` 更新。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| T1201 | 预设表字段与容器真值 | 单元 | `audio` 的 `audio_format="mp3"`，其余为 `""` |
| T1202 | `audio_format_for` | 单元 | 仅 audio 预设返回 `mp3`；显式 `format_id` 返回 `""` |
| T1203 | argv 分支 | 单元 | `audio_format` 非空 → `-x --audio-format mp3 --audio-quality 0`，无 `--merge-output-format` |
| T1204 | 默认路径不变 | 单元 | 空/空白/None → 仍 `--merge-output-format mp4`，无 `--extract-audio` |
| T1205 | `[ExtractAudio]` 解析 | 单元 | 记录最终 `.mp3` 路径（kind=`merged`） |
| T1206 | API 记录容器 | API | `/download?preset=audio` 后 `scheduler.audio_format_for(id) == "mp3"` |
| T1207 | 视频预设不转码 | API | `best`/`720p` 的 `audio_format_for` 为空 |
| T1208 | `format_id` 不隐式转码 | API | `preset=audio&format_id=137` → `audio_format_for` 为空 |
| T1209 | 真实 argv | 探针 | `probe_formats_result.json` 38/38（含 `audio_argv_*`） |

### 7.2 执行命令

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_formats.py core_engine.py core_parse.py core_scheduler.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_formats.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\release_check.py
```

### 7.3 测试原则

- argv 断言必须在**真实子进程边界**（`popen_factory`）上取，而不是只测纯函数。
- 每个 `--extract-audio` 断言都要同时断言「没有 `--merge-output-format`」（互斥）。
- 默认路径必须有专门用例（T1204），防止「只测新增分支」漏掉回归。

---

## 8. 阶段产物

- `core_formats.py`：`AUDIO_PRESET_FORMAT`、`Preset.audio_format`、`audio_format_for()`。
- `core_engine.py`：`build_command(..., audio_format=)`、audio 日志。
- `core_parse.py`：`EXTRACT_PATH_RE` + 解析分支。
- `core_control.py` / `core_scheduler.py`：`audio_format` 发送期记录与传递。
- `server.py`：`resolve_format_choice()` 返回容器并透传 `/download`。
- `MediaDock.js` 5.2（标签与文案）+ `tests/check_userscript.py` 锚点。
- `tests/test_formats.py`、`tests/test_engine.py`、`tests/probe_formats.py`（38 项）与结果文件。
- `docs/stage012-migration.md`、`docs/release-notes.md`、`docs/known-limitations.md`、
  `docs/userscript.md`、`README.md`、`plan-whole.md`；版本 `1.0.2`。

---

## 9. 风险与回滚

### 9.1 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 误改默认/视频预设 argv | 下载回归 | 互斥分支 + T1204/T1209 显式断言默认路径 |
| 用户文本进入 argv | 命令注入 | 容器名来自常量表；`format_id` 仍走原有校验 |
| FFmpeg 缺失导致仅音频失败 | 任务报错 | 依赖诊断提前报 `ffmpeg_missing`；错误码已存在 |
| 最终 `.mp3` 未被记录 | 取消后残留文件 | `[ExtractAudio]` 解析为最终产物事件（T1205/T1209） |
| 老用户以为 M4A/WAV 也能在前端选 | 体验落差 | `docs/known-limitations.md` 明确「仅音频固定 MP3」 |

### 9.2 外部依赖

- yt-dlp 的 `-x` 能力（需本机 FFmpeg；`--ffmpeg-location` 已在配置时传入）。
- 无新增 Python 第三方依赖。

### 9.3 回滚策略

1. 代码：把 `audio` 预设的 `audio_format` 置空（或删掉该字段与 `audio_format_for`），
   `build_command` 恢复「总是 `--merge-output-format mp4`」→ 行为回到 Stage-008/1.0.1。
2. 测试/文档：回滚对应用例与 release-notes 条目；版本回退 `1.0.2 → 1.0.1`。
3. 数据：无 schema 变更、无迁移，无需数据回滚。

---

## 10. 阶段验收标准

### 10.1 功能验收

- [x] 选「仅音频」产出真实 `.mp3`（argv 为 `-x --audio-format mp3 --audio-quality 0`）。
- [x] 默认与视频预设 argv 逐字不变（`--merge-output-format mp4`）。
- [x] 容器名只来自固定预设表常量；HTTP 文本仍无法进入 argv。
- [x] 最终 `.mp3` 被记录为本次运行产物（取消时可清理）。
- [x] `POST /audio`、Task 字段、状态机、存储 schema、API 错误码不变。

### 10.2 测试与质量验收

- [x] 全量单元测试通过：`ran=361 fail=0 err=0`（Stage-011 基线 355 + 本阶段 6）。
- [x] 9 个探针全部通过；`probe_formats.py` 38/38（含真实 argv 的 `audio_argv_*`）。
- [x] `tests/check_userscript.py` 通过；发布门禁 60/60（版本 `1.0.2` 一致）。
- [x] 无新增第三方依赖。

### 10.3 完成条件

- [x] `docs/known-limitations.md` 写明「仅音频固定 MP3」「要 M4A/WAV 用 `POST /audio`」。
- [x] `docs/release-notes.md` 记录 1.0.2 的行为变化与不变项。
- [x] 用户可见文案与行为一致（下拉显示 `仅音频 (MP3)`）。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-001/002/003 的影响

- 冻结的默认格式策略未变（T1204 断言）；Task 模型、状态机、队列与排序未变。

### 11.2 对 Stage-004/005/006 的影响

- 控制接口、文件清理策略、存储 schema、配置层与安全守卫未变；
  `[ExtractAudio]` 产物记录让取消清理覆盖新增的 `.mp3`。

### 11.3 对 Stage-007 ~ 011 的影响

- 平台层、媒体处理接口（`/audio`）、发布门禁、启动接管均未变；
  `/formats` 的 `presets[]` 新增 `audio_format` 字段（增量）。

### 11.4 不变约束

- 不改 Task 字段、状态集合、API 错误格式与文件策略。
- 不允许任何 HTTP 输入进入 yt-dlp argv。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-20 | 阶段准备 | Cline | 已完成 | 复现反馈：`preset=audio` 的 `-f bestaudio/best` 只选流不转码，YouTube 产出 `.webm`（Opus） | 代码与用户文件（`.webm`）一致 | 无 |
| 2026-09-20 | 任务 001 | Cline | 已完成 | `Preset.audio_format`、`AUDIO_PRESET_FORMAT`、`audio_format_for()`、标签更新 | T1201/T1202 通过 | 无 |
| 2026-09-20 | 任务 002 | Cline | 已完成 | `build_command` 互斥分支 + audio 日志；`[ExtractAudio]` 解析 | T1203-T1205 通过 | 无 |
| 2026-09-20 | 任务 003 | Cline | 已完成 | `TaskControl`/`Scheduler`/`server` 透传；前端标签与「处理中」文案 | T1206-T1208 通过；JS 结构检查通过 | 无 |
| 2026-09-20 | 任务 004 | Cline | 已完成 | 探针扩充到 38 项；版本 1.0.2；迁移文档与 release-notes/known-limitations 更新 | 361 单测 + 9 探针 + 门禁 60/60 | 无 |

---

## 13. 实际输出与计划差异

- 原计划（用户反馈）：让「仅音频」产出 MP3。
- 实际输出：实现为「预设声明容器 + 引擎互斥分支」，并补上最终产物的解析与清理覆盖。
- 差异：
  1. **容器名走发送期记录**（与 `format_expr` 同手法），不新增 Task 字段、不动 schema。
  2. **前端只改标签文案**，不提供码率/容器选择；M4A/WAV 仍走 `POST /audio`。
  3. **用户脚本版本保持 5.2**：本次 JS 改动只是标签与文案，服务端不依赖它；
      若升级到 5.3 会让未更新脚本的用户看到版本不一致提示，收益小于噪音。
      行为修复完全在服务端，旧脚本同样能拿到 MP3。
  4. **「合并中」→「处理中」**：视频合并与音频提取共用同一状态文案，避免仅音频时显示「合并中」。
- 差异影响：均为范围澄清与文案修正，不改变 Task 字段、状态机、API 形状与存储 schema。
- 处理决定：全部接受，写入 `docs/stage012-migration.md` 与 `plan-whole.md`
  （`D-019` 已确认、`C-011` 变更记录、计划版本 `0.5`）。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：允许发布 `1.0.2`
- 对 Stage-001~011 影响：无回改；361 个单测 + 9 个探针 + 发布门禁 60 项全部通过
- 回滚基线：Stage-011 / `1.0.1`（commit `f8b4820`）
- 是否更新 `plan-whole.md`：是，版本 `0.4 → 0.5`，新增 `D-019`、`C-011` 与 Stage-012 行
- 审查人：用户
- 日期：2026-09-20
