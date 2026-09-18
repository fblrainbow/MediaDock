# MediaDock 开发计划

> Local Media Downloader & Processing Toolkit
> 基于 Tampermonkey + Python + yt-dlp + FFmpeg 的本地媒体下载与处理工具

---

## 1. 项目目标

MediaDock 是一个运行在用户本地电脑上的媒体下载与处理工具。

第一阶段主要解决：

> 在浏览器打开 YouTube 视频 → 点击 MediaDock 下载按钮 → 本地自动下载 MP4 → 页面显示实时下载进度。

后续逐步扩展：

* 多任务下载
* 暂停 / 继续 / 取消
* 下载任务管理
* 多平台支持
* Video → Audio
* Video → MP3 / M4A / WAV
* 视频格式转换
* 字幕下载
* 缩略图 / 封面下载
* 更完整的本地媒体处理能力

项目不应该一开始就做成一个庞大的“万能下载器”。

采用：

> **MVP → 稳定 → 抽象 → 扩展**

的开发方式。

---

# 2. 核心设计理念

MediaDock 的核心不是 Tampermonkey。

Tampermonkey 只是浏览器端入口。

真正的核心架构：

```text
Browser
   │
   │ Tampermonkey
   ↓
MediaDock UI
   │
   │ HTTP API
   ↓
Local Python Server
   │
   ├── Task Manager
   │
   ├── Downloader
   │      ↓
   │    yt-dlp
   │
   └── Media Processor
          ↓
        FFmpeg
```

最终形成：

```text
                 MediaDock
                     │
          ┌──────────┴──────────┐
          │                     │
      Browser UI             Local UI
    Tampermonkey              Future
          │                     │
          └──────────┬──────────┘
                     ↓
              Local API Server
                     │
        ┌────────────┼────────────┐
        ↓            ↓            ↓
     Download      Task        Media
      Engine      Manager     Processor
        ↓            │            ↓
     yt-dlp          │         FFmpeg
        │            │            │
        └────────────┴────────────┘
                     ↓
               Local Storage
```

---

# 3. 技术栈

## 3.1 浏览器端

* Tampermonkey
* JavaScript
* GM_xmlhttpRequest
* HTML
* CSS

负责：

* 页面按钮
* 下载任务面板
* 进度显示
* 暂停 / 继续 / 取消按钮
* 当前页面 URL 获取
* 平台识别
* 调用本地 API

---

## 3.2 本地服务

第一阶段：

* Python
* Python HTTP Server

后续可以根据需要升级：

* FastAPI
* Uvicorn

但 MVP 阶段不要过早引入 Web Framework。

---

## 3.3 下载引擎

使用：

```text
yt-dlp
```

负责：

* YouTube 下载
* 视频格式选择
* 音频下载
* 字幕
* 元数据
* 断点续传
* 多平台支持

---

## 3.4 媒体处理

使用：

```text
FFmpeg
```

负责：

* 视频 + 音频合并
* Video → Audio
* MP4 / MKV / WebM 等格式处理
* MP3 / M4A / WAV
* 转码
* 后续媒体处理功能

---

# 4. 项目目录

建议最终目录：

```text
MediaDock/
│
├── README.md
├── LICENSE
├── plan.md
├── CHANGELOG.md
│
├── userscript/
│   └── mediadock.user.js
│
├── server/
│   ├── server.py
│   ├── config.py
│   ├── task_manager.py
│   ├── downloader.py
│   ├── media_processor.py
│   └── server.log
│
├── core/
│   ├── models.py
│   ├── enums.py
│   └── exceptions.py
│
├── adapters/
│   ├── base.py
│   ├── youtube.py
│   ├── tiktok.py
│   ├── x.py
│   └── instagram.py
│
├── tests/
│
└── downloads/
```

MVP 阶段不要求一次建立所有目录。

按照开发阶段逐步创建。

---

# 5. MVP 第一阶段

## 目标

只完成：

```text
YouTube
   ↓
Tampermonkey
   ↓
MediaDock
   ↓
Python
   ↓
yt-dlp
   ↓
FFmpeg
   ↓
MP4
```

