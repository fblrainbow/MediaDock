# Stage-007：平台 Adapter

> 依据：[plan-whole.md](plan-whole.md) `0.2` §9 Stage-007、[Stage-006.md](Stage-006.md)、[docs/stage006-migration.md](docs/stage006-migration.md)
>
> 本阶段把「平台差异」从 `server.py` 中抽出为 Adapter 层：URL 识别、平台命名、
> 视频 ID 与扩展点走同一条边界。只迁移 YouTube 现有流程并保证行为不变；
> 不新增 TikTok/X/Instagram 的真实下载能力。

---

## 1. 阶段元数据

- 阶段编号：`Stage-007`
- 阶段名称：平台 Adapter
- 总计划版本：`0.2`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-19
- 预计完成时间：2026-09-19
- 当前阻塞：无
- 前置阶段：`Stage-006` 配置、依赖与安全加固（已完成）
- 后续阶段：`Stage-008` 格式选择与 Formats API（依赖本阶段的检测与扩展点）

---

## 2. 阶段目标

```text
GET /download?url=...
        |
        v
core_security.validate_url()        (Stage-006：scheme/长度/控制字符/host)
        |
        v
core_platform.detect_platform(url)  -> (adapter, error_code, message)
        |                                   |
        |  adapter = YouTubeAdapter          +--> 400 invalid_url
        v                                   +--> 400 unsupported_platform
adapter.info(url) -> PlatformInfo{name, url, video_id}
        |
        v
Scheduler.submit(info.url, platform=info.name)
```

### 2.1 本阶段成功标准

- URL → 平台识别只有一处真值：`core_platform.DEFAULT_REGISTRY`；`server.py` 不再写死 `"youtube"`。
- YouTube 现有行为不变：`GET /download` 响应形状、Task 字段、状态机、排序、控制 API 全部回归通过。
- 非 YouTube 的 http(s) URL 被明确拒绝：400 `unsupported_platform`，且**不会**被任意 Adapter 处理、不创建 Task。
- 非法 URL 仍返回 400 `invalid_url`（Stage-006 契约不变）。
- Adapter 只有单一职责边界：识别 + 归一化 + 元信息；不碰状态机、不碰 HTTP、不碰文件清理。
- 未接入的平台不被写成「已支持」；`ready=False` 的 Adapter 只会得到 `platform_not_ready`。

---

## 3. 范围边界

### 3.1 本阶段包含

- 新增 `core_platform.py`：`PlatformInfo`、`PlatformAdapter`、`YouTubeAdapter`、`PlatformRegistry`、`detect_platform()`、`platform_names()`。
- `server.py` 的 `/download` 改为「校验 → 检测 → 归一化 → `scheduler.submit(url, platform)`」。
- 新增错误码 `unsupported_platform`(400) 与 `platform_not_ready`(400)。
- 扩展点：`registry.register(adapter)` + `ready` 标志；为 Stage-008/009 预留平台名与视频 ID。
- 测试：`tests/test_platform.py`、`tests/probe_platform.py`（真实 HTTP 链路证据）。
- 文档：`docs/stage007-migration.md`。

### 3.2 本阶段明确不包含

- 新增真实平台（TikTok/X/Instagram/Facebook）的下载能力、鉴权与 Cookie。
- 平台信息探测（调用 yt-dlp 拿标题/时长）与 `/formats`（Stage-008）。
- 音频模式与媒体处理（Stage-009）。
- 修改 `Task` 字段、状态机、API 错误格式、并发与文件策略。
- Userscript 改动（前端只发 URL，平台由服务端判定）。

---

## 4. 输入契约

### 4.1 Stage-006 已确认输入

- `core_security.validate_url()` 是唯一 URL 校验入口（scheme/长度/控制字符/host/userinfo）。
- `Task.platform` 字段自 Stage-002 起存在，默认 `"youtube"`，已在 `tasks.db` 中持久化。
- `Scheduler.submit(url, platform="youtube")` 已支持平台参数；`TaskManager.create()` 写入 `platform`。
- `core_files.video_id_from_url()` 已是 YouTube 语义（`v=`、`/shorts/`、`youtu.be/`），文件清理依赖它。

