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
| v0.4.1 | 认证策略调整（未开 Web 认证时按私有/受信任来源放行）、Console 路由改名、修双 `/api` 前缀 404；无新端点 | 同上 |
| v0.4.2 | 设置页分块与菜单图标；无新端点 | 同上 |
| v0.4.3 | `POST/GET /pairing/tokens`、`POST /pairing/tokens/{jti}/revoke`、`PATCH/DELETE /pairing/tokens/{jti}`，控制台「设备配对」面板 | `pairing` |
| v0.5.0 | `GET /browser/logs`、`POST /browser/repair`、`GET /logs`；`GET /update/status`、`GET/PUT /update/settings`、`POST /update/check`（随客户端 0.7.3 完成） | `browser`、`update` |
| v0.5.1 | `GET/PUT /trusted-addresses`；`GET /ssh/settings` 与 `GET /ssh/config` 加访问校验；移除受信头；无新增 capability | 同上 |

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
zip -r -X qwenpaw-node-bridge.zip plugin.json backend ui README.md \
  -x 'backend/__pycache__/*' 'backend/tests/*'
sha256sum qwenpaw-node-bridge.zip > SHA256SUMS
~/.qwenpaw/venv/bin/qwenpaw plugin validate .
~/.qwenpaw/venv/bin/qwenpaw plugin install .
```

正式发版由 tag 触发的 `.github/workflows/release.yml` 自动打包 zip 并生成
`SHA256SUMS`；2.3.0.post1 的 `plugin install` 只接受解压目录，不接受 zip。

安装后重启服务端，请求自述端点确认：

```bash
curl -s http://127.0.0.1:8088/api/plugins/qwenpaw-node-bridge/info
```

```json
{"plugin_version":"0.5.1","protocol_version":1,
 "server_version":"2.2.2.post1",
 "capabilities":["info","search","sse","portability","push","ssh","pairing","browser","update"]}
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

## v0.5.1：来源网段、受信任地址与国际化

本版不新增 capability，只收紧访问策略并补基础设施。

### 敏感端点访问策略

- 开启 Web 登录认证时，所有敏感端点只认有效 token。
- 未开启时，来源命中私有/回环/链路本地/CGNAT 100.64.0.0/10、IPv6 ULA 或
  `QWENPAW_BRIDGE_TRUSTED_CIDRS` 的客户端直接放行。
- 其他来源必须在 Console 的「受信任地址」中添加请求 Host，并填写来源网段；
  只有 Host 与来源 IP 同时命中才放行。App 侧也要把同一地址加入受信任地址，
  两侧都配置才生效。
- 反向代理部署需要设置 `QWENPAW_BRIDGE_TRUST_PROXY=1`，且反代覆盖
  `X-Forwarded-For` / `X-Forwarded-Host`；存在转发头但未开启时敏感端点按未认证处理。
- 旧的 `X-QwenPaw-Trusted-Host: 1` 不再接受，App 也不再发送。

### 新增端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/trusted-addresses` | 返回 `{"items":[{"host","source_cidr"}]}` |
| PUT | `/trusted-addresses` | 全量替换；条目校验与去重，非法来源网段回 400 |

`GET /ssh/settings` 与 `GET /ssh/config` 现在也走同一访问策略；公网未认证来源返回
`{"code":"trusted_address_required","message":…}`，客户端据此提示去设置页配置。

### 其他

- **文件权限**：`ssh_settings.json`、`ssh_key`、`pairing_tokens.json`、
  `trusted_addresses.json` 创建即 0600 并原子替换；数据目录 0700。
- **国际化**：后端按 `?lang` → `Accept-Language` → Console 语言 → 中文解析；
  Console 面板在渲染层统一翻译。当前只提供简体中文，旧客户端行为不变。
- **发布**：tag 触发的 workflow 产出 `qwenpaw-node-bridge.zip` 与 `SHA256SUMS`。

