# QwenPaw Node Bridge

[QwenPaw Node](https://github.com/csdeTR/QwenPaw-Node) 的配套插件，为客户端补上核心 API 没有提供的能力。

插件与客户端是**两个独立仓库**，版本号各自编号，与客户端版本不一一对应。两端之间只有「端点路径 + 响应形状」这一层契约，记录在客户端仓库的`docs/protocol.md` 第 24 节。

## 版本与能力

| 版本 | 端点 | capability |
| --- | --- | --- |
| v0.1.0 | `GET /info` | `info` |
| v0.1.1 | `GET /search` | `search` |
| v0.1.2 | `GET /events` | `sse` |
| v0.1.3 | 无新增端点；`/search` 的 `chat_id` 更正为 `session_id` | — |
| v0.2.0 | portability 导入转发（随 v0.3.0 发出） | `/portability/*` | `portability` |
| v0.3.0 | 离线推送 channel（无 HTTP 端点） | `push` |
| v0.4.0 | `WS /ssh`、`GET /ssh/config`、`GET /ssh/credential`、`GET/PUT /ssh/settings`，控制台设置面板 | `ssh` |

**客户端只按 `capabilities` 里的名字开关功能，不比对版本号**，`plugin_version`只用于展示与排查。字段改名不改变 `capabilities`，但会破坏旧客户端的解析，发版说明必须写明。

## v0.2.0 / v0.3.0：导入转发与离线推送

`/portability/*` 转发核心的 `PortabilityImportJobManager`：`sources`、`jobs`、`jobs/current`、`jobs/{id}`、`start`、`retry`、`cancel`。核心同名端点被 `_local_only` 限制为回环地址，插件在服务端进程内直接调用任务管理器，因此手机经局域网可用。**只暴露导入，没有导出**；客户端导出走备份并清理临时备份条目。

`qwenpaw_node_push` 是自定义 channel：轮询收件箱未读数与待审批数，数值上升时推到 ntfy / Bark / Telegram；启动取一次基线，不补推历史；`min_interval_seconds` 限流；令牌只写不读，日志不打印 URL 与 token。配置入口在 Web 控制台的 channel 设置里。

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
zip -r -X qwenpaw-node-bridge.zip plugin.json backend ui \
  -x 'backend/__pycache__/*' 'backend/tests/*'
~/.qwenpaw/venv/bin/qwenpaw plugin validate .
~/.qwenpaw/venv/bin/qwenpaw plugin install qwenpaw-node-bridge.zip
```

安装后重启服务端，请求自述端点确认：

```bash
curl -s http://127.0.0.1:8088/api/plugins/qwenpaw-node-bridge/info
```

```json
{"plugin_version":"0.4.1","protocol_version":1,
 "server_version":"2.2.2.post1",
 "capabilities":["info","search","sse","portability","push","ssh"]}
```

三点判据：未安装时该路径返回 404；返回 401 只说明服务端开了登录而请求没带 token，**不能**据此判断插件不存在；插件加载需要数秒，安装后立刻请求可能仍是 404，等十秒再试。

## 端点契约

### `/search` 的两个定位字段

```json
{"items":[{"session_id":"mobile-70d0c538","msg_id":"0b1f485e…","role":"assistant",
  "created_at":"2026-11-01T01:21:49","snippet":"…[sleep]…","score":-3.42}],
 "next_cursor":20,"total":31}
```

两条规则都由实测确定，与直觉相反：

1. **`session_id` 是 `history.db` 的列，不是 `/api/chats` 的对话 `id`。**两者在 `mobile-*` 形态下一一对应，但 Web 端的历史会话会出现一个 `session_id`挂多个对话。直接拿它请求 `/api/chats/{id}` 会 404，客户端要先去对话列表换出 `id`。
2. **`msg_id` 取 `dedup_key`（即 `metadata.original_id`），不能用消息 `id`。**`GET /api/chats/{id}` 每次请求都会重新生成消息 `id`，只有 `original_id` 稳定。

含中文的查询走 `LIKE` 并在插件侧截取片段，其余走 FTS5：`conversation_history_fts` 的`tokenize='porter unicode61'` 不支持中文分词。检索范围限定`kind in ('context_msg','model_turn')`，排除体积大且价值低的 `tool_result`。`q` 为空白返回 400；`limit` 默认 20、上限 100；`cursor` 是偏移量而非记录 id。

### `/events` 的推送范围

连接后先发 `ready`，随后各发一次 `inbox` 与 `approval` 的当前值，此后只在数值变化时推送；插件每 2.5 秒在连接内部轮询，客户端断开即退出生成器。

**不包含定时任务。** `CronManager` 按 workspace 隔离，取摘要需要请求上下文里的智能体标识，而事件生成器里没有 `Request`，客户端的定时任务仍走轮询。

## v0.4.1：SSH 访问策略与 Console 命名

- **Bridge 中继不再要求服务端 Web 登录认证。** 本地 PTY 仍然要求；中继只要求 Console 里配置了 SSH 目标与凭据。
- 未开 Web 认证时，`PUT /ssh/settings`、`GET /ssh/credential` 与 `WS /ssh` 只放行**私有/回环客户端**，或带 `X-QwenPaw-Trusted-Host: 1` 的请求。该标记由 App 在「受信任的连接」确认地址后、或 Console 页面勾选「此地址仅我可访问」后发送；它只是用户确认，不是密码学证明。公网部署仍应开启登录认证与 HTTPS。
- 开了 Web 认证时行为不变：敏感端点要求有效 token。
- Console 的宿主分组「扩展设置」不变；插件路由 label 由「SSH 设置」改为 **QwenPaw Node Bridge**，页面内仍只放 SSH 设置。
- 修复 Console 页面请求路径重复 `/api` 前缀导致的 404：`host.getApiUrl` 会自动补 `/api`，前端只传 `/plugins/qwenpaw-node-bridge/ssh/settings`。

## v0.4.0：SSH 通道与控制台设置

> v0.4.1 起 SSH 端点的认证策略有调整，见上一节；端点路径与设置字段不变。

给客户端的终端页补第二个来源：通过一条 WebSocket 中继跑标准 SSH。服务端本地 PTY 只能连服务端自己；SSH 还能连服务端**可到达的其它主机**。

### 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| WS | `/ssh` | 二进制帧中继到设置里的 host:port |
| GET | `/ssh/config` | 脱敏目标信息；私钥与口令永不回传 |
| GET | `/ssh/credential` | 按需下发私钥或登录密码；访问策略见 v0.4.1，`Cache-Control: no-store`，不写日志 |
| GET / PUT | `/ssh/settings` | 控制台读写完整设置；空白密钥字段保留原值；访问策略见 v0.4.1 |

### 安全约束

1. **WS 自己校验令牌。** `AuthMiddleware` 继承 `BaseHTTPMiddleware`，看不到 WebSocket 连接；插件在 `accept()` 前用 `qwenpaw.app.auth.verify_token` 校验 `Authorization: Bearer` 或 `?token=`。未开鉴权、token 无效、目标未配置一律 `1008` 关闭。
2. **只连设置里的目标。** 客户端不能指定主机；默认 `127.0.0.1:22`。并发上限 4。
3. **凭据按需下发。** `/ssh/credential` 只在服务端已开启登录鉴权时可调用；私钥或密码只放在手机端内存里。手机端还会先过地址安全校验：公网明文地址未确认自有时不请求凭据。
4. **HTTP 敏感端点自己也校验令牌。** `PUT /ssh/settings` 与 `GET /ssh/credential` 不依赖核心中间件：实测鉴权开关热切换后，带空 `Authorization: Bearer` 的请求仍能到达插件路由。插件在 handler 里再调一次 `verify_token`，失败回 401；WS 更是必须在 `accept()` 前后自行校验。
5. **主机密钥 TOFU 在客户端做。** 插件只中继字节；首次指纹确认、指纹变化告警由客户端负责。
6. **设置落盘在 `~/.qwenpaw/plugin_data/qwenpaw-node-bridge/`**，目录 `0700`、文件 `0600`；不放在插件源码目录，避免升级覆盖。私钥优先用服务端已有路径，粘贴方式才写 `ssh_key`。

### 控制台设置面板

`ui/index.js` 通过 `window.QwenPaw.registerRoutes("qwenpaw-node-bridge", …)` 挂到 Settings →「扩展设置」→「QwenPaw Node Bridge」，使用宿主注入的 `React` / `antd` / `getApiUrl` / `getApiToken`。字段为地址、端口、用户名、认证方式、私钥来源、私钥路径或内容、口令、登录密码；保存后只显示「已配置 / 未配置」。未开 Web 认证时可用「此地址仅我可访问」勾选放行公网受信任地址；页面使用 HTTP 时显示明文传输警告。

### 测试

```bash
~/.qwenpaw/venv/bin/python -m unittest discover -s backend/tests -v
```

完整契约见客户端仓库 `docs/protocol.md` 第 31.6 节。

## 三条加载器约束

1. 后端入口必须导出名为 `plugin` 的对象，并实现 `register(api)`。
2. `register_http_router` 的 `prefix` 不能是 `/`，也不能与其他插件重复。
3. 只用核心没有的子路径。`/status` 与 `/files` 下的任意子路径已被核心 `plugins.py`先注册，请求会被核心截走并返回 `Plugin '<id>' not found.`。

## 协议版本

`protocol_version` 不兼容时客户端按「未检测到」处理：更大的值说明契约已经变更，旧客户端不该继续使用本插件的端点。**新增能力只追加 `capabilities`，不递增 `PROTOCOL_VERSION`**；只有既有端点形状发生不兼容变化时才递增。字段改名不改变版本号，但会破坏旧客户端的解析，发版说明必须写明。

## 许可

本仓库代码按 [MIT 许可](LICENSE)发布，可自由使用、修改与分发，需保留版权声明。

插件运行时依赖 QwenPaw 服务端。本项目为非官方项目，与 QwenPaw 的开发团队没有隶属关系；QwenPaw 的名称与标识归其所有者，此处仅用于说明用途。
