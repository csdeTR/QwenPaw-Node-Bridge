# QwenPaw Node Bridge

[QwenPaw Node](https://github.com/csdeTR/QwenPaw-Node) 的配套插件，为客户端补上核心 API 没有提供的能力。

插件与客户端是**两个独立仓库**，版本号各自编号，与客户端版本
不一一对应。两端之间只有「端点路径 + 响应形状」这一层契约，记录在客户端仓库的
`docs/protocol.md` 第 24 节。

## 版本与能力

| 版本 | 端点 | capability |
| --- | --- | --- |
| v0.1.0 | `GET /info` | `info` |
| v0.1.1 | `GET /search` | `search` |
| v0.1.2 | `GET /events` | `sse` |
| v0.1.3 | 无新增端点；`/search` 的 `chat_id` 更正为 `session_id` | — |
| v0.2.0（计划） | `/portability/*` | `portability` |
| v0.3.0（计划） | 离线推送 channel（无 HTTP 端点） | `push` |
| v0.4.0（计划） | `WS /ssh`、`GET /ssh/config`、`GET/PUT /ssh/settings`，控制台设置面板 | `ssh` |

**客户端只按 `capabilities` 里的名字开关功能，不比对版本号**，`plugin_version`
只用于展示与排查。字段改名不改变 `capabilities`，但会破坏旧客户端的解析，
发版说明必须写明。

## 快速开始

在 QwenPaw Web 控制台的「插件」页填入下面的地址即可安装：

```
https://github.com/csdeTR/QwenPaw-Node-Bridge/releases/latest/download/qwenpaw-node-bridge.zip
```

也可以在命令行安装：

```bash
~/.qwenpaw/venv/bin/qwenpaw plugin install \
  https://github.com/csdeTR/QwenPaw-Node-Bridge/releases/latest/download/qwenpaw-node-bridge.zip
```

从源码打包时，在仓库根目录执行（`plugin validate` 接受目录，不是 zip）：

```bash
zip -r -X qwenpaw-node-bridge.zip plugin.json backend -x 'backend/__pycache__/*'
~/.qwenpaw/venv/bin/qwenpaw plugin validate .
~/.qwenpaw/venv/bin/qwenpaw plugin install qwenpaw-node-bridge.zip
```

安装后重启服务端，请求自述端点确认：

```bash
curl -s http://127.0.0.1:8088/api/plugins/qwenpaw-node-bridge/info
```

```json
{"plugin_version":"0.1.3","protocol_version":1,
 "server_version":"2.2.2.post1","capabilities":["info","search","sse"]}
```

三点判据：未安装时该路径返回 404；返回 401 只说明服务端开了登录而请求没带 token，
**不能**据此判断插件不存在；插件加载需要数秒，安装后立刻请求可能仍是 404，等十秒再试。

## 端点契约

### `/search` 的两个定位字段

```json
{"items":[{"session_id":"mobile-70d0c538","msg_id":"0b1f485e…","role":"assistant",
  "created_at":"2026-11-01T01:21:49","snippet":"…[sleep]…","score":-3.42}],
 "next_cursor":20,"total":31}
```

两条规则都由实测确定，与直觉相反：

1. **`session_id` 是 `history.db` 的列，不是 `/api/chats` 的对话 `id`。**
   两者在 `mobile-*` 形态下一一对应，但 Web 端的历史会话会出现一个 `session_id`
   挂多个对话。直接拿它请求 `/api/chats/{id}` 会 404，客户端要先去对话列表换出 `id`。
2. **`msg_id` 取 `dedup_key`（即 `metadata.original_id`），不能用消息 `id`。**
   `GET /api/chats/{id}` 每次请求都会重新生成消息 `id`，只有 `original_id` 稳定。

含中文的查询走 `LIKE` 并在插件侧截取片段，其余走 FTS5：`conversation_history_fts` 的
`tokenize='porter unicode61'` 不支持中文分词。检索范围限定
`kind in ('context_msg','model_turn')`，排除体积大且价值低的 `tool_result`。
`q` 为空白返回 400；`limit` 默认 20、上限 100；`cursor` 是偏移量而非记录 id。

### `/events` 的推送范围

连接后先发 `ready`，随后各发一次 `inbox` 与 `approval` 的当前值，此后只在数值变化时推送；
插件每 2.5 秒在连接内部轮询，客户端断开即退出生成器。

**不包含定时任务。** `CronManager` 按 workspace 隔离，取摘要需要请求上下文里的智能体标识，
而事件生成器里没有 `Request`，客户端的定时任务仍走轮询。

## v0.4.0 计划：SSH 通道与控制台设置

给客户端的终端页补第二个来源：通过一条 WebSocket 中继跑标准 SSH。服务端本地 PTY 只能连
服务端自己，且渲染依赖客户端自己写的 VT 子集；SSH 拿到真正的 `xterm-256color` PTY，
还能连服务端**可到达的其它主机**。

用 WebSocket 而不是裸 TCP：实际部署通常只暴露 8088，反代不会转发第二个端口。
宿主用 `app.include_router()` 挂载插件路由，FastAPI 会保留 `APIWebSocketRoute`，
因此 `@router.websocket("/ssh")` 可用。

三个端点：`WS /ssh`（二进制帧中继）、`GET /ssh/config`（脱敏配置，只回
`key_configured`，私钥与口令永不回传）、`GET/PUT /ssh/settings`（控制台读写）。
设置面板由插件自备 `ui/index.js`，经 `entry.frontend` 由 Console 动态 import。

**四条安全约束**：WS 必须自己校验令牌（`AuthMiddleware` 是 `BaseHTTPMiddleware`，
看不到 WebSocket）；默认只允许 `127.0.0.1:22`；主机密钥走 TOFU 不自动接受；
私钥优先用服务端已有路径，粘贴方式落盘 `0600` 且不写日志。

完整契约见客户端仓库 `docs/protocol.md` 第 31.6 节。

## 三条加载器约束

1. 后端入口必须导出名为 `plugin` 的对象，并实现 `register(api)`。
2. `register_http_router` 的 `prefix` 不能是 `/`，也不能与其他插件重复。
3. 只用核心没有的子路径。`/status` 与 `/files` 下的任意子路径已被核心 `plugins.py`
   先注册，请求会被核心截走并返回 `Plugin '<id>' not found.`。

## 协议版本

`protocol_version` 不兼容时客户端按「未检测到」处理：更大的值说明契约已经变更，
旧客户端不该继续使用本插件的端点。新增端点时必须同步递增 `PROTOCOL_VERSION`。
字段改名不改变版本号，但会破坏旧客户端的解析，发版说明必须写明。

## 许可

本仓库代码按 [MIT 许可](LICENSE)发布，可自由使用、修改与分发，需保留版权声明。

插件运行时依赖 QwenPaw 服务端。本项目为非官方项目，与 QwenPaw 的开发团队没有隶属关系；
QwenPaw 的名称与标识归其所有者，此处仅用于说明用途。
