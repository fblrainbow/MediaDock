# Stage-013：界面显示文件大小（任务行 + 清晰度下拉）

> 依据：[plan-whole.md](plan-whole.md) §12 `D-020`、[Stage-008.md](Stage-008.md)（`/formats`）、
> [Stage-003.md](Stage-003.md)（`/tasks` 契约）
>
> 用户诉求：下载界面显示文件大小；清晰度下拉框也显示不同清晰度的大小。

---

## 1. 阶段元数据

- 阶段编号：`Stage-013`
- 阶段名称：界面显示文件大小
- 总计划版本：`0.6`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-20
- 完成时间：2026-09-20
- 当前阻塞：无
- 前置阶段：`Stage-003`（任务列表契约）、`Stage-008`（`/formats` 探测模型）、`Stage-010`（发布门禁）
- 后续阶段：无

---

## 2. 阶段目标

```text
GET /formats?url=...
        |
        v
探测 formats[]（filesize / filesize_approx / tbr×duration）
        |
        v
core_formats.preset_sizes()  ->  {best,1080p,720p,480p,audio} 的估算字节数
        |
        v
presets[].size_bytes  ->  前端下拉框： 1080p · ≈245 MB

GET /tasks（每个任务）
        |
        +-- downloading -> TaskControl.total_size（yt-dlp 报出的总大小）
        +-- completed   -> core_files.task_file_size()（磁盘上真实文件大小）
        +-- 其他        -> 0（前端不显示）
        |
        v
tasks[].size_bytes  ->  前端任务行：⏳ 标题 · 42.3% · 368.6 MB · 1.5MiB/s · ETA 01:30
```

### 2.1 本阶段成功标准

- 任务行：下载中显示**总大小**（来自 yt-dlp 进度行 `of ~ 368.62MiB`），
  已完成显示磁盘上**真实文件大小**；未知时不显示（不猜、不用 0 冒充）。
- 下拉框：每个预设显示 `≈` 估算大小；拿不到就显示原标签（不带大小）。
- 估算规则明确且可测：视频预设 = 封顶内最佳视频 + 最佳音频；`audio` = 最佳音频；
  **任一组件缺失即为「未知」（0）**，绝不用部分大小冒充。
- 无 Task 字段变更、无 schema 变更、无 argv 变更、无错误码变更。
- 前端每次页面只多发一次 `/formats`（同 URL 缓存，下载时直接复用服务端缓存）。

---

## 3. 范围边界

### 3.1 本阶段包含

- `core_parse`：`SIZE_RE`、`ProgressEvent.size`、`parse_size()`（`PROGRESS_RE` 分组契约不变）。
- `core_control.TaskControl.total_size`（运行期瞬时值，**不落库**）。
- `core_engine._apply()`：把进度行里的总大小写进 control。
- `core_files.task_file_size(download_dir, url)`：已完成任务的真实文件大小。
- `core_formats`：`_approx_bytes`/`_best_video`/`_best_audio`/`preset_sizes`/`preset_choices`；
  `/formats` 的 `presets[]` 新增 `size_bytes`。
- `core_scheduler.control_for(task_id)`：只读暴露运行期 control。
- `server.task_size_bytes()` / `attach_sizes()`：`/tasks` 与 `/history` 任务项新增 `size_bytes`。
- `MediaDock.js`：`formatSize()`、任务行大小、下拉框大小（页面加载与 SPA 导航时刷新）。
- 测试与探针：`tests/test_engine.py`、`tests/test_files.py`、`tests/test_formats.py`、
  `tests/probe_formats.py`（38 → 45 项检查）。

### 3.2 本阶段明确不包含

- 新增 Task 字段或数据库列（`size_bytes` 只是 API 增量字段）。
- 显示磁盘剩余空间/网速历史/每个任务的实时已下载字节。
- 让前端解析 yt-dlp 输出（所有解析仍在服务端）。
- 改变 `/formats` 的探测时机规则（非默认预设仍需先探测，本阶段只是让页面提前探测一次）。

---

## 4. 输入契约

### 4.1 前置阶段已确认输入

- `Stage-008`：`format_entry()` 已归一化 `filesize`（`filesize` 或 `filesize_approx`），
  `formats[]` 是 `/formats` 的稳定模型；`FORMATS_CACHE` 按 URL 缓存 20 条。
- `Stage-003/005`：`/tasks`、`/history` 的任务项来自 `core_listing`，排序契约不变。
- `Stage-004`：`TaskControl` 是运行期参数的载体（`format_expr`、`media_job`、artifacts）。
- `Stage-010`：`tests/release_check.py` 60 项门禁；`documents` 版本一致性校验。

### 4.2 保持不变的约束