功能：

* YouTube 页面显示下载按钮
* 点击下载
* 调用本地 Python Server
* 创建下载任务
* 返回 task_id
* yt-dlp 开始下载
* 实时获取下载进度
* 页面显示：

  * 百分比
  * 下载速度
  * ETA
* 下载完成后显示成功

---

# 6. 当前 YouTube 格式策略

MVP 阶段暂时保持：

```text
bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]
```

并使用：

```text
--merge-output-format mp4
```

暂时不要修改格式选择逻辑。

第一阶段的重点不是追求 4K。

重点是：

> 下载流程稳定 + 任务管理稳定 + UI 稳定。

后续再增加：

```text
1080p
720p
480p
360p
Audio
Best
```

---

# 7. Task Manager

这是 MediaDock 的核心模块之一。

不要继续使用单一的：

```python
download_status
```

而是使用：

```python
tasks = {}
```

每个下载都是独立 Task。

例如：

```json
{
    "task_id": "a82f31c4",
    "status": "downloading",
    "percent": 62.4,
    "speed": "2.88MiB/s",
    "eta": "00:23",
    "url": "https://youtube.com/...",
    "title": "",
    "created_at": "...",
    "updated_at": "..."
}
```

---

# 8. Task 状态

统一定义：

```text
pending
downloading
paused
completed
error
cancelled
```

状态生命周期：

```text
pending
   │
   ↓
downloading
   │
   ├──→ paused
   │      │
   │      ↓
   │   downloading
   │
   ├──→ completed
   │
   ├──→ cancelled
   │
   └──→ error
```

---

# 9. API 设计

第一阶段：

## GET /health

检查本地服务是否运行。

返回：

```json
{
    "status": "ok"
}
```

---

## GET /download

创建下载任务。

请求：

```text
/download?url=VIDEO_URL
```

返回：

```json
{
    "task_id": "a82f31c4"
}
```

---

## GET /status

返回所有任务。

```text
/status
```

返回：

```json
{
    "a82f31c4": {
        "status": "downloading",
        "percent": 62.4,
        "speed": "2.88MiB/s",
        "eta": "00:23"
    }
}
```

后续增加：

```text
/status?id=a82f31c4
```

只查询指定任务。

---

# 10. 第二阶段：多任务

目标：

```text
同时下载：

Video A   72%
Video B   31%
Video C   15%
```

每个任务：

```text
独立 task_id
独立 subprocess
独立进度
独立状态
```

Python：

```text
Task Manager
     │
     ├── Task A → yt-dlp
     ├── Task B → yt-dlp
     └── Task C → yt-dlp
```

Tampermonkey：

```text
下载任务

┌──────────────────────────┐
│ Video A                  │
│ ███████████████░░ 72%    │
│ 2.8 MiB/s   ETA 20s      │
│ [暂停] [取消]            │
└──────────────────────────┘

┌──────────────────────────┐
│ Video B                  │
│ ██████░░░░░░░░░░ 31%     │
│ 1.5 MiB/s   ETA 1m20s    │
│ [暂停] [取消]            │
└──────────────────────────┘
```

---

# 11. 第三阶段：暂停 / 继续

暂停不能简单地：

```python
thread.pause()
```

而应该围绕 yt-dlp 进程实现。

任务：

```text
downloading
      ↓
    pause
      ↓
   paused
```

继续：

```text
paused
   ↓
resume
   ↓
downloading
```

API：

```text
POST /pause
POST /resume
POST /cancel
```

请求：

```json
{
    "task_id": "a82f31c4"
}
```

---

# 12. 断点续传

暂停 / 继续必须保证：

```text
已经下载的数据
        ↓
不要重新下载
        ↓
继续剩余部分
```

因此需要正确利用 yt-dlp 的继续下载机制。

验证：

1. 下载到 30%
2. 暂停
3. 查看文件
4. 继续
5. 确认从已有进度继续
6. 下载完成
7. FFmpeg 正常合并

---

# 13. 第四阶段：取消任务

增加：

```text
取消
```

流程：

```text
downloading
      ↓
cancel
      ↓
terminate yt-dlp
      ↓
cancelled
```

