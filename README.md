# QwenPaw Node Bridge

QwenPaw Node（Android 客户端）的配套插件，为客户端补上核心 API 没有提供的端点。

插件与客户端是**两个独立仓库**，版本号各自编号：插件从 v0.1.0 起，与客户端版本
不一一对应。两端之间只有「端点路径 + 响应形状」这一层契约，记录在客户端仓库的
`docs/protocol.md`。

## 当前状态

**v0.1.0 只提供 `GET /info` 自述端点，作用是让 QwenPaw Node 检测「插件已安装、
契约版本兼容」，还没有承载任何功能。** 对话、文件、推送仍然全部走 QwenPaw
核心 API；下表中的后续版本才会补上核心没有的端点。

## 版本与能力

| 版本 | 端点 | capability |
| --- | --- | --- |
| v0.1.0 | `GET /info` | `info` |
| v0.1.1（计划） | `GET /search` | `search` |
| v0.1.2（计划） | `GET /events` | `sse` |
| v0.2.0（计划） | `/portability/*` | `portability` |
| v0.3.0（计划） | 离线推送 channel | `push` |

**客户端只按 `capabilities` 里的名字开关功能，不比对版本号。** `plugin_version`
只用于展示与排查。

## 安装

```bash
zip -r qwenpaw-node-bridge-0.1.0.zip plugin.json backend
~/.qwenpaw/venv/bin/qwenpaw plugin validate qwenpaw-node-bridge-0.1.0.zip
~/.qwenpaw/venv/bin/qwenpaw plugin install qwenpaw-node-bridge-0.1.0.zip
```

安装后重启 QwenPaw 服务端，再请求自述端点：

```bash
curl -s http://127.0.0.1:8088/api/plugins/qwenpaw-node-bridge/info
```

预期：

```json
{"plugin_version":"0.1.0","protocol_version":1,
 "server_version":"2.2.2.post1","capabilities":["info"]}
```

未安装时该路径返回 404。返回 401 表示服务端开启了登录而请求没带 token，
**不能**据此判断插件不存在。

## 三条加载器约束

1. 后端入口必须导出名为 `plugin` 的对象，并实现 `register(api)`。
2. `register_http_router` 的 `prefix` 不能是 `/`，也不能与其他插件重复。
3. 只用核心没有的子路径。`/status` 与 `/files` 下的任意子路径已被核心
   `plugins.py` 先注册，请求会被核心截走并返回 `Plugin '<id>' not found.`。

## 协议版本

`protocol_version` 不兼容时客户端按「未检测到」处理：更大的值说明契约已经变更，
旧客户端不该继续使用本插件的端点。本文件新增端点时必须同步递增 `PROTOCOL_VERSION`。