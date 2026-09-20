# Stage-011：单实例启动接管（kill previous instance on start）

> 依据：[plan-whole.md](plan-whole.md) §12 `D-018`、[Stage-003.md](Stage-003.md)（单实例决策）、
> [Stage-010.md](Stage-010.md)（发布与回归基线）
>
> 用户诉求：`python server.py` 遇到 `WinError 10048` 时不必手动结束旧实例 ——
> **每次启动先结束之前存在的 MediaDock 实例**，然后正常启动。

---

## 1. 阶段元数据

- 阶段编号：`Stage-011`
- 阶段名称：单实例启动接管
- 总计划版本：`0.4`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-20
- 完成时间：2026-09-20
- 当前阻塞：无
- 前置阶段：`Stage-003`（单实例/端口不复用）、`Stage-006`（配置层端口来源）、
  `Stage-010`（发布门禁与文档体系）
- 后续阶段：无

---

## 2. 阶段目标

```text
python server.py
        |
        v
bind 127.0.0.1:8765 ──成功──> 正常启动
        |
      失败（WinError 10048）
        |
        v
core_instance.take_over_port(host, port)
        |
        +-- 端口空闲 ──────────────> free   （无需处理）
        +-- 占用者不是 MediaDock ──> foreign（拒绝接管，退出码 2 + 打印 PID/命令行）
        +-- 取不到占用者信息 ──────> unknown（拒绝接管，退出码 2）
        +-- 占用者 = python/pythonw 运行 server.py
                    |
                    v
            taskkill /PID <pid> /T /F  ->  等待端口释放  ->  重新 bind
                    |
                    +-- 成功 -> killed，正常启动
                    +-- 失败 -> failed，退出码 2
```

`python server.py --restart` 走同一条接管路径，但**不等冲突**，启动前主动接管。

### 2.1 本阶段成功标准

- 端口被旧 MediaDock 实例占用时，`python server.py` 能自动完成「结束旧实例 → 启动新实例」，
  全程无需人工介入，`/health` 返回 200。
- **绝不误杀**：只有「持有该端口的进程名是 python/pythonw 且命令行运行的是 `server.py`」
  才会被结束；其他占用者只报告、不处理。
- 端口空闲时零副作用（不查询、不结束任何进程）。
- 既有语义不变：任务状态机、`interrupted` 规则、存储、API、排序与文件策略全部不动。
- 全过程可测试：外部命令与「端口是否空闲」都可注入，单元测试不真实杀进程；
  真实起停由探针覆盖。

---

## 3. 范围边界

### 3.1 本阶段包含

- 新增 `core_instance.py`：端口占用者定位、MediaDock 身份判定、进程树结束、端口等待、接管编排。
- `server.py`：`--restart` 选项；绑定失败时自动接管并重试一次；占用者信息与提示日志。
- 测试：`tests/test_instance.py`（20 用例，注入 runner/probe）、
  `tests/probe_restart.py`（真实起停两个服务，14 项检查）。
- 文档：`docs/stage011-migration.md`、`docs/install.md` §3.1、`docs/known-limitations.md`、
  `docs/release-notes.md`、`docs/release-checklist.md`，版本 `1.0.0 → 1.0.1`。

### 3.2 本阶段明确不包含

- 不做「开机自启/守护进程/自动重启」；只处理「启动时端口被自己占用」。
- 不改端口、配置键、Task 字段、状态机、存储 schema、API 形状与错误码。
- 不结束非 MediaDock 进程，不做「强制释放端口」。
- 不引入第三方依赖（仍只用标准库 + 系统 `taskkill` / PowerShell）。
- 不改变 `MediaDockServer.allow_reuse_address = False`（仍然不允许两个实例共享端口）。

---

## 4. 输入契约

### 4.1 前置阶段已确认输入

- `Stage-003`：`MediaDockServer.allow_reuse_address = False`，端口冲突时退出码 `2`
  并记录日志（不静默共享端口）。
- `Stage-006`：`CONFIG.host` / `CONFIG.port` 是监听地址的唯一真值；日志分级与脱敏可用。
- `Stage-010`：`tests/release_check.py` 发布门禁、`docs/release-checklist.md` 人工项清单、
  `core_release.APP_VERSION` 版本真值与文档一致性校验。

### 4.2 保持不变的约束

- 不新增配置键；`--restart` 是命令行选项，不写进 `config.json`。
- 不改 `/health`、`/tasks`、控制 API、错误码与 `Task` 模型。
- 接管只影响「进程生命周期」，不改变任务语义：被中断的任务仍是
  `error` + `error_code=interrupted`（Stage-005）。