## v0.5.0：浏览器控制链路的日志与修复

浏览器控制链路的状态与自检仍由核心提供：`GET /api/browser/chrome/status`、
`POST /api/browser/chrome/self-test`。**Bridge 不代转、不缓存这两项**，本版本只补核心
没有的两件事：读 NM host 日志、把修复动作转发给官方 Chrome 插件。

### 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/browser/logs?lines=200` | 读取 NM host 日志尾部，按行脱敏；`lines` 取值 1–2000，默认 200 |
| POST | `/browser/repair` | 只接受 `{"action":"setup"}`，转发官方 Chrome 插件的 `setup` |

访问策略与 SSH、设备配对一致：开了 Web 登录时只认有效 token；未开时只放行私有/回环/CGNAT/组网
来源，或 App 与 Bridge 两侧都命中受信任地址的来源。修复会驱动服务端安装扩展资产与 NM host，
属于写操作，客户端应保留二次确认。

### 日志端点

返回 `{"lines": [...], "truncated": bool, "path": "..."}`。

- 读取 `WORKING_DIR/logs/nm-host.log`，默认即 `~/.qwenpaw/logs/nm-host.log`；服务端用
  `QWENPAW_WORKING_DIR` 或历史目录 `~/.copaw` 时随之变化。
- 只从文件尾部读取最多 512 KB、最多 2000 行；`truncated` 表示文件比返回的内容更长。
  从中间开始时首个半行会被丢弃。
- 文件不存在时回 404，`detail` 为 `{"code":"log_not_found","message":"..."}`。这表示
  扩展或宿主尚未启动过，不是插件故障。
- 按行脱敏后返回，替换为 `***` 的八类内容：`Authorization:`、`Bearer …`、`Cookie:`、
  `Set-Cookie:`、`token=`、`api_key=`、`X-Api-Key:`、`secret=`；JSON 形式的同名键也覆盖。
  插件自身不打印 token、密钥或完整请求头。

### 插件自身日志

`GET /logs?lines=200` 读取 `WORKING_DIR/plugin_data/qwenpaw-node-bridge/plugin.log`，
默认即 `~/.qwenpaw/plugin_data/qwenpaw-node-bridge/plugin.log`。记录设置变更、SSH 中继
连接与断开、设备配对令牌签发/吊销、浏览器日志读取与修复动作；按行使用同一套脱敏规则。
文件按 512 KB × 3 轮转。Web 控制台的「QwenPaw Node Bridge」设置页有「插件日志」块，
可刷新查看最近 300 行。

### 修复端点

转发目标为 `http://127.0.0.1:<端口>/api/chrome/setup`，回退 `/api/plugins/chrome/setup`
（插件把路由前缀写成 `/plugins/chrome` 时的实际路径），请求体固定为
`{"install_mode":"unpacked","reset":false}`。

- **端口取值**：优先用核心进程内的 `read_last_api()`（CLI 与桌面壳启动时都会写入），
  其次读环境变量 `QWENPAW_API_PORT`、`QWENPAW_PORT`，最后回落到 8088。主机固定
  `127.0.0.1`，不经过网卡，也不受服务绑定地址影响。
- **鉴权**：插件先自行校验调用方 token（与 SSH、配对同一套 `verify_token` 流程），再把
  同一个 `Authorization: Bearer` 转发给 Chrome 插件的路由，不另发内部凭据。未开 Web
  登录时改发受信任标记。
- **失败返回**：

| 状态 | `detail.code` | 含义 |
| --- | --- | --- |
| 409 | `chrome_plugin_missing` | 服务端未安装 `chrome` 插件；`install_url` 取自 `/api/plugins/catalog` 中版本号最大的条目，取不到为空串 |
| 502 | `bridge_endpoint_unavailable` | 转发失败、插件路由不存在，或插件返回其它非 2xx |
| 400 | `unsupported_action` | 动作不在白名单内 |

