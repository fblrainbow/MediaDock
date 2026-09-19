# Stage-006 迁移说明：配置、依赖与安全加固

> 日期：2026-09-19；「写死在代码里的路径/端口/日志」升级为明确的配置层，
> 并补齐依赖诊断与本地安全边界。
> 回滚点：Stage-005 已签字基线（172/172 单元测试 + 5 个探针）。

## 新模块（纯标准库）

- `core_config.py`
  - `DEFAULTS` / `Config` / `load_config(path=None, env=None)` / `config_public()`。
  - 优先级：显式路径 > 环境变量 > `config.json` > 内置默认值。
  - 非法值不阻塞启动：回退默认值 + `errors`；未识别键记 `unknown_config_key`。
  - 相对路径（`download_dir`/`log_file`/`ytdlp_path`/`ffmpeg_path`）按仓库根解析。
  - `host` 非回环且没有 `allow_lan: true` → 回退 `127.0.0.1` 并报配置错误。
- `core_deps.py`
  - `check_ytdlp` / `check_ffmpeg` / `check_download_dir` / `check_disk_space`
    / `probe_version` / `run_checks(cfg, deep=False)` / `failed_checks`。
  - 启动快照 `deep=False` 不启动任何子进程；`--check-config --probe` 才做版本探测。
  - FFmpeg 缺失是 `warn`（只影响合并），yt-dlp 缺失/配置路径不存在是 `error`。
- `core_security.py`
  - `validate_url`、`check_host_header`、`check_origin`、`sanitize_url`、
    `sanitize_path`、`redact`、`body_within_limit`、`clip`。

## 配置键

| 键 | 默认 | 约束 |
| --- | --- | --- |
| `host` | `127.0.0.1` | 非回环需要 `allow_lan` |
| `port` | `8765` | 1..65535 |
| `allow_lan` | `false` | 布尔 |
| `download_dir` | `<仓库>/downloads` | 非空，相对路径按仓库根解析 |
| `ytdlp_path` | `""` | 空 = 自动探测 |
| `ffmpeg_path` | `""` | 空 = 自动探测 |
| `log_level` | `info` | `debug`/`info`/`warning`/`error` |
| `log_file` | `<仓库>/MediaDock-server.log` | 非空 |
| `db_path` | `""` | 空 = 交给 `MEDIADOCK_DB` |
| `max_active_tasks` | `3` | 1..8（默认值不变） |
| `purge_keep` | `200` | 1..10000 |
| `request_max_bytes` | `65536` | 1024..1048576 |
| `log_line_max` | `4000` | 200..20000 |

环境变量覆盖（优先级高于文件）：
`MEDIADOCK_HOST`、`MEDIADOCK_PORT`、`MEDIADOCK_ALLOW_LAN`、
`MEDIADOCK_DOWNLOAD_DIR`、`MEDIADOCK_YTDLP`、`MEDIADOCK_FFMPEG`、
`MEDIADOCK_LOG_LEVEL`、`MEDIADOCK_LOG_FILE`、`MEDIADOCK_MAX_ACTIVE`。
`MEDIADOCK_CONFIG` 指向配置文件路径；`MEDIADOCK_DB` 语义不变（不属于配置键）。

## 数据库路径优先级

`bootstrap(db_path=...)` 显式参数 > `config.db_path` > `MEDIADOCK_DB` >
`<仓库>/tasks.db`。schema、迁移与备份策略未变（见 stage005-migration.md）。

## 启动装配

`server.apply_config(config)` 把 `Config` 落到模块真值
（`HOST`/`PORT`/`DOWNLOAD_DIR`/`YT_DLP`/`FFMPEG`/`LOG_LEVEL`/`LOG_MAX_LINE`/`LOG_FILE`），
并重新打开日志文件；`reload_config(path=None)` = `load_config` + `apply_config`。
`bootstrap(db_path=None, config=None)` 之后还会重建 `Scheduler`
（并发上限 = `Config.max_active_tasks`）、按 `Config.purge_keep` 清理并刷新依赖快照。

## `/health` 与 `--check-config`

`GET /health` 增加两个字段（既有键不变）：

```json
{
  "status": "ok",
  "storage": {"kind": "...", "db": "...", "schema_version": 2, "degraded": false, "reason": ""},
  "config": {"source": "defaults|file|env", "path": "...", "ok": true,
             "errors": [], "warnings": [], "values": { "...": "..." }},
  "dependencies": {"ok": true, "checked_at": "...",
                   "checks": [{"name": "yt-dlp", "status": "ok",
                               "path": "...", "message": "..."}]}
}
```

`config.values` 中的字符串路径已被 `sanitize_path` 缩短（用户主目录 → `~`）。

`python server.py --check-config [--config PATH] [--probe]` 打印同样的
`config` + `dependencies` JSON 且不启动服务：