### 4.2 保持不变的约束

- `GET /download` 成功响应仍是 `{"task_id": "..."}`；错误仍是 `{error_code, message}`。
- `Task` 字段与状态集合不变；`platform` 仍只是展示/分流字段，不参与状态机。
- 并发上限、FIFO 队列、暂停/取消/重试语义、文件边界策略不变。
- 不允许任何 HTTP 输入进入 argv（Stage-006 5.5 继续有效）。

### 4.3 高影响决策门禁

| 编号 | 决策 | 本阶段处理 |
| --- | --- | --- |
| D-005 | 多平台是 MVP 还是后续 | 维持后续：本阶段只落地 Adapter 边界与 YouTube 迁移，不新增平台 |
| D-004 | 默认监听 `127.0.0.1` | 不变；检测层不做网络访问，只做纯字符串判定 |

---

## 5. 阶段契约

### 5.1 适配器模型

| 成员 | 类型 | 说明 |
| --- | --- | --- |
| `name` | str | 平台标识，写入 `Task.platform`（`youtube`） |
| `ready` | bool | `False` = 已识别但未接入，返回 `platform_not_ready` |
| `hosts` | tuple | 匹配用的域名（含子域） |
| `matches(url)` | bool | 纯字符串判定，不做网络访问 |
| `normalize(url)` | str | 归一化（当前 YouTube 原样返回，仅去掉 fragment） |
| `video_id(url)` | str | 最佳努力，未知返回 `""` |
| `info(url)` | `PlatformInfo` | 组装 `{name, url, video_id}` |

### 5.2 注册表与检测

- `PlatformRegistry.register(adapter)`：按注册顺序匹配，第一个命中的 Adapter 胜出。
- `PlatformRegistry.detect(url) -> (adapter, error_code, message)`：
  1. `validate_url()` 失败 → `(None, "invalid_url", message)`；
  2. 无 Adapter 命中 → `(None, "unsupported_platform", message)`，**不创建 Task**；
  3. 命中但 `ready=False` → `(None, "platform_not_ready", message)`；
  4. 否则 `(adapter, "", "")`。
- `DEFAULT_REGISTRY` 只注册 `YouTubeAdapter`；`platform_names()` 返回已注册平台名。
- 检测只做字符串判定，因此可以被单元测试离线覆盖。

### 5.3 HTTP 契约

| 场景 | 响应 |
| --- | --- |
| `/download?url=<youtube>` | 200 `{"task_id": "..."}`；Task 的 `platform="youtube"` |
| `/download?url=<其他 http(s)>` | 400 `unsupported_platform`，无 Task 产生 |
| `/download?url=<非法>` | 400 `invalid_url` |
| `/download`（缺 url） | 400 `missing_url`（不变） |

新增错误码：`unsupported_platform`(400)、`platform_not_ready`(400)。既有错误码不变。

### 5.4 责任边界

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `core_platform` | 平台识别、归一化、视频 ID、扩展注册、错误码选择 | 状态机、HTTP、文件、配置 |
| `server.Handler` | 调用检测并把错误码转成响应 | 自己判断平台 |
| `core_scheduler` | 用 `platform` 创建 Task 并调度 | 识别 URL |
| `core_files` | 沿用 YouTube 视频 ID 做文件边界 | 平台分流 |

### 5.5 不变量

| 条件 | 必须成立 |
| --- | --- |
| 单点识别 | 平台判定只发生在 `core_platform`，其他模块不得写死平台字符串 |
| 无越权分发 | 未命中或未就绪的 URL 不会进入任何 Adapter 的 `info()` |
| 无副作用检测 | `detect()` 不访问网络、不创建 Task、不改状态 |
| 兼容 | Task 字段、状态机、API 形状、错误格式、文件策略不变 |
| 可扩展 | 新增平台只需注册 Adapter，不改 `server.py` 与状态机 |

---

## 6. 具体任务

### 任务 001：Adapter 边界与 YouTube 迁移

**涉及文件：** 新增 `core_platform.py`、`tests/test_platform.py`

