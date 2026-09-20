# Stage-010 迁移说明：发布、回归与长期扩展

> 日期：2026-09-20；应用版本 **1.0.0**，用户脚本 **5.2**，存储 schema **v2**（未变化）。
> 本阶段不改变任何既有契约，只把「能跑」变成「可交付、可验证、可回滚」。
> 回滚基线：`Stage-009 completed`（commit `545d781`）。

---

## 新模块

### `core_release.py`（纯标准库）

- 版本真值：`APP_VERSION = "1.0.0"`、`API_VERSION = "1"`、
  `USERSCRIPT_VERSION = "5.2"`、`RELEASE_DATE`。
- `is_valid_version()` / `version_tuple()`：`X.Y.Z` 形状与比较（非法输入返回 `()`）。
- `parse_userscript_version()` / `userscript_version()`：从 `// @version` 取脚本版本。
- `changelog_versions()` / `latest_changelog_version()` / `changelog_latest()`：
  解析 `docs/release-notes.md` 的 `## [x.y.z]` 条目。
- `REQUIRED_DOCS` + `missing_docs()`：发布必需文件（README、requirements、
  示例配置、用户脚本、6 份交付文档）。
- `config_key_report()`：`config.example.json` 与 `core_config.DEFAULTS` 键集合对齐。
- `release_info(schema_version)`：`/health.version` 载荷。
- `static_checks()` / `failed_names()` / `format_report()`：静态发布检查与报告。

### `tests/release_check.py`（发布门禁，60 项检查）

- 静态：文档齐全、版本形状、脚本版本、变更日志版本、README 版本、配置键对齐。
- CLI：`python server.py --check-config` 退出码 0 且输出含 `config`+`dependencies`；
  `tests/check_userscript.py` 通过。
- 运行期（真实 `server.Handler` + 真实 SQLite + 真实 HTTP，子进程用脚本化对象，
  不访问网络）：
  - `/health` 200、`version` 四字段、`version.schema == storage.schema_version`；
  - `/tasks`、`/status`、`/history`、`/audio` 形状与计数；
  - `/download` 默认 200 且 argv 使用冻结表达式；未知预设/未探测预设 400；
  - 错误码回归：`missing_url`/`invalid_url`/`unsupported_platform`/`invalid_format`/
    `task_not_found`/`invalid_task_id`/`not_found`；
  - 安全守卫：`Host: evil.com` 与 `Origin: https://evil.com` → 403；
  - 重启语义：`downloading` → `error`+`interrupted`；`paused` 保持；`completed` 与排序不变；
  - 回滚演练：备份恢复数据库后历史可见；数据库损坏时降级为内存模式仍能创建任务
    （`version.schema` 为 0）；
  - 清理：`POST /delete` 终态 200、随后 404、非终态 409。
- 产物：`tests/release_check_result.json`（含每项 `ok` 与详情），退出码 0/1。

### `tests/test_release.py`（27 个单元用例）

覆盖 `core_release` 的纯函数、`/health.version` 的真实 HTTP 行为，
以及「内存模式下 `version.schema = 0`」的降级路径。

---

## 契约增量

### `/health` 新增 `version`（唯一的行为变化）

```json
"version": { "app": "1.0.0", "api": "1", "userscript": "5.2", "schema": 2 }
```

- 其余字段（`status`/`storage`/`config`/`dependencies`）形状不变。
- 无数据库时 `schema` 为 `0`，与 `storage.schema_version` 保持一致。
- 新增错误码：无。既有错误码全部不变。

### 用户脚本 5.2

- 只新增两件事：面板标题栏显示服务端版本（`运行 n/3 · 排队 m · v1.0.0`），
  以及 `/health.version.userscript` 与自身 `@version` 不一致时的提示行。
- 任务列表排序/折叠/滚动契约（D-015/D-016）不变；
  `tests/check_userscript.py` 增加「`@version` 必须等于 `USERSCRIPT_VERSION`」检查。

### 交付文档（新增）

