# Stage-008：格式选择与 Formats API

> 依据：[plan-whole.md](plan-whole.md) `0.2` §9 Stage-008、[Stage-007.md](Stage-007.md)、
> [docs/stage007-migration.md](docs/stage007-migration.md)
>
> 在已冻结的「默认 1080p MP4」策略之上，增加可查询、可验证的格式选择：
> `/formats` 把 yt-dlp 元数据收敛为稳定模型，`/download` 只接受预设名或该模型里
> 出现过的 `format_id`，`-f` 表达式永远由服务端常量拼装。

---

## 1. 阶段元数据

- 阶段编号：`Stage-008`
- 阶段名称：格式选择与 Formats API
- 总计划版本：`0.2`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-19
- 预计完成时间：2026-09-19
- 当前阻塞：无
- 前置阶段：`Stage-007` 平台 Adapter（已完成）
- 后续阶段：`Stage-009` 音频模式与 Media Processor（复用本阶段的选择器与探测边界）

---

## 2. 阶段目标

```text
GET /formats?url=...
        |
        v
detect_platform(url)                        (Stage-007：唯一平台真值)
        |
        v
core_formats.FormatsProbe.fetch()           (yt-dlp --dump-single-json，可注入 runner)
        |
        v
200 {url, platform, video_id, title, duration, presets[], formats[], count, total}
        |                                    +--> 400 invalid_url / unsupported_platform
        |                                    +--> 502 formats_unavailable
        v
GET /download?url=...&preset=720p           (或 &format_id=137)
        |
        v
core_formats.resolve_preset() / validate_format_id() / selector_for()
        |
        v
Scheduler.submit(url, platform, format_expr) -> 引擎 argv "-f <选择器>"
```

### 2.1 本阶段成功标准

- `/formats` 的响应字段稳定且可量化；同一个 URL 重复查询字段集合一致。
- 格式选择只有一处真值：`core_formats.PRESETS` 与 `selector_for()`；`server.py`
  不拼装任何 yt-dlp 选择器，`MediaDock.js` 里也不出现选择器片段。
- **默认路径零变化**：不带 `preset`/`format_id` 时 argv 的 `-f` 仍是 Stage-001
  冻结表达式（探针 `default_expression` 断言）。
- 明确错误：表外预设 → 400 `invalid_format`；不可用预设 / 未知 `format_id`
  → 400 `format_not_available`；探测失败 → 502 `formats_unavailable`。
- 未选择格式时仍使用已验证的 MVP 默认策略；选择格式不破坏合并
  （`--merge-output-format mp4`）与最终文件检查（`-P` 与 URL 位置不变）。

---

## 3. 范围边界

### 3.1 本阶段包含

- 新增 `core_formats.py`：预设表、`format_id` 校验、探测 argv、JSON 解析、
  稳定 payload 模型、可注入进程边界的 `FormatsProbe`。
- `server.py` 新增 `GET /formats`；`/download` 接受 `preset` 与 `format_id`。
- 格式穿过调度器：`TaskControl.format_expr` + `Scheduler` 的发送期记录
  （**不新增 Task 字段、不改 schema**）。
- `core_engine.build_command(..., format_expr="")`。
- 前端：`MediaDock.js` 5.1 面板清晰度选择器 + 非默认预设先查 `/formats`。
- 测试：`tests/test_formats.py`、`tests/probe_formats.py`（真实子进程 + 真实 argv）。
- 文档：`docs/stage008-migration.md`。

### 3.2 本阶段明确不包含

- 音频提取/转码为 MP3/M4A/WAV（Stage-009）；本阶段 `audio` 只挑选音频流。
- 字幕、缩略图、播放列表、批量下载。
- 把 `format_id` 或 `preset` 写入 `Task`/`tasks.db`（避免 schema v3 与迁移）。
- 修改 `Task` 字段、状态集合、并发上限、控制 API、排序、文件清理策略。
- 新增平台（Stage-007 决议：首批只支持 YouTube）。

---

## 4. 输入契约

### 4.1 Stage-007 已确认输入

- `detect_platform(url)` 是唯一平台判定入口；`/download` 已按
  「校验 → 检测 → 归一化 → `submit(url, platform)`」组织。
- `Task.platform` 已存在并持久化；`Scheduler.submit(url, platform)` 兼容旧调用。
- `core_engine.build_command()` 是 argv 唯一构造点，`shell=False`，URL 永远最后一个参数。

### 4.2 保持不变的约束

- 无 `preset`/`format_id` 的 `/download` 响应与 argv 与 Stage-007 完全一致。
- `Task` 字段、状态机、`/status`、`/tasks`、`/history`、`/events` 形状不变。
- 请求校验顺序不变：Host/Origin 守卫 → URL 校验 → 平台检测 → 格式校验。