需要明确：

* 是否保留临时文件
* 是否删除未完成文件
* 是否允许重新开始

MVP 可以采用：

> 取消任务后停止进程，并保留必要的临时文件，由后续重新下载机制处理。

---

# 14. 第五阶段：下载任务持久化

目前：

```python
tasks = {}
```

存在内存里。

Python Server 重启后：

```text
tasks → 全部消失
```

后续使用：

```text
SQLite
```

保存：

```text
Task
├── id
├── url
├── title
├── status
├── percent
├── speed
├── eta
├── file_path
├── created_at
└── updated_at
```

这样即使：

```text
Windows 重启
Python Server 重启
```

也可以恢复任务记录。

---

# 15. 第六阶段：平台 Adapter

不要把不同网站的代码全部写进：

```text
server.py
```

建立统一接口：

```python
class PlatformAdapter:
    def match(self, url):
        pass

    def get_info(self, url):
        pass

    def create_task(self, url):
        pass
```

例如：

```text
adapters/
│
├── base.py
├── youtube.py
├── tiktok.py
├── x.py
└── instagram.py
```

统一入口：

```text
URL
 ↓
PlatformDetector
 ↓
YouTubeAdapter
TikTokAdapter
XAdapter
InstagramAdapter
```

---

# 16. 第七阶段：Video → Audio

这是 MediaDock 的重要扩展方向。

输入：

```text
video.mp4
```

输出：

```text
audio.mp3
audio.m4a
audio.wav
```

核心使用：

```text
FFmpeg
```

例如：

```text
Video
  ↓
MediaProcessor
  ↓
FFmpeg
  ↓
Audio
```

UI：

```text
视频文件
    │
    ├── MP4
    ├── MP3
    ├── M4A
    └── WAV
```

---

# 17. 第八阶段：下载 + 音频模式

YouTube 页面可以提供：

```text
┌───────────────────────────┐
│ MediaDock                 │
├───────────────────────────┤
│ 下载视频                  │
│                           │
│ ○ Best                    │
│ ○ 1080p                   │
│ ○ 720p                    │
│ ○ 480p                    │
│                           │
│ 下载音频                  │
│                           │
│ ○ M4A                     │
│ ○ MP3                     │
│                           │
│ [开始下载]                │
└───────────────────────────┘
```

---

# 18. 第九阶段：Formats API

后续增加：

```text
GET /formats?url=...
```

Python 调用：

```text
yt-dlp -F URL
```

解析格式。

返回：

```json
{
    "video": [
        {
            "height": 1080,
            "ext": "mp4",
            "format_id": "616"
        },
        {
            "height": 720,
            "ext": "mp4",
            "format_id": "..."
        }
    ],
    "audio": [
        {
            "ext": "m4a",
            "format_id": "140"
        }
    ]
}
```

然后前端动态显示可用格式。

---

# 19. 第十阶段：Media Processor

建立统一媒体处理层：

```text
media_processor.py
```

负责：

```text
Video → Audio
Video → MP3
Video → M4A
Video → WAV

Video → MP4
Video → MKV
Video → WebM

Video → GIF
```

不要让 UI 直接调用 FFmpeg。

统一：

```text
Tampermonkey
      ↓
Python API
      ↓
MediaProcessor
      ↓
FFmpeg
```

---

# 20. UI 设计

最终 UI 分成两个区域。

## 下载按钮

浏览器页面右下角：

```text
┌───────────────┐
│ ⬇ MediaDock   │
└───────────────┘
```

---

## Task Panel

点击以后：

```text
MediaDock
────────────────────────

Downloads

Video A
██████████████░░ 72%
2.88 MiB/s
ETA 00:23

[暂停] [取消]

Video B
██████░░░░░░░░░░ 31%
1.52 MiB/s
ETA 01:20

[暂停] [取消]

Video C
████████████████ 100%
✅ Completed
```

---

# 21. UI 与后端解耦

Tampermonkey 不应该知道：

```text
yt-dlp 怎么运行
FFmpeg 怎么运行
文件怎么合并
```

它只知道：

