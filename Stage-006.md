# Stage-006：配置、依赖与安全加固

> 依据：[plan-whole.md](plan-whole.md) `0.2`、[Stage-005.md](Stage-005.md)、[docs/stage005-migration.md](docs/stage005-migration.md)
>
> 本阶段把「写死在代码里的路径/端口/日志」收敛到明确配置层，补齐依赖与目录诊断，并把本地安全边界（回环绑定、输入校验、命令注入、路径边界、日志脱敏）固化为可测试契约。平台 Adapter、格式选择、媒体处理不属于本阶段。

---

## 1. 阶段元数据

- 阶段编号：`Stage-006`
- 阶段名称：配置、依赖与安全加固
- 总计划版本：`0.2`
- 当前状态：已完成
- 负责人：Cline
- 开始时间：2026-09-19
- 预计完成时间：2026-09-19
- 当前阻塞：无
- 前置阶段：`Stage-001`（安全基线）、`Stage-005`（存储层，已完成）
- 后续阶段：`Stage-007` 平台 Adapter（依赖本阶段的配置层与检测）、`Stage-009`、`Stage-010`

---

## 2. 阶段目标

```text
config.json（缺失即用默认值，非法即报诊断）
   |
   v
core_config.load_config() -> Config + diagnostics
   |
   +--> host/port（强制回环）      --> server 绑定
   +--> download_dir               --> 引擎/文件策略
   +--> yt-dlp / FFmpeg 路径       --> core_deps 检查 + 引擎命令
   +--> log_level / log_file       --> 分级 + 脱敏日志
   +--> db_path 覆盖规则           --> core_store
   |
   v
GET /health -> {status, storage, config, dependencies}（错误可见）
```

### 2.1 本阶段成功标准

- 下载目录、yt-dlp、FFmpeg、host、port、日志级别、日志文件、数据库路径全部来自配置层，代码里不再有第二处真值来源。
- `config.json` 缺失时用默认值运行并在 `/health` 显示 `source=defaults`；非法配置**不阻塞启动**，但必须在启动日志与 `/health` 明确暴露 `config.errors`。
- 启动时完成 yt-dlp / FFmpeg / 下载目录 / 磁盘空间检查，结果进入 `/health.dependencies`，检查失败不影响其他功能。
- 非回环 host 只有显式 `allow_lan: true` 才被接受；否则回退 `127.0.0.1` 并记录配置错误。
- HTTP 层强制校验 `Host`（只接受回环）与 `Origin`（存在时只接受 YouTube 域）、限制请求体大小；URL 输入做 scheme/长度/控制字符校验。
- 引擎命令始终是 argv 列表、无 shell、URL 只作为单个参数；`format_id` 之类用户输入无法进入 argv（Stage-008 才引入，届时按同一规则校验）。
- 日志与错误信息对 URL 查询串中的敏感参数、用户目录路径做脱敏，并限制单行长度。
- 未实现的来源鉴权、TLS、远程访问边界被明确写成「不支持」。

---

## 3. 范围边界

### 3.1 本阶段包含

- 新增 `core_config.py`：默认值、`config.json` 加载、类型/取值校验、环境变量覆盖、诊断信息、`--check-config` CLI。
- 新增 `core_deps.py`：yt-dlp / FFmpeg / 下载目录 / 磁盘空间检查，启动快照 + 可复用纯函数。
- 新增 `core_security.py`：URL 校验、Host/Origin 白名单、请求体大小上限、日志脱敏（URL/路径/文本）。
- `server.py` 改为配置驱动（host/port/download_dir/log/log_file/db_path），启动与 `/health` 输出配置与依赖诊断。
- `core_engine.py` 支持配置的 yt-dlp/FFmpeg 路径（`--ffmpeg-location`），保持默认格式策略不变。
- 分级日志（`debug/info/warning/error`）与脱敏。
- 新增 `config.example.json`、`docs/stage006-migration.md`，`config.json` 加入 `.gitignore`。
- 安全与依赖测试（含命令注入、路径遍历、Host/Origin、脱敏）。

### 3.2 本阶段明确不包含

- 远程访问、TLS、反向代理、多用户鉴权（明确不做，默认仅回环）。
- 平台 Adapter、`/formats`、音频模式与媒体处理（Stage-007/008/009）。
- 配置热重载（修改配置需重启服务）。
- 配置 GUI、配置分环境（dev/prod）与配置版本迁移。
- 日志文件轮转（本阶段只做分级与脱敏；轮转属于 Stage-010 发布运维）。
- 改变并发上限 3、默认 MP4 格式策略、状态机与 API 形状。

---

## 4. 输入契约

### 4.1 Stage-005 已确认输入

