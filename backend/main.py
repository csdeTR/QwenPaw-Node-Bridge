# -*- coding: utf-8 -*-
"""QwenPaw Node Bridge：供 Android 客户端使用的桥接端点。

v0.5.0 起提供 ``/browser/logs`` 与 ``/browser/repair``：读取 NM host 日志并按行脱敏，
把修复动作转发给官方 chrome 插件的 ``/setup``。能力名（`browser`）追加到
`capabilities` 数组，客户端按名字开关功能，不比对版本号。

同一版本追加 ``/update/status``、``/update/settings`` 与 ``/update/check``：按每天
固定的 HH:MM 查 GitHub latest release，结果供 Console「关于」块与客户端提示使用。
能力名（`update`）同样只追加到 `capabilities`，`protocol_version` 仍为 1。

v0.4.3 起另外提供 ``/pairing/tokens`` 系列端点：签发、登记与吊销移动端访问令牌，
供客户端「设备配对」使用。能力名（`pairing`）追加到 `capabilities` 数组，
客户端按名字开关功能，不比对版本号。

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
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from qwenpaw.app.channels.base import BaseChannel, OnReplySent, ProcessHandler

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

try:  # 插件加载器两种导入方式都要兼容
    from .i18n import resolve_lang as _resolve_lang, t as _t
except ImportError:  # pragma: no cover - 顶层模块加载
    try:
        from i18n import resolve_lang as _resolve_lang, t as _t
    except ImportError:
        def _resolve_lang(request=None):  # type: ignore[no-redef]
            return "zh"

        def _t(text, lang="zh", **kwargs):  # type: ignore[no-redef]
            return text.format(**kwargs) if kwargs else text

import logging

logger = logging.getLogger(__name__)

#: 与客户端约定的契约版本。新增**能力**只追加 CAPABILITIES，不递增此值；
#: 只有既有端点形状发生不兼容变化时才递增，否则旧客户端会直接判为未检测到。
PROTOCOL_VERSION = 1

#: 本插件声明的能力。客户端按名字开关功能，不看版本号。
CAPABILITIES = (
    "info",
    "search",
    "sse",
    "portability",
    "push",
    "ssh",
    "pairing",
    "browser",
    "update",
)

#: 配对码的类型前缀。客户端与服务端各实现一份编解码，靠协议文档里的同一条样例串对齐。
PAIR_URI_SCHEME = "qwenpaw-node"
PAIR_URI_HOST = "pair"
PAIR_URI_VERSION = 1

#: 允许签发的有效期，单位天；0 表示长期（核心按 100 年处理）。
PAIRING_ALLOWED_DAYS = (7, 30, 90, 0)

#: 单次请求最多接受多少个地址。客户端表单通常只有一两条。
PAIRING_MAX_ADDRESSES = 8

#: 推送 channel 的唯一 key。不能与内置 channel 重名。
PUSH_CHANNEL = "qwenpaw_node_push"

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


# ---------------------------------------------------------------- portability

class _CreateImportJobRequest(BaseModel):
    """用户在来源页勾选的应用。"""

    sources: list[str] = Field(min_length=1, max_length=2)


class _StartImportJobRequest(BaseModel):
    """用户在清单页勾选的资产。"""

    selections: dict
    allow_plugin_execution: bool = False


def _api_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=409 if isinstance(exc, RuntimeError) else 400,
        detail=str(exc),
    )


async def _portability_workspace(request: Request):
    """解析当前请求对应的 agent。

    插件路由不在 `/api/agents/{id}/...` 下，`request.state.agent_id` 取不到；
    `get_agent_for_request` 会回落到 `X-Agent-Id` 头，客户端每个请求都带它。
    """
    from qwenpaw.app.agent_context import get_agent_for_request

    return await get_agent_for_request(request)


async def _require_qwenpaw_backend(workspace) -> None:
    from qwenpaw.config.config import load_agent_config_async

    if (await load_agent_config_async(workspace.agent_id)).backend != "qwenpaw":
        raise RuntimeError(
            "PawPort requires the destination Agent to use the qwenpaw backend",
        )


def _portability_router() -> APIRouter:
    """portability 导入转发。

    核心同名端点位于 `/api/agents/{id}/portability/imports/*`，由 `_local_only`
    限制为回环地址；手机经局域网访问会得到 403。插件在服务端进程内运行，
    直接调用任务管理器即可，不再复用核心 router —— 否则 `_local_only` 仍会拦截。

    只暴露 sources / jobs 两类路径，不做任意路径转发。
    """
    router = APIRouter(prefix="/portability")

    @router.get("/sources")
    async def import_sources(request: Request):
        from qwenpaw.portability.providers import (
            provider_names,
            resolve_source_location,
        )

        await _portability_workspace(request)
        result = []
        for source in provider_names():
            location = resolve_source_location(source)
            result.append(
                {
                    "source": source,
                    "name": source.title(),
                    "detected": bool(
                        location.data_home_exists
                        or location.user_data_home_exists
                        or location.runtime_path
                    ),
                },
            )
        return result

    @router.post("/jobs", status_code=202)
    async def create_import_job(
        body: _CreateImportJobRequest,
        request: Request,
    ):
        from qwenpaw.app.routers.portability_imports import (
            PORTABILITY_IMPORT_JOBS,
        )

        workspace = await _portability_workspace(request)
        try:
            await _require_qwenpaw_backend(workspace)
            return await PORTABILITY_IMPORT_JOBS.create(workspace, body.sources)
        except (ValueError, RuntimeError) as exc:
            raise _api_error(exc) from exc

    @router.get("/jobs/current")
    async def current_import_job(request: Request):
        from qwenpaw.app.routers.portability_imports import (
            PORTABILITY_IMPORT_JOBS,
        )

        workspace = await _portability_workspace(request)
        return await PORTABILITY_IMPORT_JOBS.current(workspace)

    @router.get("/jobs/{job_id}")
    async def get_import_job(job_id: str, request: Request):
        from qwenpaw.app.routers.portability_imports import (
            PORTABILITY_IMPORT_JOBS,
        )

        workspace = await _portability_workspace(request)
        try:
            return await PORTABILITY_IMPORT_JOBS.snapshot(workspace, job_id)
        except (ValueError, RuntimeError) as exc:
            raise _api_error(exc) from exc

    @router.post("/jobs/{job_id}/start", status_code=202)
    async def start_import_job(
        job_id: str,
        body: _StartImportJobRequest,
        request: Request,
    ):
        from qwenpaw.app.routers.portability_imports import (
            PORTABILITY_IMPORT_JOBS,
        )
        from qwenpaw.portability.models import ImportSelection

        workspace = await _portability_workspace(request)
        try:
            await _require_qwenpaw_backend(workspace)
            selections = {
                key: ImportSelection.model_validate(value)
                for key, value in body.selections.items()
            }
            if (
                any(selection.plugins for selection in selections.values())
                and not body.allow_plugin_execution
            ):
                raise ValueError(
                    "confirm plugin code execution before importing",
                )
            return await PORTABILITY_IMPORT_JOBS.start(
                workspace,
                job_id,
                selections,
            )
        except (ValueError, RuntimeError) as exc:
            raise _api_error(exc) from exc

    @router.post("/jobs/{job_id}/retry", status_code=202)
    async def retry_import_job(
        job_id: str,
        body: _StartImportJobRequest,
        request: Request,
    ):
        from qwenpaw.app.routers.portability_imports import (
            PORTABILITY_IMPORT_JOBS,
        )
        from qwenpaw.portability.models import ImportSelection

        workspace = await _portability_workspace(request)
        try:
            await _require_qwenpaw_backend(workspace)
            selections = {
                key: ImportSelection.model_validate(value)
                for key, value in body.selections.items()
            }
            if (
                any(selection.plugins for selection in selections.values())
                and not body.allow_plugin_execution
            ):
                raise ValueError(
                    "confirm plugin code execution before importing",
                )
            return await PORTABILITY_IMPORT_JOBS.retry(
                workspace,
                job_id,
                selections,
            )
        except (ValueError, RuntimeError) as exc:
            raise _api_error(exc) from exc

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_import_job(job_id: str, request: Request):
        from qwenpaw.app.routers.portability_imports import (
            PORTABILITY_IMPORT_JOBS,
        )

        workspace = await _portability_workspace(request)
        try:
            return await PORTABILITY_IMPORT_JOBS.cancel(workspace, job_id)
        except (ValueError, RuntimeError) as exc:
            raise _api_error(exc) from exc

    return router


class NodeBridgePushChannel(BaseChannel):
    """把收件箱与审批事件推送到 ntfy / Bark / Telegram。

    这是本插件唯一能绕开「App 进程被杀就失联」的路径：事件源与 `/events`
    相同，发送发生在服务端，不依赖手机常驻。
    """

    channel = PUSH_CHANNEL
    # 不需要核心为它建消息队列：本 channel 只往外推，不接收用户消息。
    uses_manager_queue = False

    def __init__(
        self,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
        display_config=None,
        no_text_debounce: bool = True,
    ) -> None:
        super().__init__(
            process,
            on_reply_sent,
            display_config=display_config,
            no_text_debounce=no_text_debounce,
        )
        self._config = None
        self._task: asyncio.Task | None = None
        self._last_push_at = 0.0

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config,
        on_reply_sent: OnReplySent = None,
        display_config=None,
        no_text_debounce: bool = True,
    ) -> "NodeBridgePushChannel":
        instance = cls(
            process,
            on_reply_sent,
            display_config=display_config,
            no_text_debounce=no_text_debounce,
        )
        instance._config = config
        return instance

    def _get(self, name: str, default=""):
        return getattr(self._config, name, default) if self._config is not None else default

    @property
    def _provider(self) -> str:
        return str(self._get("provider", "ntfy") or "ntfy").strip().lower()

    @property
    def _target(self) -> str:
        return str(self._get("target", "") or "").strip()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self._target:
            logger.warning("推送 channel 未配置目标，跳过启动")
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("推送 channel 已停止")

    async def send(self, to_handle: str, text: str, meta=None) -> None:
        del to_handle, meta
        await self._push("QwenPaw", text)

    async def _poll_loop(self) -> None:
        """连接内的轮询：只报「数值上升」，启动时先取一次基线。"""
        last = await _snapshot()
        interval = max(2.5, float(self._get("min_interval_seconds", 30) or 30))
        while True:
            await asyncio.sleep(5.0)
            current = await _snapshot()
            lines = []
            if bool(self._get("notify_inbox", True)) and current["inbox"] > last["inbox"]:
                lines.append(f"收件箱有 {current['inbox']} 条未读")
            if bool(self._get("notify_approval", True)) and current["approval"] > last["approval"]:
                lines.append(f"待审批 {current['approval']} 条")
            if lines:
                now = time.monotonic()
                if now - self._last_push_at < interval:
                    # 限流期间不推进基线：下一次 tick 仍会看到这次变化。
                    continue
                title = "QwenPaw 提醒"
                text = "；".join(lines)
                await self._push(title, text)
                self._last_push_at = now
            last = current

    async def _push(self, title: str, text: str) -> None:
        provider = self._provider
        target = self._target
        token = str(self._get("token", "") or "").strip()
        server = str(self._get("server_url", "") or "").strip().rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if provider == "ntfy":
                    base = server or "https://ntfy.sh"
                    headers = {}
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                    response = await client.post(
                        f"{base}/{quote(target, safe='')}",
                        content=f"{title}\n{text}".encode("utf-8"),
                        headers=headers,
                    )
                    response.raise_for_status()
                elif provider == "bark":
                    base = server or "https://api.day.app"
                    response = await client.post(
                        f"{base}/push",
                        json={"device_key": target, "title": title, "body": text},
                    )
                    response.raise_for_status()
                elif provider == "telegram":
                    if not token:
                        logger.warning("Telegram 推送缺少 bot token，跳过")
                        return
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": target, "text": f"{title}\n{text}"},
                    )
                    response.raise_for_status()
                else:
                    logger.warning("未知的推送服务 %s，跳过", provider)
        except Exception as exc:  # pylint: disable=broad-except
            # 不打印 URL 与 token：Telegram 的 URL 里带 token。
            logger.warning("推送失败 provider=%s error=%s", provider, type(exc).__name__)


# ------------------------------------------------------------------- ssh

#: 同时允许的 SSH 中继连接数。SSH 只服务一个用户，4 条足够覆盖多窗口。
SSH_MAX_CONNECTIONS = 4

#: 私钥文件上限。正常的 OpenSSH 私钥只有几 KB，超过这个值说明配错了路径。
SSH_KEY_MAX_BYTES = 256 * 1024

#: 当前活动的 SSH 中继数。只在事件循环内增减，不需要额外加锁。
_ssh_active_connections = 0


def _data_dir() -> Path:
    """插件自己的数据目录。

    不放在插件源码目录：重装或升级插件时会整目录替换。``QWENPAW_NODE_BRIDGE_DATA_DIR``
    只给测试用，生产环境固定在 ``~/.qwenpaw/plugin_data/qwenpaw-node-bridge``。
    """
    override = os.environ.get("QWENPAW_NODE_BRIDGE_DATA_DIR")
    if override:
        root = Path(override).expanduser()
    else:
        from qwenpaw.constant import WORKING_DIR

        root = Path(WORKING_DIR) / "plugin_data" / "qwenpaw-node-bridge"
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


#: 插件日志文件。设置变更、SSH 连接与修复动作都写到这里，供设置页查看。
PLUGIN_LOG_MAX_BYTES = 512 * 1024
PLUGIN_LOG_BACKUPS = 2
_plugin_log_handler: logging.Handler | None = None


def _plugin_log_path() -> Path:
    return _data_dir() / "plugin.log"


def _ensure_file_logging() -> None:
    """把插件自己的日志写进数据目录，供设置页读取。

    只挂在插件 logger 上，不碰根 logger；重复加载时复用已有 handler，
    避免同一条记录写两遍。
    """
    global _plugin_log_handler  # pylint: disable=global-statement
    if _plugin_log_handler is not None:
        return
    path = _plugin_log_path()
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=PLUGIN_LOG_MAX_BYTES,
        backupCount=PLUGIN_LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _plugin_log_handler = handler


def _tail_plugin_log(limit: int) -> tuple[list[str], bool]:
    path = _plugin_log_path()
    if not path.is_file():
        return [], False
    return _tail_log_lines(path, limit)


def _require_logs_access(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if not _ssh_access_allowed(
        token=_http_token(request),
        client_host=client_host,
        headers=request.headers,
    ):
        raise HTTPException(401, "Plugin logs require authentication")


def _ssh_settings_path() -> Path:
    return _data_dir() / "ssh_settings.json"


def _ssh_key_path() -> Path:
    return _data_dir() / "ssh_key"


def _load_ssh_settings() -> dict:
    try:
        data = json.loads(_ssh_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """先以目标权限写临时文件，再原子替换。

    旧写法先写后 chmod，窗口期内是 0644；凭据文件不能有这个窗口。
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _save_ssh_settings(data: dict) -> None:
    _atomic_write_text(
        _ssh_settings_path(),
        json.dumps(data, ensure_ascii=False, indent=2),
    )


