# 变更日志（Release Notes）

> 版本号唯一真值是 `core_release.APP_VERSION`。本文件的最新条目必须与它一致，
> 不一致时 `tests/release_check.py` 会失败。
>
> 用户脚本版本是 `core_release.USERSCRIPT_VERSION`（当前 `5.2`）。

---

## [1.0.2] - 2026-09-20

「仅音频」现在真正产出 MP3。

**修复**

- 之前选「仅音频」只挑选最佳音频流（`-f bestaudio/best`），容器随平台，
  YouTube 上得到的是 `.webm`（Opus），并不是 MP3。
- 现在该预设额外让 yt-dlp 用 FFmpeg 提取音频：
  `-x --audio-format mp3 --audio-quality 0`，输出为真实 `.mp3`（不再产生 `.webm`）。
- `core_parse` 识别 `[ExtractAudio] Destination:` 行，把最终 `.mp3` 记入本次运行的产物
  （取消时能一并清理）；界面上 FFmpeg 阶段的文案由「合并中」改为「处理中」。

**不变**

- 默认预设与 1080p/720p/480p 的 argv 完全不变（仍为 `--merge-output-format mp4`）。
- `POST /audio`（把已完成的视频转成 mp3/m4a/wav）行为不变，仍是独立能力。
- Task 字段、状态机、存储 schema、API 错误码均未变化。

**验收证据**：361 个单元测试、9 个探针（`probe_formats` 38 项检查含真实 argv）、
发布检查 60 项全部通过。

---

## [1.0.1] - 2026-09-20

启动时自动接管端口，不用再手动结束旧实例。

**新增**

- `core_instance.py`：定位占用 `host:port` 的监听进程 → **只在该进程确实是**
  python/pythonw 运行的 `server.py` 时才结束其进程树 → 等待端口释放 → 允许重新绑定。
- `python server.py --restart`：启动前主动接管端口，不等端口冲突。
- 占用者不是 MediaDock 时**拒绝接管**：保留退出码 2，并打印占用者 PID、进程名与命令行。
- 证据：`tests/test_instance.py`（20 用例，注入 runner，不真实杀进程）、
  `tests/probe_restart.py`（真实启动两个服务，14 项检查全通过）。

**行为变化**

| 场景 | 1.0.0 | 1.0.1 |
| --- | --- | --- |
| 端口空闲 | 正常启动 | 正常启动（行为不变） |
| 端口被旧 MediaDock 实例占用 | 退出码 2，需要手动结束 | **自动接管**：结束旧实例后继续启动 |
| 端口被其他程序占用 | 退出码 2 | 退出码 2（不接管，打印占用者信息） |
| 接管时旧实例有运行中任务 | — | 任务变为 `error` + `error_code=interrupted`（Stage-005 语义不变） |

**验收证据**：355 个单元测试、9 个探针、发布检查 60 项全部通过。

---

## [1.0.0] - 2026-09-20

首个正式交付版本：MediaDock MVP 交付（Stage-001 ~ Stage-010 全部完成）。

**下载与任务**

- 一键下载 YouTube 视频为 MP4，默认策略保持 `height<=1080`（`D-005`）。
- 独立 Task 模型与统一状态机：`pending → downloading → paused/completed/error/cancelled`（`D-006`）。
- 最多 3 个并发下载，超出任务 FIFO 排队（`D-011`）。
- 暂停/继续（保留 `.part` 断点续传）、取消、重试、删除终态记录（`D-008`、`D-009`）。

**共享列表与前端**

- 所有 YouTube 页面共用 `/tasks` 一份任务列表，未完成按百分比降序、
  完成按完成时间倒序（`D-015`），默认容量 20 条、完成折叠、面板内滚动（`D-016`）。
- 清晰度下拉：`最佳 (1080p MP4)` / `1080p` / `720p` / `480p` / `仅音频`。
- 面板显示服务端版本；用户脚本与服务端期望版本不一致时给出提示。

