# Stage-008 迁移说明：格式选择与 Formats API

> 日期：2026-09-19；在「默认 1080p MP4」这条冻结策略之上，增加可查询、可验证的格式选择。
> 客户端只发送**预设名字**或 `/formats` 刚报告过的 `format_id`，
> yt-dlp 的 `-f` 表达式永远由服务端常量拼装。
> 回滚点：Stage-007 已签字基线（266/266 单元测试 + 6 个探针 + JS 结构检查）。

## 新模块（纯标准库）

- `core_formats.py`
  - `PRESETS` / `PRESET_BY_NAME` / `preset_names()` / `presets_public()`：
    固定顺序 `best, 1080p, 720p, 480p, audio`。
  - `Preset(name, label, kind, selector, max_height)`；`kind` 为 `video`/`audio`。
  - `resolve_preset(value) -> (preset, error_code, message)`：空值 → `best`；
    名字大小写/首尾空白归一化；表外值 → `invalid_format`。
  - `validate_format_id(value)`：`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`。
  - `selector_for(preset, format_id)`：唯一生成 `-f` 表达式的地方。
  - `build_probe_command(ytdlp, url, ffmpeg_path)`：`--dump-single-json
    --skip-download --no-playlist --no-warnings [--ffmpeg-location X] URL`。
  - `parse_probe_output(text)` → `(info, error, message)`；容忍 yt-dlp 在 JSON
    前打印告警行。
  - `format_entry(entry)` / `build_formats_payload(info, url, platform, video_id)`
    / `preset_satisfied()` / `format_id_present()`。
  - `FormatsProbe(ytdlp, ffmpeg, runner=None, timeout=60)`：`fetch()` 返回
    `(payload, error_code, message)`，进程边界可注入，永不抛异常。
  - 错误码：`invalid_format`(400)、`format_not_available`(400)、
    `formats_unavailable`(502)。

## 预设表

| 预设 | 含义 | yt-dlp 选择器 |
| --- | --- | --- |
| `best`（默认） | 与 Stage-001 完全一致 | `bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]` |
| `1080p` | ≤1080p | `bv*[height<=1080]+ba/b[height<=1080]` |
| `720p` | ≤720p | `bv*[height<=720]+ba/b[height<=720]` |
| `480p` | ≤480p | `bv*[height<=480]+ba/b[height<=480]` |
| `audio` | 仅音频流 | `bestaudio/best` |

- 默认路径零变化：不带 `preset`/`format_id` 时 `-f` 仍是 Stage-001 冻结表达式，
  探针 `default_expression` 断言这一点。
- `audio` 只挑选最佳音频流；提取 MP3/M4A 属于 Stage-009（Media Processor）。
- `--merge-output-format mp4` 与 `-P <download_dir>` 不变。

## `/formats` 契约

```text
GET /formats?url=<youtube url>
```

顺序固定：`missing_url`(400) → `detect_platform()`（`invalid_url`/`unsupported_platform` 400）
→ yt-dlp 元数据探测 → 200 或 `formats_unavailable`(502)。

响应字段（稳定）：

| 字段 | 说明 |
| --- | --- |
| `url` / `platform` / `video_id` | 归一化 URL、平台名、视频 ID |
| `title` / `uploader` / `duration` / `extractor` | 展示用元信息（已截断） |
| `default_preset` | 恒为 `best` |
| `presets` | `[{name, label, kind, selector, max_height}]` |
| `formats` | `[{format_id, ext, height, width, fps, vcodec, acodec, abr, tbr, filesize, note, presets}]`，上限 40 条 |
| `count` / `total` | 返回条数 / 探测到的总条数 |

- 每个 format 的 `presets` 表示它能满足哪些预设（音频流只标 `audio`；
  超过预设高度的流不标记该预设）。
- 探测只做元数据，不下载、不创建 Task、不写数据库。
- 成功结果按归一化 URL 缓存在内存中（FIFO 上限 20），供 `/download` 校验使用；
  `bootstrap()` 会清空缓存。

