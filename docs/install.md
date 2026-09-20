# 安装与启动（v1.0.0）

> 目标：在一台只有 Windows + Python 的机器上，按本文档完成
> 「装依赖 → 配置检查 → 启动 → 健康检查」四步。
> 发布方式（D-014）：**手动启动 + 可选开机自启脚本**，不提供安装包、不注册系统服务。

---

## 1. 前置条件

| 组件 | 要求 | 说明 |
| --- | --- | --- |
| Windows | 10/11 | 路径与命令按 PowerShell 编写 |
| Python | 3.13（本机虚拟环境）`C:\Users\Administrator\Envs\mediadock\Scripts\python.exe` | 只用标准库，无需第三方包 |
| yt-dlp | 可执行文件或 `yt-dlp` 在 PATH | 下载引擎，缺失时 `/health.dependencies` 报错 |
| FFmpeg | 可执行文件或 `ffmpeg` 在 PATH | 合并 MP4、转音频必需 |
| 浏览器 | Chrome/Edge + Tampermonkey | 前端入口 |

检查依赖是否就绪（不会启动服务）：

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe server.py --check-config --probe
```

- 退出码 `0` = 配置有效且依赖可用；`1` = 有问题；`2` = 命令行参数错误。
- `--probe` 会实际运行 `<path> --version`，用于确认 yt-dlp/FFmpeg 真能执行。

---

## 2. 安装步骤

```powershell
# 1) 进入仓库
cd E:\GitHub\MediaDock

# 2) 安装 Python 依赖（当前无第三方包，命令仍然保留以便后续升级）
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe -m pip install -r requirements.txt

# 3) 建立配置（可选；不建则使用内置默认值）
Copy-Item config.example.json config.json

# 4) 配置检查
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe server.py --check-config
```

如果 yt-dlp/FFmpeg 不在 PATH，把绝对路径填进 `config.json`：

```json
{
  "ytdlp_path": "C:\\Tools\\yt-dlp\\yt-dlp.exe",
  "ffmpeg_path": "C:\\Tools\\ffmpeg\\bin\\ffmpeg.exe"
}
```

配置键与优先级见 [configuration.md](configuration.md)。

---

## 3. 启动服务

```powershell
C:\Users\Administrator\Envs\mediadock\Scripts\python.exe server.py
```

启动日志会打印版本、监听地址、下载目录、yt-dlp/FFmpeg 位置、并发上限、
存储 schema 与依赖摘要。服务默认监听 `127.0.0.1:8765`。

> 端口已被占用时进程以退出码 `2` 结束并记录明确日志（不会静默共享端口）。
> 结束旧进程：
> `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*server.py*' }`

---

## 4. 健康检查

```powershell
curl.exe http://127.0.0.1:8765/health
```

必须看到：

| 字段 | 期望值 |
| --- | --- |
| `status` | `"ok"` |
| `version.app` | 与 `docs/release-notes.md` 最新版本一致（本版 `1.0.0`） |
| `version.schema` | `2`（数据库不可用时为 `0`） |
| `storage.kind` | `sqlite`；数据库损坏时降级为 `memory` 且 `degraded=true` |
| `config.ok` | `true`（有错误时 `config.errors` 会列出原因） |
| `dependencies.ok` | `true` |

再确认任务接口可用：

```powershell
curl.exe http://127.0.0.1:8765/tasks
curl.exe http://127.0.0.1:8765/audio
```

---

## 5. 安装用户脚本

见 [userscript.md](userscript.md)。

---

## 6. 可选：开机自启（不推荐新手直接启用）

有两种等价做法，都**不修改系统服务**：

**方案 A：启动文件夹快捷方式**

1. `Win+R` → `shell:startup` 打开启动文件夹。
2. 新建快捷方式，目标填：
   `C:\Users\Administrator\Envs\mediadock\Scripts\pythonw.exe E:\GitHub\MediaDock\server.py`
3. 起始位置填 `E:\GitHub\MediaDock`。

**方案 B：任务计划程序**

1. 创建任务 → 触发器「登录时」。
2. 操作：程序 `C:\Users\Administrator\Envs\mediadock\Scripts\pythonw.exe`，
   参数 `E:\GitHub\MediaDock\server.py`，起始于 `E:\GitHub\MediaDock`。
3. 不勾选「使用最高权限运行」（服务只监听回环地址，无需管理员）。

注意：

- 自启失败不会有弹窗，请检查 `MediaDock-server.log`。
- 修改 `config.json` 后需要重启进程才会生效。

---

## 7. 卸载

1. 结束 `server.py` 进程（见第 3 节命令）。
2. 删除启动文件夹快捷方式/计划任务（如果创建过）。
3. 删除仓库目录即可；数据都在仓库内：
   - 下载文件：`downloads/`
   - 任务数据库：`tasks.db`（及迁移备份 `tasks.db.bak`）
   - 日志：`MediaDock-server.log`
4. Tampermonkey 中删除 MediaDock 脚本。

---

## 8. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 页面按钮提示「本地服务未启动」 | 服务未启动或端口被占用 | 启动服务并 `curl /health`；检查 `MediaDock-server.log` |
| 面板提示「脚本 5.2 与服务端期望 5.x 不一致」 | 用户脚本与服务端版本不匹配 | 按 [userscript.md](userscript.md) 更新脚本 |
| 任务失败 `ffmpeg_missing` | 未配置 FFmpeg | 填 `config.json` 的 `ffmpeg_path` 或加入 PATH |
| 任务失败 `unsupported_platform` | URL 不属于已接入平台 | 当前只支持 YouTube（见 [known-limitations.md](known-limitations.md)） |
| 任务失败 `interrupted` | 服务在任务运行中被重启 | 用重试按钮重新开始 |
| 明确度选完提示 `formats_unavailable` | 探测失败或超时 | 稍后重试；确认 yt-dlp 可执行 |
| `/health.storage.degraded` 为 `true` | 数据库无法打开 | 见 [upgrade-rollback.md](upgrade-rollback.md) 的降级与恢复步骤 |