- 端口被非 MediaDock 程序占用时，行为与 Stage-003 完全一致（退出码 2 + 提示）。

---

## 5. 阶段契约

### 5.1 模块责任

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `core_instance` | 定位占用者、身份判定、结束进程树、等待端口、返回接管结论 | HTTP、任务、配置、日志实现 |
| `server.main` | 解析 `--restart`；绑定失败时调用接管并重试；输出结论日志 | 自己判断进程身份 |
| `core_config` | 提供 `host`/`port` | 不参与接管 |
| `tests/test_instance.py` | 注入 runner/probe 验证决策矩阵 | 不真实杀进程 |
| `tests/probe_restart.py` | 真实起停两个服务验证接管 | 不碰用户正在用的 8765 |

### 5.2 不变量

| 条件 | 必须成立 |
| --- | --- |
| 精准定位 | 只处理「监听该端口的那个 PID」，不按进程名批量结束 |
| 身份双重校验 | 进程名 ∈ {python, pythonw} **且** 命令行匹配 `server.py` |
| 空闲零副作用 | 端口空闲时不执行任何查询或结束命令 |
| 可降级 | 取不到占用者信息 → 不猜、不杀，退出码 2 |
| 不共享端口 | 仍然关闭地址复用；接管是「换人」，不是「共存」 |

### 5.3 接口契约

| 入口 | 行为 |
| --- | --- |
| `server.py`（无参数） | 端口空闲 → 正常启动；被旧实例占用 → 接管后启动；被其他程序占用 → 退出码 2 |
| `server.py --restart` | 启动前先接管（端口空闲时无副作用），接管失败退出码 2 |
| `server.py --check-config` | 行为不变（不触发接管） |
| `server.py <未知选项>` | 打印 `unknown option: X` 并退出码 2 |

新增错误码：无（HTTP 契约未变）。

---

## 6. 具体任务

### 任务 001：接管模块（`core_instance.py`）

- [x] `PortOwner(pid, name, command_line)` + `is_mediadock()`：进程名与命令行双重判定。
- [x] `port_owner_script()` / `encode_powershell()` / `powershell_args()`：
      用 `-EncodedCommand` 传 PowerShell，彻底避开引号与转义问题。
- [x] `parse_port_owner()`：容错解析 `pid|name|commandline`，垃圾输入返回 `None`。
- [x] `find_port_owner()` / `kill_process_tree()`（`taskkill /T /F`）/ `port_is_free()` /
      `wait_port_free()`。
- [x] `take_over_port()` 返回 `free/killed/foreign/unknown/failed` 五态结论，永不抛异常。
- [x] 所有外部命令与端口探测可注入（`runner` / `probe` / `wait`）。

### 任务 002：启动接入（`server.py`）

- [x] 抽出 `bind_server()`（返回 `(server, error)`）与 `free_port_for_start()`。
- [x] `main()` 支持 `--restart`，并拒绝未知选项（退出码 2）。
- [x] 绑定失败 → 接管 → 重试一次绑定；仍失败则保留原退出码 2 与提示。
- [x] 日志：接管对象、结论、以及 `foreign` 情况下的 PID/进程名/命令行。

### 任务 003：测试与证据

- [x] `tests/test_instance.py`：20 用例覆盖解析、身份判定、脚本编码、五态决策、
      超时与 taskkill 失败（全程注入，不杀进程）。
- [x] `tests/probe_restart.py`：真实启动 #1 → `--restart` 启动 #2 → 校验旧实例结束、
      端口所有者变化、`/health` 与 `/tasks` 可用、空闲端口上 `--restart` 正常；
      写 `tests/probe_restart_result.json`（14 项，使用临时端口/库/目录，不碰 8765）。

### 任务 004：文档与版本

- [x] `docs/stage011-migration.md`；`docs/install.md` §3.1 与 FAQ；
      `docs/known-limitations.md` 限制条目；`docs/release-notes.md` 1.0.1 条目与版本表；
      `docs/release-checklist.md` 探针数量与人工项。
