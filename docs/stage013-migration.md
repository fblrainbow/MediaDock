# Stage-013 迁移说明：界面显示文件大小

> 日期：2026-09-20；应用版本 **1.0.2 → 1.0.3**，用户脚本仍 **5.2**，存储 schema 仍 **2**。
> 回滚基线：Stage-012 / `1.0.2`。

---

## 需求

1. 下载界面显示文件大小；
2. 清晰度下拉框显示不同清晰度的大小。

---

## 数据来源（两组规则）

### 1）任务行大小

| 任务状态 | 来源 | 实现 |
| --- | --- | --- |
| `downloading` | yt-dlp 进度行 `of ~ 368.62MiB` 报出的**总大小** | `core_parse.SIZE_RE` → `ProgressEvent.size` → `parse_size()` → `TaskControl.total_size`（瞬时，不落库） |
| `completed` | 磁盘上**真实文件** | `core_files.task_file_size(download_dir, url, prefer)`：先按任务类型取对应成品（`type=audio` 取音频件，否则取视频件），没有对应类型时取最大的非临时成品 |
| 其他（pending/paused/error/cancelled） | 无 | `0` —— 前端不显示 |

### 2）下拉框预设大小（`/formats` 返回 `presets[].size_bytes`）

| 预设 | 估算规则 |
| --- | --- |
| `best` / `1080p` / `720p` / `480p` | 封顶高度内的**最佳视频件** + **最佳音频件**（即 `bv*+ba` 的合并结果） |
| `audio` | **最佳音频件**（仅音频预设；1.0.2 起会再被 FFmpeg 提取成 MP3） |

单个格式的大小按 `filesize` → `filesize_approx` → `tbr`/`abr` × `duration` 依次回退。
**任一组件缺失 → 该预设返回 0**（界面只显示原标签），绝不用部分大小冒充完整大小。

---

## 接口增量

```text
GET /formats?url=...
  presets: [ { name, label, kind, selector, max_height, audio_format, size_bytes }, ... ]

GET /tasks           -> tasks[].size_bytes
GET /history?limit=  -> tasks[].size_bytes
GET /status          -> 不变（兼容入口保持原样）
```

- 都是**增量字段**：既有字段名、顺序与含义不变，旧前端忽略即可。
- `size_bytes` 是整数（字节）；`0` 统一表示「未知」，前端据此决定是否显示。
- 无新增错误码。

---

## 前端

| 位置 | 显示 |
| --- | --- |
| 任务行（下载中） | `⏳ 标题 · 42.3% · 368.6 MB · 1.5MiB/s · ETA 01:30` |
| 任务行（已完成） | `✅ 标题 · 完成 · 10.0 MB` |
| 任务行（已暂停） | `⏸ 标题 · 已暂停 · 368.6 MB` |
| 清晰度下拉 | `1080p · ≈245 MB`（取不到大小则只显示 `1080p`） |

- 页面加载与 SPA 导航时各查一次 `/formats`（同 URL 去重），用于填充下拉框。
  该次探测同时预热服务端 `FORMATS_CACHE`，随后点击下载直接命中缓存。
- 前端只做「字节 → 可读文本」（`formatSize`），不参与任何大小计算。

---

## 行为变化

| 场景 | 1.0.2 | 1.0.3 |
| --- | --- | --- |
| 任务行 | 只有 `%`、速度、ETA | 增加大小（下载中为总量，完成后为成品大小） |
| 清晰度下拉 | 只有名称 | 名称 + `≈` 估算大小 |
| 打开视频页 | 不请求 `/formats` | 请求一次（填充下拉框，同 URL 只查一次） |
| Task 字段 / schema / 排序 / argv / 错误码 | — | 全部不变 |
| `/formats`、`/tasks`、`/history` 载荷 | — | 各新增 `size_bytes`（增量） |

---

## 证据

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 全量单元测试 | `python -m unittest discover -s tests -t .` | `ran=378 fail=0 err=0` |
| 大小相关用例 | `python -m unittest tests.test_engine tests.test_files tests.test_formats` | 全通过（T1301-T1309） |
| 真实 HTTP 探针 | `python tests\probe_formats.py` | `FORMATS PROBE OK checks=45`（含 `preset_size_*`、`tasks_have_size_bytes`、`history_has_size_bytes`） |
| 全部探针 | 9 个 `tests/probe_*.py` | 全部 rc=0 |
| 用户脚本结构 | `python tests\check_userscript.py` | `JS STRUCTURE OK (708 lines)` |
| 发布门禁 | `python tests\release_check.py` | `checks=60 passed=60 failed=0`（版本 `1.0.3`） |
| 静态检查 | VS Code Problems | 0（顺带清理了测试文件的类型标注） |

---

## 注意事项与已知边界

- **估算不是承诺**：`≈` 来自探测元数据；合并/提取环节会有偏差，实际以成品为准。
- **拿不到就不显示**：例如某个视频没有 ≤480p 的格式时，`480p` 的大小为 `0`，
  下拉框只显示 `480p`（不会借用 1080p 的数字）。
- **仅音频的「总大小」是中间音频流**：提取成 MP3 后，最终大小以任务完成后显示的成品为准。
- **完成态大小按任务类型取成品**：同一个视频既有 `.webm` 又有 `.mp3` 时，下载任务显示视频件、
  「转音频」任务显示音频件，不会互相借用（没有对应类型时回退到最大文件）。
- **重启后运行期大小消失**：`total_size` 是瞬时值；重启后任务按 Stage-005 矩阵变为
  `error`/`interrupted`，不再显示大小。
- **不会因为大小增加排序抖动**：大小完全不参与 `/tasks` 排序。

---

## 回滚

1. 前端：移除 `formatSize`/`applyPresetSizes`/`refreshPresetSizes` 与任务行里的 size 拼接
   → 界面回到 1.0.2。
2. 服务端：去掉 `/tasks`、`/history` 的 `attach_sizes()` 调用与 `presets[].size_bytes`
   （增量字段，旧前端本就会忽略）→ 行为回到 1.0.2。
3. 数据：无 schema 变更、无迁移，无需数据回滚。