- `resolve_db_path(explicit)`：显式参数 > `MEDIADOCK_DB` > `<仓库>/tasks.db`；配置层应接管这个入口。
- `server.bootstrap(db_path)` 装配 storage + manager + scheduler；`storage_info()` 暴露 `kind/db/schema_version/degraded/reason`。
- `core_files.is_inside()` 提供真实路径边界校验；`cleanup_task_files()` 只删配置下载目录内的本次文件。
- `core_engine.resolve_ytdlp()` 当前按 `C:\Tools\yt-dlp\yt-dlp.exe` → `yt-dlp.exe` → `yt-dlp` 顺序探测。
- `server.log()` 是全局日志出口（文件 + 控制台），所有模块通过 logger 回调写入。
- `Task` 字段、状态机、4 个控制 API、`/history`、`/events`、`POST /delete` 形状已冻结。
- Stage-005 交接的未完成项：配置系统、日志轮转、来源校验。

### 4.2 保持不变的约束

- 默认监听回环地址；不默认暴露局域网。
- `Task` 字段、状态机、排序契约、并发上限 3、文件保留/删除策略不变。
- Userscript 不运行 yt-dlp/FFmpeg、不拼接命令、不推断状态；不新增非 YouTube 来源校验依赖。
- 不改变默认 `height<=1080` MP4 格式表达式。
- 数据库 schema 与 API 错误格式不变。

### 4.3 高影响决策门禁

| 编号 | 决策 | 本阶段处理 |
| --- | --- | --- |
| D-004 | 默认监听 `127.0.0.1` | 强化为「强制回环」：非回环 host 需要显式 `allow_lan: true`，否则回退并报配置错误 |
| D-010 | 是否长期兼容 `GET /download` | 继续兼容；本阶段只加强 URL 校验，不改变方法 |
| D-014 | 发布方式（手动/开机/安装包） | 本阶段只保证 `python server.py` 与 `--check-config` 可用，发布方式留 Stage-010 |

---

## 5. 阶段契约

### 5.1 配置模型（`core_config.Config`）

```text
config.json (可选)  --+
环境变量             --+--> load_config() -> Config(source, path, errors, warnings)
内置默认值           --+
```

| 键 | 类型 | 默认值 | 约束 | 用途 |
| --- | --- | --- | --- | --- |
| `host` | str | `127.0.0.1` | 非空；非回环必须有 `allow_lan` | 监听地址 |
| `port` | int | `8765` | 1..65535 | 监听端口 |
| `allow_lan` | bool | `false` | 只能是布尔 | 非回环监听开关 |
| `download_dir` | str | `<仓库>/downloads` | 非空；相对路径按仓库根解析 | 下载目录（唯一真值） |
| `ytdlp_path` | str | `""` | 空 = 自动探测 | yt-dlp 可执行文件 |
| `ffmpeg_path` | str | `""` | 空 = 自动探测 | FFmpeg 目录/可执行文件 |
| `log_level` | str | `info` | `debug`/`info`/`warning`/`error` | 日志级别 |
| `log_file` | str | `<仓库>/MediaDock-server.log` | 非空；相对路径按仓库根解析 | 日志文件 |
| `db_path` | str | `""` | 空 = 交给 `resolve_db_path()` | 数据库文件 |
| `max_active_tasks` | int | `3` | 1..8 | 并发上限（默认值不变） |
| `purge_keep` | int | `200` | 1..10000 | 终态记录保留条数 |
| `request_max_bytes` | int | `65536` | 1024..1048576 | POST 请求体上限 |
| `log_line_max` | int | `4000` | 200..20000 | 单行日志字符上限 |

- `Config` 为 frozen dataclass；`source ∈ {defaults, file, env}`、`path`、`errors`、`warnings` 附带诊断。
- 非法值**不阻塞启动**：该项回退默认值并把原因写入 `errors`，启动日志与 `/health.config.errors` 均可看到。
- 未识别的键写入 `errors`（`unknown_config_key`），避免拼写错误被静默忽略。
- 数据库路径优先级：`bootstrap(db_path=...)` 显式参数 > `config.db_path` > `MEDIADOCK_DB` > `<仓库>/tasks.db`。

### 5.2 回环强制（D-004 强化）

| 输入 | `allow_lan` | 结果 |
| --- | --- | --- |
| `127.0.0.1` / `localhost` / `::1` | 任意 | 采纳，无警告 |
| 其他地址（`0.0.0.0`、`192.168.x.x`、空串） | `false` | 回退 `127.0.0.1` + `errors` |
| 其他地址 | `true` | 采纳 + `warnings`（明确记录暴露风险） |

### 5.3 安全契约（`core_security`）

| 函数 | 契约 | 失败结果 |
| --- | --- | --- |
| `validate_url(url, max_len=2048)` | scheme 只能是 http/https；有 host；无控制字符/空格/引号；长度受限 | `(False, "invalid_url", 原因)` |
| `check_host_header(value, port=None)` | Host 只能是回环名（含 `:port`） | `(False, 原因)` |
| `check_origin(origin)` | 缺失 → 通过；存在则必须属于 YouTube 域（含 `youtu.be`、`youtube-nocookie.com`）；`null` 拒绝 | `(False, 原因)` |
| `sanitize_url(url)` | 保留 scheme/host/path；敏感查询参数值替换为 `***`；userinfo 密码替换为 `***` | 始终返回 str |
| `sanitize_path(path, home=None)` | 用户主目录替换为 `~`，超长截断 | 始终返回 str |
| `redact(text, max_len)` | 对文本中的 URL 应用 `sanitize_url`、对路径应用 `sanitize_path`，再截断 | 始终返回 str |
| `body_within_limit(length, limit)` | 0 ≤ length ≤ limit | `False` → 413 |