| 退出码 | 含义 |
| --- | --- |
| 0 | 配置有效且依赖快照 `ok` |
| 1 | 存在配置错误或依赖硬错误（yt-dlp 缺失、下载目录不可写等） |
| 2 | 命令行参数错误 |

## HTTP 加固

| 项 | 规则 | 失败响应 |
| --- | --- | --- |
| `Host` | 只接受 `127.0.0.1`/`localhost`/`::1`（可带端口） | 403 `forbidden_host` |
| `Origin` | 缺失放过；存在必须是 YouTube 域（`youtube.com`/`youtu.be`/`youtube-nocookie.com` 及其子域）；`null`/`*` 拒绝 | 403 `forbidden_origin` |
| 请求体 | `Content-Length` ≤ `request_max_bytes` | 413 `payload_too_large` |
| `/download?url=` | `validate_url()`：http/https、有 host、无控制字符/空格/引号、长度 ≤ 2048、禁止 userinfo | 400 `invalid_url` |

- 守卫对 `GET`/`POST`/`OPTIONS` 全部生效，集中在 `Handler._guard()`。
- Userscript 场景不受影响：浏览器请求带 `Origin: https://www.youtube.com`，
  回环 `Host` 天然满足；服务端仍返回 `Access-Control-Allow-Origin: *`。
- 命令行始终是 argv 列表、无 shell：URL 只作为最后一个参数，且以 `-` 开头的
  输入在 scheme 校验处就被拒绝，无法变成 yt-dlp 参数。

## 日志

- `log(*args, level="info")` 按 `log_level` 过滤（默认 `info`）。
- 每行先脱敏再截断：URL 敏感查询参数（`token`/`sig`/`key`/`apikey`/`auth`…）值
  替换为 `***`，userinfo 密码替换为 `***`，用户主目录替换为 `~`，
  整行（含时间戳）不超过 `log_line_max`。
- 日志文件来自 `Config.log_file`；打开失败时只在 stderr 提示并继续运行。
- 仍未实现日志轮转（属于 Stage-010 发布运维）。

## 依赖检查

| 检查 | 结论 |
| --- | --- |
| `yt-dlp` | 配置路径存在 → `ok`；配置路径不存在 → `error`；自动探测失败 → `missing` |
| `FFmpeg` | 同上，但自动探测失败降级为 `warn`（只影响合并） |
| `download_dir` | 不存在则创建，然后写探针文件验证可写；失败 → `error` |
| `disk_space` | 剩余空间 `< 500 MiB` → `warn`；无法读取 → `error` |

## 测试与证据

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_config.py` | T601-T608、T620、T625、T626：默认值、文件/环境变量优先级、非法值与未知键、回环强制、路径解析、`config_public` 脱敏、`--check-config` 退出码、配置驱动并发上限 |
| `tests/test_security.py` | T609-T614、T630：URL/Host/Origin 校验、脱敏、长度截断、体积上限 |
| `tests/test_deps.py` | T615-T619：可执行文件/目录/磁盘检查、`deep` 开关不 spawn 进程、版本探测 |
| `tests/test_sec_api.py` | T614、T620-T625、T627：真实 HTTP 的 403/413/400、正常下载回归、`/health` 诊断、日志分级与脱敏落盘 |
| `tests/probe_security.py` | 真实 config.json + 日志文件 + SQLite + HTTP：配置生效、依赖快照、Host/Origin/体积/URL 守卫、日志脱敏、坏配置不阻塞、路径边界、argv 形状、`--check-config` 子进程（证据见 `tests/probe_security_result.json`） |
| `tests/check_userscript.py` | 未改动 Userscript，结构校验仍通过 |

## 已知限制（交接 Stage-007/008/009/010）

- 配置修改需要重启服务（无热重载）；UI 不提供配置编辑。
- 只支持单实例、回环访问；不提供 TLS、反向代理与多用户鉴权。
- `--check-config --probe` 会真实执行 `<exe> --version`；普通启动不会。
- 日志无轮转，长跑需要人工清理。
- `max_active_tasks` 可配置，但默认 3 与 Stage-004 冻结值一致，未做高并发压力测试。
- Host/Origin 守卫只保护本地服务边界，不是完整的 CSRF/鉴权方案。

## 回滚

1. 删除 `config.json`（或保留但把值改回默认）即回到 Stage-005 行为。
2. 代码回滚：`server.py` 的模块级真值（`DOWNLOAD_DIR`/`YT_DLP`/`LOG_FILE`/`DB_PATH`）
   与旧名字兼容，旧测试与探针无需修改。
3. 数据回滚：本阶段未修改数据库 schema，沿用 `docs/stage005-migration.md` 的备份/还原步骤。
4. 安全回滚：如需临时放宽，可只在 `Handler._guard()` 单点调整并留档。

