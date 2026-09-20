# Stage-010：发布、回归与长期扩展

> 依据：[plan-whole.md](plan-whole.md) `0.2` §9 Stage-010、[Stage-009.md](Stage-009.md)、
> [docs/stage009-migration.md](docs/stage009-migration.md)
>
> 阶段 001-009 交付的是「能跑的功能」，本阶段交付的是「能重复安装、能验证、
> 能交付、能回滚的版本」：把散落在 9 份阶段文件里的契约收敛成一份可执行发布检查，
> 并把版本号、变更日志、升级/回滚、已知限制和发布后观察写成文档。

---

## 1. 阶段元数据

- 阶段编号：`Stage-010`
- 阶段名称：发布、回归与长期扩展
- 总计划版本：`0.3`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-20
- 预计完成时间：2026-09-20
- 当前阻塞：无
- 前置阶段：`Stage-005`（持久化）、`Stage-008`（格式选择）、`Stage-009`（媒体处理），
  `Stage-001`~`Stage-009` 全部已完成
- 后续阶段：无（本阶段同时输出长期扩展边界）

---

## 2. 阶段目标

```text
core_release.APP_VERSION = "1.0.0"        <-- 版本号唯一真值
        |
        +--> /health.version {app, api, userscript, schema}
        +--> README.md / docs/release-notes.md（变更日志同源）
        +--> MediaDock.js @version（前端版本一致性锚点）
        |
tests/release_check.py
        |
        +--> 静态检查：文档齐全 / 版本一致 / 示例配置键对齐
        +--> 运行期检查：真实 HTTP 全链路 + 重启 + 回滚演练 + 安全守卫
        +--> tests/release_check_result.json（可复核证据）

docs/install.md  docs/configuration.md  docs/userscript.md
docs/release-notes.md  docs/known-limitations.md
docs/upgrade-rollback.md  docs/release-checklist.md
```

### 2.1 本阶段成功标准

- **一条命令给出发布结论**：`tests\release_check.py` 退出码 0 表示「可交付」，
  退出码 1 表示「不可交付」，并把每项检查写入 `tests/release_check_result.json`。
- **版本只有一个真值**：`core_release.APP_VERSION`；`/health.version.app`、
  `docs/release-notes.md` 最新条目、`README.md` 版本行必须与它一致，
  不一致时发布检查失败（不允许两处手写版本号各自漂移）。
- **新环境可照文档启动**：`docs/install.md` 的步骤在只有 Python + venv 的机器上
  可以完成「装依赖 → `--check-config` → 启动 → `/health` 200」。
- **回归可执行**：Stage-001~009 的 308 个单测 + 8 个探针 + JS 结构检查 + 发布检查
  全部通过，且发布检查覆盖重启、回滚、错误码与安全守卫。
- **可回滚**：代码回滚（git 基线）、数据回滚（`tasks.db` 备份/恢复）、
  降级运行（无 `config.json`、数据库不可用 → 内存模式）三条路径都在文档里
  写明命令，并由发布检查实际演练其中两条。
- **有观察窗口和异常升级规则**：`docs/release-checklist.md` 写明发布后观察项、
  观察窗口、阈值触发条件和升级/回滚条件。
- **已知限制成文**：`docs/known-limitations.md` 逐条列出不支持场景，
  不再散落在 9 份阶段文件的「差异」小节里。

---

## 3. 范围边界

### 3.1 本阶段包含

- 新增 `core_release.py`：版本真值、文档清单校验、变更日志解析、
  `config.example.json` 与 `core_config.DEFAULTS` 键对齐校验、`release_info()`。
- `server.py` 的 `/health` 增加 `version` 字段（`app`/`api`/`userscript`/`schema`）。
- `MediaDock.js` 5.2：显示服务端版本，并在用户脚本版本与服务端期望不一致时给出提示。
- 新增 `tests/test_release.py`（静态检查单元测试）。
- 新增 `tests/release_check.py` + `tests/release_check_result.json`
  （真实 HTTP 全链路、重启、回滚演练、安全守卫）。
- 新增文档：`README.md`、`docs/install.md`、`docs/configuration.md`、
  `docs/userscript.md`、`docs/release-notes.md`、`docs/known-limitations.md`、
  `docs/upgrade-rollback.md`、`docs/release-checklist.md`、`docs/stage010-migration.md`。