```text
/download
/status
/pause
/resume
/cancel
```

后端负责所有实际工作。

这样未来即使：

```text
Tampermonkey
```

替换成：

```text
Vue
Electron
Desktop App
Web UI
```

后端都可以继续使用。

---

# 22. 配置系统

不要把路径永久写死在业务代码里。

早期可以：

```python
DOWNLOAD_DIR = ...
YT_DLP = ...
FFMPEG = ...
```

后续改成：

```text
config.json
```

例如：

```json
{
    "download_dir": "J:\\Download\\Story\\test",
    "yt_dlp": "C:\\Tools\\yt-dlp\\yt-dlp.exe",
    "ffmpeg": "C:\\Tools\\ffmpeg\\bin\\ffmpeg.exe",
    "server": {
        "host": "127.0.0.1",
        "port": 8765
    }
}
```

---

# 23. 安全设计

Local API 默认：

```text
127.0.0.1
```

不要默认监听：

```text
0.0.0.0
```

避免让局域网其他设备直接调用下载服务。

URL 必须进行基本校验。

文件路径必须限制在配置的下载目录范围内。

不要允许 HTTP API 任意执行系统命令。

---

# 24. 日志

统一：

```text
server.log
```

记录：

```text
Server started
Task created
Download started
Progress
Download completed
Download failed
Task cancelled
FFmpeg started
FFmpeg completed
```

错误需要包含：

```text
task_id
url
command
error
```

方便排查问题。

---

# 25. 错误处理

至少处理：

```text
Python Server 未启动
yt-dlp 不存在
FFmpeg 不存在
URL 无效
网络错误
YouTube 视频不可访问
视频不存在
格式不存在
磁盘空间不足
下载失败
FFmpeg 合并失败
任务取消
```

前端不要只显示：

```text
下载失败
```

应该尽量显示：

```text
❌ 下载失败

原因：
yt-dlp returned exit code 1
```

---

# 26. 测试策略

不要每增加一个功能就同时修改整个系统。

按照：

```text
Backend
   ↓
API
   ↓
Tampermonkey
   ↓
UI
```

逐层测试。

---

## Test 1

直接执行：

```text
yt-dlp
```

确认下载正常。

---

## Test 2

Python Server：

```text
/health
```

确认：

```json
{
    "status": "ok"
}
```

---

## Test 3

浏览器访问：

```text
/download?url=...
```

确认创建任务。

---

## Test 4

访问：

```text
/status
```

确认：

```text
percent
speed
eta
status
```

实时变化。

---

## Test 5

Tampermonkey 点击下载。

确认：

```text
按钮
 ↓
任务创建
 ↓
进度显示
 ↓
完成
```

---

## Test 6

多任务：

```text
A
B
C
```

同时下载。

---

## Test 7

暂停：

```text
A
 ↓
Pause
 ↓
Paused
 ↓
Resume
 ↓
Downloading
```

---

## Test 8

取消：

```text
A
 ↓
Cancel
 ↓
Cancelled
```

---

# 27. 开发顺序

严格按照以下顺序开发。

```text
Phase 1
│
├── YouTube 下载
├── Tampermonkey UI
├── Python Server
├── yt-dlp
├── FFmpeg
└── 基础进度
        ↓
Phase 2
│
├── Task Manager
├── task_id
├── 多任务
└── 任务列表
        ↓
Phase 3
│
├── Pause
├── Resume
└── Cancel
        ↓
Phase 4
│
├── SQLite
├── Task Persistence
└── History
        ↓
Phase 5
│
├── Platform Adapter
├── TikTok
├── X
└── Instagram
        ↓
Phase 6
│
├── Format Selection
├── 1080p
├── 720p
├── Audio
└── Formats API
        ↓
Phase 7
│
├── Video → Audio
├── MP3
├── M4A
└── WAV
        ↓
Phase 8
│
├── Media Processor
├── Video Conversion
├── GIF
└── Subtitle
```

---

# 28. 当前第一阶段明确不做

为了避免项目失控，MVP 暂时不做：