- 插件在 `/api/plugins` 列表里存在、但 `/setup` 仍回 404 或 405 时，按路由不可用处理
  （502），不误报成「未安装」。核心对未匹配的路径按请求方法区分：GET 回 404，
  POST 回 405（实测 2.3.0.post1），所以「插件未安装」不能只看 404。
- 重复调用是幂等的：`reset` 固定为 `false`，不会清空已有配置。
- 成功时回 200，形状为 `{"ok": true, "action": "setup", "result": <插件返回原文>}`。

### 更新检查与关于

插件每天在固定时间查一次 GitHub 的 latest release，结果供 Console 的「关于」块与
客户端提示使用。能力名 `update`，端点如下。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/update/status` | 设置与最近一次检查结果的合并视图 |
| GET | `/update/settings` | 只读四个设置字段 |
| PUT | `/update/settings` | 修改设置；`check_time` 必须是 `HH:MM`，布尔字段只接受真布尔值 |
| POST | `/update/check` | 立即检查；失败回 502，`detail.code` 为 `update_check_failed` |

`GET /update/status` 返回 `enabled`、`check_time`、`notify_console`、`notify_client`、
`update_available`、`latest_version`、`latest_url`、`checked_at`、`last_check_date`、
`error`、`plugin_version`、`protocol_version`、`server_version`，另附 `repo_url` 供
界面显示仓库链接。访问策略与 SSH、浏览器、配对一致：开了 Web 登录时只认有效 token；
未开时只放行私有/回环/CGNAT/组网来源，或 App 与 Bridge 两侧都命中受信任地址的来源。

- **默认值**：`enabled=true`、`check_time=09:00`、`notify_console=true`、
  `notify_client=true`。设置写在 `~/.qwenpaw/plugin_data/qwenpaw-node-bridge/update_settings.json`，
  目录 `0700`、文件 `0600`，与 SSH 设置同一套原子写。
- **定时判断**：服务端每 60 秒判断一次「已到 `check_time` 且当天未检查」。两条行为由此
  自动成立：到点当天查一次；服务在 `check_time` 之后启动时，第一次轮询就补查。
  任务随插件生命周期启停，热重载时卸载流程先取消旧任务再起新任务，不会出现两个循环
  同时写状态。
- **请求**：`User-Agent: qwenpaw-node-bridge/<版本>`、`Accept: application/vnd.github+json`，
  超时 10 秒、跟随重定向、读环境变量里的代理。代理配置本身解析失败时直连重试一次
  （实测 httpx 0.28 在 `no_proxy` 含 `[::1]` 时抛 `InvalidURL: Invalid port: ':1]'`，
  此时请求尚未发出）。`draft` 与 `prerelease` 不算新版本。版本比较按数字段，
  `0.10.0` 高于 `0.9.9`。
- **失败处理**：`error` 记录最近一次失败，`update_available`、`latest_version`、
  `latest_url` 保留上一次成功的结论——网络抖动不该把「有新版本」抹成「已是最新」。
  `checked_at` 与 `last_check_date` 无论成败都推进，避免失败后每分钟重试。
- **Console**：设置页末尾的「关于」块显示插件版本、协议版本、服务端版本、仓库链接、
  最近检查时间与结果，提供自动检查、每日时间、Console 提示、QP Node 提示四项设置，
  以及「检查更新」按钮。页面加载时若有新版本且开了 Console 提示，弹一次轻提示
  （模块级标记，切块或重复加载不会重复弹）。
- **日志筛选**：「插件日志」块可按时间段（1 小时 / 24 小时 / 7 天 / 全部）、功能点
  （设置 / SSH / 配对 / 浏览器 / 更新 / 推送 / 其他）与关键词筛选，并显示「筛选后 / 总行数」。
  分类取自行内关键字，无时间戳的续行沿用上一行的时间与分类。筛选全在前端完成，
  切换条件不重新请求；日志框用透明背景与中性描边，深色模式下同样可读。

### 测试

```bash
~/.qwenpaw/venv/bin/python -m unittest discover -s backend/tests -v
```

## v0.4.3：设备配对

为手机端签发访问令牌：`POST /pairing/tokens` 生成一枚专用令牌并返回配对二维码载荷与 PNG；
`GET /pairing/tokens` 列出已签发令牌（只含元数据）；`POST /pairing/tokens/{jti}/revoke` 按 jti 吊销；
`PATCH /pairing/tokens/{jti}` 改登记名（已吊销的拒绝），`DELETE /pairing/tokens/{jti}` 删除已吊销的条目。
控制台设置页「设备配对」排在「SSH 中继」之前，可改名、吊销与删除已吊销条目。
路由同时写了顶层 `title` 与 `meta.title`：2.3.0 的 `registerRoutes` 读前者生成路由元数据，
2.2.2 读后者；缺省会回落到路径末段（`/ssh` 显示成 “Ssh”）。
控制台设置页新增「设备配对」块：可选 7 / 30 / 90 天或长期，填写服务器地址后生成二维码，
令牌只显示一次，可复制；下方列表可逐个吊销。

- 登记表 `pairing_tokens.json` **不存令牌原文**，只有 jti、用户名、签发与到期时间、是否已吊销。
- 按 jti 吊销依赖核心内部的 `_add_to_revocation_list`（`revoke_token` 用的就是它）；核心改名时
  接口回 `immediate: false`，本地记录仍标记吊销，令牌到期自然失效。因此依赖核心 ≥ 2.2.2。
- 服务端未启用 Web 登录时签发回 409，列表返回空——此时令牌不被校验，签了也没有意义。
- 配对码格式 `qwenpaw-node://pair?v=1&addr=…&token=…`，与客户端 `PairQr` 对应，
  样例与约定见客户端 `docs/protocol.md` 第 37 节。