## `/download` 契约变化

| 场景 | 结果 |
| --- | --- |
| `/download?url=<youtube>` | 不变：200，`-f` = 冻结策略 |
| `/download?url=...&preset=best` | 200，同上 |
| `/download?url=...&preset=<非默认>` 且**没有**先查 `/formats` | 400 `format_not_available`（"call /formats before choosing a preset"） |
| `/download?url=...&preset=<非默认>` 且已查过 `/formats` 且该预设可用 | 200，`-f` = 预设选择器 |
| `/download?url=...&preset=<非默认>` 且该预设不可用（如 1080p 视频选…） | 400 `format_not_available` |
| `/download?url=...&preset=<表外值>` | 400 `invalid_format` |
| `/download?url=...&format_id=<合法 id>` 且 id 在 `/formats` 结果中 | 200，`-f` = `<id>+ba/<id>/b` |
| `/download?url=...&format_id=<形状非法>` | 400 `invalid_format` |
| `/download?url=...&format_id=<不在结果中>` | 400 `format_not_available` |

- 新错误码只在前端主动选择格式时出现；既有调用完全不受影响。
- `format_id` 必须「形状合法」且「出现在本进程刚探测到的列表里」，因此
  HTTP 输入无法进入 argv（`-` 开头、空格、`;`、`|`、`/`、`+` 都不允许）。

## 格式如何穿过调度器（Task 字段不变）

- `Task` / `tasks.db` **没有**新增字段：格式不写入 Task，避免 schema v3 与
  历史记录迁移。
- `Scheduler.submit(url, platform, format_expr)` / `admit(..., format_expr)`
  把解析后的表达式存入调度器内存 `_formats[task_id]`；`format_for(task_id)`
  可查询；`DownloadEngine` 通过 `TaskControl.format_expr` 读取它。
- 因此：暂停→继续、失败→重试都会沿用同一表达式；但**服务重启后**内存记录丢失，
  对 `interrupted` 任务重试会回到默认策略（已知限制，见 Stage-008.md 13）。
- `core_engine.build_command(..., format_expr="")` 仍是 argv 的唯一构造点；
  空值 → 冻结策略，URL 仍是最后一个参数。

## 前端（MediaDock.js 5.1）

- 面板标题栏新增 `<select id="mediadock-preset">`，选项与服务端预设同名同序。
- 选择保存在 `localStorage['mediadock.preset']`；非法/缺失值回退 `best`。
- 选非默认预设时先 `GET /formats?url=...`：失败则按钮显示
  `formats_unavailable`（或服务端错误码），不会盲发下载。
- 前端**不**拼装任何 yt-dlp 选择器；`tests/check_userscript.py` 增加了
  「JS 里不得出现 `bv*`/`bestaudio`/`+ba/`/`height<=`」的检查。
- 版本号 `5.0 → 5.1`，锚点清单同步更新（`'/formats?url='`、`PRESET_OPTIONS`、
  `mediadock-preset`、`仅音频`）。

## 兼容性与既有测试

- Task 字段、状态机、排序、并发上限、控制 API、存储 schema、配置层、
  平台检测与文件边界全部不变。
- 既有用例无需改动：不带 `preset`/`format_id` 的请求走完全相同的代码路径。
- 新增 `tests/test_formats.py`（22 用例）与 `tests/probe_formats.py`（30 项检查）。

## 回滚

1. 代码：`server.py` 的 `/formats` 路由与 `/download` 的格式分支可整体移除，
   `scheduler.submit(url, platform)` 的默认参数保证行为回到 Stage-007。
2. 数据：无 schema 变化，无迁移、无备份需求。
3. 前端：`MediaDock.js` 回退到 5.0（去掉 select 与 `/formats` 调用），
   `tests/check_userscript.py` 的锚点同步回退。