---

## 5. 阶段契约

### 5.1 模块责任

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `core_formats` | 预设表、`format_id` 校验、探测 argv、JSON → 稳定模型、错误码 | HTTP、Task、调度、文件 |
| `server.Handler` | 调用探测与校验，把错误码映射为状态码 | 拼装 yt-dlp 选择器 |
| `core_scheduler` | 记录并复用发送期 `format_expr` | 解析格式、访问网络 |
| `core_engine` | 用 `format_expr` 构造 argv 并执行 | 决定格式 |
| `MediaDock.js` | 展示预设、发送预设名 | 生成选择器 |

### 5.2 不变量

| 条件 | 必须成立 |
| --- | --- |
| 单点选择器 | 只有 `core_formats.selector_for()` 生成 `-f` 表达式 |
| 输入不可入 argv | `preset` 必须命中固定表；`format_id` 必须形状合法且出现在 `/formats` 结果中 |
| 默认不变 | 空选择 → Stage-001 冻结表达式 |
| 无副作用探测 | `/formats` 不下载、不建 Task、不写数据库；只缓存元数据 |
| 兼容 | Task 字段、状态机、错误格式、并发与文件策略不变 |

### 5.3 HTTP 契约

| 场景 | 响应 |
| --- | --- |
| `/formats?url=<youtube>` | 200 稳定 payload |
| `/formats`（缺 url） | 400 `missing_url` |
| `/formats?url=<非法/未接入>` | 400 `invalid_url` / `unsupported_platform`（不启动进程） |
| `/formats` 探测失败/超时 | 502 `formats_unavailable` |
| `/download?...&preset=<非默认>` 未先查 `/formats` | 400 `format_not_available` |
| `/download?...&preset=<表外>` | 400 `invalid_format` |
| `/download?...&format_id=<形状非法 / 不在结果中>` | 400 `invalid_format` / `format_not_available` |

新增错误码：`invalid_format`(400)、`format_not_available`(400)、`formats_unavailable`(502)。
既有错误码不变。

---

## 6. 具体任务

### 任务 001：格式模型与探测（`core_formats.py`、`tests/test_formats.py`）

- [x] 预设表固定且有序：`best, 1080p, 720p, 480p, audio`；默认保持冻结表达式。
- [x] `validate_format_id()` 拒绝 `--exec=calc`、`-f`、空格、`;`、`|`、`/`、`+`、超长、换行。
- [x] `build_probe_command()`：`--dump-single-json --skip-download`，无 `-f`，URL 最后。
- [x] `parse_probe_output()` 容忍前置告警行，垃圾输入返回 `formats_unavailable`。
- [x] 稳定 payload：字段集合与每条 format 的字段集合被测试锁定。

### 任务 002：HTTP 接入与调度穿透

- [x] `GET /formats` 在平台检测之后、进程启动之前完成校验。
- [x] `/download` 的 `preset`/`format_id` 校验；表达式经 `Scheduler` 传给引擎。
- [x] `TaskControl.format_expr` + `Scheduler.format_for()`；暂停/重试沿用同一表达式。
- [x] `bootstrap()` 清空格式缓存；缓存 FIFO 上限 20。

### 任务 003：前端、探针与文档

- [x] `MediaDock.js` 5.1：清晰度下拉、`localStorage` 记忆、非默认预设先查 `/formats`。
- [x] `tests/check_userscript.py` 增加「JS 不得出现 yt-dlp 选择器片段」检查。
- [x] `tests/probe_formats.py`：真实子进程记录 argv + spawn 边界记录下载 argv。
- [x] `docs/stage008-migration.md`、`Stage-008.md`、`plan-whole.md` 状态更新。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| T801 | 预设表顺序与字段 | 单元 | `best,1080p,720p,480p,audio`；字段稳定 |
| T802 | 空值/大小写/空白归一化 | 单元 | 空值与 `1080P` → 合法预设 |
| T803 | 表外预设 | 单元 | `invalid_format` |
| T804 | `selector_for` | 单元 | 只由常量与合法 id 生成 |
| T805 | 探测 argv 形状 | 单元 | 无 `-f`、URL 最后、含 `--dump-single-json` |
| T806 | 输出解析 | 单元 | 告警行容错；垃圾 → `formats_unavailable` |
| T807 | payload 模型 | 单元 | 顶层/条目字段集合锁定；`presets` 标记正确 |
| T808 | `FormatsProbe.fetch` | 单元 | 成功/rc≠0/垃圾/无路径 四条路径 |
| T809 | `/formats` HTTP | API | 200 形状；缺 url/非法/未接入 400 且不启动进程；失败 502 且不缓存 |
| T810 | `/download` 默认 | API | 200；表达式 = 冻结策略 |
| T811 | `preset` 校验 | API | 未查询 → `format_not_available`；表外 → `invalid_format`；查询后生效 |
| T812 | `format_id` 校验与 argv | API | 形状非法 → `invalid_format`；不在结果中 → `format_not_available`；合法 id 进入 argv |
| T813 | 探针链路 | 探针 | `probe_formats_result.json` 全部 true（含真实子进程 argv） |

