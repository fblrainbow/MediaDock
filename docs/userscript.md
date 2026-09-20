# 用户脚本安装与更新（`MediaDock.js` 5.2）

> 前端只负责「页面入口 + 请求 + 展示服务端状态」，不运行 yt-dlp/FFmpeg，
> 也不拼接任何下载参数（不变量：Userscript 不得生成 yt-dlp 选择器）。

---

## 1. 安装

1. 浏览器安装 Tampermonkey 扩展。
2. 打开 Tampermonkey 面板 → 「添加新脚本」。
3. 删除模板内容，粘贴 `MediaDock.js` 全文，`Ctrl+S` 保存。
4. 打开 `https://www.youtube.com/watch?v=...` 或 `https://www.youtube.com/shorts/...`。
5. 页面右下角出现 MediaDock 面板，标题右侧显示 `运行 x/3 · 排队 y · v1.0.0`。

脚本头部的权限只指向本地服务：

```javascript
// @version      5.2
// @match        https://www.youtube.com/watch*
// @match        https://www.youtube.com/shorts/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
```

---

## 2. 更新

1. 用仓库中最新 `MediaDock.js` 覆盖 Tampermonkey 里的旧内容并保存。
2. 刷新 YouTube 页面。

版本一致性由发布检查强制：

- 用户脚本 `@version` 必须等于 `core_release.USERSCRIPT_VERSION`
  （当前 `5.2`），`tests/check_userscript.py` 会校验脚本内
  `USERSCRIPT_VERSION` 常量与 `@version` 一致。
- 运行时脚本读取 `/health.version.userscript`；若与服务端期望值不同，
  面板顶部显示「⚠ 脚本 5.2 与服务端期望 5.x 不一致，请更新 MediaDock.js」。
  这样「旧脚本配新服务端」不会变成难查的行为差异。

---

## 3. 面板行为

| 元素 | 行为 |
| --- | --- |
| 下载按钮 | 提交当前页面 URL；非默认清晰度会先查询 `/formats` |
| 清晰度下拉 | `最佳 (1080p MP4)` / `1080p` / `720p` / `480p` / `仅音频 (MP3)`；选择持久化在本地 |
| 标题栏摘要 | `运行 n/上限 · 排队 m · v版本`，点击标题折叠/展开列表 |
| 任务行 | 未完成任务全部显示；完成任务折叠为「已完成 N 个」，展开后最多 100 行 |
| 行内按钮 | 由服务端状态推导：暂停/取消、继续/取消、重试/删除、删除 |
| 任务列表 | 每秒轮询 `/tasks`，所有标签页共享同一份列表与排序 |
| 面板高度 | 约 20 行可见，超出在面板内滚动，不遮挡 YouTube 主内容 |

排序与显示契约（未完成按百分比降序、完成按完成时间倒序、默认 20 条容量）
由服务端 `core_listing` 定义，前端不做二次排序。

---

## 4. 排错

| 现象 | 处理 |
| --- | --- |
| 面板一直「连接中…」或「服务未启动」 | 确认 `server.py` 在跑，且 `curl http://127.0.0.1:8765/health` 返回 200 |
| 按钮提示「清晰度参数无效」 | 下拉选择不在服务端预设表中，重新选择或刷新页面 |
| 提示「该清晰度不可用」 | 该视频没有对应清晰度，换一个预设；或先点一次下拉触发 `/formats` |
| 修改脚本后无变化 | 检查是否保存成功，并刷新页面（SPA 导航不会重载脚本） |
| 无 Node.js 环境下的结构校验 | `python tests\check_userscript.py`（括号平衡、锚点、无模板字符串等） |