- [x] `core_release.APP_VERSION` `1.0.0 → 1.0.1`；`plan-whole.md` §12 `D-018`、§13 `C-010`。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| T1101 | PowerShell 输出解析 | 单元 | 正常/多行/缺命令行/垃圾输入 → 正确或 `None` |
| T1102 | MediaDock 身份判定 | 单元 | python/pythonw + `server.py` → True；chrome/其他脚本/无 pid → False |
| T1103 | 脚本模板与编码 | 单元 | 端口/host 替换无残留；`-EncodedCommand` 可解码回原脚本 |
| T1104 | 端口空闲 | 单元 | `status=free`，且**不执行任何外部命令** |
| T1105 | 占用者无法识别 / 非 MediaDock | 单元 | `unknown` / `foreign`，**不执行 taskkill** |
| T1106 | 占用者是 MediaDock | 单元 | 调用 `taskkill /PID <pid> /T /F` → 等待端口 → `killed` |
| T1107 | taskkill 失败 / 端口仍占用 | 单元 | `failed` + 明确 `detail` |
| T1108 | `port_is_free` / `wait_port_free` | 单元 | 真实占用 socket 返回 False；轮询到释放返回 True；超时 False |
| T1109 | 真实起停接管 | 探针 | `probe_restart_result.json` 14/14（含旧实例结束、端口所有者变化） |

