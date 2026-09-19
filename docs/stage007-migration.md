# Stage-007 迁移说明：平台 Adapter

> 日期：2026-09-19；「下载 URL 一律当作 YouTube」升级为显式的平台检测边界。
> 平台判定从 `server.py` 抽到 `core_platform.py`，未接入平台从「隐式失败」升级为
> 「显式 400 拒绝」。
> 回滚点：Stage-006 已签字基线（248/248 单元测试 + 6 个探针）。

## 新模块（纯标准库）

- `core_platform.py`
  - `PlatformInfo(name, url, video_id)`：Adapter 对一条 URL 的最小描述。
  - `PlatformAdapter`：`name` / `ready` / `hosts` / `matches()` / `normalize()` /
    `video_id()` / `info()`，只做识别、归一化与元信息，不碰状态机、HTTP、文件。
  - `YouTubeAdapter`：唯一接入下载流程的平台；`hosts` 覆盖
    `youtube.com` 及其子域（`www.`/`m.`/`music.`/`www.youtube-nocookie.com`）与 `youtu.be`。
  - `PlatformRegistry`：`register()` / `adapters()` / `names()` / `get()` /
    `match()` / `detect()`；按注册顺序匹配，第一个命中的 Adapter 胜出。
  - `DEFAULT_REGISTRY`、`detect_platform(url, registry=None)`、`platform_names(registry=None)`。
  - 错误码常量：`invalid_url`、`unsupported_platform`、`platform_not_ready`。

## 检测契约

`PlatformRegistry.detect(url) -> (adapter, error_code, message)`，顺序固定：

| 顺序 | 条件 | 返回 |
| --- | --- | --- |
| 1 | `core_security.validate_url()` 失败 | `(None, "invalid_url", 原因)` |
| 2 | 没有 Adapter 命中 host | `(None, "unsupported_platform", "platform not supported for host <host>")` |
| 3 | 命中但 `ready is False` | `(None, "platform_not_ready", "platform <name> is not available yet")` |
| 4 | 命中且就绪 | `(adapter, "", "")` |

- 检测是纯字符串运算：不访问网络、不建 Task、不写数据库、不改状态。
- `validate_url()` 仍是唯一 URL 校验入口，只是由检测层代 `server.py` 调用。
- `detect()` 不调用 `info()`；由 HTTP 层在确认拿到 adapter 之后调用，
  因此未命中/未就绪的 URL 永远不会进入任何 Adapter 的 `info()`（无越权分发）。

## HTTP 契约变化

| 场景 | 变化前（Stage-006） | 变化后（Stage-007） |
| --- | --- | --- |
| `/download?url=<youtube>` | 200 `{"task_id": "..."}` | 不变；Task `platform="youtube"` |
| `/download?url=<其他合法 http(s)>` | 200，随后由 yt-dlp 失败 | **400 `unsupported_platform`**，不创建 Task |
| `/download?url=<非法>` | 400 `invalid_url` | 不变 |
| `/download`（缺 url） | 400 `missing_url` | 不变 |
| `/status`、`/tasks`、`/health`、控制接口 | — | 形状不变 |

新增错误码：`unsupported_platform`(400)、`platform_not_ready`(400)。
`platform_not_ready` 目前只有测试/扩展点会触发，默认注册表里的平台都是就绪的。

`server.py` 的 `/download` 现在是：

```python
adapter, code_name, message = detect_platform(url)
if adapter is None:
    self._json(_error(code_name, message, 400)[0], code=400)
    return
info = adapter.info(url)
created = scheduler.submit(info.url, platform=info.name)
```

`server.py` 中不再出现任何平台字符串（探针 `server_has_no_platform_literal` 校验）。

## URL 归一化

- `normalize()` 目前只去掉 `#fragment`，不重写查询串：yt-dlp 必须看到与 Stage-006
  相同的 URL。参数级归一化（例如 `list`/`t` 的处理）留到 Stage-008 评估。
- 归一化后的 URL 才写入 `Task.url`，也是唯一交给下载引擎的形式。

## 扩展新平台的步骤

1. 写一个 `PlatformAdapter` 子类：`name`（写入 `Task.platform`）、`hosts`，
   必要时覆盖 `normalize()` / `video_id()`。
2. 确认该平台的下载/鉴权真的可用后再把 `ready` 置为 `True`；否则保持 `False`，
   检测层会返回 `platform_not_ready`。
3. 在 `DEFAULT_REGISTRY` 注册（或提供自己的 registry 给 `detect_platform()`）。
4. 不需要改 `server.py`、调度器、状态机或数据库 schema；`platform` 字段自
   Stage-002 起就已存在并持久化。

## 兼容性与既有测试

- Task 字段、状态集合、排序、并发上限、控制 API、文件清理边界、配置层与安全守卫
  全部不变。
- Stage-001..006 中通过 HTTP `GET /download` 创建的用例原本使用
  `https://example.com/...` 作为「任意合法 URL」。Stage-007 之后该形状会被
  `unsupported_platform` 拒绝，因此这些用例改为等价的 YouTube URL
  （`tests/test_apiv2.py`、`tests/test_control_api.py`、`tests/test_history.py`、
  `tests/test_listing.py`、`tests/test_sec_api.py`、`tests/probe_security.py`）。
  只影响测试夹具，不影响产品契约；`invalid_url` 用例（`ftp://`、控制字符、超长、
  userinfo）保持 `example.com` 不变。
- `scheduler.submit()` / `manager.create()` 不受影响：平台由调用方给出，默认仍是
  `"youtube"`，所以直接调用这些接口的既有测试无需改动。

## 未接入平台（明确不支持）

`tiktok.com`、`x.com`/`twitter.com`、`instagram.com`、`facebook.com` 以及任何其他
host 都会被 `400 unsupported_platform` 拒绝。没有 Cookie/鉴权、没有平台信息探测、
没有 `/formats`，这些都不在 Stage-007 范围内。发布说明必须写成
「支持平台 = YouTube」，不能把显式拒绝描述成已支持。

## 回滚

1. 代码：把 `server.py` 的 `/download` 恢复为 `scheduler.submit(url)`
   （默认平台仍是 `youtube`），或在 `DEFAULT_REGISTRY` 里注册并集的 Adapter。
2. 数据：无 schema 变化，`platform` 字段早已存在，无需迁移或备份。
3. 错误码：`unsupported_platform` 只是新增分支；移除后非 YouTube URL 会回到
   「由 yt-dlp 拒绝」的旧行为（HTTP 仍返回 200 并创建一个最终失败的 Task）。
