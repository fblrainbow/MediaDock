# Stage-012 迁移说明：仅音频预设产出 MP3

> 日期：2026-09-20；应用版本 **1.0.1 → 1.0.2**，用户脚本仍 **5.2**，存储 schema 仍 **2**。
> 回滚基线：Stage-011 / `1.0.1`。

---

## 问题

选「仅音频」下载得到的是 `.webm`（YouTube 的 Opus 音频流），不是 MP3：

```text
2亿阅读量神作！Dan Koe：如何在一天内彻底改变自己的人生 [pUa0Cb78OuU].webm
```

原因：`core_formats.PRESETS` 里的 `audio` 预设只做「选流」（Stage-008 的既定边界
——只负责挑出最佳音频流，转码留给 Stage-009）：

```python
Preset("audio", "Audio only", "audio", "bestaudio/best", 0)   # 旧
```

而 yt-dlp 对 `bestaudio` 的默认输出容器就是平台的原始容器（YouTube 上是 `.webm`/Opus）。
Stage-009 的 `POST /audio` 确实能转 MP3，但它只被 API 调用，前端从来没接上。

---

## 改动

| 位置 | 变化 |
| --- | --- |
| `core_formats.Preset` | 新增 `audio_format: str = ""`（预设需要转码成哪个容器） |
| `core_formats.PRESETS` | `audio` → `Preset("audio", "Audio only (MP3)", "audio", "bestaudio/best", 0, "mp3")`；其余预设为 `""` |
| `core_formats.audio_format_for()` | 显式 `format_id` 时返回 `""`（不做隐式转码） |
| `core_engine.build_command()` | 新增 `audio_format` 参数，与合并策略**互斥** |
| `core_parse` | 新增 `EXTRACT_PATH_RE`，把 `[ExtractAudio] Destination:` 解析为最终产物 |
| `core_control.TaskControl` | 新增 `audio_format` |
| `core_scheduler` | `submit/admit(..., audio_format=)`、`_audio`、`audio_format_for(task_id)` |
| `server.resolve_format_choice()` | 返回 `(expression, audio_format, code, message)` |
| `MediaDock.js` | 标签 `仅音频 (MP3)`；FFmpeg 阶段文案「合并中」→「处理中」 |

### argv 对比

```text
# 默认 / 视频预设（未变，逐字一致）
yt-dlp -f bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4] \
       --merge-output-format mp4 --newline --no-playlist [-P <dir>] <url>

# 仅音频（新）
yt-dlp -f bestaudio/best \
       --extract-audio --audio-format mp3 --audio-quality 0 \
       --newline --no-playlist [-P <dir>] <url>
```

- 两者**不会同时出现**（互斥分支）。
- 容器名 `mp3` 是 `core_formats.AUDIO_PRESET_FORMAT` 常量，HTTP 文本依旧进不了 argv。

---

## 行为变化

| 场景 | 1.0.1 | 1.0.2 |
| --- | --- | --- |
| 选「仅音频」 | 产出 `.webm`/`.m4a`（平台原始容器，Opus/AAC） | 产出 **`.mp3`** |
| 选 Best/1080p/720p/480p | `--merge-output-format mp4` | 不变 |
| 不选清晰度（默认） | 1080p MP4 | 不变 |
| `/formats` 响应的 `presets[]` | 5 个字段 | 多一个 `audio_format`（增量） |
| `POST /audio`（mp3/m4a/wav） | 可用 | 不变 |
| Task 字段 / 状态机 / schema / 错误码 | — | 全部不变 |

---

## 证据

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 全量单元测试 | `python -m unittest discover -s tests -t .` | `ran=361 fail=0 err=0` |
| 格式/引擎用例 | `python -m unittest tests.test_formats tests.test_engine` | `Ran 43 tests ... OK` |
| 真实 argv 探针 | `python tests\probe_formats.py` | `FORMATS PROBE OK checks=38`（含 `audio_argv_extract/format_mp3/quality/no_merge/url_last`） |
| 全部探针 | 9 个 `tests/probe_*.py` | 全部 rc=0 |
| 用户脚本结构 | `python tests\check_userscript.py` | `JS STRUCTURE OK (638 lines)` |
| 发布门禁 | `python tests\release_check.py` | `checks=60 passed=60 failed=0`（版本 `1.0.2`） |

---

## 注意事项与已知边界

- **「仅音频」固定 MP3**：前端不提供码率或容器选择；需要 `.m4a`/`.wav`
  请用 `POST /audio?target=m4a|wav`（Stage-009）。
- **提取依赖 FFmpeg**：由 yt-dlp 调用 FFmpeg 完成；缺失时任务失败，
  `server.py --check-config` 会提前报 `ffmpeg_missing`。
- **中间文件会被 yt-dlp 删除**：提取完成后 yt-dlp 删除下载的音频流（不加 `-k`），
  最终只留下 `.mp3`；取消清理会把本次运行的临时文件与 `.mp3` 一并删除。
- **用户脚本版本仍为 5.2**：本次 JS 改动只是标签与文案，服务端行为不依赖它，
  因此不强制用户更新脚本（避免「脚本版本不一致」的噪音提示）。
- **老任务不受影响**：已经下载成 `.webm` 的旧任务记录仍指向原文件；
  想转 MP3 可以对那个已完成的下载任务调用 `POST /audio?target=mp3`。

---

## 回滚

1. 代码：把 `audio` 预设的 `audio_format` 置空（或移除字段与 `audio_format_for()`），
   `build_command` 回到「总是 `--merge-output-format mp4`」
   → 行为回到 Stage-008 / `1.0.1`。
2. 测试/文档：删除 `T1201-T1209` 对应用例与 release-notes 条目；版本回退 `1.0.2 → 1.0.1`。
3. 数据：无 schema 变更、无迁移，无需数据回滚；已产生的 `.mp3` 只是普通下载文件。