- 明确 D-014：发布方式为「手动启动 + 可选开机自启脚本」，不做安装包、不做 Windows 服务。
- 长期扩展评估：写入 `docs/release-notes.md` 的「下一步候选」，只记录不实现。

### 3.2 本阶段明确不包含

- 新增平台（Stage-007 决议 D-013 不变：首批只支持 YouTube）。
- 新增媒体处理能力（剪辑、字幕、缩略图、批量队列）。
- 安装包、MSI、Docker、Windows 服务、开机自启的自动注册（只提供脚本与文档）。
- 修改 `Task` 字段、状态集合、存储 schema（保持 v2）、排序与显示契约。
- 修改既有 API 的路径、方法、成功响应形状与既有错误码。
- 把阶段文件的历史记录重写为统一格式。

---

## 4. 输入契约

### 4.1 前置阶段已确认输入

- `Stage-005`：SQLite schema v2、`tasks.db` 迁移前自动备份、`interrupted` 语义、
  `/history` 与 `/events`、`POST /delete`、`storage` 载荷（含 `schema_version`/`degraded`）。
- `Stage-006`：`core_config`（优先级 `显式路径 > 环境变量 > config.json > 内置默认`）、
  `core_deps.run_checks()`、`core_security`、`python server.py --check-config`（退出码 0/1/2）。
- `Stage-008`：`GET /formats` 与格式选择错误码；`Stage-009`：`GET/POST /audio`、
  `type="audio"` 媒体任务与 `core_media` 错误码。
- `plan-whole.md` §11 统一测试策略：单元 → 集成 → 端到端 → 回归 → 发布后验证，
  以及 §11.2 的最低测试矩阵（含「发布」一行）。
- 既有测试资产：308 个单测、8 个探针（各带 `probe_*_result.json`）、
  `tests/check_userscript.py`。

### 4.2 保持不变的约束

- 不改 `Task` 字段、状态集合、状态机、`/tasks` 排序与显示契约。
- 不改存储 schema（仍 v2）、不改并发上限默认值（3）、不改文件清理策略（D-009）。
- 不改既有 API 的路径/方法/成功响应形状/既有错误码；`/health` 只新增字段。
- 不要求任何新的第三方 Python 包（`requirements.txt` 仍无第三方依赖）。
- `config.example.json` 的键必须与 `core_config.DEFAULTS` 完全一致。

---

## 5. 阶段契约

### 5.1 模块责任

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `core_release` | 版本真值、文档清单、变更日志解析、配置键对齐、`release_info()` | HTTP、启动服务、跑探针 |
| `server.Handler` | `/health` 增加 `version`（只读 `core_release`） | 计算版本、读写文档 |
| `tests/release_check.py` | 组装静态 + 运行期检查，产出结论与 JSON 证据 | 修改产品代码 |
| `tests/test_release.py` | `core_release` 纯函数单元测试 | 启动服务 |
| `docs/*` | 安装、配置、前端安装、变更日志、限制、升级回滚、发布清单 | 定义契约（契约在 Stage 文件） |
| `MediaDock.js` | 展示服务端版本 + 版本不一致提示 | 决定发布内容 |

### 5.2 不变量

| 条件 | 必须成立 |
| --- | --- |
| 版本单点 | 只有 `core_release.APP_VERSION` 是版本真值，其他位置由检查保证一致 |
| 只读发布检查 | 发布检查不修改仓库文件（只写 `tests/release_check_result.json`） |
| 无网络依赖 | 静态检查 + 运行期检查在本机离线可执行（不访问 YouTube） |
| 临时隔离 | 运行期检查用临时目录做 `download_dir`/`db_path`，不污染仓库 `downloads/` 与 `tasks.db` |
| 兼容 | 既有 API 与 `Task` 模型不变；`/health` 新字段为增量 |
| 可回滚 | 新增文件可整体删除回到 Stage-009 行为；`/health.version` 是有害性最低的增量 |

### 5.3 版本与交付契约