### 5.4 依赖检查契约（`core_deps`）

| 检查 | 说明 | 状态 |
| --- | --- | --- |
| `check_ytdlp(cfg)` | 配置了路径必须存在；否则按候选探测 | `ok`/`missing`/`error` |
| `check_ffmpeg(cfg)` | 配置了路径必须存在；否则 PATH 探测 | `ok`/`missing`（合并需要，仅告警） |
| `check_download_dir(cfg)` | 不存在则尝试创建；创建后写探针文件 | `ok`/`error` |
| `check_disk_space(path, min_free_mb=500)` | `shutil.disk_usage` 剩余空间 | `ok`/`warn`/`error` |
| `probe_version(path)` | 运行 `<path> --version`（仅 `--check-config --probe`） | `ok`/`error` |

- `run_checks(cfg)` 返回 `{ok, checked_at, checks: [...]}`；单项失败不影响其他检查，也不阻塞启动。
- 启动时使用 `deep=False`（不启动任何子进程）；`--check-config --probe` 才做版本探测。
- 结果进入 `/health.dependencies`。

### 5.5 HTTP 加固契约

| 项 | 规则 | 失败响应 |
| --- | --- | --- |
| `Host` | 必须是回环（`127.0.0.1`/`localhost`/`::1`，可带端口） | 403 `forbidden_host` |
| `Origin` | 缺失放过；存在必须是 YouTube 域 | 403 `forbidden_origin` |
| 请求体 | `Content-Length` ≤ `request_max_bytes` | 413 `payload_too_large` |
| `/download?url=` | 走 `validate_url()`（scheme/长度/控制字符/host） | 400 `invalid_url` |
| `task_id` | 保持 `^[A-Za-z0-9_-]{1,64}$` | 400 `invalid_task_id` |
| 命令行 | 始终 argv 列表、`shell=False`；URL 只作为最后一个参数；无可变成 flag 的用户输入（URL 以 `-` 开头已被 scheme 校验拒绝） | 不适用 |

新增错误码：`forbidden_host`(403)、`forbidden_origin`(403)、`payload_too_large`(413)。既有错误码不变。

### 5.6 责任边界

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| `core_config` | 默认值、文件/环境变量加载、校验、诊断 | 创建目录、启动进程、写日志 |
| `core_deps` | 可执行文件/目录/磁盘检查与版本探测 | 安装依赖、修改配置 |
| `core_security` | URL/Host/Origin/体积校验与脱敏纯函数 | 读配置、发 HTTP 响应 |
| `server.apply_config()` | 把 `Config` 落到模块真值并重开日志文件 | 解析配置、校验取值 |
| `Handler` | 调用安全校验、返回错误码 | 自己实现校验规则 |
| `core_engine` | 按配置使用 yt-dlp/FFmpeg 路径构造 argv | 读 `config.json` |

### 5.7 不变量

| 条件 | 必须成立 |
| --- | --- |
| 单一真值 | 端口/目录/可执行文件/日志/数据库路径只有 `Config` 一处来源 |
| 降级启动 | 配置缺失、非法或依赖缺失都不阻止服务启动 |
| 错误可见 | 任何配置错误必须出现在启动日志与 `/health.config.errors` |
| 回环默认 | 未显式 `allow_lan` 时不可能监听非回环地址 |
| 无命令注入 | 任何 HTTP 输入都不能变成 shell 命令或额外 argv |
| 路径边界 | 下载与清理只发生在 `Config.download_dir` 内 |
| 脱敏 | 日志与 `/health` 不含敏感查询参数明文与完整用户目录路径 |
| 兼容 | Task 字段、状态机、排序契约、API 形状、错误格式不变 |

---

## 6. 具体任务

> 下列任务的完成标准由第 10 节验收与第 12 节执行记录逐项覆盖。

### 任务 001：配置层与校验

**涉及文件：** 新增 `core_config.py`、`tests/test_config.py`

**内容：** `DEFAULTS`、`Config`、`load_config(path=None, env=None)`、逐键校验与类型强转、环境变量覆盖、未知键诊断、回环强制、`config_public()`。

**完成标准：**

- [ ] 无 `config.json` 时全部默认值可用，`source=defaults`、`errors=()`。
- [ ] 合法文件被采纳，`source=file`、`path` 指向该文件。
- [ ] 类型错误/超范围/非法枚举回退默认值并记录 `errors`，不抛异常。
- [ ] 未知键记录 `unknown_config_key`。
- [ ] `host=0.0.0.0` 且 `allow_lan=false` → 回退 `127.0.0.1` + `errors`；`allow_lan=true` → 采纳 + `warnings`。
- [ ] 环境变量覆盖文件，`source=env`。

### 任务 002：安全纯函数

**涉及文件：** 新增 `core_security.py`、`tests/test_security.py`