- Task 字段、状态集合、存储 schema（v2）、排序与显示契约不变。
- yt-dlp argv（含「仅音频 = MP3」）不变；`/formats` 成功响应只做增量扩展。
- 错误码不变；HTTP 输入不进入 argv。
- 前端不生成任何 yt-dlp 参数，也不做「服务端才算数」的算术（只做字节→可读文本）。

---

## 5. 阶段契约

### 5.1 模块责任

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `core_parse` | 从进度行取出总大小 token 并转成字节 | 决定展示与否 |
| `core_control` | 承载本次运行的总大小（瞬时） | 持久化、参与排序 |
| `core_engine` | 把进度事件里的总大小写进 control | 读取磁盘、算预设大小 |
| `core_files` | 已完成任务的真实文件大小（路径边界内） | 估算 |
| `core_formats` | 按预设估算大小（固定规则） | 落库、渲染 |
| `core_scheduler` | 只读暴露 control | 参与展示逻辑 |
| `server` | 组装 `size_bytes` 到 `/tasks`、`/history` | 计算估算 |
| `MediaDock.js` | 字节 → 可读文本，展示 | 计算大小 |

### 5.2 不变量

| 条件 | 必须成立 |
| --- | --- |
| 未知即 0 | 无法确定时返回 0，前端不显示；不用近似值冒充 |
| 不落库 | 大小不进 Task、不进 SQLite；重启后按磁盘重新计算 |
| 路径封闭 | 只统计 `download_dir` 内、带该视频 id 标记的非临时文件 |
| 增量兼容 | `/formats`、`/tasks`、`/history` 只新增字段，既有字段与顺序不变 |
| 排序稳定 | 大小不参与任何排序（`/tasks` 顺序与 Stage-003 完全一致） |

### 5.3 接口契约（增量）

| 入口 | 新增字段 |
| --- | --- |
| `GET /formats` | `presets[].size_bytes`（int，0 = 未知） |
| `GET /tasks` | `tasks[].size_bytes` |
| `GET /history` | `tasks[].size_bytes` |
| `GET /status` | 不变（兼容入口，保持原样） |

新增错误码：无。

---

## 6. 具体任务

### 任务 001：大小解析（`core_parse.py`、`core_control.py`、`core_engine.py`）

- [x] `SIZE_RE` 独立正则 + `ProgressEvent.size`；`PROGRESS_RE` 的分组契约保持不变
      （`tests/test_baseline.py` 依赖 `group(2)`）。
- [x] `parse_size("368.62MiB") -> int`（二进制单位；`MB` 按十进制；非法返回 0）。
- [x] `TaskControl.total_size` 瞬时字段；引擎在进度事件里更新。

### 任务 002：已完成文件大小（`core_files.py`）

- [x] `task_file_size(download_dir, url, prefer)`：先按任务类型取对应成品
      （`type=audio` 取音频件，否则取视频件），没有对应类型时取最大者；
      只统计 `download_dir` 内、带 `[<video id>]` 标记、非临时文件；目录不可读/无匹配返回 0。

### 任务 003：预设估算（`core_formats.py`）

- [x] `_approx_bytes()`：`filesize` → `filesize_approx` → `tbr/abr × duration`。
- [x] `_best_video(max_height)` / `_best_audio()`：按高度、帧率、码率取最佳。
- [x] `preset_sizes()`：视频预设 = 视频 + 音频；`audio` = 音频；缺件即 0。
- [x] `preset_choices()` / `presets_public(formats, duration)`：`presets[]` 带 `size_bytes`；
      `build_formats_payload` 传入 `formats` 与 `duration`。

### 任务 004：接口与前端（`core_scheduler`、`server.py`、`MediaDock.js`）

- [x] `Scheduler.control_for()`（只读）。
- [x] `server.task_size_bytes()` + `attach_sizes()`，接入 `/tasks` 与 `/history`。
- [x] `formatSize()`；任务行显示大小（下载中/完成/暂停）。
- [x] `applyPresetSizes()` + `refreshPresetSizes()`：页面加载与 SPA 导航各查一次（同 URL 去重）。

### 任务 005：测试、证据与文档

- [x] `tests/test_engine.py`（大小解析、单位换算）、`tests/test_files.py`（`task_file_size`）、
      `tests/test_formats.py`（估算规则 + 3 个 API 用例）。
- [x] `tests/probe_formats.py`：新增预设大小与任务/历史 `size_bytes` 断言（45 项）。
- [x] 版本 `1.0.2 → 1.0.3`、`docs/stage013-migration.md`、release-notes/userscript/
      known-limitations/README 与 `plan-whole.md` 更新。
