# Stage-001 基线快照（2026-09-19）

> 对应 `Stage-001.md` 任务 001 / 任务 007 输出。
> 方法：静态读码 + `tests/` 自动化基线 + 假 yt-dlp 链路探针。
> 未执行真实 YouTube 下载（T011/T012 未执行）。

## 1. 环境（任务 002）
- Python 3.13.15（venv `C:\Users\Administrator\Envs\mediadock\Scripts\python.exe`）
- `YT_DLP = C:\Tools\yt-dlp\yt-dlp.exe`，`yt-dlp --version = 2026.08.19`
- ffmpeg `9.0.1-essentials_build-www.gyan.dev`，PATH 可用
- `DOWNLOAD_DIR = E:\GitHub\MediaDock\downloads`，目录存在
- 服务绑定 `127.0.0.1:8765`（`server.py main()`），不监听 `0.0.0.0`
- 依赖缺失行为：`FileNotFoundError` -> task `status=error` + 日志
  `yt-dlp not found at ...`（读码确认，未实际卸载依赖做破坏测试）

## 2. API 基线（任务 001/004，`server.py Handler`）
- `GET /health` -> 200 `{"status": "ok"}`（实测通过，见 `unittest_result.txt`）
- `GET /status` -> 200 全量任务 dict（实测通过）
- `GET /status?id=...` -> 200 单任务；不存在 -> 404 `{"error": "task not found"}`（实测通过）
- `GET /download?url=...`：
  - 缺少 url -> 400 `Missing url`（纯文本，非 JSON，实测通过）
  - 非 http/https -> 400 `Invalid url`（纯文本，实测通过）
  - 合法 url -> 200 `{"task_id": "<uuid8>"}` 并起后台线程（假 yt-dlp 探针实测通过）
  - 注意：错误体是纯文本而非 JSON；成功体是 JSON。这是 Stage-002 错误格式统一的输入。
- `OPTIONS` -> 200，CORS `Access-Control-Allow-Origin: *`
- 未知路径 -> 404 `Not Found`
- 保持 `GET /download` 创建任务的兼容行为；是否新增 POST 由 Stage-002 决定。

## 3. Task 字段基线（任务 001，`download_video()` 初始 + 更新）
初始字段：
`status=downloading, percent=0.0, speed="", eta="", url, title="", created_at, updated_at`
流转（读码 + 测试确认）：
- 进度行 -> `percent/speed/eta` 更新（`PROGRESS_RE`）
- 合并行（`[Merger]|Merging formats`） -> `percent=99.0, speed=merging`
- 进程返回 0 -> `status=completed, percent=100.0, speed="", eta=""`
- 返回非 0 / 异常 -> `status=error`（percent 保持最后值；假探针中为 0.0）
- `_get/_all` 返回拷贝；`_update` 刷新 `updated_at`（秒精度 ISO）
- `task_id = str(uuid4())[:8]`；200 个样本无碰撞（测试）
- 内存 `tasks = {}` + `tasks_lock`；重启后丢失，只能重建任务，不能恢复旧进程。

## 4. yt-dlp / 合并 / 日志 / 前端
- 命令：`<YT_DLP> -f "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]" --merge-output-format mp4 --newline --no-playlist -P <DOWNLOAD_DIR> <url>`
- 进度正则同形覆盖普通/HLS；`[download] Destination:` 行跳过（测试覆盖）。
- HLS 百分比可能回退：以返回码 + 最终状态为准。
- 标题回填首个 `[info] <title>: Downloading ...`；失败任务 `title=""`。
- 日志 `MediaDock-server.log` 含完整 URL/命令/原始输出；Stage-006 前需脱敏。
- 前端 `MediaDock.js v2.1`：固定按钮 + `currentTaskId` 单任务 + 1s 轮询；
  合并显示合并中 99%；3s 后回空闲；SPA 保活；本阶段只记录行为，未做手工浏览器验证。

## 5. 已知限制 / Stage-002 输入
1. `server.py` 单文件承载 HTTP + Task + 下载 + 解析 + 配置。
2. 内存任务，重启丢失；不得承诺恢复运行中子进程。
3. 前端单 `currentTaskId`；Stage-003 必须重做任务列表。
4. 无暂停/继续/取消/删除/重试 API；无 SQLite；无多平台；无动态格式。
5. 错误体文本/JSON 不统一（`/download` 400 为文本）。
6. 真实下载 T011/T012 未执行：只做到失败路径可观测，成功路径仍需后续真实验证。

## 6. 测试证据
- `tests/unittest_result.txt`：15/15 通过（T001/T003-T010 对应项）。
- `tests/probe_chain_run.txt` + `tests/probe_chain_result.json`：假 yt-dlp 使任务进入 error，全量包含该任务。
- 未执行：T011/T012 真实下载；T014-T016 手工/重启回归（只记录行为）。
- 命令（venv `C:\Users\Administrator\Envs\mediadock\Scripts\python.exe`）：`python -m unittest discover -s tests -v`；`python tests/probe_chain.py`