**内容：** `validate_url`、`check_host_header`、`check_origin`、`sanitize_url`、`sanitize_path`、`redact`、`body_within_limit`。

**完成标准：**

- [ ] `ftp://`/`javascript:`/无 host/超长/含控制字符/含空格 → 拒绝。
- [ ] `Host: evil.com` 拒绝；`127.0.0.1:8765`、`localhost`、`[::1]:8765` 通过。
- [ ] `Origin: https://evil.com` 拒绝；`https://www.youtube.com`、`https://youtu.be` 通过；缺失与 `null` 语义明确。
- [ ] `sanitize_url` 保留 `?v=`，屏蔽 `token/sig/key/auth` 等，屏蔽 userinfo 密码。
- [ ] `redact` 把主目录替换为 `~` 并限制长度，不改变普通文本。

### 任务 003：依赖与环境诊断

**涉及文件：** 新增 `core_deps.py`、`tests/test_deps.py`

**内容：** `check_ytdlp`、`check_ffmpeg`、`check_download_dir`、`check_disk_space`、`probe_version`、`run_checks(cfg, deep=False)`。

**完成标准：**

- [ ] 配置的路径不存在 → `error` 并回退探测，不抛异常。
- [ ] 目录不存在时被创建；不可写时返回 `error` 且不影响启动。
- [ ] 磁盘检查阈值可注入（测试用大阈值触发 `warn`）。
- [ ] `deep=False` 不启动任何子进程；`deep=True` 用真实解释器验证版本探测。

### 任务 004：server 配置驱动与 `/health` 诊断

**涉及文件：** `server.py`、`tests/test_config.py`

**内容：** `apply_config()`、`reload_config()`、`effective_db_path()`、`bootstrap(db_path=None, config=None)`、`main()` 按 `Config` 绑定，`/health` 返回 `config`+`dependencies`，`download_dir/log_file/db_path/max_active/purge_keep` 全部来自配置。

**完成标准：**

- [ ] `DOWNLOAD_DIR`/`YT_DLP`/`DB_PATH`/`LOG_FILE`/并发上限由 `Config` 决定，代码无第二处真值。
- [ ] 重载配置后下载目录、并发上限、日志文件真实生效（探针验证）。
- [ ] `/health` 含 `config{source,path,errors,warnings,values}` 与 `dependencies{ok,checks}`。
- [ ] 配置错误时服务仍启动，`/health.config.errors` 非空。

### 任务 005：HTTP 加固

**涉及文件：** `server.py`、`core_security.py`、`tests/test_sec_api.py`

**内容：** Handler 的 Host/Origin 守卫、请求体上限、`/download` URL 校验替换为 `validate_url()`。

**完成标准：**

- [ ] 非回环 `Host` → 403 `forbidden_host`；`Origin: http://evil.com` → 403 `forbidden_origin`；YouTube `Origin` 通过。
- [ ] 超过 `request_max_bytes` 的 POST → 413 `payload_too_large`。
- [ ] 非法 URL（ftp、控制字符、超长）→ 400 `invalid_url`。
- [ ] 既有 GET/POST/OPTIONS 与全部错误码回归通过。

### 任务 006：分级日志与脱敏

**涉及文件：** `server.py`、`tests/test_sec_api.py`

**内容：** `log(*args, level="info")` 级别过滤、`core_security.redact` 应用到每行、`log_line_max` 截断；日志文件按配置打开。

**完成标准：**

- [ ] `log_level=warning` 时不写 info 行，warning/error 仍写。
- [ ] 日志行中的敏感查询参数被替换为 `***`，主目录替换为 `~`，超长行被截断。
- [ ] 日志文件路径来自配置（探针验证真实写入）。

### 任务 007：回归、探针与文档

**涉及文件：** `config.example.json`、`docs/stage006-migration.md`、`.gitignore`、`requirements.txt`、`tests/probe_security.py`、`Stage-006.md`、`plan-whole.md`

**内容：** 示例配置、迁移/回滚说明、`config.json` 加入 `.gitignore`、安全探针（真实 HTTP + 真实配置 + 真实数据库）与证据 JSON、更新计划状态。

**完成标准：**

- [ ] `unittest discover` 全量通过（Stage-002..005 无回归）。
- [ ] 5 个既有探针 + `probe_security.py` 全部 OK，证据写入 `tests/probe_security_result.json`。
- [ ] `py_compile` 全模块通过，无新增第三方依赖（仍为标准库）。
- [ ] 文档记录配置迁移、回滚与未实现能力（TLS/远程访问/日志轮转）。

---

## 7. 测试计划

### 7.1 测试矩阵

