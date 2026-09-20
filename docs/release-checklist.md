# 发布检查清单与发布后观察

> 发布门禁是**可执行**的：`tests/release_check.py` 退出码 `0` 才允许交付。
> 本文档说明它覆盖什么、人工还要补什么、以及发布后如何观察和升级处理。

---

## 1. 发布前检查（自动）

一条命令：

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\release_check.py
```

结果：

- 文本报告（`[OK  ]` / `[FAIL]` 逐项）；
- `tests/release_check_result.json`（机器可复核，含每项详情与时间戳）；
- 退出码 `0` = 可发布，`1` = 不可发布。

### 1.1 静态检查

| 检查 | 内容 |
| --- | --- |
| `required_files` | README、requirements、示例配置、用户脚本、6 份交付文档均存在 |
| `app_version_shape` | `APP_VERSION` 是 `X.Y.Z` |
| `userscript_version` | `MediaDock.js @version` == `USERSCRIPT_VERSION` |
| `changelog_version` | `docs/release-notes.md` 最新条目 == `APP_VERSION` |
| `readme_version` | README 中出现的版本号与 `APP_VERSION` 一致 |
| `config_example_keys` | `config.example.json` 键集合 == `core_config.DEFAULTS` 键集合 |

### 1.2 运行期检查（真实 HTTP，离线）

| 组 | 内容 |
| --- | --- |
| 版本/健康 | `/health` 200，`version` 四字段正确，`version.schema == storage.schema_version` |
| 核心接口 | `/tasks`、`/history`、`/audio`、`/status` 200 且形状稳定 |
| 错误码 | `missing_url`、`invalid_url`、`unsupported_platform`、`invalid_format`、`task_not_found`、`invalid_task_id`、`not_found` 状态码与既有阶段一致 |
| 控制接口 | 未知任务 404、非法 id 400、非法状态 409 |
| 安全守卫 | 非法 `Host` → 403 `forbidden_host`；非法 `Origin` → 403 `forbidden_origin` |
| 重启语义 | `downloading` 行重启后变 `error`+`interrupted`；`paused` 保持；`completed` 记录与排序不变 |
| 回滚演练 | 用备份恢复数据库后可启动且历史可见；数据库不可用时降级为内存模式仍能创建任务 |
| 清理 | `POST /delete` 终态 200 且随后 404；非终态 409 `not_deletable` |
| CLI | `server.py --check-config` 退出码 0，输出含 `config` + `dependencies` |

---

## 2. 发布前检查（人工，无法自动化）

发布检查不访问网络，因此以下项目必须人工确认：

- [ ] 在真实 YouTube 页面上完成一次下载（默认清晰度），文件落到 `downloads/`。
- [ ] 选择一次非默认清晰度（如 `720p`）完成下载，确认合并成 MP4。
- [ ] 对一个已完成的下载任务调用 `POST /audio?target=mp3`，用播放器确认音频可播放。
- [ ] 暂停 → 继续 → 取消 → 重试 各操作一次，按钮状态与服务端一致。
- [ ] 打开两个 YouTube 标签页，确认任务列表同步、排序一致。
- [ ] 结束服务后重启，确认 `interrupted` 语义与 `/history` 历史记录符合预期。
- [ ] 检查 `MediaDock-server.log` 中没有未脱敏的完整 URL 或异常堆栈。
- [ ] **启动接管**：先启动一个实例，再用 `python server.py --restart` 启动第二个，
      确认旧实例被结束、新实例正常服务（`/health` 200）；再确认旧实例里运行中的任务
      变为 `error` + `interrupted`，可重试。

**其他必跑命令**

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_release.py core_config.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\check_userscript.py
# 9 个探针（probe_restart.py 会真实启停两个服务并验证端口接管）
Get-ChildItem tests\probe_*.py | ForEach-Object { C:\Users\Administrator\Envs\mediadock\Scripts\python.exe $_.FullName }
```

---

## 3. 发布后观察窗口

| 项目 | 内容 |
| --- | --- |
| 观察窗口 | 首次发布后 **7 天**，或前 **10 次**完整下载流程 |
| 观察频率 | 每天查看一次日志与 `/health`；每次失败任务都看 |
| 记录方式 | 每次异常记录：时间、`task_id`、`error_code`、当时操作、是否可复现 |

| 观测项 | 方法 | 正常表现 |
| --- | --- | --- |
| 服务可用性 | `curl /health` | `status=ok`，`config.ok=true`，`dependencies.ok=true` |
| 错误级别日志 | 搜索 `MediaDock-server.log` 中的 `ERROR` 与 `Traceback` | 无新增 |
| 失败任务分布 | `curl "/history?status=error"` | 无集中出现的单一 `error_code` |
| 重启中断 | 统计 `error_code=interrupted` | 只出现在真实重启/关机之后 |
| 数据库状态 | `/health.storage` | `kind=sqlite`、`degraded=false`、`schema_version=2` |
| 磁盘占用 | 查看 `downloads/` 与 `tasks.db` 大小 | 增长与下载量匹配 |
| 资源使用 | 任务管理器看 `python.exe` 内存/CPU | 空闲时接近 0 CPU，任务结束不持续占用 |
| 并发表现 | `/tasks` 的 `active_count` | 长期不超过 `max_active_tasks` |

---

## 4. 异常升级规则

| 触发条件 | 处理 |
| --- | --- |
| 单次任务失败 | 记录 `task_id` + `error_code`；用户用重试按钮重来，不升级 |
| 同一 `error_code` 连续失败 ≥ 3 次 | 停止新增功能，检查日志与依赖；必要时 `--check-config --probe` |
| 核心流程不可用（无法创建任务/无法完成下载） | 立即按 [upgrade-rollback.md](upgrade-rollback.md) 回滚到 `545d781` |
| `/health.storage.degraded = true` | 停服 → 备份 `tasks.db` → 用 `tasks.db.bak` 恢复 → 重启验证 |
| 出现 `interrupted` 激增但无重启 | 排查端口冲突与进程被杀；检查是否有第二个实例 |
| 日志出现端口占用（退出码 2） | 结束旧进程再启动；确认没有两个 MediaDock 在跑 |
| 用户脚本与服务端版本不一致提示 | 按 [userscript.md](userscript.md) 更新脚本后刷新页面 |
| 发现安全问题（路径越界、命令注入迹象） | 立即停服 + 回滚，并按 `plan-whole.md` §13 走需求变更流程评估 |

**升级为「新阶段」的条件**：任何需要改 `Task` 字段、状态集合、存储 schema、
API 形状或安全边界的修复，都不能在本阶段内直接改；必须先回到
`plan-whole.md` 做影响评估。

---

## 5. 发布记录模板

每次发布填写：

```text
发布版本：
用户脚本版本：
发布日期：
发布前检查：release_check.py 退出码 __ / 单元测试 __ 个 / 探针 __ 个
人工检查：下载 __ 音频 __ 控制 __ 多页同步 __
观察窗口：____ 至 ____
期间异常：
  - 时间 / task_id / error_code / 处理结果
回滚：无 / 时间 + 原因
结论：接受 / 回滚 / 需要新阶段修复
```