| 对象 | 值 | 检查位置 |
| --- | --- | --- |
| 应用版本 | `core_release.APP_VERSION = "1.0.0"` | `/health.version.app`、README、release-notes 最新条目 |
| API 契约代 | `core_release.API_VERSION = "1"` | `/health.version.api` |
| 用户脚本 | `core_release.USERSCRIPT_VERSION = "5.2"` | `MediaDock.js @version`、`check_userscript.py` |
| 存储 schema | `2`（来自 `core_store`） | `/health.version.schema`、`/health.storage.schema_version` |
| 发布方式 | 手动启动 + 可选开机自启脚本（D-014） | `docs/install.md` |
| 回滚基线 | `Stage-009 completed`（commit `545d781`） | `docs/upgrade-rollback.md` |

### 5.4 HTTP / CLI 增量

| 场景 | 响应 |
| --- | --- |
| `GET /health` | 200，新增 `version: {app, api, userscript, schema}`；其余字段不变 |
| `GET /health` 在无数据库时 | `version.schema = 0`（与 `storage.schema_version` 一致） |
| `python server.py --check-config` | 退出码不变（0/1/2） |

新增错误码：无。既有错误码全部不变。

---

## 6. 具体任务

### 任务 001：版本真值与静态发布检查（`core_release.py`、`tests/test_release.py`）

- 涉及文件：`core_release.py`（新增）、`tests/test_release.py`（新增）。
- 内容：
  - `APP_VERSION`/`API_VERSION`/`USERSCRIPT_VERSION`/`RELEASE_DATE` 常量。
  - `is_valid_version()`/`version_tuple()`：`X.Y.Z` 形状校验与比较。
  - `parse_userscript_version(source)`：从 `// @version x.y` 取版本（找不到返回 `""`）。
  - `latest_changelog_version(text)`：`## [1.0.0] - 2026-09-20` 形式取最新版本。
  - `REQUIRED_DOCS` + `missing_docs(base_dir)`：发布必需文档存在性。
  - `config_key_report(example_path, defaults)`：示例文件与 `core_config.DEFAULTS` 的
    键集合差异（缺失/多余/顺序）。
  - `release_info(schema_version=None)`：`/health.version` 载荷。
  - `format_report(checks)`：文本报告（OK/FAIL 逐项）。
- 依赖：无（只用标准库）。

### 任务 002：`/health.version` 与用户脚本版本一致性

- 涉及文件：`server.py`、`MediaDock.js`、`tests/check_userscript.py`。
- 内容：
  - `server.py` 导入 `core_release`，`/health` 增加 `version`（`schema` 取 `storage_info()`）。
  - `MediaDock.js` 5.2：面板状态行显示服务端版本；当 `/health.version.userscript`
    与自身 `@version` 不同时显示「脚本版本与服务端不一致」提示。
  - `check_userscript.py` 版本锚点 `5.1` → `5.2`，新增 `SERVER_VERSION` 与
    `userscriptVersion`/`versionSkew` 锚点。
- 依赖：任务 001。

### 任务 003：发布检查脚本（`tests/release_check.py`）

- 涉及文件：`tests/release_check.py`（新增）、`tests/release_check_result.json`（产物）。
- 内容：静态检查 + 运行期检查（详细清单见 §7.1），输出 JSON 与退出码。
- 运行期检查使用临时目录（`MEDIADOCK_DB`、`download_dir` 均为临时路径），
  引擎用脚本化假进程，**不访问网络**。
- 依赖：任务 001、002。

### 任务 004：交付文档

- 涉及文件：`README.md`、`docs/install.md`、`docs/configuration.md`、
  `docs/userscript.md`、`docs/release-notes.md`、`docs/known-limitations.md`、
  `docs/upgrade-rollback.md`、`docs/release-checklist.md`。
- 内容：安装与启动、配置键/环境变量、用户脚本安装与更新、变更日志（与
  `APP_VERSION` 一致）、已知限制、升级与回滚（含 D-014 决议）、发布检查清单与
  发布后观察/异常升级规则。
- 依赖：无（可与 001-003 并行）。

### 任务 005：发布检查执行与证据归档

- 涉及文件：`docs/stage010-migration.md`、`tests/release_check_result.json`。
- 内容：按 §7.2 命令顺序执行全量回归 + 发布检查，记录真实输出摘要；
  差异与遗留问题写入迁移文档与本文件 §12/§13。