### 7.2 建议命令

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_formats.py core_scheduler.py core_engine.py core_control.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_formats.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\check_userscript.py
```

### 7.3 测试原则

- 探测边界可注入（`runner` / `server.FORMATS_RUNNER`），单元与 API 测试不联网。
- 探针使用**真实子进程**记录 argv；下载 argv 用 `popen_factory` 边界记录
  （下载 `-f` 表达式含 `<`，`.bat` 会被 cmd 重定向，故不走真实 spawn）。
- 默认路径必须被显式断言（T810/T813 `default_expression`）。

---

## 8. 阶段产物

- `core_formats.py`：预设表、`validate_format_id`、`selector_for`、`build_probe_command`、
  `parse_probe_output`、`format_entry`、`build_formats_payload`、`preset_satisfied`、
  `format_id_present`、`FormatsProbe`、3 个错误码。
- `server.py`：`GET /formats`；`/download` 的 `preset`/`format_id`；`FORMATS_RUNNER`/
  `FORMATS_PROBE_FACTORY` 测试缝；`FORMATS_CACHE`；`resolve_format_choice()`。
- `core_control.py`：`TaskControl.format_expr`。
- `core_scheduler.py`：`submit/admit(format_expr)`、`_formats`、`format_for()`。
- `core_engine.py`：`build_command(format_expr=)`；引擎记录所选格式。
- `MediaDock.js` 5.1 + `tests/check_userscript.py` 更新。
- `tests/test_formats.py`（22 用例）、`tests/probe_formats.py` 与
  `tests/probe_formats_result.json`（30 项检查）。
- `docs/stage008-migration.md`，`plan-whole.md` 状态与变更记录 C-007。

---

## 9. 风险与回滚

### 9.1 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 用户文本进入 yt-dlp argv | 命令注入 | 预设表 + `format_id` 正则 + 必须命中 `/formats` 结果；探针断言真实 argv |
| 预设不可用导致下载失败 | 用户看到错误任务 | 非默认预设要求先探测，且 `preset_satisfied()` 前置拒绝 |
| 默认策略被改坏 | MVP 回归 | `best` 预设 = 冻结表达式；T810/T813 显式断言 |
| 探测耗时/超时 | 前端等待 | `PROBE_TIMEOUT=60s`；前端 60s 超时并提示 `formats_unavailable` |
| 探测结果过期 | 选到已消失的格式 | `/formats` 成功结果缓存；`/download` 只接受缓存内 id；缓存随 `bootstrap()` 清空 |
| 格式随重启丢失 | 重试回到默认 | 已记录为已知限制（13.1），不承诺持久化 |

### 9.2 外部依赖

- 仅标准库（`json`、`re`、`subprocess`、`dataclasses`）。
- 运行时依赖本机 yt-dlp 可执行文件（`--dump-single-json`）；缺失时 502。

### 9.3 回滚策略

1. 移除 `/formats` 路由与 `/download` 的格式分支 → 行为回到 Stage-007（默认策略）。
2. `Scheduler.submit/admit` 的 `format_expr` 默认空值 → 无格式记录，引擎走冻结表达式。
3. 前端回退 5.0 并同步 `tests/check_userscript.py` 锚点。
4. 无数据回滚：无 schema、无迁移、无新增 Task 字段。

---

## 10. 阶段验收标准

### 10.1 功能验收

- [x] `/formats` 对 YouTube URL 返回稳定 payload；对非法/未接入 URL 400 且不启动进程。
- [x] 预设与 `format_id` 选择可真正改变下载 argv，且默认路径不变。
- [x] 表外/不可用/未知选择都有明确错误码。

### 10.2 测试与质量验收

- [x] 全量单元测试通过（`Ran 288 tests ... OK`，无 Stage-002..007 回归）。
- [x] 7 个探针全部 OK（既有 6 个 + `probe_formats.py` 30 项检查）。
- [x] `py_compile` 通过；`check_userscript.py` 结构检查通过（591 行）。
- [x] 无新增第三方依赖（`requirements.txt` 未变）。

### 10.3 进入 Stage-009 的条件

- [x] 格式选择边界可用，Stage-009 可复用 `PRESETS`/`selector_for()` 与探测边界。
- [x] `audio` 预设已就位，音频提取/转码明确留给 Stage-009。
- [x] 遗留限制（格式不持久化、无字幕/播放列表）已交接。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-001/002/003 的影响

- `/download` 响应与错误格式不变；Task 字段、状态机、排序、队列语义不变。
- 引擎 argv 默认值不变；`-P` 与 URL 位置不变。

### 11.2 对 Stage-004/005/006/007 的影响

- 控制 API、文件清理边界、存储 schema、配置层、安全守卫、平台检测全部不变。
- 暂停/继续/重试沿用同一 `format_expr`；重启后回落默认（已记录）。

### 11.3 对 Stage-009/010 输出

- Stage-009：复用 `core_formats` 的选择器与探测边界；`audio` 预设只选流不转码；
  媒体处理任务沿用 `platform`/`type` 字段。
- Stage-010：发布说明需写明「格式选择不写入历史记录，重启后重试回落默认策略」，
  并把 `--dump-single-json` 探测纳入依赖与发布检查说明。

### 11.4 不变约束

- 不修改 `Task` 字段、状态集合、API 错误格式与文件策略。
- 不允许任何 HTTP 输入进入 yt-dlp argv。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-19 | 阶段准备 | Cline | 已完成 | 核对 Stage-007 输出与 `build_command()`/`Scheduler.submit()` 契约，冻结预设表、错误码与「格式不入 Task」策略 | Stage-007 基线 266/266 单测 + 6 个探针 + JS 检查 | 无 |
| 2026-09-19 | 任务 001 | Cline | 已完成 | 新增 `core_formats.py`：预设表、`validate_format_id`、`selector_for`、探测 argv、JSON 解析、稳定 payload、`FormatsProbe` | `test_formats.py` T801-T808 通过 | 无 |
| 2026-09-19 | 任务 002 | Cline | 已完成 | `server.py` 新增 `/formats` 与 `/download` 格式分支；`TaskControl.format_expr` + `Scheduler` 发送期记录 + `build_command(format_expr=)` | T809-T812 通过；全量 288/288；默认表达式断言通过 | 无 |
| 2026-09-19 | 任务 003 | Cline | 已完成 | `MediaDock.js` 5.1 清晰度选择器、`probe_formats.py`（真实子进程 argv + spawn 边界 argv）、迁移文档与计划更新 | 7 个探针全 OK；`check_userscript.py` OK（591 行） | 无 |

---

## 13. 实际输出与计划差异

- 原计划输出：`/formats` 契约、格式模型与解析、前端选择器、`format_id` 校验、默认回退。
- 实际输出：全部实现，另加 `tests/probe_formats.py` 的「真实子进程记 argv」证据。
- 差异：
  1. **格式不写入 `Task`/数据库**：改为 `TaskControl.format_expr` + `Scheduler._formats`
     内存记录，避免 schema v2→v3 迁移与历史记录变更。已知限制：服务重启后重试回落默认策略。
  2. **非默认预设必须"先探测"**：`/download` 不接受未经 `/formats` 验证的预设，
     比计划更严格，用来保证「选定格式前验证格式仍可用」。
  3. **`format_id` 必须出现在 `/formats` 结果中**：只做形状校验不足以防止拼装出
     非法选择器，因此要求「形状合法 + 命中本进程探测结果」。
  4. **`audio` 预设只选音频流**：MP3/M4A 提取属 Stage-009，本阶段不提前实现。
  5. **`.bat` 假 yt-dlp 只用于探测命令**：下载 argv 含 `<`，cmd.exe 会当重定向，
     因此下载侧用 `popen_factory` 边界记录 argv（与 `probe_control.py` 同一手法）。
  6. **Userscript 版本 5.0 → 5.1**：`tests/check_userscript.py` 的版本锚点与新增
     预设锚点同步更新，并新增「JS 不得出现 yt-dlp 选择器」检查。
- 差异影响：均为范围澄清与安全收紧，不改变 Task 字段、状态机与既有 API 形状。
- 处理决定：全部接受并写入 `docs/stage008-migration.md`；`plan-whole.md` 增加变更记录
  C-007 并把状态改为「Stage-008 已完成，Stage-009 未开始」。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：允许进入 Stage-009
- 对 Stage-001..007 影响：`/download` 默认路径响应与 argv 不变；Task 字段、状态机、调度、
  存储、配置、安全守卫与平台检测不变；既有 6 个探针、JS 结构检查与 266 个单测全部回归通过
  （迁移后全量 288/288）
- 对 Stage-009/010 影响：Stage-009 复用 `core_formats` 的选择器/探测边界与 `audio` 预设；
  发布说明需写明格式选择不持久化、重启后重试回落默认策略
- 是否更新 `plan-whole.md`：是，当前状态改为「Stage-008 已完成，Stage-009 未开始」，
  Stage-008 完成时间 `2026-09-19`，新增变更记录 C-007（版本仍 `0.2`）
- 审查人：用户
- 日期：2026-09-19
