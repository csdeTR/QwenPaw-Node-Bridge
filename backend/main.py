# -*- coding: utf-8 -*-
"""QwenPaw Node Bridge：供 Android 客户端使用的桥接端点。

v0.1.0 只提供一个自述端点 `GET /info`，用于回答「配套插件装了没有、是哪一版、
支持哪些能力」。后续能力（`search` / `events` / `portability`）逐版追加到
`capabilities` 数组，客户端按名字开关功能，不比对版本号。

三条硬约束（都来自 qwenpaw 的插件加载器）：

1. 模块必须导出名为 ``plugin`` 的对象，并实现 ``register(api)``；加载器用
   ``getattr(module, "plugin")`` 取它。
2. 路由前缀不能是 ``/``，也不能与其他插件重复（registry 会抛 ``ValueError``）。
3. 只使用核心 ``plugins.py`` 没有的子路径：``/status`` 与 ``/files`` 下的任意
   子路径已被核心 router 先注册，请求到不了这里。``/info``、``/search``、
   ``/events`` 是安全的。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

#: 与客户端约定的契约版本。本文件新增端点时必须一起递增。
PROTOCOL_VERSION = 1

#: 本插件声明的能力。客户端按名字开关功能，不看版本号。
CAPABILITIES = ("info",)

#: 路由前缀。最终路径是 ``/api/plugins/qwenpaw-node-bridge``。
ROUTE_PREFIX = "/plugins/qwenpaw-node-bridge"


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

        api.register_http_router(router, prefix=ROUTE_PREFIX)
        logger.info(
            "qwenpaw-node-bridge registered at %s (protocol=%s)",
            ROUTE_PREFIX,
            PROTOCOL_VERSION,
        )


plugin = BridgePlugin()