**持久化与历史**

- SQLite `tasks.db`（schema v2），迁移前自动备份。
- `/history` 查询终态任务、`/events` 查询状态事件流、`POST /delete` 删除记录（`D-017`）。
- 重启语义（`D-012`）：运行中/排队中的任务变为 `error` + `error_code=interrupted`
  且不自动恢复；`paused` 保持暂停；`completed` 记录与排序不变。

**配置、依赖与安全**

- 配置层 `core_config`：优先级 `显式路径 > 环境变量 > config.json > 内置默认`。
- 依赖诊断 `core_deps` 与 `python server.py --check-config [--probe]`（退出码 0/1/2）。
- 默认只监听 `127.0.0.1`（`D-004`）；Host/Origin 守卫、路径边界、日志脱敏、
  POST body 上限。

**平台与格式**

- 平台层 `core_platform`：唯一平台判定入口，首批只支持 YouTube（`D-013`）；
  其他平台显式拒绝 `unsupported_platform`。
- `/formats` 探测与格式校验：`-f` 表达式永远由服务端常量拼装，
  未选择格式时使用冻结的默认表达式。

**音频（Stage-009）**

- `POST /audio` 把已完成的视频转成 MP3/M4A/WAV，输出与下载任务同目录。
- 复用同一状态机、控制接口、并发上限、`/tasks` 排序与 `/history`。
- 失败不留下「看起来已完成」的文件：只有 `rc=0` 且输出存在且非空才算完成。

**发布（Stage-010）**

- `/health` 新增 `version {app, api, userscript, schema}`。
- 新增 `core_release.py`（版本真值 + 静态发布检查）。
- 新增 `tests/release_check.py` 发布门禁：静态检查 + 真实 HTTP 全链路 +
  重启语义 + 回滚演练 + 安全守卫，结果写入 `tests/release_check_result.json`。
- 新增交付文档：安装、配置、用户脚本、已知限制、升级回滚、发布清单。

**验收证据（2026-09-20）**

- 单元测试：`Ran 335 tests ... OK`。
- 探针：8 个全部 OK（chain/control/multi/persist/security/platform/formats/media）。
- 发布检查：60 项全部通过，退出码 0。
- 用户脚本结构检查：通过（637 行）。

---

## 版本对应关系

| 应用版本 | 用户脚本 | 存储 schema | 阶段 |
| --- | --- | --- | --- |
| `1.0.2` | `5.2` | `2` | Stage-012（仅音频 = MP3） |
| `1.0.1` | `5.2` | `2` | Stage-011（启动接管端口） |
| `1.0.0` | `5.2` | `2` | Stage-001 ~ Stage-010 |
| （开发期） | `5.1` | `2` | Stage-008 |
| （开发期） | `5.0` | `2` | Stage-005 |
| （开发期） | `4.0` | `2` | Stage-004 |
| （开发期） | `3.0` | `1` | Stage-003 |

---

## 下一步候选（只记录，不实现）

以下内容来自 Stage-010 的「长期扩展重新评估」，需要新的需求评估与阶段文件，
不会在未评估的情况下直接加入：

| 候选 | 价值 | 需要先确认 |
| --- | --- | --- |
| 第二批平台 Adapter（如 TikTok/Instagram） | 覆盖更多来源 | 鉴权方式、Cookie 策略、平台条款 |
| 字幕/缩略图下载与内嵌 | 提升可用性 | 文件名与清理策略、Task 字段是否扩展 |
| 清晰度选择写入历史 | 重试可回到原清晰度 | 需要 schema v3 迁移与回滚方案 |
| 音频格式与码率选择 | 更细的输出控制 | 与「固定目标表」的安全边界平衡 |
| 任务详情页/日志下载 | 排错更方便 | 前端复杂度与信息暴露范围 |
| 安装包 / Windows 服务 | 免手动启动 | 管理员权限、卸载残留、更新方式 |