- 依赖：任务 001-004。

### 任务 006：计划收口与长期扩展边界

- 涉及文件：`plan-whole.md`。
- 内容：状态改为「全部阶段已完成」、版本 `0.3`；Stage-009/010 完成时间；
  D-014 决议为已确认；新增变更记录 C-009（本阶段增量清单）。
  「下一步候选」不进入计划，只记录在 `docs/release-notes.md`。
- 依赖：任务 005。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| R1001 | 版本形状与比较 | 单元 | `1.0.0` 合法；`1.0`/`v1.0.0`/空 非法 |
| R1002 | 用户脚本版本解析 | 单元 | 从真实 `MediaDock.js` 头取到 `5.2`；缺失返回 `""` |
| R1003 | 变更日志最新版本 | 单元 | 解析出 `1.0.0`；空/无条目返回 `""` |
| R1004 | 必需文档存在性 | 单元 | 仓库内齐全；临时空目录全部报缺失 |
| R1005 | 配置键对齐 | 单元 | `config.example.json` 与 `DEFAULTS` 键集合一致 |
| R1006 | `release_info` 载荷 | 单元 | 字段集合锁定；`schema` 缺省为 0 |
| R1007 | 静态发布检查 | 单元 | 版本一致 / 文档齐全 / 配置对齐 全通过 |
| R1008 | `/health.version` | API | 200；`app`=`APP_VERSION`、`api`=`1`、`userscript`=`5.2`、`schema`=2 |
| R1009 | 无数据库时版本 | API | 内存模式下 `version.schema == storage.schema_version == 0` |
| R1010 | 全链路健康检查 | 发布 | `/health`、`/tasks`、`/history`、`/audio` 均 200 且形状稳定 |
| R1011 | 错误码回归 | 发布 | `missing_url`/`invalid_url`/`unsupported_platform`/`invalid_format`/`task_not_found`/`invalid_task_id`/`not_found` 与既有阶段一致 |
| R1012 | 安全守卫回归 | 发布 | 非法 `Host` → 403 `forbidden_host`；非法 `Origin` → 403 `forbidden_origin` |
| R1013 | 重启语义回归 | 发布 | `downloading` 行重启后为 `error` + `interrupted`；`paused` 保持；`completed` 记录与排序不变 |
| R1014 | 回滚演练（数据） | 发布 | 用迁移前备份恢复数据库后服务可启动、历史记录可见 |
| R1015 | 回滚演练（降级） | 发布 | 数据库不可用 → `degraded=true` 内存模式仍能创建任务 |
| R1016 | 终态删除回归 | 发布 | `POST /delete` 终态 200 且后续查询 404；非终态 409 |
| R1017 | `--check-config` | 发布 | 退出码 0，输出含 `config`+`dependencies` |
| R1018 | JS 结构检查 | 发布 | `check_userscript.py` 通过（含 5.2 与版本一致性锚点） |
| R1019 | 既有探针回归 | 发布 | 8 个探针全部 OK（含 Stage-009 媒体探针） |
| R1020 | 全量单元测试 | 回归 | `Ran 335 tests ... OK`，无 Stage-001~009 回归 |

