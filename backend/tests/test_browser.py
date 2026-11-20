# -*- coding: utf-8 -*-
"""浏览器能力（v0.5.0）的单测：日志脱敏与行数上限、修复白名单与转发结果。

不启动真实服务端：路由挂在一个最小的 FastAPI 实例上，转发层 ``_self_request``
整体打桩。这样能覆盖真实的访问校验、状态码与错误 detail 形状，又不需要
核心进程、chrome 插件或网络。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI
from starlette.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402  pylint: disable=wrong-import-position

PREFIX = "/api/plugins/qwenpaw-node-bridge"

LOG_LINES = [
    "2026-11-20T10:00:00 INFO native host starting",
    "2026-11-20T10:00:01 INFO GET /x?token=abc123&lines=5",
    "2026-11-20T10:00:02 ERROR Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "2026-11-20T10:00:03 WARN Set-Cookie: sid=deadbeef; HttpOnly",
    "2026-11-20T10:00:04 DEBUG X-Api-Key: sk-live-abcdef api_key=plain-key secret=topsecret",
]

SECRETS = (
    "abc123",
    "eyJhbGciOiJIUzI1NiJ9",
    "deadbeef",
    "sk-live-abcdef",
    "plain-key",
    "topsecret",
)


def _client() -> TestClient:
    """回环来源的 TestClient。未开鉴权时这正是放行条件。"""
    app = FastAPI()
    app.include_router(main._browser_router(), prefix=PREFIX)
    return TestClient(app, client=("127.0.0.1", 50123))


class BrowserTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.log_path = Path(self.temp_dir.name) / "logs" / "nm-host.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        path_patch = mock.patch.object(
            main,
            "_nm_host_log_path",
            return_value=self.log_path,
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        # 默认按「未开 Web 登录 + 回环来源」放行；鉴权用例单独覆盖该项。
        auth_patch = mock.patch.object(main, "_ssh_auth_enabled", return_value=False)
        auth_patch.start()
        self.addCleanup(auth_patch.stop)

    def _write_log(self, lines=None) -> Path:
        text = "\n".join(lines if lines is not None else LOG_LINES) + "\n"
        self.log_path.write_text(text, encoding="utf-8")
        return self.log_path

    # ---- 日志：脱敏 ----

    def test_log_redacts_secrets(self):
        self._write_log()
        response = _client().get(f"{PREFIX}/browser/logs", params={"lines": 50})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        joined = "\n".join(payload["lines"])
        for secret in SECRETS:
            self.assertNotIn(secret, joined)
        self.assertEqual(payload["lines"][0], LOG_LINES[0])
        self.assertIn("Authorization: ***", joined)
        self.assertIn("Set-Cookie: ***", joined)
        self.assertIn("X-Api-Key: ***", joined)
        self.assertIn("token=***", joined)
        self.assertEqual(payload["path"], str(self.log_path))
        self.assertFalse(payload["truncated"])

    def test_log_redacts_json_keys_and_bearer(self):
        line = '{"api_key": "json-key", "note": "Bearer abcdefgh12345678"}'
        sanitized = main._sanitize_log_line(line)
        self.assertNotIn("json-key", sanitized)
        self.assertNotIn("abcdefgh12345678", sanitized)
        self.assertIn("***", sanitized)

    def test_log_redacts_dict_repr_credentials(self):
        line = "headers={'authorization': 'Basic dXNlcjpwYXNz'}"
        sanitized = main._sanitize_log_line(line)
        self.assertNotIn("dXNlcjpwYXNz", sanitized)
        self.assertIn("***", sanitized)

    # ---- 日志：行数上限与截断 ----

    def test_log_keeps_tail_and_marks_truncated(self):
        self._write_log()
        payload = _client().get(
            f"{PREFIX}/browser/logs",
            params={"lines": 2},
        ).json()
        self.assertEqual(len(payload["lines"]), 2)
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["lines"][-1].startswith("2026-11-20T10:00:04"))

    def test_log_all_lines_when_under_limit(self):
        self._write_log()
        payload = _client().get(
            f"{PREFIX}/browser/logs",
            params={"lines": 100},
        ).json()
        self.assertEqual(len(payload["lines"]), len(LOG_LINES))
        self.assertFalse(payload["truncated"])

    def test_log_rejects_out_of_range_lines(self):
        self._write_log()
        client = _client()
        self.assertEqual(
            client.get(f"{PREFIX}/browser/logs", params={"lines": 0}).status_code,
            422,
        )
        self.assertEqual(
            client.get(f"{PREFIX}/browser/logs", params={"lines": 2001}).status_code,
            422,
        )

    def test_tail_drops_partial_first_line(self):
        # 每行 40 字节左右，64 字节的读取窗口必然从行中间开始。
        self._write_log([f"line-{index:04d}-{'x' * 30}" for index in range(20)])
        with mock.patch.object(main, "BROWSER_LOG_MAX_BYTES", 64):
            payload = main._read_nm_host_log(500)
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["lines"])
        for line in payload["lines"]:
            self.assertTrue(line.startswith("line-"))

    # ---- 日志：文件不存在 ----

    def test_log_not_found(self):
        response = _client().get(f"{PREFIX}/browser/logs")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "log_not_found")

    # ---- 修复：动作白名单 ----

    def test_repair_rejects_other_actions(self):
        forwarded = mock.AsyncMock()
        with mock.patch.object(main, "_forward_chrome_setup", forwarded):
            response = _client().post(
                f"{PREFIX}/browser/repair",
                json={"action": "reset"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "unsupported_action")
        forwarded.assert_not_awaited()

    def test_repair_rejects_missing_action(self):
        response = _client().post(f"{PREFIX}/browser/repair", json={})
        self.assertEqual(response.status_code, 400)

    # ---- 修复：转发 ----

    def test_repair_forwards_setup_with_same_token(self):
        calls = []

        async def fake_request(method, path, *, headers=None, json_body=None):
            calls.append((method, path, headers, json_body))
            return httpx.Response(200, json={"installed": True})

        with mock.patch.object(main, "_self_request", new=fake_request):
            response = _client().post(
                f"{PREFIX}/browser/repair",
                json={"action": "setup"},
                headers={"authorization": "Bearer pair-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        method, path, headers, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/chrome/setup")
        self.assertEqual(headers["authorization"], "Bearer pair-token")
        self.assertEqual(body, {"install_mode": "unpacked", "reset": False})

    def test_repair_forwards_trusted_header_without_token(self):
        calls = []

        async def fake_request(method, path, *, headers=None, json_body=None):
            calls.append((method, path, headers, json_body))
            return httpx.Response(200, json={})

        with mock.patch.object(main, "_self_request", new=fake_request):
            _client().post(
                f"{PREFIX}/browser/repair",
                json={"action": "setup"},
                headers={"x-qwenpaw-trusted-host": "1"},
            )
        self.assertEqual(calls[0][2].get("x-qwenpaw-trusted-host"), "1")

    # ---- 修复：插件未装 / 路由缺失 / 转发失败 ----

    @staticmethod
    def _repair_with(fake_request) -> httpx.Response:
        with mock.patch.object(main, "_self_request", new=fake_request):
            return _client().post(
                f"{PREFIX}/browser/repair",
                json={"action": "setup"},
            )

    def test_repair_reports_missing_plugin_with_install_url(self):
        async def fake_request(method, path, *, headers=None, json_body=None):
            if method == "POST":
                # 实测核心对未匹配的 POST 回 405，而不是 404。
                return httpx.Response(405, json={"detail": "Method Not Allowed"})
            if path == "/api/plugins":
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json={
                    "plugins": [
                        {
                            "plugin_id": "chrome",
                            "version": "0.1.0",
                            "install_url": "https://example.test/chrome-0.1.0.zip",
                        },
                        {
                            "plugin_id": "chrome",
                            "version": "0.1.1",
                            "install_url": "https://example.test/chrome-0.1.1.zip",
                        },
                        {
                            "plugin_id": "other",
                            "version": "9.9.9",
                            "install_url": "https://example.test/other.zip",
                        },
                    ],
                },
            )

        response = self._repair_with(fake_request)
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "chrome_plugin_missing")
        self.assertEqual(
            detail["install_url"],
            "https://example.test/chrome-0.1.1.zip",
        )

    def test_repair_treats_404_and_405_alike(self):
        async def fake_request(method, path, *, headers=None, json_body=None):
            if method == "POST":
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json=[])

        detail = self._repair_with(fake_request).json()["detail"]
        self.assertEqual(detail["code"], "chrome_plugin_missing")

    def test_repair_missing_plugin_without_catalog_returns_empty_url(self):
        async def fake_request(method, path, *, headers=None, json_body=None):
            if method == "POST":
                return httpx.Response(404)
            if path == "/api/plugins":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"plugins": []})

        detail = self._repair_with(fake_request).json()["detail"]
        self.assertEqual(detail["code"], "chrome_plugin_missing")
        self.assertEqual(detail["install_url"], "")

    def test_repair_installed_but_route_missing(self):
        async def fake_request(method, path, *, headers=None, json_body=None):
            if method == "POST":
                return httpx.Response(404)
            return httpx.Response(200, json=[{"id": "chrome"}])

        response = self._repair_with(fake_request)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"]["code"],
            "bridge_endpoint_unavailable",
        )

    def test_repair_forward_failure(self):
        async def fake_request(method, path, *, headers=None, json_body=None):
            raise httpx.ConnectError("connection refused")

        response = self._repair_with(fake_request)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"]["code"],
            "bridge_endpoint_unavailable",
        )

    def test_repair_upstream_error(self):
        async def fake_request(method, path, *, headers=None, json_body=None):
            return httpx.Response(500, json={"detail": "boom"})

        response = self._repair_with(fake_request)
        self.assertEqual(response.status_code, 502)

    # ---- 鉴权 ----

    def test_auth_rejects_anonymous_when_enabled(self):
        self._write_log()
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=True):
            with mock.patch.object(main, "_ssh_token_user", return_value=None):
                logs = _client().get(f"{PREFIX}/browser/logs")
                repair = _client().post(
                    f"{PREFIX}/browser/repair",
                    json={"action": "setup"},
                )
        self.assertEqual(logs.status_code, 401)
        self.assertEqual(repair.status_code, 401)

    def test_auth_accepts_valid_token(self):
        self._write_log()
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=True):
            with mock.patch.object(
                main,
                "_ssh_token_user",
                side_effect=lambda token: "user" if token == "good" else None,
            ):
                response = _client().get(
                    f"{PREFIX}/browser/logs",
                    headers={"authorization": "Bearer good"},
                )
        self.assertEqual(response.status_code, 200)

    def test_auth_rejects_public_client_when_disabled(self):
        self._write_log()
        app = FastAPI()
        app.include_router(main._browser_router(), prefix=PREFIX)
        client = TestClient(app, client=("8.8.8.8", 50123))
        self.assertEqual(client.get(f"{PREFIX}/browser/logs").status_code, 401)

    # ---- 端口与清单 ----

    def test_api_base_prefers_core_port(self):
        with mock.patch(
            "qwenpaw.config.utils.read_last_api",
            return_value=("0.0.0.0", 9123),
        ):
            self.assertEqual(main._server_api_base(), "http://127.0.0.1:9123")

    def test_api_base_falls_back_to_env_then_default(self):
        with mock.patch(
            "qwenpaw.config.utils.read_last_api",
            side_effect=RuntimeError("unavailable"),
        ):
            with mock.patch.dict(os.environ, {"QWENPAW_API_PORT": "9333"}):
                self.assertEqual(main._server_api_base(), "http://127.0.0.1:9333")
            with mock.patch.dict(os.environ, {"QWENPAW_API_PORT": "not-a-port"}):
                self.assertEqual(
                    main._server_api_base(),
                    f"http://127.0.0.1:{main.DEFAULT_API_PORT}",
                )

    def test_plugin_log_tail_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"QWENPAW_NODE_BRIDGE_DATA_DIR": tmp},
            ):
                path = Path(tmp) / "plugin.log"
                path.write_text(
                    "one\n"
                    "Authorization: Bearer abc123secret\n"
                    "password=topsecret\n",
                    encoding="utf-8",
                )
                lines, truncated = main._tail_plugin_log(3)
                self.assertFalse(truncated)
                self.assertEqual(
                    lines,
                    [
                        "one",
                        "Authorization: Bearer abc123secret",
                        "password=topsecret",
                    ],
                )
                sanitized = "\n".join(main._sanitize_log_line(line) for line in lines)
                self.assertNotIn("abc123secret", sanitized)
                self.assertNotIn("topsecret", sanitized)

    def test_manifest_declares_browser(self):
        manifest = json.loads(
            (ROOT_DIR / "plugin.json").read_text(encoding="utf-8"),
        )
        self.assertEqual(manifest["version"], "0.5.0")
        self.assertIn("browser", manifest["meta"]["capabilities"])
        self.assertEqual(
            tuple(manifest["meta"]["capabilities"]),
            tuple(main.CAPABILITIES),
        )


if __name__ == "__main__":
    unittest.main()
