# 配置说明

> 配置来源与优先级的唯一真值在 `core_config.py`；本文档是它的可读版本。
> 校验失败不会阻止启动：错误会出现在 `Config.errors`，并回落到内置默认值。

---

## 1. 优先级

```text
显式路径（server.py --check-config --config <file>）
    > 环境变量（MEDIADOCK_*）
        > config.json（仓库根目录，或 MEDIADOCK_CONFIG 指定的路径）
            > 内置默认值（core_config.DEFAULTS）
```

查看当前生效值：`curl http://127.0.0.1:8765/health` 的 `config.values`，
或运行 `server.py --check-config`。

---

## 2. 配置键

| 键 | 默认值 | 合法范围 | 说明 |
| --- | --- | --- | --- |
| `host` | `"127.0.0.1"` | 回环地址，或非回环且 `allow_lan=true` | 监听地址；非回环地址未开 `allow_lan` 时报错并回落 |
| `port` | `8765` | 1-65535 | 监听端口 |
| `allow_lan` | `false` | 布尔 | 允许非回环监听；开启后局域网可访问（会记录 warning） |
| `download_dir` | `<repo>/downloads` | 相对路径按仓库根解析 | 下载与音频输出目录（路径边界的根） |
| `ytdlp_path` | `""` | 字符串 | yt-dlp 路径；留空则用 PATH |
| `ffmpeg_path` | `""` | 字符串 | FFmpeg 路径；留空则用 PATH |
| `log_level` | `"info"` | `debug`/`info`/`warning`/`error` | 日志级别 |
| `log_file` | `<repo>/MediaDock-server.log` | 路径 | 日志文件；打不开时只在 stderr 提示，不影响启动 |
| `db_path` | `""` | 路径 | 数据库路径；留空 = `<repo>/tasks.db`（兼容 `MEDIADOCK_DB`） |
| `max_active_tasks` | `3` | 1-8 | 并发下载上限，超出任务进入 FIFO 队列 |
| `purge_keep` | `200` | 1-10000 | 终态任务记录的保留条数 |
| `request_max_bytes` | `65536` | 1024-1048576 | POST body 上限，超出 → 413 `payload_too_large` |
| `log_line_max` | `4000` | 200-20000 | 单行日志最大长度 |

`config.example.json` 的键必须与 `core_config.DEFAULTS` 完全一致，
发布检查（`tests/release_check.py`）会强制校验，避免示例与代码漂移。

---

## 3. 环境变量

| 变量 | 对应键 |
| --- | --- |
| `MEDIADOCK_CONFIG` | 配置文件路径本身（不是键） |
| `MEDIADOCK_HOST` | `host` |
| `MEDIADOCK_PORT` | `port` |
| `MEDIADOCK_ALLOW_LAN` | `allow_lan` |
| `MEDIADOCK_DOWNLOAD_DIR` | `download_dir` |
| `MEDIADOCK_YTDLP` | `ytdlp_path` |
| `MEDIADOCK_FFMPEG` | `ffmpeg_path` |
| `MEDIADOCK_LOG_LEVEL` | `log_level` |
| `MEDIADOCK_LOG_FILE` | `log_file` |
| `MEDIADOCK_MAX_ACTIVE` | `max_active_tasks` |
| `MEDIADOCK_DB` | 数据库路径；由 `core_store.resolve_db_path` 处理，优先级低于显式 `db_path` |

空字符串视为「未设置」。

---

## 4. 示例

最小可用（全部默认）：

```json
{}
```

自定义下载目录与并发：

```json
{
  "download_dir": "D:\\Media\\MediaDock",
  "max_active_tasks": 2,
  "purge_keep": 500
}
```

临时改用另一份配置做诊断：

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe server.py --check-config --config .\config.test.json
```

---

## 5. 安全相关行为

- 非回环 `host` 未开 `allow_lan` → 记入 `errors` 并回落 `127.0.0.1`。
- 非回环 `host` + `allow_lan=true` → 记入 `warnings`（API 可被局域网访问）。
- `/health.config` 中的路径经过 `sanitize_path`（`~` 简写）后才返回。
- 日志经过 `redact`（脱敏 URL 参数）与 `clip`（长度截断）。