def _write_inline_key(key: str) -> None:
    _atomic_write_text(_ssh_key_path(), key)


def _read_inline_key() -> str | None:
    try:
        return _ssh_key_path().read_text(encoding="utf-8")
    except OSError:
        return None


def _delete_inline_key() -> None:
    try:
        _ssh_key_path().unlink()
    except OSError:
        pass


def _masked_ssh_settings() -> dict:
    """给界面看的设置。私钥内容、口令与登录密码永不回传。"""
    data = _load_ssh_settings()
    auth = str(data.get("auth", "key")).lower()
    key_source = str(data.get("key_source", "file")).lower()
    key_path = str(data.get("key_path", ""))
    if auth == "key":
        if key_source == "inline":
            key_configured = _read_inline_key() is not None
        else:
            key_configured = bool(key_path) and Path(key_path).expanduser().is_file()
    else:
        key_configured = False
    return {
        "host": str(data.get("host", "127.0.0.1")),
        "port": int(data.get("port", 22) or 22),
        "user": str(data.get("user", "")),
        "auth": auth,
        "key_source": key_source,
        "key_path": key_path,
        "key_configured": key_configured,
        "password_configured": bool(str(data.get("password", ""))),
        "passphrase_configured": bool(str(data.get("passphrase", ""))),
        "configured": bool(str(data.get("user", "")).strip()),
    }


def _validate_ssh_settings(body: dict, current: dict) -> dict:
    """校验并合并设置；空白密钥字段表示保留原值。"""
    host = str(body.get("host", current.get("host", "127.0.0.1"))).strip()
    if not host:
        raise ValueError("主机地址不能为空")
    try:
        port = int(body.get("port", current.get("port", 22)))
    except (TypeError, ValueError):
        raise ValueError("端口必须是整数") from None
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    user = str(body.get("user", current.get("user", ""))).strip()
    if not user:
        raise ValueError("用户名不能为空")
    auth = str(body.get("auth", current.get("auth", "key"))).strip().lower()
    if auth not in ("key", "password"):
        raise ValueError("认证方式只能是密钥或密码")
    key_source = str(
        body.get("key_source", current.get("key_source", "file")),
    ).strip().lower()
    if key_source not in ("file", "inline"):
        raise ValueError("私钥来源只能是服务端路径或粘贴私钥")
    key_path = str(body.get("key_path", current.get("key_path", ""))).strip()
    if auth == "key" and key_source == "file":
        if not key_path:
            raise ValueError("密钥认证需要填写服务端私钥路径")
        if not Path(key_path).expanduser().is_file():
            raise ValueError("私钥路径不存在或不是文件")

    next_data = dict(current)
    next_data.update(
        {
            "host": host,
            "port": port,
            "user": user,
            "auth": auth,
            "key_source": key_source,
            "key_path": key_path,
        },
    )

    private_key = body.get("private_key")
    if isinstance(private_key, str) and private_key.strip():
        if auth != "key" or key_source != "inline":
            raise ValueError("只有「密钥 + 粘贴私钥」方式才需要私钥内容")
        next_data["_new_inline_key"] = private_key.strip() + "\n"
    password = body.get("password")
    if isinstance(password, str) and password:
        next_data["password"] = password
    passphrase = body.get("passphrase")
    if isinstance(passphrase, str) and passphrase:
        next_data["passphrase"] = passphrase

    if auth == "password" and not str(next_data.get("password", "")):
        raise ValueError("密码认证需要填写登录密码")
    if (
        auth == "key"
        and key_source == "inline"
        and "_new_inline_key" not in next_data
        and _read_inline_key() is None
    ):
        raise ValueError("粘贴私钥方式需要填写私钥内容")
    return next_data