**内容：** `PlatformInfo`、`PlatformAdapter`、`YouTubeAdapter`（`hosts`/`video_id`/`normalize`）、`PlatformRegistry`、`DEFAULT_REGISTRY`、`detect_platform()`、`platform_names()`。

**完成标准：**

- [x] `youtube.com`/`www.youtube.com`/`m.youtube.com`/`youtu.be`/`youtube-nocookie.com` 命中 `YouTubeAdapter`。
- [x] `video_id()` 覆盖 `watch?v=`、`/shorts/`、`youtu.be/`；未知返回 `""`。
- [x] 未注册域名返回 `unsupported_platform`，且 `info()` 未被调用（间谍 Adapter 计数为 0）。
- [x] `ready=False` 的 Adapter 返回 `platform_not_ready`。
- [x] 注册顺序决定优先级（同域多 Adapter 时第一个胜出）。

### 任务 002：HTTP 接入与回归

**涉及文件：** `server.py`、`tests/test_platform.py`

**内容：** `/download` 使用检测结果；`scheduler.submit(info.url, platform=adapter.name)`；错误码映射。

**完成标准：**

- [x] YouTube URL → 200 且 Task `platform=="youtube"`。
- [x] `https://www.tiktok.com/@a/video/1` 等 → 400 `unsupported_platform`，该任务不存在。
- [x] 非法 URL 仍 400 `invalid_url`；缺 url 仍 400 `missing_url`。
- [x] Stage-002..006 的 `/download` 相关测试全部回归通过。

### 任务 003：探针与文档

**涉及文件：** `tests/probe_platform.py`、`docs/stage007-migration.md`、`Stage-007.md`、`plan-whole.md`

**内容：** 真实 HTTP 链路探针（含扩展点演示）、迁移说明、计划状态更新。

**完成标准：**

- [x] 探针证据写入 `tests/probe_platform_result.json`，全部检查为 true。
- [x] 文档记录 Adapter 契约、错误码、扩展步骤与未接入平台说明。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| T701 | YouTube 域名匹配 | 单元 | 5 类域名命中 `youtube` |
| T702 | 非 YouTube 域名 | 单元 | `unsupported_platform`，不创建 Task |
| T703 | 非法 URL | 单元 | `invalid_url`（沿用 Stage-006 校验） |
| T704 | 视频 ID 提取 | 单元 | `watch`/`shorts`/`youtu.be` 正确，未知为空串 |
| T705 | 未就绪 Adapter | 单元 | `platform_not_ready` |
| T706 | 注册顺序优先级 | 单元 | 第一个命中的 Adapter 胜出 |
| T707 | 未命中不调用 `info()` | 单元 | 间谍 Adapter 计数为 0（无越权分发） |
| T708 | `/download` YouTube | API | 200；`Task.platform=="youtube"` |
| T709 | `/download` 不支持平台 | API | 400 `unsupported_platform`，任务不存在 |
| T710 | 既有 `/download` 回归 | 回归 | `invalid_url`/`missing_url` 与 Stage-001..006 一致 |
| T711 | 平台检测无副作用 | 单元 | 不访问网络、不写数据库、不改状态 |
| T712 | 扩展点 | 单元 | 运行期注册新 Adapter 即被检测到 |
| T713 | 探针链路 | 探针 | `probe_platform_result.json` 全部 true |