## v0.4.2：设置页分块与菜单图标

- 设置页改为「页面 + 可分块」结构：菜单项仍只有一个「QwenPaw Node Bridge」，页内按功能分成可折叠的卡片，目前只有「SSH 中继」一块。后续加设置只需在 `ui/index.js` 的 `BLOCKS` 里追加一项，不用再注册路由。
- 菜单图标由文字「SSH」换成终端图形。宿主的菜单渲染对 `icon` 做 `isValidElement` 判断：传 React 元素按图形画，传字符串只会渲染成一段文字，所以这里传的是 SVG 元素。
- 端点、字段与保存逻辑均未变。

## v0.4.1：SSH 访问策略与 Console 命名

- **Bridge 中继不再要求服务端 Web 登录认证。** 本地 PTY 仍然要求；中继只要求 Console 里配置了 SSH 目标与凭据。
- 未开 Web 认证时，`PUT /ssh/settings`、`GET /ssh/credential` 与 `WS /ssh` 只放行**私有/回环客户端**，或带 `X-QwenPaw-Trusted-Host: 1` 的请求。该标记由 App 在「受信任的连接」确认地址后、或 Console 页面勾选「此地址仅我可访问」后发送；它只是用户确认，不是密码学证明。公网部署仍应开启登录认证与 HTTPS。（v0.5.1 起该受信头已移除，改为来源网段 + 两侧受信任地址，见上文 v0.5.1 小节。）
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

`ui/index.js` 通过 `window.QwenPaw.registerRoutes("qwenpaw-node-bridge", …)` 挂到 Settings →「扩展设置」→「QwenPaw Node Bridge」，使用宿主注入的 `React` / `antd` / `getApiUrl` / `getApiToken`。字段为地址、端口、用户名、认证方式、私钥来源、私钥路径或内容、口令、登录密码；保存后只显示「已配置 / 未配置」。未开 Web 认证时，私有/回环/组网来源直接可用；其他地址需在「受信任地址」块与 App 两侧配置。页面使用 HTTP 时显示明文传输警告。

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