def _apply_ssh_settings(body: dict) -> dict:
    current = _load_ssh_settings()
    data = _validate_ssh_settings(body, current)
    new_key = data.pop("_new_inline_key", None)
    if new_key is not None:
        _write_inline_key(new_key)
    if data.get("auth") != "key" or data.get("key_source") != "inline":
        # 不再使用粘贴的私钥时删除它，避免多留一份明文。
        _delete_inline_key()
    _save_ssh_settings(data)
    logger.info(
        "SSH settings updated host=%s port=%s user=%s auth=%s",
        data.get("host", ""),
        data.get("port", 22),
        data.get("user", ""),
        data.get("auth", ""),
    )
    return _masked_ssh_settings()


def _ssh_auth_enabled() -> bool:
    try:
        from qwenpaw.app.auth import is_auth_enabled

        return bool(is_auth_enabled())
    except Exception:  # pylint: disable=broad-except
        return False


def _ssh_token_user(token: str | None) -> str | None:
    if not token:
        return None
    try:
        from qwenpaw.app.auth import (
            has_registered_users,
            is_auth_enabled,
            verify_token,
        )

        if not is_auth_enabled() or not has_registered_users():
            return None
        return verify_token(token)
    except Exception:  # pylint: disable=broad-except
        return None


def _ws_token(websocket: WebSocket) -> str | None:
    authorization = websocket.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    token = websocket.query_params.get("token")
    return token.strip() if token else None


def _http_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    token = request.query_params.get("token")
    return token.strip() if token else None


def _trusted_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """额外视为私有的来源网段；用于组网工具分配非标准地址段。"""
    raw = os.environ.get("QWENPAW_BRIDGE_TRUSTED_CIDRS", "")
    networks = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            logger.warning("忽略无效的 QWENPAW_BRIDGE_TRUSTED_CIDRS 项: %s", value)
    return tuple(networks)