### 7.2 执行命令

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_release.py core_config.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\release_check.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\check_userscript.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe server.py --check-config
```

### 7.3 测试原则

- 发布检查必须**离线可跑**：不访问 YouTube，不下载真实媒体。
- 发布检查的产物是 `tests/release_check_result.json`（可复核、可对比），
  文本报告只作为人读摘要。
- 静态检查不依赖 `CWD`：一律以 `core_release` 所在目录为仓库根。
- 运行期检查使用真实 `server.Handler` + 真实 SQLite + 真实 HTTP，
  只把子进程替换为脚本化对象（与 `probe_persist.py` 同一手法）。

---

## 8. 阶段产物

- `core_release.py`：版本真值 + 发布静态检查纯函数。
- `server.py`：`/health.version`。
- `MediaDock.js` 5.2 + `tests/check_userscript.py` 版本锚点。
- `tests/test_release.py`、`tests/release_check.py`、`tests/release_check_result.json`。
- `README.md` + 7 份 `docs/*` 交付文档 + `docs/stage010-migration.md`。
- `plan-whole.md` 收口（状态、版本 `0.3`、D-014、C-009）。

---

## 9. 风险与回滚

### 9.1 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 文档与代码漂移 | 用户按文档启动失败 | 发布检查校验文档存在性、版本一致性与配置键对齐 |
| `/health` 增量破坏前端 | 任务面板白屏 | 新字段为增量；`check_userscript.py` 保持只读已有字段 |
| 发布检查污染仓库 | 误删真实下载或数据库 | 运行期检查用临时 `download_dir`/`db_path` |
| 回滚演练误删数据 | 真实 `tasks.db` 丢失 | 演练只操作临时目录中的库与备份 |
| 版本号多处手写 | 版本漂移 | `APP_VERSION` 单点 + 检查强制一致 |
| 过度扩张范围 | 本阶段做不完 | 长期候选只记录不实现；不做安装包/服务/新平台 |

### 9.2 外部依赖

- 仅标准库；运行期检查不依赖 yt-dlp/FFmpeg 是否存在（用脚本化子进程边界）。
- `--check-config` 与探针仍按既有约定使用本机 yt-dlp/FFmpeg（缺失时如实报告）。

### 9.3 回滚策略

1. 代码：删除 `core_release.py`、`tests/release_check.py`、`tests/test_release.py`
   与交付文档；`server.py` 去掉 `/health.version`；`MediaDock.js` 回退 5.1 并同步
   `check_userscript.py` 锚点 → 行为回到 Stage-009。
2. 数据：无 schema 变更、无迁移，无需数据回滚；如需回到 Stage-009 基线，
   使用 `docs/upgrade-rollback.md` 的 git + 数据库备份步骤。
3. 前端：Tampermonkey 重新安装上一版本源码即可，不影响服务端。

---

## 10. 阶段验收标准

### 10.1 功能验收

- [x] 新环境可按 `docs/install.md` 完成「依赖 → `--check-config` → 启动 → `/health` 200」。
- [x] 版本号单点可验证：`/health.version.app`、README、`release-notes` 最新条目一致。
- [x] `tests/release_check.py` 退出码 0，`tests/release_check_result.json` 全部 `true`。
- [x] 核心流程（创建、状态、错误、控制、删除）、重启语义与安全守卫均通过回归。
- [x] 回滚路径至少两条（数据恢复、降级运行）被实际演练。
- [x] 已知限制与不支持场景成文（`docs/known-limitations.md`）。
- [x] 发布后观察窗口与异常升级规则成文（`docs/release-checklist.md`）。

### 10.2 测试与质量验收

- [x] 全量单元测试通过（`Ran 335 tests ... OK`，无 Stage-001~009 回归）。
- [x] 8 个探针全部 OK；`check_userscript.py` 通过。
- [x] `py_compile` 通过；`server.py --check-config` 退出码 0。
- [x] 无新增第三方依赖（`requirements.txt` 仅注释变化或不变）。

### 10.3 完成条件（进入「已交付」）

- [x] `plan-whole.md` 收口到版本 `0.3` 且状态为全部阶段已完成。
- [x] `docs/stage010-migration.md` 记录升级、回滚与遗留限制。
- [x] 阶段 001-010 的端到端链路（下载 → 状态 → 格式 → 音频 → 历史 → 发布检查）无断裂。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-001/002/003 的影响

- Task 字段、状态机、`/download`/`/status`/`/tasks` 形状不变。
- 排序与显示契约（D-015/D-016）不变；发布检查把它们记为回归项。

### 11.2 对 Stage-004/005/006/007 的影响

- 控制 API、文件清理策略、存储 schema、配置层与安全守卫不变。
- `/health` 新增 `version`，`storage`/`config`/`dependencies` 保持不变。

### 11.3 对 Stage-008/009 的影响

- 格式选择与音频接口不变；本阶段只把它们的错误码纳入回归清单。
- 已知限制（格式不持久化、音频需先完成下载任务）纳入 `docs/known-limitations.md`。

### 11.4 不变约束

- 不修改 Task 字段、状态集合、API 错误格式与文件策略。
- 不新增第三方依赖；不新增平台；不做安装包与系统服务。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-20 | 阶段准备 | Cline | 已完成 | 核对 Stage-009 输出、`plan-whole.md` §9/§11 与既有测试资产；冻结版本真值、发布检查范围与 D-014 决议 | Stage-009 基线 308/308 单测 + 8 个探针 + JS 检查 | 无 |
| 2026-09-20 | 任务 001 | Cline | 已完成 | 新增 `core_release.py`（版本真值、文档清单、变更日志解析、配置键对齐、`release_info`）与 `tests/test_release.py` | `tests.test_release` 27 个用例全部通过 | 无 |
| 2026-09-20 | 任务 002 | Cline | 已完成 | `/health` 增加 `version`；`MediaDock.js` 5.2 显示服务端版本 + 版本不一致提示；`check_userscript.py` 锚点更新 | R1008-R1009 通过；JS 结构检查通过（617 行） | 无 |
| 2026-09-20 | 任务 003 | Cline | 已完成 | 新增 `tests/release_check.py`：静态检查 + 真实 HTTP 全链路 + 重启 + 回滚演练 + 安全守卫 | 60 项检查全部通过，退出码 0 | 无 |
| 2026-09-20 | 任务 004 | Cline | 已完成 | 新增 `README.md` 与 7 份交付文档（install/configuration/userscript/release-notes/known-limitations/upgrade-rollback/release-checklist） | 文档存在性与版本一致性检查通过 | 无 |
| 2026-09-20 | 任务 005 | Cline | 已完成 | 全量回归 + 发布检查执行，证据写入 `tests/release_check_result.json` 与 `docs/stage010-migration.md` | 335/335 单测 + 8 探针 + 发布检查 60/60 | 无 |
| 2026-09-20 | 任务 006 | Cline | 已完成 | `plan-whole.md` 收口：状态、版本 `0.3`、Stage-009/010 完成时间、D-014 决议、变更记录 C-009 | 计划与实现一致 | 无 |

---

## 13. 实际输出与计划差异

- 原计划输出：启动/依赖/配置/Userscript 文档、全链路回归与最小发布检查、
  版本号与变更日志、升级与回滚步骤、发布后观察规则、已知限制、长期扩展评估。
- 实际输出：全部实现，另加两点证据化产物：
  1. `core_release.APP_VERSION` 作为唯一版本真值，并用发布检查强制 README、
     `release-notes` 与 `/health.version.app` 三处一致（计划只要求「明确版本号」）。
  2. `tests/release_check_result.json` 把「最小发布检查」变成可复核的机器结论，
     而不是只有一段人工命令清单。
- 差异：
  1. **D-014 决议为「手动启动 + 可选开机自启脚本」**：不提供安装包、不注册
     Windows 服务（避免管理员权限与卸载残留），文档给出任务计划程序配置方式。
  2. **`MediaDock.js` 升到 5.2**：仅新增服务端版本展示与版本不一致提示，
     不改任务面板排序/折叠/滚动契约（仍遵守 D-015/D-016）。
  3. **长期扩展只记录不实现**：候选（多平台、字幕、任务详情页）写入
     `docs/release-notes.md`，不在本阶段新增功能。
  4. **发布检查离线可跑**：不访问 YouTube，用脚本化子进程覆盖下载/音频路径，
     因此发布检查可在 CI 或断网环境重复执行。
- 差异影响：均为交付方式的澄清与证据强化，不改变 Task 字段、状态机、
  存储 schema 与既有 API 形状；对 Stage-001~009 无回改要求。
- 处理决定：全部接受并写入 `docs/stage010-migration.md`；`plan-whole.md`
  升级到 `0.3` 并新增变更记录 C-009。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：Stage-001~010 全部完成，MediaDock 进入「可交付、可验证、可回滚」状态
- 对 Stage-001~009 影响：无回改；全部回归通过（335 个单测 + 8 个探针 + JS 结构检查 +
  发布检查 60 项）。`/health` 仅新增 `version` 字段，其余响应形状不变
- 回滚基线：`Stage-009 completed`（commit `545d781`）；数据无 schema 变更
- 是否更新 `plan-whole.md`：是，版本 `0.2` → `0.3`，状态改为「全部阶段已完成」，
  D-014 决议已确认，新增变更记录 C-009
- 审查人：用户
- 日期：2026-09-20