- [x] 顺带修复测试文件里的静态类型问题（VS Code Problems 回到 0）。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| T1301 | 进度行总大小 | 单元 | `of ~ 50.00MiB` → `size="50.00MiB"`；`PROGRESS_RE` 分组不变 |
| T1302 | 单位换算 | 单元 | `1MiB=1048576`、`1MB=1000000`、`1.5GiB`、`500B`；非法 → 0 |
| T1303 | `task_file_size` | 单元 | 按类型取对应成品（视频/音频）；临时文件/其他视频/未知 id/坏目录 → 0 |
| T1304 | 预设估算 | 单元 | `1080p = 1234567+3200000`；`720p` 用 `filesize_approx` 且更小；`audio` = 音频件 |
| T1305 | 缺件即未知 | 单元 | 只有视频件时视频预设 = 0（不用部分大小冒充） |
| T1306 | 码率回退 | 单元 | 只有 `abr` 且有 duration → 用 `abr×duration` 估算 |
| T1307 | `/formats` 大小 | API | `presets[].size_bytes` 为 int 且 1080p > 720p |
| T1308 | `/tasks` 运行中大小 | API | 长任务上写 `control.total_size` → 行里 `size_bytes` 一致 |
| T1309 | `/tasks` 完成大小 | API | 等于 `task_file_size()`（无文件时为 0） |
| T1310 | 真实 HTTP 证据 | 探针 | `probe_formats_result.json` 45/45（含 `preset_size_*`、`tasks_have_size_bytes`） |

