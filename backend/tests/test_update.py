# -*- coding: utf-8 -*-
"""更新检查（v0.5.0）的单测：设置默认值与校验、补查判定、版本比较、
GitHub 返回的四种情形与端点形状。

不联网：GitHub 的 ``httpx.AsyncClient`` 整体打桩，返回固定的 release 对象。
"""
import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from starlette.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402  pylint: disable=wrong-import-position

PREFIX = "/api/plugins/qwenpaw-node-bridge"
PLUGIN_VERSION = "0.5.0"


class _FakeResponse:
    """够用的 httpx.Response 替身：只看 ``status_code`` 与 ``json()``。"""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeAsyncClient:
    """替换 ``main.httpx.AsyncClient``，记录 URL 与请求头。"""

    instances: list = []

    def __init__(self, response=None, error=None, **kwargs):
        self.response = response
        self.error = error
        self.kwargs = kwargs
        self.url = ""
        self.headers = {}
        _FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None):
        self.url = url
        self.headers = headers or {}
        if self.error is not None:
            raise self.error
        return self.response


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(main._update_router(PLUGIN_VERSION), prefix=PREFIX)
    return TestClient(app, client=("127.0.0.1", 50123))


class UpdateSettingsTest(unittest.TestCase):
    """设置：默认值、校验、落盘权限。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        env = mock.patch.dict(
            os.environ,
            {"QWENPAW_NODE_BRIDGE_DATA_DIR": self.temp_dir.name},
        )
        env.start()
        self.addCleanup(env.stop)
        # 端点默认按「未开 Web 登录 + 回环来源」放行。
        auth = mock.patch.object(main, "_ssh_auth_enabled", return_value=False)
        auth.start()
        self.addCleanup(auth.stop)

    # ---- 默认值 ----

    def test_defaults_when_file_missing(self):
        settings = main._load_update_settings()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["check_time"], "09:00")
        self.assertTrue(settings["notify_console"])
        self.assertTrue(settings["notify_client"])

    def test_broken_file_falls_back_to_defaults(self):
        main._update_settings_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(main._load_update_settings(), main._default_update_settings())

    def test_bad_stored_values_are_ignored(self):
        main._update_settings_path().write_text(
            json.dumps(
                {
                    "enabled": "yes",
                    "check_time": "25:99",
                    "notify_console": 0,
                    "notify_client": None,
                },
            ),
            encoding="utf-8",
        )
        settings = main._load_update_settings()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["check_time"], "09:00")
        self.assertTrue(settings["notify_console"])
        self.assertTrue(settings["notify_client"])

    # ---- 校验 ----

    def test_accepts_valid_settings(self):
        current = main._default_update_settings()
        settings = main._validate_update_settings(
            {
                "enabled": False,
                "check_time": "23:59",
                "notify_console": False,
                "notify_client": True,
            },
            current,
        )
        self.assertEqual(
            settings,
            {
                "enabled": False,
                "check_time": "23:59",
                "notify_console": False,
                "notify_client": True,
            },
        )

    def test_rejects_bad_check_time(self):
        current = main._default_update_settings()
        for text in ("9:0", "24:00", "12:60", "0900", "", "  ", "09:00:00"):
            with self.assertRaises(ValueError, msg=text):
                main._validate_update_settings({"check_time": text}, current)

    def test_check_time_required_shape_but_optional_field(self):
        # 不传时保留原值；传了就必须合法。默认值保证结果永远可解析。
        current = main._default_update_settings()
        self.assertEqual(
            main._validate_update_settings({}, current)["check_time"],
            "09:00",
        )

    def test_rejects_non_bool(self):
        current = main._default_update_settings()
        for name in ("enabled", "notify_console", "notify_client"):
            with self.assertRaises(ValueError, msg=name):
                main._validate_update_settings({name: "false"}, current)
            with self.assertRaises(ValueError, msg=name):
                main._validate_update_settings({name: 0}, current)

    def test_parse_check_time(self):
        self.assertEqual(main._parse_check_time("00:00"), (0, 0))
        self.assertEqual(main._parse_check_time(" 09:05 "), (9, 5))
        self.assertIsNone(main._parse_check_time("24:00"))
        self.assertIsNone(main._parse_check_time(None))

    # ---- 端点 ----

    def test_put_then_get_settings(self):
        response = _client().put(
            f"{PREFIX}/update/settings",
            json={
                "enabled": False,
                "check_time": "07:30",
                "notify_console": False,
                "notify_client": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["check_time"], "07:30")
        self.assertFalse(response.json()["notify_client"])

        stored = _client().get(f"{PREFIX}/update/settings").json()
        self.assertFalse(stored["enabled"])
        self.assertEqual(stored["check_time"], "07:30")

        path = main._update_settings_path()
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(path.with_suffix(".tmp").exists())

    def test_put_rejects_bad_time_with_400(self):
        response = _client().put(
            f"{PREFIX}/update/settings",
            json={"check_time": "25:00"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("HH:MM", response.json()["detail"])

    def test_put_rejects_non_bool_with_400(self):
        response = _client().put(
            f"{PREFIX}/update/settings",
            json={"notify_console": "false"},
        )
        self.assertEqual(response.status_code, 400)

    def test_endpoints_require_access(self):
        app = FastAPI()
        app.include_router(main._update_router(PLUGIN_VERSION), prefix=PREFIX)
        client = TestClient(app, client=("8.8.8.8", 50123))
        self.assertEqual(client.get(f"{PREFIX}/update/status").status_code, 401)
        self.assertEqual(client.get(f"{PREFIX}/update/settings").status_code, 401)
        self.assertEqual(client.post(f"{PREFIX}/update/check").status_code, 401)


class UpdateScheduleTest(unittest.TestCase):
    """补查判定：纯函数，固定日期与时区无关。"""

    def test_not_due_before_check_time(self):
        self.assertFalse(
            main._update_due(
                datetime(2026, 11, 21, 8, 59),
                "09:00",
                "",
            ),
        )

    def test_due_at_check_time(self):
        self.assertTrue(
            main._update_due(datetime(2026, 11, 21, 9, 0), "09:00", ""),
        )

    def test_catch_up_when_started_after_check_time(self):
        # 服务在 14:00 才启动，当天还没检查过：第一次轮询就补查。
        self.assertTrue(
            main._update_due(datetime(2026, 11, 21, 14, 0), "09:00", ""),
        )

    def test_not_due_twice_in_one_day(self):
        self.assertFalse(
            main._update_due(datetime(2026, 11, 21, 14, 0), "09:00", "2026-11-21"),
        )

    def test_due_again_next_day(self):
        self.assertTrue(
            main._update_due(datetime(2026, 11, 22, 9, 1), "09:00", "2026-11-21"),
        )

    def test_invalid_time_falls_back_to_default(self):
        self.assertFalse(
            main._update_due(datetime(2026, 11, 21, 8, 0), "not-a-time", ""),
        )
        self.assertTrue(
            main._update_due(datetime(2026, 11, 21, 9, 0), "not-a-time", ""),
        )


class VersionCompareTest(unittest.TestCase):
    def test_numeric_compare_not_string(self):
        self.assertGreater(
            main._version_key("0.10.0"),
            main._version_key("0.9.9"),
        )

    def test_tag_prefix_stripped(self):
        self.assertEqual(main._release_version({"tag_name": "v0.6.0"}), "0.6.0")
        self.assertEqual(main._release_version({"tag_name": "0.6.0"}), "0.6.0")
        self.assertEqual(main._release_version({}), "")

    def test_equal_version_is_not_an_update(self):
        self.assertFalse(
            main._version_key("0.5.0") > main._version_key(PLUGIN_VERSION),
        )


class UpdateCheckTest(unittest.IsolatedAsyncioTestCase):
    """GitHub 请求：成功、失败、draft、prerelease。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        env = mock.patch.dict(
            os.environ,
            {"QWENPAW_NODE_BRIDGE_DATA_DIR": self.temp_dir.name},
        )
        env.start()
        self.addCleanup(env.stop)
        _FakeAsyncClient.instances = []

    def _patch_github(self, response=None, responses=None, error=None):
        """把 ``httpx.AsyncClient`` 换成假实现。

        ``responses`` 按调用顺序依次返回，用来构造「先成功后失败」的序列。
        """
        queue = list(responses) if responses else []
        if response is not None:
            queue.append(response)

        def factory(**client_kwargs):
            if error is not None:
                return _FakeAsyncClient(error=error, **client_kwargs)
            picked = queue[0] if len(queue) == 1 else queue.pop(0)
            return _FakeAsyncClient(response=picked, **client_kwargs)

        patcher = mock.patch.object(main.httpx, "AsyncClient", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_success_marks_update_available(self):
        self._patch_github(
            response=_FakeResponse(
                200,
                {
                    "tag_name": "v0.6.0",
                    "html_url": "https://github.com/csdeTR/QwenPaw-Node-Bridge/releases/tag/v0.6.0",
                    "draft": False,
                    "prerelease": False,
                },
            ),
        )
        state = await main._check_for_update(PLUGIN_VERSION)
        self.assertTrue(state["update_available"])
        self.assertEqual(state["latest_version"], "0.6.0")
        self.assertIn("v0.6.0", state["latest_url"])
        self.assertEqual(state["error"], "")
        self.assertGreater(state["checked_at"], 0)
        self.assertEqual(state["last_check_date"], main._local_date_key())

        instance = _FakeAsyncClient.instances[0]
        self.assertEqual(instance.url, main.UPDATE_RELEASE_URL)
        self.assertEqual(
            instance.headers["User-Agent"],
            f"qwenpaw-node-bridge/{PLUGIN_VERSION}",
        )
        self.assertEqual(instance.headers["Accept"], "application/vnd.github+json")
        self.assertTrue(instance.kwargs.get("trust_env"))
        self.assertTrue(instance.kwargs.get("follow_redirects"))
        self.assertEqual(instance.kwargs.get("timeout"), 10.0)

    async def test_same_version_is_not_an_update(self):
        self._patch_github(
            response=_FakeResponse(200, {"tag_name": "v0.5.0"}),
        )
        state = await main._check_for_update(PLUGIN_VERSION)
        self.assertFalse(state["update_available"])
        self.assertEqual(state["latest_version"], "0.5.0")
        self.assertEqual(state["error"], "")

    async def test_failure_keeps_last_success(self):
        self._patch_github(
            responses=[
                _FakeResponse(
                    200,
                    {
                        "tag_name": "v0.6.0",
                        "html_url": "https://example.test/v0.6.0",
                    },
                ),
                _FakeResponse(503),
            ],
        )
        first = await main._check_for_update(PLUGIN_VERSION)
        self.assertTrue(first["update_available"])
        self.assertEqual(first["error"], "")

        failed = await main._check_for_update(PLUGIN_VERSION)
        self.assertIn("HTTP 503", failed["error"])
        # 网络抖动不能把「有新版本」抹成「已是最新」。
        self.assertTrue(failed["update_available"])
        self.assertEqual(failed["latest_version"], "0.6.0")
        self.assertEqual(failed["latest_url"], "https://example.test/v0.6.0")
        self.assertGreaterEqual(failed["checked_at"], first["checked_at"])
        self.assertEqual(failed["last_check_date"], main._local_date_key())

    async def test_draft_or_prerelease_is_skipped(self):
        self._patch_github(
            responses=[
                _FakeResponse(200, {"tag_name": "v0.9.0", "draft": True}),
                _FakeResponse(
                    200,
                    {"tag_name": "v0.9.1", "prerelease": True},
                ),
            ],
        )
        for _ in range(2):
            state = await main._check_for_update(PLUGIN_VERSION)
            self.assertFalse(state["update_available"])
            self.assertEqual(state["error"], "")

    async def test_missing_version_is_an_error(self):
        self._patch_github(response=_FakeResponse(200, {"name": "no tag"}))
        state = await main._check_for_update(PLUGIN_VERSION)
        self.assertIn("版本号", state["error"])
        self.assertFalse(state.get("update_available", False))

    async def test_network_error_is_recorded(self):
        self._patch_github(error=main.httpx.ConnectError("boom"))
        state = await main._check_for_update(PLUGIN_VERSION)
        self.assertIn("ConnectError", state["error"])

    async def test_proxy_config_error_falls_back_to_direct(self):
        # 实测 httpx 0.28 在 no_proxy 含 [::1] 时抛 InvalidURL；此时请求尚未发出。
        calls = []

        async def fake_get(url, headers, *, trust_env):
            calls.append(trust_env)
            if trust_env:
                raise main.httpx.InvalidURL("Invalid port: ':1]'")
            return _FakeResponse(200, {"tag_name": "v0.6.0"})

        with mock.patch.object(main, "_github_get", new=fake_get):
            state = await main._check_for_update(PLUGIN_VERSION)
        self.assertEqual(calls, [True, False])
        self.assertTrue(state["update_available"])
        self.assertEqual(state["error"], "")


class UpdateStatusTest(unittest.TestCase):
    """status 形状与手动检查的返回。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        env = mock.patch.dict(
            os.environ,
            {"QWENPAW_NODE_BRIDGE_DATA_DIR": self.temp_dir.name},
        )
        env.start()
        self.addCleanup(env.stop)
        auth = mock.patch.object(main, "_ssh_auth_enabled", return_value=False)
        auth.start()
        self.addCleanup(auth.stop)

    def test_status_shape(self):
        payload = _client().get(f"{PREFIX}/update/status").json()
        expected = {
            "enabled",
            "check_time",
            "notify_console",
            "notify_client",
            "update_available",
            "latest_version",
            "latest_url",
            "checked_at",
            "last_check_date",
            "error",
            "plugin_version",
            "server_version",
        }
        self.assertTrue(expected.issubset(payload.keys()))
        self.assertEqual(payload["plugin_version"], PLUGIN_VERSION)
        self.assertEqual(payload["check_time"], "09:00")
        self.assertFalse(payload["update_available"])
        self.assertEqual(payload["error"], "")
        self.assertEqual(payload["checked_at"], 0)

    def test_status_reflects_saved_settings(self):
        _client().put(
            f"{PREFIX}/update/settings",
            json={"notify_client": False, "check_time": "21:15"},
        )
        payload = _client().get(f"{PREFIX}/update/status").json()
        self.assertFalse(payload["notify_client"])
        self.assertEqual(payload["check_time"], "21:15")

    def test_manual_check_returns_status_on_success(self):
        async def fake_check(plugin_version):
            # 端点返回的是落盘后的状态，替身也要写一次状态文件。
            state = {
                "update_available": True,
                "latest_version": "0.6.0",
                "latest_url": "https://example.test",
                "checked_at": 1,
                "last_check_date": "2026-11-21",
                "error": "",
            }
            main._atomic_write_json(main._update_state_path(), state)
            return state

        with mock.patch.object(main, "_check_for_update", new=fake_check):
            response = _client().post(f"{PREFIX}/update/check")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["update_available"])
        self.assertEqual(response.json()["plugin_version"], PLUGIN_VERSION)

    def test_manual_check_returns_502_on_failure(self):
        async def fake_check(plugin_version):
            return {"error": "RuntimeError: GitHub 返回 HTTP 500"}

        with mock.patch.object(main, "_check_for_update", new=fake_check):
            response = _client().post(f"{PREFIX}/update/check")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"]["code"],
            "update_check_failed",
        )


class UpdateLoopTest(unittest.IsolatedAsyncioTestCase):
    """定时任务：到点触发、可取消、重复启动不留两个循环。"""

    async def test_loop_fires_when_due_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"QWENPAW_NODE_BRIDGE_DATA_DIR": tmp},
            ):
                main._atomic_write_json(
                    main._update_settings_path(),
                    {
                        "enabled": True,
                        "check_time": "00:00",
                        "notify_console": True,
                        "notify_client": True,
                    },
                )
                called = asyncio.Event()

                async def fake_check(plugin_version):
                    called.set()
                    return {}

                with mock.patch.object(
                    main,
                    "_check_for_update",
                    new=fake_check,
                ), mock.patch.object(main, "UPDATE_POLL_SECONDS", 0.01):
                    main._start_update_loop(PLUGIN_VERSION)
                    await asyncio.wait_for(called.wait(), timeout=2.0)
                    task = main._update_task
                    self.assertIsNotNone(task)
                    await main._stop_update_loop()
                    self.assertIsNone(main._update_task)
                    self.assertTrue(task.cancelled() or task.done())

    async def test_disabled_settings_never_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"QWENPAW_NODE_BRIDGE_DATA_DIR": tmp},
            ):
                main._atomic_write_json(
                    main._update_settings_path(),
                    {
                        "enabled": False,
                        "check_time": "00:00",
                        "notify_console": True,
                        "notify_client": True,
                    },
                )
                called = asyncio.Event()

                async def fake_check(plugin_version):
                    called.set()
                    return {}

                with mock.patch.object(
                    main,
                    "_check_for_update",
                    new=fake_check,
                ), mock.patch.object(main, "UPDATE_POLL_SECONDS", 0.01):
                    main._start_update_loop(PLUGIN_VERSION)
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(called.wait(), timeout=0.2)
                    await main._stop_update_loop()


class UpdateManifestTest(unittest.TestCase):
    def test_manifest_declares_update(self):
        manifest = json.loads(
            (ROOT_DIR / "plugin.json").read_text(encoding="utf-8"),
        )
        self.assertEqual(manifest["version"], "0.5.0")
        self.assertIn("update", manifest["meta"]["capabilities"])
        self.assertEqual(
            tuple(manifest["meta"]["capabilities"]),
            tuple(main.CAPABILITIES),
        )
        self.assertEqual(main.PROTOCOL_VERSION, 1)


if __name__ == "__main__":
    unittest.main()