| 编号 | 场景 | 类型 | 预期结果 |
| --- | --- | --- | --- |
| T601 | 无配置文件 | 单元 | 全默认值，`source=defaults`，无错误 |
| T602 | 合法配置文件 | 单元 | 值被采纳，`source=file`，`path` 正确 |
| T603 | 非法类型/超范围/枚举 | 单元 | 回退默认 + `errors` 非空，不抛异常 |
| T604 | 未知键 | 单元 | `unknown_config_key` |
| T605 | 非回环 host | 单元 | 无 `allow_lan` 回退并报错；有则采纳并警告 |
| T606 | 环境变量覆盖 | 单元 | 覆盖文件值，`source=env` |
| T607 | 相对路径解析 | 单元 | 相对 `download_dir` 变为仓库内绝对路径 |
| T608 | `--check-config` CLI | 集成 | 退出码区分 0/1，输出含 errors/dependencies |
| T609 | URL 校验 | 单元 | 六类非法输入拒绝，合法 URL 通过 |
| T610 | Host 校验 | 单元 | 非回环拒绝，回环名（含端口/IPv6）通过 |
| T611 | Origin 校验 | 单元 | 非 YouTube 拒绝，缺失/null 语义明确 |
| T612 | URL 脱敏 | 单元 | 敏感键值与 userinfo 密码被屏蔽，普通参数保留 |
| T613 | 路径/文本脱敏 | 单元 | 主目录变 `~`，超长截断 |
| T614 | 请求体上限 | 单元+集成 | 超限 → 413 `payload_too_large` |
| T615 | yt-dlp 检查 | 单元 | 配置缺失路径报错；真实可执行文件报 ok |
| T616 | FFmpeg 检查 | 单元 | 缺失为 `missing`（不阻塞启动） |
| T617 | 下载目录创建/可写 | 单元 | 不存在则创建；只读 → `error` |
| T618 | 磁盘空间检查 | 单元 | 正常 `ok`；大阈值 `warn` |
| T619 | 依赖快照不含子进程 | 单元 | `deep=False` 不调用 `subprocess` |
| T620 | `/health` 配置与依赖 | API | `config`/`dependencies` 存在且结构正确 |
| T621 | 配置错误仍可服务 | 集成 | 启动成功，`/health.config.errors` 非空 |
| T622 | Host 守卫 | API | 非回环 Host → 403 `forbidden_host` |
| T623 | Origin 守卫 | API | 恶意 Origin → 403；YouTube Origin → 200 |
| T624 | 日志级别过滤 | 单元 | `warning` 级别下 info 行不落盘 |
| T625 | 日志脱敏落盘 | 集成 | 日志文件无敏感参数明文、无完整主目录 |
| T626 | 配置驱动并发上限 | 集成 | `max_active_tasks=1` 时第 2 个任务排队 |
| T627 | 既有 API 回归 | 回归 | Stage-002..005 全部 GET/POST/OPTIONS 通过 |
| T628 | Userscript 结构 | 静态 | `check_userscript.py` rc=0（本阶段不改 JS） |
| T629 | 安全探针 | 探针 | `probe_security_result.json` 全部 true |
| T630 | 命令注入面 | 单元 | 恶意 URL 无法进入 argv 或产生额外参数 |

