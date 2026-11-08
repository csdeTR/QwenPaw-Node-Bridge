# QwenPaw Node Bridge

QwenPaw Node（Android 客户端）的配套插件，为客户端补上核心 API 没有提供的端点。

插件与客户端是**两个独立仓库**，版本号各自编号：插件从 v0.1.0 起，与客户端版本
不一一对应。两端之间只有「端点路径 + 响应形状」这一层契约，记录在客户端仓库的
`docs/protocol.md` 第 24 节。

## 当前状态

**v0.1.3 提供 `GET /info`、`GET /search` 与 `GET /events`。** 对话、文件、审批等
主链路仍然走 QwenPaw 核心 API，本插件只补三样核心没有的能力：

- `/info`：让客户端检测「插件已安装、契约版本兼容」；
- `/search`：会话历史全文检索（核心 API 没有按内容搜索的端点）；
- `/events`：收件箱未读数与待审批条数的 `text/event-stream` 推送。

## 版本与能力

| 版本 | 端点 | capability |
| --- | --- | --- |
| v0.1.0 | `GET /info` | `info` |
| v0.1.1 | `GET /search` | `search` |
| v0.1.2 | `GET /events` | `sse` |
| v0.1.3 | 无新增端点；`/search` 的 `chat_id` 字段更正为 `session_id` | — |
| v0.2.0（计划） | `/portability/*` | `portability` |
| v0.3.0（计划） | 离线推送 channel | `push` |

**客户端只按 `capabilities` 里的名字开关功能，不比对版本号。** `plugin_version`
只用于展示与排查。v0.1.3 的字段改名不改变 `capabilities`，但会改变 `/search`
的响应形状，客户端需要同步更新，见下节。

## 安装

安装包挂在 [Releases](https://github.com/csdeTR/QwenPaw-Node-Bridge/releases) 下，
资产名固定为 `qwenpaw-node-bridge.zip`，因此链接有两种写法：

- 最新版：`https://github.com/csdeTR/QwenPaw-Node-Bridge/releases/latest/download/qwenpaw-node-bridge.zip`
- 指定版本：`https://github.com/csdeTR/QwenPaw-Node-Bridge/releases/download/v0.1.3/qwenpaw-node-bridge.zip`

在 QwenPaw 的 Web 控制台「插件」页填写上面的地址即可安装，也可以在命令行安装：

```bash
~/.qwenpaw/venv/bin/qwenpaw plugin install \
  https://github.com/csdeTR/QwenPaw-Node-Bridge/releases/latest/download/qwenpaw-node-bridge.zip
```

从源码打包时，在仓库根目录执行：

```bash
zip -r -X qwenpaw-node-bridge.zip plugin.json backend -x 'backend/__pycache__/*'
~/.qwenpaw/venv/bin/qwenpaw plugin validate .
~/.qwenpaw/venv/bin/qwenpaw plugin install qwenpaw-node-bridge.zip
```

`plugin validate` 接受的是目录，不是 zip；从源码校验时直接传仓库根目录。
`__pycache__` 不进包，它是本地运行留下的字节码缓存。

安装后重启 QwenPaw 服务端，再请求自述端点：

```bash
curl -s http://127.0.0.1:8088/api/plugins/qwenpaw-node-bridge/info
```

预期：

```json
{"plugin_version":"0.1.3","protocol_version":1,
 "server_version":"2.2.2.post1","capabilities":["info","search","sse"]}
```

未安装时该路径返回 404。返回 401 表示服务端开启了登录而请求没带 token，
**不能**据此判断插件不存在。

插件加载需要数秒，安装后立刻请求可能仍返回 404，等十秒再试。

## `/search` 的两个定位字段

```
GET /api/plugins/qwenpaw-node-bridge/search?q=<关键词>&limit=20&cursor=<偏移量>
```

```json
{"items":[{"session_id":"mobile-70d0c538","msg_id":"0b1f485e…","role":"assistant",
  "created_at":"2026-11-01T01:21:49","snippet":"…[sleep]…","score":-3.42}],
 "next_cursor":20,"total":31}
```

两条规则都由实测确定，与直觉相反：

1. **`session_id` 是 `history.db` 的 `session_id` 列，不是 `/api/chats` 的对话 `id`。**
   两者在 `mobile-*` 形态下一一对应，但 Web 端的历史会话会出现一个 `session_id`
   挂多个对话。直接拿它请求 `/api/chats/{id}` 会返回 404，客户端必须先用它到
   对话列表换出对话 `id`。
2. **`msg_id` 取 `dedup_key`（即消息对象的 `metadata.original_id`），不能用消息 `id`。**
   `GET /api/chats/{id}` 每次请求都会重新生成消息 `id`，只有 `original_id` 稳定。

`q` 为空白返回 400；`limit` 默认 20、上限 100；`cursor` 是偏移量而不是记录 id。
含中文的查询走 `LIKE` 并在插件侧截取片段，其余走 FTS5 的 `snippet()` 与 `bm25()`：
`conversation_history_fts` 的 `tokenize='porter unicode61'` 不支持中文分词。
检索范围限定 `kind in ('context_msg','model_turn')`，排除体积大且价值低的 `tool_result`。

## `/events` 的推送范围

```
GET /api/plugins/qwenpaw-node-bridge/events
Accept: text/event-stream
```

```
event: ready
data: {"protocol_version":1}

event: inbox
data: {"unread_count":3}

event: approval
data: {"pending":1}
```

连接建立后先发 `ready`，随后各发一次 `inbox` 与 `approval` 的当前值，此后只在数值
变化时推送。插件每 2.5 秒在连接内部轮询一次，客户端断开即退出生成器。

**不包含定时任务。** `CronManager` 按 workspace 隔离，取摘要需要请求上下文里的智能体
标识，而事件生成器里没有 `Request`。客户端的定时任务仍走轮询。

## 三条加载器约束

1. 后端入口必须导出名为 `plugin` 的对象，并实现 `register(api)`。
2. `register_http_router` 的 `prefix` 不能是 `/`，也不能与其他插件重复。
3. 只用核心没有的子路径。`/status` 与 `/files` 下的任意子路径已被核心
   `plugins.py` 先注册，请求会被核心截走并返回 `Plugin '<id>' not found.`。

## 协议版本

`protocol_version` 不兼容时客户端按「未检测到」处理：更大的值说明契约已经变更，
旧客户端不该继续使用本插件的端点。本文件新增端点时必须同步递增 `PROTOCOL_VERSION`。

字段改名不同于新增端点（不改变 `PROTOCOL_VERSION`），但会破坏旧客户端的解析，
发版说明必须写明。
