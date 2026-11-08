# -*- coding: utf-8 -*-
"""QwenPaw Node Bridge：供 Android 客户端使用的桥接端点。

v0.1.3 提供 `GET /info`、`GET /search` 与 `GET /events`。后续能力（`portability`）
逐版追加到 `capabilities` 数组，客户端按名字开关功能，不比对版本号。

三条硬约束（都来自 qwenpaw 的插件加载器）：

1. 模块必须导出名为 ``plugin`` 的对象，并实现 ``register(api)``；加载器用
   ``getattr(module, "plugin")`` 取它。
2. 路由前缀不能是 ``/``，也不能与其他插件重复（registry 会抛 ``ValueError``）。
3. 只使用核心 ``plugins.py`` 没有的子路径：``/status`` 与 ``/files`` 下的任意
   子路径已被核心 router 先注册，请求到不了这里。``/info``、``/search``、
   ``/events`` 是安全的。

检索为什么不用纯 FTS5：``history.db`` 的索引是
``fts5(content, content='conversation_history', tokenize='porter unicode61')``，
``unicode61`` 把连续的 CJK 字符当成**一个 token**。实测 ``match '缓存'`` 命中 0 条，
而 ``match '"工作目录中存在"'`` 命中 1 条。因此中文查询一律走 ``LIKE``，
英文查询仍走 FTS5（porter 词干化在那里是有用的）。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import logging

logger = logging.getLogger(__name__)

#: 与客户端约定的契约版本。本文件新增端点时必须一起递增。
PROTOCOL_VERSION = 1

#: 本插件声明的能力。客户端按名字开关功能，不看版本号。
CAPABILITIES = ("info", "search", "sse")

#: 事件流的轮询间隔，与客户端原先 `push-messages` 的轮询周期一致。
EVENT_POLL_SECONDS = 2.5

#: 路由前缀。最终路径是 ``/api/plugins/qwenpaw-node-bridge``。
ROUTE_PREFIX = "/plugins/qwenpaw-node-bridge"

#: 命中片段里高亮词用的包裹字符，与客户端约定一致。
SNIPPET_OPEN = "["
SNIPPET_CLOSE = "]"

#: 片段左右各留多少字符。
SNIPPET_PAD_LEFT = 28
SNIPPET_PAD_RIGHT = 56

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

#: 检索范围：只查用户与助手可见的正文。
#:
#: ``history.db`` 的 ``kind`` 不是 API 的 ``type``。实测（2.2.2.post1 与 2.3.0b2 一致）
#: 只有三种值：``context_msg``（role=user）、``model_turn``（role=assistant 正文）、
#: ``tool_result``（role=assistant 工具输出）。按 API 的 ``type=message`` 写
#: ``kind='message'`` 会一条都查不到。``tool_result`` 体积大且对用户是噪音，排除。
_MESSAGE_KIND = "kind in ('context_msg', 'model_turn')"


def _server_version() -> str:
    """服务端版本。

    读的是分发包的元数据，不是 ``qwenpaw.__version__``：后者在 2.2.2.post1 里
    是一个模块，``str()`` 出来是 ``<module 'qwenpaw.__version__' from ...>``，
    客户端原样显示会得到一串没有意义的路径。拿不到时返回空串而不是抛异常：
    这个字段只用于排查「两端配错版本」。
    """
    try:
        from importlib.metadata import version

        return version("qwenpaw")
    except Exception:  # pylint: disable=broad-except
        return ""


def _agent_dirs() -> list[Path]:
    """所有智能体的工作区目录。

    用核心的 ``get_agent_dirs()``，不要自己扫目录：自定义 ``workspace_dir`` 与
    已删除的智能体扫不出来、也过滤不掉。
    """
    try:
        from qwenpaw.config.utils import get_agent_dirs

        return get_agent_dirs()
    except Exception:  # pylint: disable=broad-except
        logger.exception("读取智能体目录失败")
        return []


def _connect(db: Path) -> sqlite3.Connection:
    """只读打开 history.db。

    服务端进程同时在写这个库，只读连接能避免插件成为一个写入方；
    ``timeout`` 让偶发的写锁不至于立刻抛 ``database is locked``。
    """
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)


def _fts_query(q: str) -> str:
    """把用户输入拼成 FTS5 查询。

    逐词加引号成短语，避免 ``-``、``*``、``:`` 被当成 FTS5 语法；
    引号本身按 FTS5 规则双写转义。
    """
    words = [w for w in q.split() if w]
    return " ".join('"{}"'.format(w.replace('"', '""')) for w in words)


def _like_snippet(content: str, q: str) -> str:
    """LIKE 分支的命中片段。

    SQLite 没有针对中文可用的 ``snippet()``，这里自己截一段并用方括号包住命中词。
    """
    lowered = content.lower()
    pos = lowered.find(q.lower())
    if pos < 0:
        return content[: SNIPPET_PAD_LEFT + SNIPPET_PAD_RIGHT]
    start = max(0, pos - SNIPPET_PAD_LEFT)
    end = min(len(content), pos + len(q) + SNIPPET_PAD_RIGHT)
    head = "…" if start > 0 else ""
    tail = "…" if end < len(content) else ""
    return (
        head
        + content[start:pos]
        + SNIPPET_OPEN
        + content[pos:pos + len(q)]
        + SNIPPET_CLOSE
        + content[pos + len(q):end]
        + tail
    )


def _search_like(db: Path, q: str, agent: str | None, take: int) -> tuple[list[dict], int]:
    """中文查询：``content LIKE '%q%'``，按时间倒序。"""
    sql_where = f"{_MESSAGE_KIND} and content like ?"
    params: list = [f"%{q}%"]
    if agent:
        sql_where += " and agent_id = ?"
        params.append(agent)
    try:
        with _connect(db) as conn:
            total = conn.execute(
                f"select count(*) from conversation_history where {sql_where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"select seq, session_id, role, created_at, content, dedup_key"
                f" from conversation_history where {sql_where}"
                " order by created_at desc, seq desc limit ?",
                params + [take],
            ).fetchall()
    except sqlite3.Error:
        logger.exception("检索失败 db=%s", db)
        return [], 0
    items = [
        {
            "session_id": row[1],
            "msg_id": row[5] or str(row[0]),
            "role": row[2],
            "created_at": row[3],
            "snippet": _like_snippet(row[4] or "", q),
            "score": 0.0,
        }
        for row in rows
    ]
    return items, total


def _search_fts(db: Path, q: str, agent: str | None, take: int) -> tuple[list[dict], int]:
    """英文查询：FTS5，命中片段交给 ``snippet()``。"""
    match = _fts_query(q)
    if not match:
        return [], 0
    sql_where = f"{_MESSAGE_KIND} and f.content match ?"
    params: list = [match]
    if agent:
        sql_where += " and h.agent_id = ?"
        params.append(agent)
    try:
        with _connect(db) as conn:
            total = conn.execute(
                "select count(*) from conversation_history_fts f"
                " join conversation_history h on h.seq = f.rowid"
                f" where {sql_where}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                "select h.seq, h.session_id, h.role, h.created_at,"
                " snippet(conversation_history_fts, 0, ?, ?, '…', 12),"
                " bm25(conversation_history_fts), h.dedup_key"
                " from conversation_history_fts f"
                " join conversation_history h on h.seq = f.rowid"
                f" where {sql_where}"
                " order by h.created_at desc, h.seq desc limit ?",
                [SNIPPET_OPEN, SNIPPET_CLOSE] + params + [take],
            ).fetchall()
    except sqlite3.Error:
        logger.exception("检索失败 db=%s", db)
        return [], 0
    items = [
        {
            "session_id": row[1],
            "msg_id": row[6] or str(row[0]),
            "role": row[2],
            "created_at": row[3],
            "snippet": row[4],
            "score": float(-(row[5] or 0.0)),
        }
        for row in rows
    ]
    return items, total


async def _snapshot() -> dict:
    """取一份事件快照。

    只取进程级、插件能直接读到的两项：

    - ``inbox``：未读事件数，走 ``inbox_store.query_events``，与
      ``GET /api/console/inbox/events`` 同源。
    - ``approval``：待审批数，读 ``ApprovalService._pending``。这是私有字段，
      但 ``GET /api/console/push-messages`` 也是这么读的，属于同一取数口径。

    **不含定时任务。** ``CronManager`` 按 workspace（智能体）隔离，要由
    ``Request`` 解析出当前 agent 才能拿到；插件没有请求上下文，读它的持久化文件
    又依赖未公开的 schema。定时任务列表因此继续由客户端按原方式刷新。

    任何一项取不到就沿用上一次的值：事件流是增量提示，不是数据源，缺一项
    不该让整条流断掉。
    """
    inbox = 0
    approvals = 0
    try:
        from qwenpaw.app.inbox_store import query_events

        _, _, unread = await query_events(limit=1)
        inbox = int(unread)
    except Exception:  # pylint: disable=broad-except
        logger.exception("读取收件箱未读数失败")
    try:
        from qwenpaw.app.approvals import get_approval_service

        service = get_approval_service()
        # pylint: disable=protected-access
        async with service._lock:
            approvals = len(service._pending)
    except Exception:  # pylint: disable=broad-except
        logger.exception("读取待审批数失败")
    return {"inbox": inbox, "approval": approvals}


def _sse(event: str, data: dict) -> str:
    """拼一帧 SSE。``data`` 只放一行 JSON，用不到多行拼接。"""
    return "event: {}\ndata: {}\n\n".format(event, json.dumps(data, ensure_ascii=False))


class BridgePlugin:
    """插件主体。"""

    def register(self, api) -> None:
        router = APIRouter()

        @router.get("/info")
        async def info():
            """自述：版本、契约版本、服务端版本与能力清单。

            ``plugin_version`` 直接读 manifest，不另存常量——两处各维护一份
            迟早会对不上。
            """
            return {
                "plugin_version": str(api.manifest.get("version", "")),
                "protocol_version": PROTOCOL_VERSION,
                "server_version": _server_version(),
                "capabilities": list(CAPABILITIES),
            }

        @router.get("/search")
        async def search(
            q: str = Query(..., description="关键词"),
            limit: int = Query(20, ge=1, le=50),
            cursor: int = Query(0, ge=0, description="已取到的条数，翻页时回传 next_cursor"),
            x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
        ):
            """会话全文检索。

            ``cursor`` 是**偏移量**，不是某条记录的 id：``seq`` 只在单个
            ``history.db`` 内唯一，多个智能体之间拼不成全序。

            每条命中带两个定位字段。``session_id`` 是 ``history.db`` 的
            ``session_id`` 列，**不是** ``/api/chats`` 返回的对话 ``id``：两者
            在 ``mobile-*`` 形态下恰好一一对应，但 Web 端的历史会话会出现多个
            对话共用一个 ``session_id``。客户端要先用它到对话列表里换出对话
            ``id``。``msg_id`` 取 ``dedup_key``，服务端每次请求重新生成的
            消息 ``id`` 不稳定，不能用来定位。
            """
            keyword = q.strip()
            if not keyword:
                raise HTTPException(status_code=400, detail="q 不能为空")
            if len(keyword) > 200:
                raise HTTPException(status_code=400, detail="q 过长")

            dirs = _agent_dirs()
            items: list[dict] = []
            total = 0
            take = cursor + limit + 1
            for d in dirs:
                db = d / "history.db"
                if not db.exists():
                    continue
                if _CJK.search(keyword):
                    part, count = _search_like(db, keyword, x_agent_id, take)
                else:
                    part, count = _search_fts(db, keyword, x_agent_id, take)
                items.extend(part)
                total += count

            items.sort(key=lambda it: (it["created_at"] or "", it["msg_id"]), reverse=True)
            window = items[cursor:cursor + limit]
            next_cursor = cursor + len(window) if len(items) > cursor + limit else None
            return {"items": window, "next_cursor": next_cursor, "total": total}

        @router.get("/events")
        async def events(request: Request):
            """事件流（SSE）。

            连接建立后先发一帧 ``ready``，随后只在快照变化时发帧：
            ``inbox`` 带未读数，``approval`` 带待审批数。**不回放历史事件**，
            客户端只按这两项做增量提示，丢帧不影响正确性。

            轮询循环挂在连接自己的生成器里，而不是 ``register_startup_hook``
            起的常驻任务：常驻任务要处理应用生命周期、多实例订阅者与优雅退出，
            而本插件只服务「一个设备一条连接」，随连接生灭最简单，也没有泄漏风险。
            """
            async def stream():
                last = await _snapshot()
                yield _sse("ready", {"protocol_version": PROTOCOL_VERSION})
                yield _sse("inbox", {"unread_count": last["inbox"]})
                yield _sse("approval", {"pending": last["approval"]})
                while True:
                    await asyncio.sleep(EVENT_POLL_SECONDS)
                    if await request.is_disconnected():
                        break
                    current = await _snapshot()
                    for name in ("inbox", "approval"):
                        if current[name] != last[name]:
                            payload = (
                                {"unread_count": current["inbox"]}
                                if name == "inbox"
                                else {"pending": current["approval"]}
                            )
                            yield _sse(name, payload)
                    last = current

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    # 关掉反向代理的缓冲，否则帧会被攒到连接结束才下发。
                    "X-Accel-Buffering": "no",
                },
            )

        api.register_http_router(router, prefix=ROUTE_PREFIX)
        logger.info(
            "qwenpaw-node-bridge registered at %s (protocol=%s)",
            ROUTE_PREFIX,
            PROTOCOL_VERSION,
        )


plugin = BridgePlugin()