| 文档 | 内容 |
| --- | --- |
| `README.md` | 能力总览、快速开始、API 表、目录结构、文档索引 |
| `docs/install.md` | 安装、启动、健康检查、可选开机自启（D-014）、卸载、常见问题 |
| `docs/configuration.md` | 配置键、环境变量、优先级、安全相关行为 |
| `docs/userscript.md` | 脚本安装、更新、版本一致性、面板行为、排错 |
| `docs/release-notes.md` | 1.0.0 变更日志、版本对应表、下一步候选 |
| `docs/known-limitations.md` | 已知限制与不支持场景（按平台/格式/媒体/任务/安全/前端/诊断分组） |
| `docs/upgrade-rollback.md` | 备份、升级、代码/数据/降级/前端回滚、回滚验收、决策树 |
| `docs/release-checklist.md` | 发布前自动+人工检查、观察窗口、异常升级规则、发布记录模板 |

---

## 决策

| 编号 | 决策 | 结论 |
| --- | --- | --- |
| D-014 | 发布方式 | **手动启动 + 可选开机自启脚本**；不做安装包、不注册 Windows 服务（避免管理员权限与卸载残留），文档给出启动文件夹与任务计划程序两种做法 |
| — | 版本号单点 | `core_release.APP_VERSION` 是唯一真值，README/变更日志/`/health` 由检查强制一致 |
| — | 长期扩展 | 候选（第二批平台、字幕/缩略图、格式写入历史、安装包等）只写入 `docs/release-notes.md`，不在本阶段实现 |

---

## 验证证据（2026-09-20）

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 编译 | `python -m py_compile server.py core_release.py` | rc=0 |
| 全量单元测试 | `python -m unittest discover -s tests -t .` | `Ran 335 tests ... OK` |
| 发布门禁 | `python tests\release_check.py` | `RELEASE CHECK OK checks=60 passed=60 failed=0`（rc=0） |
| 探针 | 8 个 `tests/probe_*.py` | 全部 rc=0（chain/control/multi/persist/security/platform/formats/media） |
| 用户脚本 | `python tests\check_userscript.py` | `JS STRUCTURE OK (637 lines, ... anchors present)` |
| 配置诊断 | `python server.py --check-config` | rc=0，输出含 `config` + `dependencies` |

回归结论：Stage-001~009 的接口、状态机、存储、排序、文件策略、平台与格式边界
均未发生变化；`/health` 只有增量字段。

---

## 环境注意（本次实际遇到）

写入 `server.py` 时命中 **EPERM/拒绝访问**：即使卸载了 360 的进程，
其内核文件过滤驱动（`360FsFlt`、`360AntiSteal`、`360Box64`、`360qpesv`、`DsArk`）
仍然加载，按路径锁住了该文件（读允许、写与重命名拒绝，ACL 正常，
UNC 管理共享路径同样被拒）。**重启 Windows 后驱动卸载，写入恢复正常。**

排查手法（可复用）：

```powershell
# 是否可写
try { $fs=[System.IO.File]::Open((Resolve-Path server.py),'Open','ReadWrite','None'); $fs.Close(); "WRITABLE" } catch { "LOCKED" }
# ACL 与属性
icacls server.py; (Get-Item server.py).Attributes
# 残留的过滤驱动（关键判据）
fltmc filters | Select-String "360|DsArk|QHSafe"
```

结论：遇到「单文件写被拒但 ACL 正常」时，先看 `fltmc filters`，
驱动级锁定只能靠重启解决，不要在运行中强行卸载安全软件驱动。

---

## 回滚

1. 代码：删除 `core_release.py`、`tests/release_check.py`、`tests/test_release.py`
   与新增文档；`server.py` 去掉 `/health.version`；`MediaDock.js` 回退 5.1 并同步
   `tests/check_userscript.py` 锚点 → 行为回到 Stage-009。
2. 数据：**无 schema 变化、无迁移**，`tasks.db` 无需回滚；
   如需回到 Stage-009 基线，用 `git checkout 545d781`。
3. 前端：Tampermonkey 重装旧脚本源码即可，与服务端松耦合。

---

## 遗留与交接

- 手工验证项（发布会话清单）见 `docs/release-checklist.md` 第 2 节：
  真实 YouTube 下载、非默认清晰度、音频转换、控制按钮、多页同步、重启语义、日志脱敏。
- 已知限制成文于 `docs/known-limitations.md`，其中最重要三条：
  清晰度不持久化（重启后重试回落默认）、重启不自动恢复运行中任务、只支持 YouTube。
- 长期扩展候选成文于 `docs/release-notes.md`，需要新阶段评估后才能实施。