* 4K 专门优化
* 所有视频网站
* 登录账号管理
* Cookie 管理 UI
* 云端下载
* WebSocket
* Docker
* Vue
* Electron
* SQLite
* FFmpeg 高级参数 UI
* 下载历史
* 用户系统

先完成：

> **YouTube → MP4 → 进度显示**

---

# 29. 第一阶段完成标准

只有满足下面所有条件，才进入第二阶段：

```text
[✓] Windows 开机自动启动 Server

[✓] YouTube 页面出现 MediaDock 下载按钮

[✓] 点击按钮可以创建 Task

[✓] Server 返回 task_id

[✓] yt-dlp 正常下载

[✓] FFmpeg 正常合并

[✓] /status 可以获得实时进度

[✓] UI 显示百分比

[✓] UI 显示速度

[✓] UI 显示 ETA

[✓] 下载完成显示 Completed

[✓] 下载失败显示 Error

[✓] Server 重启后可以正常重新下载
```

---

# 30. 项目长期目标

MediaDock 最终不是：

> 一个油猴下载脚本。

而是：

> **一个本地媒体下载与处理平台。**

最终架构：

```text
                       MediaDock
                           │
             ┌─────────────┴─────────────┐
             │                           │
        Download Engine             Media Engine
             │                           │
          yt-dlp                       FFmpeg
             │                           │
      ┌──────┼──────┐             ┌──────┼──────┐
      │      │      │             │      │      │
   YouTube TikTok   X           Audio  Video   Image
      │      │      │             │      │
      └──────┴──────┘             └──────┴──────┘
             │                           │
             └─────────────┬─────────────┘
                           ↓
                     Task Manager
                           │
                           ↓
                      Local Storage
```

---

# 31. 开发原则

### 原则 1：一次只解决一个问题

不要同时开发：

```text
多平台 + 4K + 暂停 + SQLite + Vue
```

一次只完成一个阶段。

---

### 原则 2：先跑通，再抽象

例如：

```text
server.py
```

先跑通。

确认以后再拆：

```text
task_manager.py
downloader.py
media_processor.py
```

不要为了“架构漂亮”提前制造大量文件。

---

### 原则 3：UI 与后端解耦

Tampermonkey 只负责：

```text
UI + API
```

Python 负责：

```text
任务 + 下载 + 媒体处理
```

---

### 原则 4：所有下载都是 Task

不要再设计：

```text
当前下载
```

而应该从一开始就认为：

```text
每一次下载 = 一个 Task
```

这样多任务、暂停、继续、取消、历史记录都可以自然扩展。

---

### 原则 5：yt-dlp 是下载引擎

不要在 Tampermonkey 里重新实现 YouTube：

```text
格式解析
签名解析
视频流处理
音视频合并
```

这些交给 yt-dlp + FFmpeg。

---

### 原则 6：MediaDock 自己负责“调度”

MediaDock 的核心价值：

```text
网页入口
    +
任务管理
    +
下载控制
    +
媒体处理
```

而不是重新造一个 yt-dlp。

---

# 32. GitHub 项目定位

Repository：

```text
MediaDock
```

建议简介：

```text
A local media downloader and processing toolkit powered by yt-dlp and FFmpeg.
```

中文：

```text
基于 yt-dlp 和 FFmpeg 的本地媒体下载与处理工具。
```

核心关键词：

```text
Tampermonkey
yt-dlp
FFmpeg
YouTube
Video Downloader
Media Processing
Python
Local Server
```

---

# 33. 当前开发任务

现在不要直接进入多平台。

第一项任务：

```text
MediaDock Phase 1
```

目标：

```text
现有 yt-dlp-server
        ↓
改造成 MediaDock Server
        ↓
现有 Tampermonkey
        ↓
改造成 MediaDock Userscript
        ↓
YouTube 下载
        ↓
task_id
        ↓
实时进度
```

第一阶段完成以后，再进入：

```text
Phase 2：多任务下载管理
```

然后：

```text
Phase 3：暂停 / 继续 / 取消
```

再进入：

```text
Phase 4：任务持久化
```

最后逐渐扩展成为完整的：

> **MediaDock Local Media Platform**