### 7.2 建议命令

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_platform.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_platform.py
```

### 7.3 测试原则

- 检测层是纯函数，测试不依赖网络、不启动 yt-dlp。
- HTTP 层测试沿用 `tests/helpers.py` 的 `InstantEngine`，不产生真实下载。
- 探针使用临时数据库与真实 `server.Handler`，只替换进程 spawn 边界。

---

## 8. 阶段产物

- `core_platform.py`：`PlatformInfo`、`PlatformAdapter`、`YouTubeAdapter`、`PlatformRegistry`、`DEFAULT_REGISTRY`、`detect_platform()`、`platform_names()`。
- `server.py`：`/download` 走检测层，新增 `unsupported_platform`/`platform_not_ready` 错误码。
- `tests/test_platform.py`（T701-T712，18 用例）、`tests/probe_platform.py` 与证据 JSON `tests/probe_platform_result.json`。
- 既有 HTTP 用例的夹具 URL 更新：`test_apiv2.py`、`test_control_api.py`、`test_history.py`、`test_listing.py`、`test_sec_api.py`、`probe_security.py` 的「正常下载」路径改用 YouTube URL。
- `docs/stage007-migration.md`，`plan-whole.md` 状态与变更记录 C-006。

---

## 9. 风险、依赖与回滚

### 9.1 风险

| 风险 | 影响 | 缓解措施 |
| --- | --- | --- |
| 检测误判导致合法 YouTube URL 被拒 | 用户下载失败 | `hosts` 用「域名或子域」精确匹配并覆盖 `m.`/`music.`/`nocookie`；回归测试覆盖 |
| 平台字符串被别处写死 | 扩展时漏改 | `Task.platform` 只由 `info().name` 提供；测试断言 `platform=="youtube"` |
| 未就绪平台被当成可用 | 误导用户 | `ready=False` → `platform_not_ready`；文档明确列出未接入平台 |
| 检测层偷偷做网络访问 | 启动/请求变慢或失败 | 契约与测试要求纯字符串判定（T711） |
| 新增错误码破坏前端 | Userscript 显示异常 | 前端只按 `error_code` 展示文本；`message` 已可读；Stage-002..006 回归覆盖 |

### 9.2 外部依赖

- 仅标准库（`urllib.parse`、`dataclasses`）。
- 无新增第三方包；无网络访问。

### 9.3 回滚策略

1. 代码回滚：`server.py` 的 `/download` 恢复为直接 `scheduler.submit(url)` 即可（默认平台仍是 `youtube`）。
2. 数据回滚：`platform` 字段早已存在，无需 schema 迁移。
3. 错误码回滚：`unsupported_platform` 只是新增分支；去掉后非 YouTube URL 会回到「被 yt-dlp 拒绝」的旧行为。

---

## 10. 阶段验收标准

### 10.1 平台边界验收

- [x] 平台识别只有 `core_platform` 一处真值，`server.py` 不再写死平台字符串。
- [x] YouTube 现有流程行为不变（响应形状、Task 字段、状态机、控制 API）。
- [x] 未命中/未就绪 URL 明确拒绝且不创建 Task、不进入 Adapter。

### 10.2 测试与质量验收

- [x] 全量单元测试通过（Stage-002..006 无回归）。
- [x] 探针全部 OK（既有 5 个探针 + `probe_platform.py`）。
- [x] `py_compile` 通过，无新增第三方依赖。
- [x] 未接入平台没有被写成「已支持」。

### 10.3 进入 Stage-008 的条件

- [x] 平台检测与 `Task.platform` 可作为 `/formats` 的前置判定。
- [x] Adapter 扩展点可用（Stage-008 只处理 YouTube 格式）。
- [x] 遗留限制（无鉴权平台、无平台信息探测）已交接。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-001/002/003 的影响

- `/download` 响应与错误格式不变；Task 字段与状态机不变；调度与队列语义不变。
- 平台判定从「隐式默认」变为「显式检测」，默认仍是 `youtube`。

### 11.2 对 Stage-004/005/006 的影响

- 控制 API、文件清理边界、存储 schema、配置层与安全守卫全部不变。
- 新增错误码与既有错误码并存；安全守卫仍先于平台检测执行。

### 11.3 对 Stage-008/009/010 输出

- Stage-008：`/formats?url=` 必须先过 `detect_platform()`，只对 `ready` 平台查询。
- Stage-009：媒体处理任务沿用同一 `platform` 字段与 `Task.type` 预留位。
- Stage-010：发布检查需说明「支持平台 = YouTube」，其余平台为显式拒绝。

### 11.4 不变约束

- 不修改 `Task` 字段、状态集合、API 错误格式与文件策略。
- 不引入网络访问或凭据存储；不承诺未接入平台的可用性。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-19 | 阶段准备 | Cline | 已完成 | 核对 Stage-006 输出、`Task.platform` 与 `Scheduler.submit(platform=)` 契约，冻结 Adapter 边界与错误码 | Stage-006 基线 248/248 单测 + 5 个探针 + JS 结构检查 | 无 |
| 2026-09-19 | 任务 001 | Cline | 已完成 | 新增 `core_platform.py`：`PlatformInfo`/`PlatformAdapter`/`YouTubeAdapter`/`PlatformRegistry`/`detect_platform`/`platform_names` | `test_platform.py` 通过：T701/T704 域名与视频 ID、T702/T703/T705/T706/T707/T711/T712 | 无 |
| 2026-09-19 | 任务 002 | Cline | 已完成 | `server.py` `/download` 改为检测层驱动并传 `platform`；移除 `server.py` 的平台字符串 | T708/T709/T710 通过；`Task.platform=="youtube"`、fragment 归一化、400 不含 `task_id` | 无 |
| 2026-09-19 | 任务 003 | Cline | 已完成 | `tests/probe_platform.py`（18 项检查）、`docs/stage007-migration.md`、`plan-whole.md` 状态更新；既有 HTTP 夹具 URL 迁移到 YouTube | 全量 266/266 单测通过；6 个探针全 OK；`check_userscript.py` OK（472 行） | 无 |

---

## 13. 实际输出与计划差异

- 原计划输出：Adapter 边界、YouTube 迁移、平台 Detector、不支持错误码、测试与文档。
- 实际输出：全部实现（`core_platform.py`、`server.py` 接入、2 个错误码、`tests/test_platform.py`、`tests/probe_platform.py`、`docs/stage007-migration.md`）。
- 差异：
  1. **未新增 `/platforms` 接口**：计划只要求 Detector 与错误码；`platform_names()` 先作为模块能力保留，接口留给 Stage-008。
  2. **`normalize()` 仅去掉 fragment**：本阶段不重写 URL，避免改变 yt-dlp 行为；参数级归一化留待 Stage-008。
  3. **`video_id()` 复用 `core_files.video_id_from_url()`**：文件清理边界仍以既有 YouTube 语义为准，Adapter 只转发，避免两套解析。
  4. **非 http(s) 输入仍返回 `invalid_url`**：`unsupported_platform` 只对合法 http(s) 但未注册的域名生效，保持 Stage-006 契约。
  5. **既有 HTTP 夹具 URL 迁移**：Stage-001..006 的「正常下载」用例原本用 `https://example.com/...` 充当任意合法 URL；Stage-007 后该形状按设计被 400 `unsupported_platform` 拒绝，因此这些用例改用等价的 YouTube URL（仅测试夹具，产品契约不变）。`invalid_url` 用例（`ftp://`、控制字符、超长、userinfo）保持在 `example.com` 上，用于证明校验先于平台判定。
  6. **`detect()` 不调用 `info()`**：由 HTTP 层在拿到 adapter 后调用，保证「未命中/未就绪的 URL 不进入任何 Adapter 的 `info()`」可用间谍 Adapter 直接断言。
- 差异影响：均为范围澄清，不改变 Task 字段、状态机与既有 API 形状。
- 处理决定：全部接受并写入 `docs/stage007-migration.md`；`plan-whole.md` 增加变更记录 C-006 并把状态改为「Stage-007 已完成，Stage-008 未开始」，同时把 D-013 的决议固化为「首批正式支持平台 = YouTube，其余显式拒绝」。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：允许进入 Stage-008
- 对 Stage-001..006 影响：`/download` 响应形状与错误格式不变（新增 400 分支并存）；Task 字段、状态机、调度、存储、配置与安全守卫不变；既有 5 个探针、JS 结构检查与 248 个单测全部回归通过（迁移后全量 266/266）
- 对 Stage-008/009/010 影响：`/formats` 必须先过 `detect_platform()`；媒体处理任务沿用 `platform`/`type` 字段；发布说明需写明仅 YouTube 已接入
- 是否更新 `plan-whole.md`：是，当前状态改为「Stage-007 已完成，Stage-008 未开始」，Stage-007 完成时间 `2026-09-19`，新增变更记录 C-006（版本仍 `0.2`）
- 审查人：用户
- 日期：2026-09-19