### 7.2 执行命令

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_instance.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_restart.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\release_check.py
```

### 7.3 测试原则

- 单元测试**绝不**真实结束进程：外部命令一律走注入的 `runner`。
- 探针只使用临时端口、临时数据库与临时下载目录，**绝不**触碰用户正在使用的 8765。
- 探针只结束自己启动的进程（`kill_process_tree`），异常路径在 `finally` 里兜底清理。
- 断言必须区分「谁持有端口」：venv 的 `python.exe` 是 shim，服务进程是其子进程，
  因此不能拿 `Popen.pid` 当服务 PID（本次实际踩到并已在探针中修正）。

---

## 8. 阶段产物

- `core_instance.py`：`PortOwner`、`is_mediadock`、`port_owner_script`、`encode_powershell`、
  `powershell_args`、`parse_port_owner`、`find_port_owner`、`kill_process_tree`、
  `port_is_free`、`wait_port_free`、`take_over_port`。
- `server.py`：`bind_server()`、`free_port_for_start()`、`--restart`、未知选项校验。
- `tests/test_instance.py`（20 用例）、`tests/probe_restart.py`（14 项检查）
  与 `tests/probe_restart_result.json`。
- `docs/stage011-migration.md`、`docs/install.md`、`docs/known-limitations.md`、
  `docs/release-notes.md`、`docs/release-checklist.md`、`README.md`、`plan-whole.md`。
- 版本：`1.0.1`（用户脚本仍 `5.2`，schema 仍 `2`）。

---

## 9. 风险与回滚

### 9.1 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 误杀非 MediaDock 进程 | 用户其他程序被杀 | 端口所有者 + 进程名 + `server.py` 命令行三重条件；`foreign` 明确拒绝 |
| 杀死正在下载的旧实例 | 任务中断 | 记录为 `error`/`interrupted` 并可重试；行为写入 `docs/known-limitations.md` |
| PowerShell 不可用/被策略限制 | 无法定位占用者 | 返回 `unknown`，不猜测、不杀进程，退化为退出码 2 |
| 端口释放慢（TIME_WAIT） | 接管后仍绑不上 | `wait_port_free` 轮询最多 10s；仍失败则退出码 2 并说明原因 |
| 用户以为「可以同时跑两个实例」 | 任务表不一致 | 仍关闭地址复用；接管语义写入 install/known-limitations |

### 9.2 外部依赖

- 系统 `taskkill`（Windows 自带）与 PowerShell（`Get-NetTCPConnection` / `Get-CimInstance`）。
- 无新增 Python 第三方依赖。

### 9.3 回滚策略

1. 删除 `core_instance.py` 与 `--restart` 分支，`main()` 恢复为「绑定失败 → 退出码 2」
   → 行为回到 Stage-003/1.0.0。
2. 删除 `tests/test_instance.py`、`tests/probe_restart.py` 与结果文件。
3. 版本回退到 `1.0.0` 并同步 `docs/release-notes.md`（无数据影响：无 schema 变更）。

---

## 10. 阶段验收标准

### 10.1 功能验收

- [x] 端口被旧 MediaDock 实例占用时，`python server.py` 自动接管并成功启动
      （`/health` 200，无需人工介入）。
- [x] `python server.py --restart` 在端口空闲时正常启动，在被占用时先接管再启动。
- [x] 占用者不是 MediaDock 时**不接管**，退出码 2 并打印 PID/进程名/命令行。
- [x] 端口空闲时零副作用（不查询、不杀进程）。
- [x] 既有语义不变：任务状态机、`interrupted` 规则、存储 schema、API 与排序不动。

### 10.2 测试与质量验收

- [x] `tests/test_instance.py` 20 用例通过。
- [x] `tests/probe_restart.py` 14/14 通过（真实起停两个服务）。
- [x] 全量单元测试通过：`ran=355 fail=0 err=0`（Stage-010 基线 335 + 本阶段 20）。
- [x] 9 个探针全部通过；发布门禁 60/60（`release_check.py` 退出码 0）。
- [x] `py_compile` 与 VS Code Problems 均无错误；无新增第三方依赖。

### 10.3 完成条件

- [x] 版本 `1.0.1` 在 `/health.version.app`、`README.md` 与 `docs/release-notes.md` 一致
      （由发布门禁强制）。
- [x] `docs/install.md` 写明接管语义与「接管会中断运行中任务」。
- [x] `docs/known-limitations.md` 记录「接管依赖 PowerShell」「不接管非 MediaDock 进程」等边界。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-001/002/003 的影响

- 不改 Task 模型、状态机、排序与队列语义。
- `Stage-003` 的「不共享端口」原则保留；只把「冲突后退出」升级为「先接管再启动」，
  并在 `plan-whole.md` 记录 `D-018`。

### 11.2 对 Stage-004/005/006 的影响

- 控制接口与文件清理策略不变；被接管的旧实例任务仍按 `Stage-005` 矩阵变为
  `error` + `interrupted`。
- 配置层未新增键；`host`/`port` 仍是监听地址唯一真值。

### 11.3 对 Stage-007 ~ 010 的影响

- 平台层、格式选择、媒体处理、发布门禁与文档体系均未改动；
  发布门禁仍 60/60，新增行为由探针与人工项覆盖。

### 11.4 不变约束

- 不允许两个实例共享同一端口。
- 不允许结束「身份不明确」的进程。
- 不修改 Task 字段、状态集合、API 错误格式与文件策略。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-20 | 阶段准备 | Cline | 已完成 | 复现用户报错（`WinError 10048`）；确认占用者为 `pythonw.exe server.py`（pid 3184）；冻结「只杀持有本端口的 MediaDock」方案 | 手工定位占用者成功 | 无 |
| 2026-09-20 | 任务 001 | Cline | 已完成 | 新增 `core_instance.py`（定位/判定/结束/等待/五态结论，全注入） | `test_instance.py` 20 用例通过 | 首次实现踩到 PowerShell `{0}` 与 `str.format` 冲突，已改为 token 替换 |
| 2026-09-20 | 任务 002 | Cline | 已完成 | `server.py` 抽出 `bind_server()`/`free_port_for_start()`，新增 `--restart` 与未知选项校验，绑定失败自动接管重试 | `py_compile` 通过；Problems 0 | 无 |
| 2026-09-20 | 任务 003 | Cline | 已完成 | `tests/probe_restart.py` 真实起停两个服务（临时端口/库/目录） | 14/14 通过 | 探针最初用 `Popen.pid` 当服务 PID，发现 venv python 是 shim（真实 PID 是子进程），已改为以端口所有者为真值 |
| 2026-09-20 | 任务 004 | Cline | 已完成 | 版本 `1.0.1`、`docs/stage011-migration.md`、install/known-limitations/release-notes/release-checklist/README 与计划更新 | 发布门禁同步校验版本一致性 | 无 |

---

## 13. 实际输出与计划差异

- 原计划（用户诉求）：每次启动先结束之前存在的实例。
- 实际输出：把「结束之前存在的实例」实现为**精准接管**——只结束「持有该端口的、
  身份确认为 MediaDock 的」进程，并额外提供 `--restart` 主动接管。
- 差异：
  1. **不是按进程名批量结束**：只针对端口占用者，避免误杀同机其他 python 程序。
  2. **非 MediaDock 占用者拒绝接管**：保留 Stage-003 的退出码 2 行为并增强提示
     （打印 PID/进程名/命令行），而不是「无论如何都要腾出端口」。
  3. **新增 `--restart`**：用户可选择在启动前主动接管，而不必依赖冲突触发。
  4. **探针口径修正**：venv 的 `python.exe` 是 shim，服务进程是其子进程；
     探针改为以「端口所有者的 PID」为真值判断接管是否换人。
- 差异影响：均为安全收紧与可测性改进；不改变 Task 字段、状态机、存储 schema 与 API。
- 处理决定：全部接受，写入 `docs/stage011-migration.md` 与 `plan-whole.md`
  （`D-018` 已确认、`C-010` 变更记录、计划版本 `0.4`）。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：允许发布 `1.0.1`
- 对 Stage-001~010 影响：无回改；355 个单测 + 9 个探针 + 发布门禁 60 项全部通过
- 回滚基线：Stage-010 / `1.0.0`（commit `21e389d` 之后的 `c7763aa`）
- 是否更新 `plan-whole.md`：是，版本 `0.3 → 0.4`，新增 `D-018`、`C-010` 与 Stage-011 行
- 审查人：用户
- 日期：2026-09-20