def _proxy_enabled() -> bool:
    return os.environ.get("QWENPAW_BRIDGE_TRUST_PROXY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _has_forwarded_headers(headers) -> bool:
    return any(
        headers.get(key)
        for key in ("x-forwarded-for", "x-real-ip", "x-forwarded-host")
    )


def _is_private_client(host: str | None) -> bool:
    """回环、私有、链路本地、CGNAT 与额外组网网段。"""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_private or address.is_loopback or address.is_link_local:
        return True
    if address in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return any(address in network for network in _trusted_networks())


def _normalize_host(raw: str) -> str | None:
    value = (raw or "").strip().lower()
    if not value:
        return None
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        value = value[1:end]
    else:
        value = value.split(":", 1)[0]
    value = value.rstrip(".")
    return value or None


def _forwarded_client(peer: str | None, headers) -> str | None:
    """只在显式信任反代、且直连对端本身是私有/回环时采信转发头。"""
    if not _proxy_enabled() or not _is_private_client(peer):
        return None
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            # 只取最右一跳：它是可信反代追加的，左侧可由客户端伪造。
            return parts[-1]
    real_ip = headers.get("x-real-ip", "").strip()
    return real_ip or None


def _request_host(headers) -> str | None:
    host = headers.get("host", "")
    if _proxy_enabled():
        forwarded = headers.get("x-forwarded-host", "")
        if forwarded:
            host = forwarded.split(",")[-1].strip()
    return _normalize_host(host)


def _trusted_addresses_path() -> Path:
    return _data_dir() / "trusted_addresses.json"


def _load_trusted_addresses() -> list[dict]:
    try:
        data = json.loads(_trusted_addresses_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        host = _normalize_host(str(item.get("host", "")))
        source = str(item.get("source_cidr", "") or "").strip() or None
        if host:
            result.append({"host": host, "source_cidr": source})
    return result


def _save_trusted_addresses(items: list[dict]) -> None:
    _atomic_write_text(
        _trusted_addresses_path(),
        json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2),
    )


def _validate_trusted_addresses(body: dict) -> list[dict]:
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ValueError("items 必须是数组")
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("每一项必须是对象")
        host = _normalize_host(str(item.get("host", "")))
        if not host:
            raise ValueError("地址不能为空")
        if host in seen:
            raise ValueError(f"地址重复: {host}")
        seen.add(host)
        source = str(item.get("source_cidr", "") or "").strip()
        if source:
            try:
                ipaddress.ip_network(source, strict=False)
            except ValueError as exc:
                raise ValueError(f"来源网段无效: {source}") from exc
        result.append({"host": host, "source_cidr": source or None})
    return result


def _trusted_address_matches(host: str | None, client_ip: str | None) -> bool:
    """Host 命中条目且来源 IP 命中条目网段。

    未写 source_cidr 的条目不对公网来源生效；私有/组网来源在调用方已直接放行。
    """
    normalized = _normalize_host(host or "")
    if not normalized or not client_ip:
        return False
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    for item in _load_trusted_addresses():
        if item["host"] != normalized or not item.get("source_cidr"):
            continue
        try:
            if address in ipaddress.ip_network(item["source_cidr"], strict=False):
                return True
        except ValueError:
            continue
    return False


def _sensitive_access_allowed(
    *,
    token: str | None,
    client_host: str | None,
    headers,
) -> bool:
    """敏感端点的统一访问策略。

    开启 Web 认证时只认有效 token；未开启时放行以下任一种：
    1. 来源是私有/回环/组网网段；
    2. 请求 Host 命中 Bridge 受信任地址，且来源 IP 命中该条目的网段。
    反代转发头未显式开启 TRUST_PROXY 时按不确定处理，不因回环而对公网放行。
    """
    if _ssh_auth_enabled():
        return bool(_ssh_token_user(token))
    forwarded = _forwarded_client(client_host, headers)
    effective = forwarded or client_host
    proxy_unknown = _has_forwarded_headers(headers) and not _proxy_enabled()
    if not proxy_unknown and _is_private_client(effective):
        return True
    return _trusted_address_matches(_request_host(headers), effective)


def _ssh_access_allowed(
    *,
    token: str | None,
    client_host: str | None,
    headers,
) -> bool:
    return _sensitive_access_allowed(
        token=token,
        client_host=client_host,
        headers=headers,
    )


def _require_ssh_access(request: Request) -> None:
    """插件的 HTTP 敏感端点自己做一次访问校验。

    不能只依赖核心的 ``AuthMiddleware``：实测在服务端热切换鉴权开关后，带空
    ``Authorization: Bearer`` 的请求仍能到达插件路由。因此这里同时检查鉴权开关、
    用户集合与客户端来源。
    """
    client_host = request.client.host if request.client else None
    if not _ssh_access_allowed(
        token=_http_token(request),
        client_host=client_host,
        headers=request.headers,
    ):
        lang = _resolve_lang(request)
        if not _ssh_auth_enabled():
            # 给客户端一个可识别的 code，便于提示「去 Bridge 设置页添加受信任地址」。
            raise HTTPException(
                401,
                {
                    "code": "trusted_address_required",
                    "message": _t(
                        "来源地址不在私有网段；请开启登录认证，"
                        "或在 Bridge 设置页添加受信任地址并绑定来源网段",
                        lang,
                    ),
                },
            )
        raise HTTPException(401, _t("SSH 需要登录认证", lang))


def _require_browser_access(request: Request) -> None:
    """浏览器端点的访问校验，策略与 SSH、pairing 相同。

    修复端点会驱动服务端安装扩展资产与 NM host，属于写操作；日志端点可能含有
    地址与内部标识。两者都不能依赖核心中间件：鉴权开关热切换时它会整段跳过。
    开了 Web 登录时只认有效 token，没开时只放行私有/回环地址或受信任标记。
    """
    client_host = request.client.host if request.client else None
    if not _ssh_access_allowed(
        token=_http_token(request),
        client_host=client_host,
        headers=request.headers,
    ):
        raise HTTPException(401, "Browser endpoints require authentication")


# ---------------------------------------------------------------------------
# 设备配对：签发与登记移动端访问令牌
# ---------------------------------------------------------------------------


def _pairing_path() -> Path:
    return _data_dir() / "pairing_tokens.json"


def _load_pairing() -> dict:
    """读登记文件。**只存元数据，不存令牌原文。**

    结构 ``{"version": 1, "items": {jti: {label, username, issued_at, expires_at, revoked}}}``。
    读取时顺手清掉已过期的条目，避免文件无限增长；已吊销的过期条目也算过期。
    """
    try:
        data = json.loads(_pairing_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "items": {}}
    if not isinstance(data, dict):
        return {"version": 1, "items": {}}
    items = data.get("items")
    if not isinstance(items, dict):
        items = {}
    now = int(time.time())
    kept = {
        jti: item
        for jti, item in items.items()
        if isinstance(item, dict) and int(item.get("expires_at", 0) or 0) > now
    }
    data["version"] = 1
    data["items"] = kept
    return data


def _save_pairing(data: dict) -> None:
    _atomic_write_text(
        _pairing_path(),
        json.dumps(data, ensure_ascii=False, indent=2),
    )


def _pair_payload(addresses: list[str], token: str) -> str:
    """拼配对码。格式与客户端 `PairQr` 一一对应，改一处必须同步另一处。"""
    parts = [f"v={PAIR_URI_VERSION}"]
    for address in addresses:
        parts.append("addr=" + quote(address, safe=""))
    parts.append("token=" + quote(token, safe=""))
    return f"{PAIR_URI_SCHEME}://{PAIR_URI_HOST}?" + "&".join(parts)


def _pair_qrcode_image(payload: str) -> str:
    """base64 PNG。用 ``segno``，不 import 核心的 qrcode_auth_handler。

    那个模块在顶层 import ``cryptography``，只为扫码失败时就把它整包拉起来不划算。
    """
    import base64
    import io

    import segno

    code = segno.make(payload, error="M")
    buf = io.BytesIO()
    code.save(buf, kind="png", scale=6, border=2)
    return base64.b64encode(buf.getvalue()).decode()


def _pairing_auth_enabled() -> bool:
    return _ssh_auth_enabled()


def _require_pairing_access(request: Request) -> str:
    """配对端点的访问校验，返回调用者用户名。

    三种放行途径，按可信度从高到低：
    1. 核心中间件验过 token 并写入 ``request.state.user``；
    2. 自行校验请求里的 token（中间件在鉴权热切换时会整段跳过）；
    3. 服务端未启用登录时，放行私有/回环来源，用户名记为 ``default``——
       这时令牌不会被执行链校验，签发端会另行返回 409。
    """
    state_user = getattr(request.state, "user", None)
    if state_user:
        return str(state_user)
    user = _ssh_token_user(_http_token(request))
    if user:
        return user
    if not _pairing_auth_enabled():
        client_host = request.client.host if request.client else None
        if _sensitive_access_allowed(
            token=_http_token(request),
            client_host=client_host,
            headers=request.headers,
        ):
            return "default"
    raise HTTPException(401, "Pairing requires authentication")


def _core_revoke_jti(jti: str, exp: int) -> bool:
    """按 jti 吊销。

    核心只公开 ``revoke_token(token)``，而它需要令牌原文；把原文存进插件数据目录
    等于多一处凭据存储。改为直接用核心吊销表内部使用的 ``_add_to_revocation_list``，
    ``verify_token`` 会用同一个 ``_is_token_revoked`` 检查。

    这是私有符号，升级核心可能改名，因此失败返回 False，由调用方降级为
    「已从列表移除，令牌将在到期时间失效」。
    """
    try:
        from qwenpaw.app.auth import _add_to_revocation_list

        _add_to_revocation_list(jti, int(exp))
        return True
    except Exception:  # pylint: disable=broad-except
        logger.warning("core does not support revoking by jti; token expires naturally")
        return False


def _long_expiry_epoch() -> int:
    """「长期」令牌的固定到期时刻。

    彩蛋，不写进对外文档。核心的 ``create_token`` 会把有效期截到 100 年，
    到不了这个时刻，因此这条路自己按同样的载荷格式签一枚。
    """
    from datetime import datetime

    return int(datetime(2143, 9, 21, 10, 43).timestamp())


def _create_long_token(username: str) -> str:
    """按核心的令牌格式签发一枚固定到期日的令牌。

    格式与 ``qwenpaw.app.auth.create_token`` 一致：``base64url(payload).hmac_sha256``。
    核心升级若改了签名格式，这里会验签失败，届时回落到核心的 100 年令牌。
    """
    import base64
    import hashlib
    import hmac
    import secrets

    try:
        from qwenpaw.app.auth import _get_jwt_secret

        now = int(time.time())
        exp = _long_expiry_epoch()
        payload = json.dumps(
            {
                "sub": username,
                "exp": exp,
                "iat": now,
                "jti": secrets.token_hex(16),
            },
        )
        payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
        sig = hmac.new(
            _get_jwt_secret().encode(),
            payload_b64.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{payload_b64}.{sig}"
    except Exception:  # pylint: disable=broad-except
        from qwenpaw.app.auth import create_token

        return create_token(username, 0)


def _token_claims(token: str) -> tuple[int, str]:
    """从令牌里读出 ``exp`` 与 ``jti``。

    只解析载荷，不验签：令牌刚由 ``create_token`` 生成，验签由客户端与服务端各自做。
    解析失败返回 ``(0, "")``，调用方据此拒绝签发。
    """
    import base64

    try:
        payload_b64 = token.split(".", 1)[0]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp", 0) or 0), str(payload.get("jti", "") or "")
    except Exception:  # pylint: disable=broad-except
        return 0, ""


class PairingEditError(Exception):
    """登记条目的改名 / 删除校验失败。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _rename_pairing_item(data: dict, jti: str, label: str) -> str:
    """改登记条目的标签。只改本地元数据，令牌本身不受影响。"""
    text = label.strip()[:64]
    if not text:
        raise PairingEditError(400, "标签不能为空")
    item = data["items"].get(jti)
    if not item:
        raise PairingEditError(404, "未找到该令牌")
    if item.get("revoked"):
        # 已吊销的只能删除，避免把作废条目改成看起来仍有效的名字。
        raise PairingEditError(409, "已吊销的令牌不能改名")
    item["label"] = text
    return text


def _delete_pairing_item(data: dict, jti: str) -> None:
    """删除登记条目。只允许删除已吊销的令牌。"""
    item = data["items"].get(jti)
    if not item:
        raise PairingEditError(404, "未找到该令牌")
    if not item.get("revoked"):
        raise PairingEditError(409, "请先吊销再删除")
    del data["items"][jti]


def _pairing_item_view(item: dict) -> dict:
    """登记条目对外的形状。**不含令牌原文**。"""
    return {
        "label": str(item.get("label", "")),
        "username": str(item.get("username", "")),
        "issued_at": int(item.get("issued_at", 0) or 0),
        "expires_at": int(item.get("expires_at", 0) or 0),
        "revoked": bool(item.get("revoked", False)),
    }


def _validate_pairing_request(body: dict) -> dict:
    """校验签发请求。"""
    try:
        days = int(body.get("expires_in_days", 30))
    except (TypeError, ValueError):
        raise ValueError("有效期必须是整数天") from None
    if days not in PAIRING_ALLOWED_DAYS:
        allowed = "、".join(str(d) for d in PAIRING_ALLOWED_DAYS if d)
        raise ValueError(f"有效期只能是 {allowed} 天或 0（长期）")

    raw_addresses = body.get("addresses")
    if not isinstance(raw_addresses, list):
        raise ValueError("addresses 必须是数组")
    addresses = []
    for entry in raw_addresses:
        text = str(entry or "").strip()
        if not text:
            continue
        if not text.startswith(("http://", "https://")):
            raise ValueError("地址必须以 http:// 或 https:// 开头")
        addresses.append(text.rstrip("/"))
    if not addresses:
        raise ValueError("至少需要一个服务器地址")
    if len(addresses) > PAIRING_MAX_ADDRESSES:
        raise ValueError(f"地址最多 {PAIRING_MAX_ADDRESSES} 条")

    label = str(body.get("label", "")).strip()[:64]
    return {"days": days, "addresses": addresses, "label": label}


def _ssh_credential() -> dict:
    """按需组装凭据。调用方必须先确认访问策略已放行。

    这个返回值含私钥或登录密码，**不能写进任何日志**，也不能放进错误 detail。
    """
    data = _load_ssh_settings()
    if not str(data.get("user", "")).strip():
        raise HTTPException(404, "SSH 目标尚未配置")
    auth = str(data.get("auth", "key")).lower()
    if auth == "password":
        password = str(data.get("password", ""))
        if not password:
            raise HTTPException(409, "SSH 登录密码尚未配置")
        return {"auth": "password", "password": password}

    key_source = str(data.get("key_source", "file")).lower()
    if key_source == "inline":
        private_key = _read_inline_key()
    else:
        key_path = Path(str(data.get("key_path", ""))).expanduser()
        if not key_path.is_file():
            raise HTTPException(409, "SSH 私钥文件不可用")
        if key_path.stat().st_size > SSH_KEY_MAX_BYTES:
            raise HTTPException(413, "SSH 私钥文件过大")
        try:
            private_key = key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(409, "SSH 私钥文件不可读") from exc
    if not private_key:
        raise HTTPException(409, "SSH 私钥尚未配置")
    return {
        "auth": "key",
        "private_key": private_key,
        "passphrase": str(data.get("passphrase", "")),
    }


# ---------------------------------------------------------------------------
# 浏览器控制链路：NM host 日志与修复转发
# ---------------------------------------------------------------------------

#: 客户端不传 lines 时返回多少行。
BROWSER_LOG_DEFAULT_LINES = 200

#: 单次最多返回多少行。日志可能很大，这里只截尾部。
BROWSER_LOG_MAX_LINES = 2000

#: 从文件尾部最多读多少字节。2000 行正常日志远小于这个值；设上限是为了避免
#: 一份几百 MB 的日志把内存吃满。
BROWSER_LOG_MAX_BYTES = 512 * 1024

#: 日志相对 ``WORKING_DIR`` 的位置，由官方 NM host 写入。
BROWSER_LOG_RELATIVE = ("logs", "nm-host.log")

#: 官方 chrome 插件的 id，用于判断「插件是否已安装」与从目录取安装地址。
CHROME_PLUGIN_ID = "chrome"

#: 修复转发的目标。首选核心文档与插件源码确认的 ``/api/chrome/setup``；
#: 插件路由由 ``register_http_router(prefix)`` 挂在 ``/api`` + prefix 下，
#: 若该插件把 prefix 写成 ``/plugins/chrome``，则实际路径是后者，故留一个回退。
CHROME_SETUP_PATHS = ("/api/chrome/setup", "/api/plugins/chrome/setup")

#: 转发体。``unpacked`` 表示就地加载扩展目录，不写入 Chrome 商店扩展。
CHROME_SETUP_BODY = {"install_mode": "unpacked", "reset": False}

CHROME_PLUGINS_PATH = "/api/plugins"
CHROME_CATALOG_PATH = "/api/plugins/catalog"

#: ``POST /browser/repair`` 只接受的动作。其余一律 400。
BROWSER_REPAIR_ACTIONS = ("setup",)

#: 插件与核心同进程，走回环即可；端口读不到时才回落到核心的默认值。
DEFAULT_API_PORT = 8088

#: 自请求超时。setup 要落盘扩展资产与 NM host，比普通读接口慢。
SELF_REQUEST_TIMEOUT = 20.0

#: 脱敏替换结果。要求可逆性为零：原文一旦匹配就整体丢弃。
REDACTED = "***"

#: JSON 里以引号包裹的敏感键。放在最前：先整体吃掉值，后面的规则才不会被引号打断。
_REDACT_JSON = re.compile(
    r"(?i)(\"(?:authorization|proxy-authorization|cookie|set-cookie|token|"
    r"access_token|refresh_token|api_key|apikey|x-api-key|secret|password|passphrase)\""
    r"\s*:\s*)\"(?:[^\"\\]|\\.)*\"",
)

#: Cookie 头值含多个 ``k=v``，只遮第一个会漏，整段遮到行尾。
_REDACT_COOKIE_HEADER = re.compile(
    r"(?i)\b(set-cookie|cookie)\s*:\s*[^\r\n]*",
)

#: 只遮令牌本身，保留行内的其余字段（状态码、路径）便于排查。
_REDACT_AUTH_HEADER = re.compile(
    r"(?i)\b(proxy-authorization|authorization|x-api-key)\s*:\s*(?:bearer\s+)?[^\s,;]+",
)

#: 裸 ``Bearer`` / ``Basic`` 凭据，例如写在 URL、字典 repr 或错误文本里。
_REDACT_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]{8,}=*")

#: 键值对形式：``token=``、``api_key=``、``secret=`` 等。
_REDACT_PAIR = re.compile(
    r"(?i)\b(access_token|refresh_token|api_key|apikey|token|secret|password|passphrase)"
    r"\s*=\s*[^\s&,;\"']+",
)


def _working_dir() -> Path:
    """服务端工作目录。日志写在它下面，而不是 ``Path.home()``。

    ``WORKING_DIR`` 会跟随 ``QWENPAW_WORKING_DIR`` 或历史安装目录 ``~/.copaw``，
    直接拼 ``~/.qwenpaw`` 在这两种部署里会读错文件。
    """
    try:
        from qwenpaw.constant import WORKING_DIR

        return Path(WORKING_DIR)
    except Exception:  # pylint: disable=broad-except
        return Path.home() / ".qwenpaw"


def _nm_host_log_path() -> Path:
    return _working_dir().joinpath(*BROWSER_LOG_RELATIVE)


def _sanitize_log_line(line: str) -> str:
    """按行脱敏。**只用于返回值，不用于日志**。

    覆盖 Authorization / Bearer / Cookie / Set-Cookie / token= / api_key= /
    X-Api-Key / secret= 这八类；整行返回，不做截断，客户端按原样显示。
    """
    text = _REDACT_JSON.sub(r'\1"' + REDACTED + '"', line)
    text = _REDACT_COOKIE_HEADER.sub(r"\1: " + REDACTED, text)
    text = _REDACT_AUTH_HEADER.sub(r"\1: " + REDACTED, text)
    text = _REDACT_BEARER.sub(r"\1 " + REDACTED, text)
    text = _REDACT_PAIR.sub(r"\1=" + REDACTED, text)
    return text


def _tail_log_lines(path: Path, limit: int) -> tuple[list[str], bool]:
    """读文件末尾最多 ``limit`` 行，返回 ``(行, 是否被截断)``。

    只从尾部读 ``BROWSER_LOG_MAX_BYTES`` 字节：按行遍历整份日志在日志轮转
    之前会一次吃掉几百 MB。从中间开始时首行可能是半行，丢弃。
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        read_size = min(size, BROWSER_LOG_MAX_BYTES)
        handle.seek(size - read_size)
        raw = handle.read(read_size)

    truncated = read_size < size
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if truncated and lines:
        lines = lines[1:]
    if len(lines) > limit:
        lines = lines[-limit:]
        truncated = True
    return lines, truncated


def _read_nm_host_log(limit: int) -> dict:
    """返回给客户端的日志形状。文件不存在时抛 ``FileNotFoundError``。"""
    path = _nm_host_log_path()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    lines, truncated = _tail_log_lines(path, limit)
    return {
        "lines": [_sanitize_log_line(line) for line in lines],
        "truncated": truncated,
        "path": str(path),
    }


def _server_api_base() -> str:
    """本机核心 API 的基地址，主机固定回环。

    端口优先取核心进程内的 ``read_last_api()``：CLI 与桌面壳启动时都会
    ``write_last_api``，与 ``/api/ws/chrome`` 的解析方式一致。取不到时再读环境
    变量，最后才回落到 8088。主机不用 ``read_last_api`` 里的绑定地址：那是
    ``0.0.0.0`` 或某块网卡，走回环既确定又不需要经过网卡。
    """
    port = None
    try:
        from qwenpaw.config.utils import read_last_api

        info = read_last_api()
        if info:
            port = int(info[1])
    except Exception:  # pylint: disable=broad-except
        port = None
    if port is None or not 0 < port < 65536:
        port = None
        for name in ("QWENPAW_API_PORT", "QWENPAW_PORT"):
            raw = os.environ.get(name, "").strip()
            if not raw:
                continue
            try:
                candidate = int(raw)
            except ValueError:
                continue
            if 0 < candidate < 65536:
                port = candidate
                break
    if port is None:
        port = DEFAULT_API_PORT
    return f"http://127.0.0.1:{port}"


def _self_headers(request: Request) -> dict:
    """自请求的头部。

    同一个 token 原样转发给 chrome 插件的路由：核心的 ``AuthMiddleware`` 会再验一次，
    换成一个内部凭据等于绕过调用方的权限。未开鉴权时改发受信任标记，与
    ``X-QwenPaw-Trusted-Host`` 的既有语义一致——该标记由客户端显式确认后携带。
    """
    headers = {"accept": "application/json"}
    token = _http_token(request)
    if token:
        headers["authorization"] = f"Bearer {token}"
    agent_id = str(request.headers.get("x-agent-id", "")).strip()
    if agent_id:
        headers["x-agent-id"] = agent_id
    return headers


async def _self_request(
    method: str,
    path: str,
    *,
    headers: dict | None = None,
    json_body: dict | None = None,
) -> httpx.Response:
    """向本机核心发一次请求。

    ``trust_env=False``：环境里的 ``HTTP_PROXY`` 会让回环请求绕到代理，
    表现为「端口通但读不到插件路由」。
    """
    async with httpx.AsyncClient(
        timeout=SELF_REQUEST_TIMEOUT,
        trust_env=False,
    ) as client:
        return await client.request(
            method,
            _server_api_base() + path,
            headers=headers or {},
            json=json_body,
        )


def _version_key(version: str) -> tuple:
    """把 ``0.1.1`` 变成可比较的元组；非数字段直接忽略。"""
    return tuple(int(part) for part in re.findall(r"\d+", version or "")[:4])


async def _chrome_plugin_listed(headers: dict) -> bool | None:
    """``/api/plugins`` 里是否有 id 为 ``chrome`` 的插件。

    返回 ``None`` 表示问不出来（核心异常或非 200），调用方不能据此断言未安装。
    """
    try:
        response = await _self_request("GET", CHROME_PLUGINS_PATH, headers=headers)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    entries = payload if isinstance(payload, list) else payload.get("plugins", [])
    if not isinstance(entries, list):
        return None
    return any(
        isinstance(item, dict) and str(item.get("id", "")).strip() == CHROME_PLUGIN_ID
        for item in entries
    )


async def _chrome_install_url(headers: dict) -> str:
    """从插件目录取 chrome 的安装地址，取不到返回空串。

    目录里可能同时有多个版本（实测 0.1.0 与 0.1.1），取版本号最大的那条；
    不编造地址，也不回落到 GitHub 或官网链接。
    """
    try:
        response = await _self_request("GET", CHROME_CATALOG_PATH, headers=headers)
    except httpx.HTTPError:
        return ""
    if response.status_code != 200:
        return ""
    try:
        payload = response.json()
    except ValueError:
        return ""
    entries = payload.get("plugins", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        return ""
    candidates = [
        item
        for item in entries
        if isinstance(item, dict)
        and str(item.get("plugin_id", "")).strip() == CHROME_PLUGIN_ID
    ]
    if not candidates:
        return ""
    newest = max(
        candidates,
        key=lambda item: _version_key(str(item.get("version", ""))),
    )
    return str(newest.get("install_url", "")).strip()


#: 插件路由「不存在」的状态码。核心对未匹配的路径按方法区分：GET 回 404，
#: 其余方法回 405（实测 2.3.0.post1：``POST /api/nope-nope`` 是 405）。两者都
#: 说明目标路由没挂上，只凭 404 判断会把「插件未安装」误报成「转发失败」。
_ROUTE_MISSING_STATUS = (404, 405)


def _unavailable(detail: str = "") -> HTTPException:
    return HTTPException(
        502,
        detail={
            "code": "bridge_endpoint_unavailable",
            "message": detail or "无法连接核心的 Chrome 插件路由",
        },
    )


async def _forward_chrome_setup(headers: dict) -> dict:
    """把 ``setup`` 转发给 chrome 插件，返回客户端可见的结果。

    路由缺失与「路径不存在」在核心里是同一种返回（见 ``_ROUTE_MISSING_STATUS``），
    只能再用 ``/api/plugins`` 确认插件是否安装。插件在列表里但路由仍缺失，说明
    是前缀或方法变了，回 ``bridge_endpoint_unavailable``。
    """
    response = None
    for path in CHROME_SETUP_PATHS:
        try:
            response = await _self_request(
                "POST",
                path,
                headers=headers,
                json_body=dict(CHROME_SETUP_BODY),
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "browser repair: forward to %s failed (%s)",
                path,
                type(exc).__name__,
            )
            raise _unavailable() from exc
        if response.status_code not in _ROUTE_MISSING_STATUS:
            break

    if response is None:
        # 候选列表为空属于编码错误，按端点不可用处理，不把空指针带出去。
        raise _unavailable()
    if response.status_code in _ROUTE_MISSING_STATUS:
        listed = await _chrome_plugin_listed(headers)
        if listed:
            raise _unavailable("Chrome 插件已安装，但 /setup 路由不存在")
        install_url = "" if listed is None else await _chrome_install_url(headers)
        raise HTTPException(
            409,
            detail={
                "code": "chrome_plugin_missing",
                "message": "服务端未安装官方 Chrome 插件，安装后才能执行修复",
                "install_url": install_url,
            },
        )
    if response.status_code >= 300:
        logger.warning(
            "browser repair: chrome setup returned %s",
            response.status_code,
        )
        raise _unavailable(f"Chrome 插件返回 HTTP {response.status_code}")

    try:
        result = response.json()
    except ValueError:
        result = response.text[:2000]
    return {"ok": True, "action": "setup", "result": result}


def _browser_router() -> APIRouter:
    """浏览器控制链路的两条端点。

    只补核心没有的能力：状态与自测走核心的 ``/api/browser/chrome/*``，
    这里不重复代转；安装与修复的正主是官方 chrome 插件。
    """
    router = APIRouter(prefix="/browser")

    @router.get("/logs")
    async def browser_logs(
        request: Request,
        lines: int = Query(
            BROWSER_LOG_DEFAULT_LINES,
            ge=1,
            le=BROWSER_LOG_MAX_LINES,
            description="返回尾部多少行",
        ),
    ):
        """读 NM host 日志尾部，按行脱敏。

        文件不存在时回 404 ``log_not_found``：这是「扩展/宿主还没跑过」的正常
        状态，不是插件故障。``truncated`` 表示文件比返回的内容更长。
        """
        _require_browser_access(request)
        logger.info("browser logs read requested lines=%s", lines)
        try:
            payload = _read_nm_host_log(lines)
        except FileNotFoundError as exc:
            raise HTTPException(
                404,
                detail={
                    "code": "log_not_found",
                    "message": "未找到 NM host 日志",
                },
            ) from exc
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.post("/repair")
    async def browser_repair(request: Request, body: dict):
        """只接受白名单动作 ``setup``，转发给官方 chrome 插件。

        不把客户端给的路径或参数透传到核心；转发体固定为 ``install_mode=unpacked``
        且 ``reset=false``，重复调用是幂等的。
        """
        _require_browser_access(request)
        action = str((body or {}).get("action", "")).strip()
        if action not in BROWSER_REPAIR_ACTIONS:
            raise HTTPException(
                400,
                detail={
                    "code": "unsupported_action",
                    "message": "仅支持 action=setup",
                },
            )
        logger.info("browser repair action=%s", action)
        result = await _forward_chrome_setup(_self_headers(request))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return router


# ---------------------------------------------------------------------------
# 更新检查：GitHub latest release、每日定时与 Console「关于」块
# ---------------------------------------------------------------------------

#: 检查时间的默认值，也是 ``check_time`` 缺项或写坏时的回退值。
UPDATE_DEFAULT_CHECK_TIME = "09:00"

#: 定时任务的轮询间隔。判断精确到分钟，60 秒足够，也便于「过了检查点才启动」
#: 的补查在一个周期内发生。
UPDATE_POLL_SECONDS = 60.0

#: 只查这个仓库的 latest release。换成 ``/releases`` 列表会让 draft/prerelease
#: 也要自己过滤，latest 端点已经排除了 draft。
UPDATE_RELEASE_URL = (
    "https://api.github.com/repos/csdeTR/QwenPaw-Node-Bridge/releases/latest"
)

#: 「关于」块与取不到 ``html_url`` 时用的仓库地址。
UPDATE_REPO_URL = "https://github.com/csdeTR/QwenPaw-Node-Bridge"

#: 设置与最近一次检查结果分开存：前者由用户维护，后者由定时任务写。
UPDATE_SETTINGS_FILE = "update_settings.json"
UPDATE_STATE_FILE = "update_state.json"

#: ``HH:MM``，24 小时制。允许 ``09:00``，不接受 ``9:0`` 与 ``24:00``。
_UPDATE_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

#: 定时任务句柄。热重载会重新执行本模块，旧模块的句柄随之丢失，因此取消旧任务
#: 依赖 shutdown hook（卸载插件时一定会跑），而不是这个变量。
_update_task: asyncio.Task | None = None


def _update_settings_path() -> Path:
    return _data_dir() / UPDATE_SETTINGS_FILE


def _update_state_path() -> Path:
    return _data_dir() / UPDATE_STATE_FILE


def _atomic_write_json(path: Path, data: dict) -> None:
    """与 ``_save_ssh_settings`` 同一套原子写：临时文件 + 0600 + rename。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _default_update_settings() -> dict:
    return {
        "enabled": True,
        "check_time": UPDATE_DEFAULT_CHECK_TIME,
        "notify_console": True,
        "notify_client": True,
    }


def _parse_check_time(text: object) -> tuple[int, int] | None:
    """``HH:MM`` 解析成 ``(时, 分)``。定时判断与界面共用这一套规则。"""
    match = _UPDATE_TIME_RE.match(str(text or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _load_update_settings() -> dict:
    """读设置，缺项与坏值一律按默认值补齐，不抛异常。

    文件可能被手工改坏或来自旧版本；更新检查坏了不该让设置页打不开。
    """
    defaults = _default_update_settings()
    try:
        data = json.loads(_update_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    settings = dict(defaults)
    for name in ("enabled", "notify_console", "notify_client"):
        if isinstance(data.get(name), bool):
            settings[name] = data[name]
    if _parse_check_time(data.get("check_time")) is not None:
        settings["check_time"] = str(data["check_time"]).strip()
    return settings


def _bool_field(body: dict, name: str, current: bool) -> bool:
    """只接受真布尔值：``"false"`` 是真值字符串，按 ``bool()`` 会变成开启。"""
    if name not in body:
        return current
    value = body[name]
    if not isinstance(value, bool):
        raise ValueError(f"{name} 必须是布尔值")
    return value


def _validate_update_settings(body: dict, current: dict) -> dict:
    """校验并合并设置。只写这四个字段，未知字段忽略。"""
    settings = {
        "enabled": _bool_field(body, "enabled", current["enabled"]),
        "check_time": current["check_time"],
        "notify_console": _bool_field(
            body,
            "notify_console",
            current["notify_console"],
        ),
        "notify_client": _bool_field(
            body,
            "notify_client",
            current["notify_client"],
        ),
    }
    if "check_time" in body:
        text = str(body.get("check_time") or "").strip()
        if not text:
            raise ValueError("检查时间不能为空")
        if _parse_check_time(text) is None:
            raise ValueError("检查时间必须是 HH:MM 格式，例如 09:00")
        settings["check_time"] = text
    return settings


def _load_update_state() -> dict:
    try:
        data = json.loads(_update_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _local_date_key(moment: datetime | None = None) -> str:
    """本地日期键。用户填的 HH:MM 是墙上时间，判断必须用同一时区。"""
    return (moment or datetime.now()).strftime("%Y-%m-%d")


def _update_due(now: datetime, check_time: str, last_check_date: str) -> bool:
    """今天该不该跑一次检查。纯函数，单测直接调。

    两条规则合并成一个判断：到了检查点、且今天没查过。服务在检查点之后启动时，
    第一次轮询就满足「已过检查点」，于是立即补查，不需要单独的启动分支。
    """
    if str(last_check_date or "") == _local_date_key(now):
        return False
    parsed = _parse_check_time(check_time) or _parse_check_time(
        UPDATE_DEFAULT_CHECK_TIME,
    )
    return (now.hour, now.minute) >= parsed


def _release_version(payload: dict) -> str:
    """从 release 里取版本号。``tag_name`` 可能带 ``v`` 前缀。"""
    tag = str(payload.get("tag_name", "") or "").strip()
    return tag[1:].strip() if tag.startswith("v") else tag


async def _github_get(url: str, headers: dict, *, trust_env: bool) -> httpx.Response:
    """发一次 GitHub 请求。``trust_env`` 决定是否读环境里的代理设置。"""
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        trust_env=trust_env,
    ) as client:
        return await client.get(url, headers=headers)


async def _fetch_latest_release(plugin_version: str) -> dict:
    """查 GitHub 的 latest release，返回原始对象。

    独立 client、``trust_env=True``：服务器上常见 ``HTTPS_PROXY`` 才能出网；
    与自请求的 ``trust_env=False`` 相反，那个是为了避免回环请求绕到代理。

    代理配置本身解析失败时直连重试一次。实测 httpx 0.28 在 ``no_proxy`` 含
    ``[::1]`` 时把方括号当成端口，抛 ``InvalidURL: Invalid port: ':1]'``；
    此时请求还没发出去，重试不会重复提交。网络本身不通时直连也会失败，
    与不重试的结果相同。
    """
    headers = {
        "User-Agent": f"qwenpaw-node-bridge/{plugin_version or 'unknown'}",
        "Accept": "application/vnd.github+json",
    }
    try:
        response = await _github_get(UPDATE_RELEASE_URL, headers, trust_env=True)
    except httpx.InvalidURL:
        logger.warning("[update] proxy environment unusable; retrying without it")
        response = await _github_get(UPDATE_RELEASE_URL, headers, trust_env=False)
    if response.status_code != 200:
        raise RuntimeError(f"GitHub 返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("GitHub 返回内容不是 JSON") from None
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub 返回内容不是对象")
    return payload


async def _check_for_update(plugin_version: str) -> dict:
    """执行一次检查并落盘。

    **失败只记 ``error``，保留上次成功结论**：网络抖动不该把「有新版本」抹成
    「已是最新」。``checked_at`` 与 ``last_check_date`` 无论如何都推进，避免
    失败后每分钟重试把 GitHub 打到限流。
    """
    state = _load_update_state()
    now = int(time.time())
    try:
        payload = await _fetch_latest_release(plugin_version)
        latest = _release_version(payload)
        if not latest:
            raise RuntimeError("GitHub 未返回版本号")
        if payload.get("draft") or payload.get("prerelease"):
            state.update(
                {
                    "update_available": False,
                    "latest_version": latest,
                    "latest_url": str(payload.get("html_url", "") or ""),
                    "error": "",
                },
            )
            logger.info("[update] latest release skipped (draft/prerelease) tag=%s", latest)
        else:
            available = _version_key(latest) > _version_key(plugin_version)
            state.update(
                {
                    "update_available": available,
                    "latest_version": latest,
                    "latest_url": str(
                        payload.get("html_url", "") or UPDATE_REPO_URL,
                    ),
                    "error": "",
                },
            )
            logger.info(
                "[update] check ok current=%s latest=%s update_available=%s",
                plugin_version,
                latest,
                available,
            )
    except Exception as exc:  # pylint: disable=broad-except
        state["error"] = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("[update] check failed error=%s", state["error"])
    state["checked_at"] = now
    state["last_check_date"] = _local_date_key()
    _atomic_write_json(_update_state_path(), state)
    return state


def _update_status(plugin_version: str) -> dict:
    """设置与最近一次检查结果的合并视图。``error`` 为空表示最近一次成功。"""
    settings = _load_update_settings()
    state = _load_update_state()
    return {
        "enabled": settings["enabled"],
        "check_time": settings["check_time"],
        "notify_console": settings["notify_console"],
        "notify_client": settings["notify_client"],
        "update_available": bool(state.get("update_available", False)),
        "latest_version": str(state.get("latest_version", "") or ""),
        "latest_url": str(state.get("latest_url", "") or ""),
        "checked_at": int(state.get("checked_at", 0) or 0),
        "last_check_date": str(state.get("last_check_date", "") or ""),
        "error": str(state.get("error", "") or ""),
        "plugin_version": plugin_version,
        "protocol_version": PROTOCOL_VERSION,
        "server_version": _server_version(),
        "repo_url": UPDATE_REPO_URL,
    }


async def _update_loop(plugin_version: str) -> None:
    """每 60 秒判断一次；单次失败不退出循环。"""
    logger.info(
        "[update] scheduler started check_time=%s",
        _load_update_settings()["check_time"],
    )
    try:
        while True:
            try:
                settings = _load_update_settings()
                if settings["enabled"] and _update_due(
                    datetime.now(),
                    settings["check_time"],
                    str(_load_update_state().get("last_check_date", "")),
                ):
                    await _check_for_update(plugin_version)
            except asyncio.CancelledError:
                raise
            except Exception:  # pylint: disable=broad-except
                logger.exception("[update] scheduler tick failed")
            await asyncio.sleep(UPDATE_POLL_SECONDS)
    except asyncio.CancelledError:
        logger.info("[update] scheduler stopped")
        raise


def _start_update_loop(plugin_version: str) -> None:
    """启动定时任务。同一模块内重复调用时先取消旧任务。"""
    global _update_task  # pylint: disable=global-statement
    if _update_task is not None and not _update_task.done():
        _update_task.cancel()
    _update_task = asyncio.create_task(_update_loop(plugin_version))


async def _stop_update_loop() -> None:
    """取消定时任务并等它退出。热重载与停服都会走到这里。"""
    global _update_task  # pylint: disable=global-statement
    task = _update_task
    _update_task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pylint: disable=broad-except
        logger.exception("[update] scheduler exit failed")


def _require_update_access(request: Request) -> None:
    """更新端点的访问校验，策略与 SSH、浏览器、配对相同。"""
    client_host = request.client.host if request.client else None
    if not _ssh_access_allowed(
        token=_http_token(request),
        client_host=client_host,
        headers=request.headers,
    ):
        raise HTTPException(401, "Update endpoints require authentication")


def _update_router(plugin_version: str) -> APIRouter:
    """更新检查的四条端点。

    ``GET /update/status`` 给「关于」块与客户端读取；``GET/PUT /update/settings``
    只碰设置；``POST /update/check`` 立即检查，失败回 502 且状态文件里同时留下
    ``error``，界面据此区分「检查失败」与「没有新版本」。
    """
    router = APIRouter(prefix="/update")

    @router.get("/status")
    async def update_status(request: Request):
        _require_update_access(request)
        return JSONResponse(
            _update_status(plugin_version),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/settings")
    async def get_update_settings(request: Request):
        _require_update_access(request)
        return JSONResponse(
            _load_update_settings(),
            headers={"Cache-Control": "no-store"},
        )

    @router.put("/settings")
    async def put_update_settings(request: Request, body: dict):
        _require_update_access(request)
        try:
            settings = _validate_update_settings(body, _load_update_settings())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        _atomic_write_json(_update_settings_path(), settings)
        logger.info(
            "[update] settings updated enabled=%s check_time=%s"
            " notify_console=%s notify_client=%s",
            settings["enabled"],
            settings["check_time"],
            settings["notify_console"],
            settings["notify_client"],
        )
        return JSONResponse(settings, headers={"Cache-Control": "no-store"})

    @router.post("/check")
    async def check_update_now(request: Request):
        _require_update_access(request)
        logger.info("[update] manual check requested")
        state = await _check_for_update(plugin_version)
        if state.get("error"):
            raise HTTPException(
                502,
                detail={
                    "code": "update_check_failed",
                    "message": str(state["error"]),
                },
            )
        return JSONResponse(
            _update_status(plugin_version),
            headers={"Cache-Control": "no-store"},
        )

    return router


class BridgePlugin:
    """插件主体。"""

    def register(self, api) -> None:
        _ensure_file_logging()
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

        @router.get("/logs")
        async def plugin_logs(
            request: Request,
            lines: int = Query(200, ge=1, le=1000, description="返回尾部多少行"),
        ):
            """插件自己的日志（设置变更、SSH 连接、修复动作）。

            行内容按浏览器日志同一套规则脱敏；只读接口，不写入日志文件。
            """
            _require_logs_access(request)
            raw, truncated = _tail_plugin_log(lines)
            return {
                "lines": [_sanitize_log_line(line) for line in raw],
                "truncated": truncated,
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

        @router.get("/ssh/settings")
        async def ssh_settings(request: Request):
            """脱敏设置。私钥、口令与登录密码不回传。"""
            _require_ssh_access(request)
            return _masked_ssh_settings()

        @router.put("/ssh/settings")
        async def update_ssh_settings(request: Request, body: dict):
            """写设置；空白密钥字段表示保留原值。

            自己校验访问策略：核心中间件在鉴权开关热切换的边界上不可靠（实测空 Bearer
            仍能到达插件），这条写路径又直接落盘私钥/口令，不能只靠外层。
            """
            _require_ssh_access(request)
            try:
                return _apply_ssh_settings(body)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        @router.get("/ssh/config")
        async def ssh_config(request: Request):
            """给手机端的脱敏目标信息；私钥与口令永不在这里出现。"""
            _require_ssh_access(request)
            data = _masked_ssh_settings()
            if not data["configured"]:
                raise HTTPException(404, "SSH 目标尚未配置")
            return {
                "host": data["host"],
                "port": data["port"],
                "user": data["user"],
                "auth": data["auth"],
                "key_configured": data["key_configured"],
                "key_source": data["key_source"],
            }

        @router.get("/trusted-addresses")
        async def trusted_addresses(request: Request):
            """Bridge 侧的受信任地址列表。

            与 App 侧受信任地址是两套独立配置：敏感下发必须两边都命中。
            """
            _require_ssh_access(request)
            return {"items": _load_trusted_addresses()}

        @router.put("/trusted-addresses")
        async def update_trusted_addresses(request: Request, body: dict):
            """全量替换受信任地址。条目可带来源网段，抵御 Host 头伪造。"""
            _require_ssh_access(request)
            try:
                items = _validate_trusted_addresses(body)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            _save_trusted_addresses(items)
            logger.info("trusted addresses updated count=%s", len(items))
            return {"items": items}

        @router.get("/ssh/credential")
        async def ssh_credential(request: Request):
            """按需下发凭据。

            不依赖核心中间件的鉴权：这里返回私钥或登录密码，必须自己校验访问策略。
            """
            _require_ssh_access(request)
            return JSONResponse(
                _ssh_credential(),
                headers={"Cache-Control": "no-store"},
            )

        @router.websocket("/ssh")
        async def ssh_relay(websocket: WebSocket):
            """二进制帧中继到设置里的 host:port。

            **AuthMiddleware 看不到 WebSocket 连接**，因此 token 必须在这里校验。
            只连设置里的目标，不接受客户端指定主机；并发数受限。
            """
            global _ssh_active_connections  # pylint: disable=global-statement
            token = _ws_token(websocket)

            async def reject(code: int, reason: str) -> None:
                """先完成握手再关闭。

                Starlette 在 ``accept()`` 之前 `close()` 会变成 HTTP 403，客户端分不清
                「没登录」「没配置」与「连不上」。先 accept 再发关闭帧，才能把原因带给客户端。
                """
                await websocket.accept()
                await websocket.close(code=code, reason=reason)

            client_host = websocket.client.host if websocket.client else None
            if not _ssh_access_allowed(
                token=token,
                client_host=client_host,
                headers=websocket.headers,
            ):
                await reject(1008, "unauthorized")
                return
            data = _load_ssh_settings()
            host = str(data.get("host", "")).strip()
            try:
                port = int(data.get("port", 22) or 22)
            except (TypeError, ValueError):
                port = 22
            if not host or not str(data.get("user", "")).strip():
                await reject(1008, "not configured")
                return
            if _ssh_active_connections >= SSH_MAX_CONNECTIONS:
                await reject(1013, "busy")
                return

            await websocket.accept()
            _ssh_active_connections += 1
            writer = None
            logger.info("SSH relay connected target=%s:%s", host, port)
            try:
                try:
                    reader, writer = await asyncio.open_connection(host, port)
                except OSError:
                    logger.warning("SSH relay target unreachable target=%s:%s", host, port)
                    await websocket.close(code=1011, reason="target unreachable")
                    return

                async def socket_to_ws():
                    try:
                        while True:
                            chunk = await reader.read(16384)
                            if not chunk:
                                break
                            await websocket.send_bytes(chunk)
                    except (
                        WebSocketDisconnect,
                        RuntimeError,
                        ConnectionError,
                    ):
                        pass

                async def ws_to_socket():
                    try:
                        while True:
                            message = await websocket.receive()
                            if message.get("type") == "websocket.disconnect":
                                break
                            chunk = message.get("bytes")
                            if not chunk:
                                # 只接受二进制帧；文本帧丢弃。
                                continue
                            writer.write(chunk)
                            await writer.drain()
                    except (
                        WebSocketDisconnect,
                        RuntimeError,
                        ConnectionError,
                    ):
                        pass

                tasks = {
                    asyncio.create_task(socket_to_ws()),
                    asyncio.create_task(ws_to_socket()),
                }
                _, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:  # pylint: disable=broad-except
                        pass
                _ssh_active_connections -= 1
                try:
                    await websocket.close()
                except Exception:  # pylint: disable=broad-except
                    pass
                logger.info("SSH relay disconnected target=%s:%s", host, port)

        @router.post("/pairing/tokens")
        async def issue_pairing_token(request: Request, body: dict):
            """签发一枚移动端访问令牌，并给出二维码载荷与图片。

            令牌是**凭据**：不回显在列表里，不进日志。二维码等于把这枚令牌画在屏幕上，
            页面必须提示不要外传。
            """
            username = _require_pairing_access(request)
            if not _pairing_auth_enabled():
                # 服务端未启用登录时令牌不会被执行链校验，签了也没有意义。
                raise HTTPException(409, "Authentication is not enabled")
            try:
                spec = _validate_pairing_request(body)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

            from qwenpaw.app.auth import create_token

            # 正数按秒计；0 走固定到期日的「长期」。
            if spec["days"]:
                expiry_seconds = spec["days"] * 24 * 3600
                token = create_token(username, expiry_seconds)
            else:
                expiry_seconds = 0
                token = _create_long_token(username)
            exp, jti = _token_claims(token)
            if not jti:
                raise HTTPException(500, "服务端生成的令牌缺少 jti，无法登记")

            now = int(time.time())
            data = _load_pairing()
            data["items"][jti] = {
                "label": spec["label"],
                "username": username,
                "issued_at": now,
                "expires_at": exp or (now + expiry_seconds),
                "revoked": False,
            }
            _save_pairing(data)
            logger.info(
                "pairing token issued jti=%s user=%s label=%s",
                jti,
                username,
                spec["label"],
            )
            payload = _pair_payload(spec["addresses"], token)
            try:
                image = _pair_qrcode_image(payload)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("pairing qrcode generation failed: %s", exc)
                image = ""
            return JSONResponse(
                {
                    "token": token,
                    "jti": jti,
                    "username": username,
                    "issued_at": now,
                    "expires_at": data["items"][jti]["expires_at"],
                    "payload": payload,
                    "qrcode_img": image,
                },
                headers={"Cache-Control": "no-store"},
            )

        @router.get("/pairing/tokens")
        async def list_pairing_tokens(request: Request):
            """已签发的令牌。只回元数据，**不回令牌原文**。"""
            _require_pairing_access(request)
            if not _pairing_auth_enabled():
                return JSONResponse(
                    {"items": []},
                    headers={"Cache-Control": "no-store"},
                )
            data = _load_pairing()
            items = [
                {"jti": jti, **_pairing_item_view(item)}
                for jti, item in sorted(
                    data["items"].items(),
                    key=lambda kv: int(kv[1].get("issued_at", 0) or 0),
                    reverse=True,
                )
            ]
            return JSONResponse(
                {"items": items},
                headers={"Cache-Control": "no-store"},
            )

        @router.post("/pairing/tokens/{jti}/revoke")
        async def revoke_pairing_token(request: Request, jti: str):
            """按 jti 吊销。

            核心没有「按 jti 吊销」的公开接口，这里直接用吊销表；失败时仍把本地记录
            标为已吊销，界面据此提示「该令牌将在到期时间失效」。
            """
            _require_pairing_access(request)
            data = _load_pairing()
            item = data["items"].get(jti)
            if not item:
                raise HTTPException(404, "未找到该令牌")
            applied = _core_revoke_jti(jti, int(item.get("expires_at", 0) or 0))
            item["revoked"] = True
            _save_pairing(data)
            logger.info("pairing token revoked jti=%s immediate=%s", jti, applied)
            return JSONResponse(
                {"revoked": True, "immediate": applied},
                headers={"Cache-Control": "no-store"},
            )

        @router.patch("/pairing/tokens/{jti}")
        async def rename_pairing_token(request: Request, jti: str, body: dict):
            """改名。只改登记表里的标签，令牌本身不受影响。"""
            _require_pairing_access(request)
            data = _load_pairing()
            try:
                label = _rename_pairing_item(data, jti, str(body.get("label", "")))
            except PairingEditError as exc:
                raise HTTPException(exc.status, exc.detail) from exc
            _save_pairing(data)
            return JSONResponse(
                {"renamed": True, "label": label},
                headers={"Cache-Control": "no-store"},
            )

        @router.delete("/pairing/tokens/{jti}")
        async def delete_pairing_token(request: Request, jti: str):
            """删除登记条目。只允许删除已吊销的令牌。"""
            _require_pairing_access(request)
            data = _load_pairing()
            try:
                _delete_pairing_item(data, jti)
            except PairingEditError as exc:
                raise HTTPException(exc.status, exc.detail) from exc
            _save_pairing(data)
            return JSONResponse(
                {"deleted": True},
                headers={"Cache-Control": "no-store"},
            )

        router.include_router(_portability_router())
        router.include_router(_browser_router())
        # 版本读 manifest，与 /info 的 plugin_version 同源。
        plugin_version = str(api.manifest.get("version", ""))
        router.include_router(_update_router(plugin_version))
        api.register_http_router(router, prefix=ROUTE_PREFIX)
        api.register_channel(
            channel_class=NodeBridgePushChannel,
            label="QwenPaw Node 推送",
            description="把收件箱与审批事件推送到 ntfy / Bark / Telegram",
            config_fields=[
                {
                    "name": "provider",
                    "label": "推送服务",
                    "type": "select",
                    "options": ["ntfy", "bark", "telegram"],
                    "required": True,
                    "default": "ntfy",
                },
                {
                    "name": "target",
                    "label": "目标（ntfy 主题 / Bark Key / Telegram chat id）",
                    "type": "text",
                    "required": True,
                },
                {
                    "name": "token",
                    "label": "令牌（ntfy 或 Telegram 需要时填写）",
                    "type": "password",
                },
                {
                    "name": "server_url",
                    "label": "服务地址（自建服务时填写）",
                    "type": "text",
                },
                {
                    "name": "notify_inbox",
                    "label": "推送收件箱未读",
                    "type": "switch",
                    "default": True,
                },
                {
                    "name": "notify_approval",
                    "label": "推送待审批",
                    "type": "switch",
                    "default": True,
                },
                {
                    "name": "min_interval_seconds",
                    "label": "最短推送间隔（秒）",
                    "type": "number",
                    "default": 30,
                },
            ],
        )
        # 更新检查的定时任务挂在插件生命周期上。热重载会重新执行本模块：
        # 卸载时 registry 先跑一次 shutdown hook 取消旧任务，新模块再起新任务，
        # 因此不会出现两个循环同时写状态文件。
        register_startup = getattr(api, "register_startup_hook", None)
        register_shutdown = getattr(api, "register_shutdown_hook", None)
        if register_startup and register_shutdown:
            register_startup(
                hook_name="qwenpaw_node_bridge_update",
                callback=lambda: _start_update_loop(plugin_version),
                priority=60,
            )
            register_shutdown(
                hook_name="qwenpaw_node_bridge_update",
                callback=_stop_update_loop,
                priority=60,
            )
        else:
            logger.warning(
                "[update] server has no lifecycle hooks; scheduled checks disabled",
            )
        logger.info(
            "qwenpaw-node-bridge registered at %s (protocol=%s)",
            ROUTE_PREFIX,
            PROTOCOL_VERSION,
        )


plugin = BridgePlugin()