### 7.2 执行命令

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_formats.py core_parse.py core_files.py core_control.py core_engine.py core_scheduler.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_formats.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\check_userscript.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\release_check.py
```

### 7.3 测试原则

- 「未知 = 0」必须有专门用例（T1305），防止为了让数字好看而编造大小。
- 估算规则要用真实探测字段验证（`filesize` 与 `filesize_approx` 各一条）。
- 运行期大小必须用「仍在下载」的任务验证（瞬时引擎会直接完成，测不到该分支）。
- 前端只做格式化：新增锚点检查（`formatSize`/`size_bytes`/`applyPresetSizes`）证明
  脚本里没有自己算大小的逻辑。

---

## 8. 阶段产物

- `core_parse.py`：`SIZE_RE`、`ProgressEvent.size`、`parse_size()`。
- `core_control.py`：`TaskControl.total_size`。
- `core_engine.py`：进度事件写入总大小。
- `core_files.py`：`task_file_size()`。
- `core_formats.py`：`preset_sizes()`、`preset_choices()`、`presets_public(formats, duration)`。
- `core_scheduler.py`：`control_for()`。
- `server.py`：`task_size_bytes()`、`attach_sizes()`（`/tasks`、`/history`）。
- `MediaDock.js`：`formatSize()`、任务行大小、下拉框大小。
- 测试：`tests/test_engine.py`、`tests/test_files.py`、`tests/test_formats.py`、
  `tests/probe_formats.py`（45 项）与结果文件；`tests/check_userscript.py` 新锚点。
- 文档：`docs/stage013-migration.md`、release-notes、userscript、known-limitations、
  README、`plan-whole.md`；版本 `1.0.3`。

---

## 9. 风险与回滚

### 9.1 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 估算与实际差得多 | 用户预期落空 | UI 明确用 `≈`；`known-limitations` 说明来源与偏差（合并/提取） |
| 缺件时显示部分大小 | 误导（如只算视频） | 缺件即 0（T1305 + 探针 `preset_size_480p_unknown`） |
| 页面加载多跑一次 yt-dlp | 首屏变慢/多一次网络请求 | 每 URL 只查一次、服务端缓存 20 条，下载时直接复用 |
| 磁盘 stat 影响 `/tasks` 性能 | 轮询变慢 | 只对 `completed` 任务做一次目录扫描（`list_dir` 单层），实测门禁与探针均在毫秒级 |
| 大小进入排序 | 列表抖动 | 明确不变量：大小不参与任何排序 |

### 9.2 外部依赖

- 无新增依赖（标准库 + 已有 yt-dlp 探测字段）。

### 9.3 回滚策略

1. 前端：去掉 `formatSize`/`applyPresetSizes` 与任务行里的 size 展示 → 界面回到 1.0.2。
2. 服务端：删除 `attach_sizes` 调用与 `presets[].size_bytes` 即可（字段是增量，
   旧前端会忽略），行为回到 1.0.2。
3. 数据：无 schema 变更、无迁移，无需数据回滚。

---

## 10. 阶段验收标准

### 10.1 功能验收

- [x] 任务行：下载中显示总大小，已完成显示真实文件大小，未知不显示。
- [x] 下拉框：每个预设显示 `≈` 估算大小；拿不到则显示原标签。
- [x] 估算规则可解释：视频 = 视频件 + 音频件，`audio` = 音频件，缺件即未知。
- [x] 打开视频页会填充下拉框大小，且同 URL 不重复探测。
- [x] Task 字段、schema、排序、argv、错误码全部不变。

### 10.2 测试与质量验收

- [x] 全量单元测试通过：`ran=378 fail=0 err=0`（Stage-012 基线 361 + 本阶段 17）。
- [x] 9 个探针全部通过；`probe_formats.py` 45/45。
- [x] `tests/check_userscript.py` 通过（708 行）；发布门禁 60/60（版本 `1.0.3` 一致）。
- [x] VS Code Problems = 0（含测试文件的类型标注清理）；无新增第三方依赖。

### 10.3 完成条件

- [x] `docs/known-limitations.md` 记录「估算来源与偏差」「缺件即未知」「完成态取最大成品」。
- [x] `docs/userscript.md` 记录任务行与下拉框的大小展示。
- [x] 版本 `1.0.3` 在 `/health.version.app`、README 与 release-notes 一致。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-001/002/003 的影响

- 冻结策略、Task 模型、状态机、排序契约不变；`/tasks` 只新增字段。

### 11.2 对 Stage-004/005/006 的影响

- 控制接口与文件策略不变；`total_size` 是运行期瞬时值，不参与持久化与重启矩阵。
- 配置层与安全守卫未变；磁盘扫描仍受 `download_dir` 路径边界约束。

### 11.3 对 Stage-007 ~ 012 的影响

- 平台层、`/audio`、发布门禁、启动接管、MP3 预设均未变；
  `/formats` 载荷与任务项各新增一个字段（增量，旧前端忽略）。

### 11.4 不变约束

- 不新增 Task 字段、不改 state/schema/排序/argv/错误码。
- 大小一律「未知即 0」，不允许用近似值冒充确定值。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-20 | 阶段准备 | Cline | 已完成 | 核对 `/formats` 已有 `filesize` 字段、`/tasks` 契约与前端渲染点，冻结「未知即 0」规则 | 现有载荷字段确认 | 无 |
| 2026-09-20 | 任务 001 | Cline | 已完成 | `SIZE_RE` + `parse_size()` + `TaskControl.total_size` + 引擎写入 | T1301/T1302 通过；`test_baseline` 分组契约未破 | 无 |
| 2026-09-20 | 任务 002 | Cline | 已完成 | `task_file_size()`（路径边界 + 最大成品 + 临时文件排除） | T1303 通过（5 个用例） | 无 |
| 2026-09-20 | 任务 003 | Cline | 已完成 | 估算函数与 `presets[].size_bytes`（含码率×时长回退） | T1304-T1307 通过 | 无 |
| 2026-09-20 | 任务 004 | Cline | 已完成 | `control_for()` + `attach_sizes()` + 前端 `formatSize`/任务行/下拉框 | T1308/T1309 通过；JS 结构检查通过（708 行） | 无 |
| 2026-09-20 | 任务 005 | Cline | 已完成 | 探针扩到 45 项；版本 1.0.3；文档与计划更新；测试文件类型清理 | 378 单测 + 9 探针 + 门禁 60/60 + Problems 0 | 无 |

---

## 13. 实际输出与计划差异

- 原计划（用户诉求）：任务界面显示文件大小 + 下拉框显示各清晰度大小。
- 实际输出：两处都实现，并把「大小从哪来」固化成两组明确规则（运行期总大小/磁盘成品大小；
  预设估算/缺件即未知），另加探针与 API 用例作为证据。
- 差异：
  1. **新增的是 API 增量字段而非 Task 字段**：`size_bytes` 只出现在 `/formats`、`/tasks`、
     `/history` 的响应里；Task 模型与 schema 完全不动，重启后按磁盘重算。
  2. **运行期总大小走 `TaskControl`（瞬时）**，不落库：与 `format_expr`/`media_job` 同一手法。
  3. **缺件即未知（0）而不是部分相加**：宁可让界面不显示，也不显示会误导的偏小数字。
  4. **页面加载会先探测一次**（每 URL 一次，服务端缓存）：让下拉框尽早有大小，
     同时让后续下载直接命中缓存。
  5. **顺带清理测试文件的类型标注**：VS Code Problems 回到 0（`manager.get()` 收窄、
     HTTP 助手返回类型、Optional 断言），只动测试，不改产品行为。
- 差异影响：均为范围澄清与工程质量改善，不改变 Task 字段、状态机、存储 schema 与 API 既有形状。
- 处理决定：全部接受，写入 `docs/stage013-migration.md` 与 `plan-whole.md`
  （`D-020` 已确认、`C-012` 变更记录、计划版本 `0.6`）。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：允许发布 `1.0.3`
- 对 Stage-001~012 影响：无回改；378 个单测 + 9 个探针 + 发布门禁 60 项全部通过
- 回滚基线：Stage-012 / `1.0.2`（commit `9d2fcbc`）
- 是否更新 `plan-whole.md`：是，版本 `0.5 → 0.6`，新增 `D-020`、`C-012` 与 Stage-013 行
- 审查人：用户
- 日期：2026-09-20