### 7.2 建议命令

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m py_compile server.py core_task.py core_manager.py core_parse.py core_engine.py core_scheduler.py core_listing.py core_control.py core_files.py core_store.py core_config.py core_deps.py core_security.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m unittest discover -s tests -t .
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_security.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_persist.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_multi.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_chain.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\probe_control.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe tests\check_userscript.py
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe server.py --check-config
```

### 7.3 测试原则

- 配置测试用 `tempfile` 目录里的 `config.json` 与显式 `env` 字典，不改进程环境、不写仓库里的 `config.json`。
- 不依赖网络、不启动真实 yt-dlp：可执行文件检查用真实存在的解释器路径（`sys.executable`）或临时文件替代。
- 依赖检查默认 `deep=False`（不 spawn 进程）；版本探测用 `sys.executable --version` 验证真实调用路径。
- 安全测试同时覆盖纯函数与真实 HTTP（`ThreadingHTTPServer` + 自定义 `Host`/`Origin` 头）。
- 单元测试沿用 `tests/__init__.py` 的 `MEDIADOCK_DB=:memory:`，绝不写真实 `tasks.db`；探针用临时目录。
- 配置驱动行为（并发上限、目录）通过再 `bootstrap()` 应用新 `Config` 后断言。

---

## 8. 阶段产物

- `core_config.py`：`DEFAULTS`、`Config`、`load_config()`、`config_public()`、`resolve_path()`。
- `core_security.py`：`validate_url()`、`check_host_header()`、`check_origin()`、`sanitize_url()`、`sanitize_path()`、`redact()`、`body_within_limit()`。
- `core_deps.py`：`check_ytdlp()`、`check_ffmpeg()`、`check_download_dir()`、`check_disk_space()`、`probe_version()`、`run_checks()`。
- `server.py`：`apply_config()`/`reload_config()`/`effective_db_path()`、`/health` 的 `config`+`dependencies`、Host/Origin/体积守卫、分级+脱敏日志、`--check-config` CLI。
- `core_engine.py`：`resolve_ffmpeg()` 与可选 `--ffmpeg-location`（默认行为不变）。
- `config.example.json`、`docs/stage006-migration.md`，`.gitignore` 排除 `config.json`。
- `tests/test_config.py`、`tests/test_security.py`、`tests/test_deps.py`、`tests/test_sec_api.py`、`tests/probe_security.py` 与证据 JSON。

---

## 9. 风险、依赖与回滚

### 9.1 风险

| 风险 | 影响 | 缓解措施 |
| --- | --- | --- |
| 配置错误导致服务无法启动 | 用户完全不可用 | 非法值一律回退默认并暴露 `errors`，绝不抛出 |
| 用户在 `config.json` 里写成非回环地址 | 局域网暴露 | 强制 `allow_lan` 才放行，默认回退 `127.0.0.1` 并报错 |
| 端口被占用 | 启动失败 | 保持 Stage-001 行为：明确日志 + rc=2，并提示占用排查 |
| 加固校验误伤前端 | 正常请求被 403 | `Origin` 缺失放过；CORS 与回环 Host 覆盖 Userscript 场景；回归测试覆盖既有 API |
| 日志脱敏破坏可诊断性 | 排查变难 | 只屏蔽敏感键值与主目录前缀，保留 scheme/host/path 与错误码 |
| 日志文件不可写 | 丢日志或异常 | 打开失败回退 `stderr` 并继续运行；错误写入 `/health.config.errors` |
| 依赖缺失被当成崩溃 | 用户以为服务坏了 | 依赖检查只报告，不阻塞启动；`/health.dependencies` 明示 |
| 配置的 yt-dlp/FFmpeg 路径被注入 | 命令执行 | 只接受本机路径字符串作为单个 argv 元素，不拼 shell；不做下载/解压 |
| `config.json` 泄漏本地路径 | 隐私 | `.gitignore` 排除；示例文件写占位值 |

### 9.2 外部依赖

- 仅标准库：`json`、`os`、`shutil`、`subprocess`、`dataclasses`、`urllib.parse`（无新增第三方包）。
- 可写目录（下载目录、日志文件、数据库文件）；FFmpeg 可选（缺失只影响合并，进入告警）。
- 现有 yt-dlp 安装（路径可由配置覆盖）。

### 9.3 回滚策略

1. 代码回滚：删除或不提供 `config.json` 即回到内置默认值（下载目录、端口、日志、并发上限与 Stage-005 一致）。
2. 行为回滚：`DOWNLOAD_DIR`、`YT_DLP`、`STORE` 相关模块级名字保留，旧测试/探针脚本无需修改即可运行。
3. 数据回滚：数据库与备份策略不变（见 `docs/stage005-migration.md`）；本阶段不修改 schema。
4. 安全回滚：Host/Origin 守卫集中在 `Handler._guard_request()`，如需临时放宽可在该单点关闭并留档。

---

## 10. 阶段验收标准

### 10.1 配置验收

- [x] 下载目录、yt-dlp、FFmpeg、host、port、日志级别、日志文件、数据库路径、并发上限全部来自 `Config`。
- [x] 无 `config.json` 时以默认值运行，`/health.config.source=defaults`。
- [x] 非法配置不阻塞启动，`/health.config.errors` 明确列出原因，对应项回退默认。
- [x] 非回环 host 只有在 `allow_lan=true` 时才被采纳。
- [x] `--check-config` 可在不启动服务的情况下输出配置与依赖诊断，退出码区分正常/有错。

### 10.2 安全验收

- [x] 非回环 `Host` 与恶意 `Origin` 请求被 403 拒绝，YouTube 来源与无 Origin 请求正常。
- [x] 超过上限的 POST 请求体被 413 拒绝。
- [x] 非法 URL（scheme/长度/控制字符/host）被 400 拒绝，且不进入 argv。
- [x] 下载与清理路径始终位于 `Config.download_dir` 内（沿用 `core_files.is_inside`）。
- [x] 日志与 `/health` 中 URL 敏感参数、userinfo 密码与用户主目录被脱敏。

### 10.3 依赖与诊断验收

- [x] yt-dlp/FFmpeg/下载目录/磁盘空间检查结果进入 `/health.dependencies`，失败不影响下载以外功能的响应。
- [x] 依赖检查默认不 spawn 子进程；版本探测为显式选项。
- [x] 配置错误、依赖缺失与权限不足均有明确诊断文本。

### 10.4 质量与边界验收

- [x] 全量单元测试通过（Stage-002..005 无回归），5 个既有探针 + 安全探针全部 OK。
- [x] `py_compile` 全部模块通过，无新增第三方依赖。
- [x] 未实现能力（TLS、远程访问、多用户鉴权、日志轮转、配置热重载）未被写成已支持。
- [x] `config.json` 未被纳入版本控制，`config.example.json` 提供可复制模板。

### 10.5 进入 Stage-007 的条件

- [x] 配置层与依赖检测可被 Adapter 复用（`Config` 提供 yt-dlp/FFmpeg 路径与下载目录）。
- [x] 安全边界（Host/Origin/体积/URL 校验/脱敏）已冻结，Stage-008 的 `format_id` 校验可沿用同一规则。
- [x] 遗留限制（无 TLS、无远程访问、无日志轮转、无配置热重载）已交接。
- [x] 阶段完成影响检查已完成。

---

## 11. 阶段衔接检查

### 11.1 对 Stage-001 的影响

- 默认监听 `127.0.0.1:8765`、默认下载目录、默认 MP4 策略、默认并发上限 3 全部保持不变。
- 新增的 Host/Origin/体积守卫是对 Stage-001 安全基线的强化，不改变既有成功响应形状。

### 11.2 对 Stage-002/003/004 的影响

- `Task` 字段、状态机、控制 API、错误码、调度/FIFO/槽位语义不变。
- 并发上限来源改为 `Config.max_active_tasks`（默认 3），测试仍可通过 `bootstrap()` 注入不同值。
- 日志出口仍是 `server.log()`；新增级别过滤与脱敏，调用点不变。

### 11.3 对 Stage-005 的影响

- 数据库路径优先级增加 `config.db_path`，`MEDIADOCK_DB` 与显式 `bootstrap(db_path)` 仍然有效（显式参数 > 配置 > 环境变量 > 默认）。
- `purge_terminal` 保留条数来源变为 `Config.purge_keep`（默认 200），schema 与重启矩阵不变。

### 11.4 对 Stage-007/008/009/010 输出

- Stage-007/008 通过 `Config` 获取 yt-dlp/FFmpeg 路径与下载目录，不再自行探测。
- Stage-008 的 `format_id` 必须按 Stage-006 的同一校验规则处理，禁止直接拼入 argv。
- Stage-009 的 FFmpeg 调用复用 `resolve_ffmpeg()` 与配置路径。
- Stage-010 需要把 `config.example.json` 与 `--check-config` 纳入发布检查，并补齐日志轮转。

### 11.5 不变约束

- Userscript 不直接执行 yt-dlp/FFmpeg、不推断状态、不新增平台依赖。
- 不默认监听 `0.0.0.0`；不提供远程访问与鉴权。
- 不改变默认 MP4 格式表达式、路径边界规则与 API 错误格式。

---

## 12. 执行记录

| 日期 | 任务 | 负责人 | 状态 | 执行内容 | 验证结果 | 阻塞/遗留问题 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-19 | 阶段准备 | Cline | 已完成 | 核对 Stage-001/Stage-005 输出、`MEDIADOCK_DB` 与模块级真值清点，冻结配置键与安全契约 | Stage-005 基线 172/172 | 无 |
| 2026-09-19 | 任务 001 | Cline | 已完成 | 新增 `core_config.py`：`DEFAULTS`/`Config`/`load_config`/`config_public`，逐键校验、环境变量覆盖、未知键诊断、回环强制、相对路径解析 | `test_config.py` 通过：默认值、文件/环境变量优先级、非法值回退、未知键、回环回退 | 无 |
| 2026-09-19 | 任务 002 | Cline | 已完成 | 新增 `core_security.py`：`validate_url`/`check_host_header`/`check_origin`/`sanitize_url`/`sanitize_path`/`redact`/`body_within_limit`/`clip` | `test_security.py` 通过：6 类非法 URL、Host/Origin 白名单、敏感参数与 userinfo 脱敏、长度截断 | 无 |
| 2026-09-19 | 任务 003 | Cline | 已完成 | 新增 `core_deps.py`：yt-dlp/FFmpeg/下载目录/磁盘检查 + 可选版本探测；`core_engine.resolve_ffmpeg()` | `test_deps.py` 通过：配置路径缺失报错、目录创建与写探针、阈值可注入、`deep=False` 不 spawn 进程 | 无 |
| 2026-09-19 | 任务 004 | Cline | 已完成 | `server.py` 配置驱动：`apply_config`/`reload_config`/`effective_db_path`/`bootstrap(config=)`；`/health` 增加 `config`+`dependencies`；`--check-config` CLI | `test_config.py` 断言下载目录/并发上限/日志文件/数据库路径来自配置；`--check-config` 退出码 0/1/2 | 无 |
| 2026-09-19 | 任务 005 | Cline | 已完成 | `Handler._guard()` 统一 Host/Origin 校验（GET/POST/OPTIONS）、请求体上限、`/download` 改用 `validate_url` | `test_sec_api.py` 通过：403 `forbidden_host`/`forbidden_origin`、413 `payload_too_large`、400 `invalid_url`、既有接口回归 | 无 |
| 2026-09-19 | 任务 006 | Cline | 已完成 | `log(*args, level=...)` 级别过滤 + `redact` 脱敏 + 整行截断；日志文件可配置并支持重开 | `test_sec_api.py` 日志用例通过：`warning` 级别丢弃 info、`token=***`、主目录变 `~`、行长 ≤ 4000 | 无 |
| 2026-09-19 | 任务 007 | Cline | 已完成 | `config.example.json`、`docs/stage006-migration.md`、`.gitignore` 排除 `config.json`、`requirements.txt` 说明、`tests/probe_security.py` | 全量单测 248/248；probe_security 35 项检查 + 既有 4 个探针 + JS 结构检查全部 OK | 无 |
| 2026-09-19 | 回归修复 | Cline | 已完成 | 修复 3 项：`log()` 未把时间戳计入行长度上限、`test_no_file_uses_defaults` 误用 `BASE_DIR` 补丁、`run_check_config` 把 `--check-config` 当未知选项（导致 CLI 恒返回 2） | 248/248 通过；probe_security CLI 三项检查转为 true | 无 |
| 2026-09-19 | 探针加固 | Cline | 已完成 | `probe_control.py` 第二步存在竞态（断言早于第二次运行的 `on_start` 回调）：改为轮询等待第二次运行启动后再断言 | probe_control OK（`resume_kept_breakpoint=True`） | 无 |

---

## 13. 实际输出与计划差异

- 原计划输出：配置层、依赖诊断、HTTP/日志加固、`config.example.json`、迁移文档与测试。
- 实际输出：全部实现，摘要如下。
  - `core_config.py`（`DEFAULTS`/`Config`/`load_config`/`config_public`/`resolve_path`）。
  - `core_deps.py`（4 项检查 + `probe_version` + `run_checks`/`failed_checks`）。
  - `core_security.py`（URL/Host/Origin 校验与脱敏纯函数）。
  - `server.py`：`apply_config`/`reload_config`/`effective_db_path`/`bootstrap(config=)`、
    `Handler._guard()`、分级脱敏日志、`--check-config` CLI、`/health` 的 `config`+`dependencies`。
  - `core_engine.py`：`resolve_ffmpeg()`、可选 `--ffmpeg-location`。
  - `config.example.json`、`docs/stage006-migration.md`、4 个测试模块、`probe_security.py`。
- 差异：
  1. **新增配置键 `max_active_tasks`（默认 3）与 `purge_keep`（默认 200）**：Stage-004 曾把
     「可配置并发数」划给 Stage-006，这里落地为配置项；默认值与冻结值一致，未改变行为。
  2. **`FFMPEG` 实际取值可能来自自动探测**：`apply_config` 在 `ffmpeg_path` 为空时调用
     `resolve_ffmpeg()`，因此本机存在 `C:\Tools\ffmpeg\bin\ffmpeg.exe` 时 argv 会增加
     `--ffmpeg-location`（合并更可靠）。默认格式表达式与文件策略未变。
  3. **`--check-config` 退出码语义**：`1` 同时表示「配置有错误」与「依赖快照不 ok」
     （例如本机没有 yt-dlp），计划里只写了「区分正常/有错」。
  4. **日志整行上限**：`log_line_max` 约束的是含时间戳的整行（计划只写「单行长度」），
     因此先脱敏再按整行截断。
  5. **`Origin` 允许列表为常量而非配置项**：`youtube.com`/`youtu.be`/`youtube-nocookie.com`
     及其子域写在 `core_security`，避免把安全边界交给可编辑配置；计划未明确其来源。
  6. **`/health` 中 `config.values` 的路径先脱敏**（主目录 → `~`），排查时看到的是缩短路径。
  7. **`probe_control.py` 被修改**：原断言依赖「引擎线程先于主线程完成回调」的时序，
     加入 `ffmpeg:` 日志行后暴露为偶发失败；改为轮询等待第二次运行启动（只影响探针本身）。
  8. **`MEDIADOCK_DB` 不作为配置键**：仍由 `core_store.resolve_db_path()` 处理，
     避免测试环境变量被误判为 `source=env`；`config.db_path` 优先级在它之上。
- 差异影响：1/2/5 是范围与默认值的澄清（均不改变既有行为）；3/4/6 是诊断口径的细化；
  7 是测试稳定性修复；8 是保持 Stage-005 契约。均不改变 Task 字段、状态机与既有 API 形状。
- 处理决定：全部接受并写入 `docs/stage006-migration.md`；`plan-whole.md` 增加变更记录 C-005
  并把当前状态改为「Stage-006 已完成，Stage-007 未开始」。

---

## 14. 阶段完成签字

- 阶段状态：已完成
- 阶段验收结论：允许进入 Stage-007
- 对 Stage-001 影响：默认监听 `127.0.0.1:8765`、默认下载目录与 MP4 策略不变；安全基线被强化为可测试的 Host/Origin/体积/URL/脱敏约束
- 对 Stage-002/003/004/005 影响：`Task` 字段、状态机、控制 API、错误码、调度语义、存储与重启矩阵不变；日志出口仍是 `server.log()`；并发上限来源改为 `Config.max_active_tasks`（默认 3）
- 对 Stage-007/008/009/010 影响：Adapter 与格式选择通过 `Config` 获取 yt-dlp/FFmpeg 路径与下载目录；`format_id` 必须沿用同一校验规则；Stage-009 复用 `resolve_ffmpeg()`；Stage-010 需把 `config.example.json` 与 `--check-config` 纳入发布检查并补齐日志轮转
- 是否更新 `plan-whole.md`：是，当前状态改为「Stage-006 已完成，Stage-007 未开始」，Stage-006 完成时间 `2026-09-19`，新增变更记录 C-005（版本仍 `0.2`）
- 审查人：用户
- 日期：2026-09-19


