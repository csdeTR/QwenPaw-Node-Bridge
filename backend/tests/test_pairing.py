# -*- coding: utf-8 -*-
"""设备配对端点与登记表的最小单测。

不启动服务端：签发、登记、吊销这几段都不经过 FastAPI 路由函数，而是直接验证
纯逻辑，避免为了一个端点拉起整台应用。路由层的访问校验由 test_ssh_settings 里
同一套 `_require_*` 用例覆盖。

核心的 ``create_token`` 与吊销表都在服务端 venv 里；这里对二者打桩，只验证本插件
自己的形状：登记条目、脱敏视图、按 jti 吊销的降级行为。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402  pylint: disable=wrong-import-position


def _request(headers=None, client=("127.0.0.1", 50000), user=None):
    raw_headers = [
        (key.lower().encode(), value.encode())
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "query_string": b"",
        "client": client,
    }
    request = Request(scope)
    if user is not None:
        request.state.user = user
    return request


class PairingTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {"QWENPAW_NODE_BRIDGE_DATA_DIR": self.temp_dir.name},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    # ---- 载荷 ----

    def test_payload_shape_matches_documented_sample(self):
        payload = main._pair_payload(
            ["http://10.10.0.211:8088", "https://a.example.com:8443"],
            "eyJzdWIiOiJkZW1vIn0.c2ln",
        )
        self.assertEqual(
            payload,
            "qwenpaw-node://pair?v=1"
            "&addr=http%3A%2F%2F10.10.0.211%3A8088"
            "&addr=https%3A%2F%2Fa.example.com%3A8443"
            "&token=eyJzdWIiOiJkZW1vIn0.c2ln",
        )

    def test_payload_encodes_reserved_characters(self):
        payload = main._pair_payload(["http://h:1"], "a+b%c?d&e")
        self.assertIn("token=a%2Bb%25c%3Fd%26e", payload)

    # ---- 请求校验 ----

    def test_expiry_days_whitelist(self):
        spec = main._validate_pairing_request(
            {"expires_in_days": 0, "addresses": ["http://127.0.0.1:8088"]},
        )
        self.assertEqual(spec["days"], 0)
        with self.assertRaises(ValueError):
            main._validate_pairing_request(
                {"expires_in_days": 12, "addresses": ["http://127.0.0.1:8088"]},
            )

    def test_addresses_require_scheme(self):
        with self.assertRaises(ValueError):
            main._validate_pairing_request(
                {"expires_in_days": 7, "addresses": ["10.0.0.1:8088"]},
            )

    def test_addresses_trailing_slash_trimmed(self):
        spec = main._validate_pairing_request(
            {"expires_in_days": 7, "addresses": ["http://127.0.0.1:8088/"]},
        )
        self.assertEqual(spec["addresses"], ["http://127.0.0.1:8088"])

    def test_address_count_capped(self):
        addresses = [f"http://10.0.0.{i}:8088" for i in range(1, 10)]
        with self.assertRaises(ValueError):
            main._validate_pairing_request(
                {"expires_in_days": 7, "addresses": addresses},
            )

    # ---- 登记表 ----

    def _seed(self, jti="abc", exp_offset=3600):
        now = int(main.time.time())
        main._save_pairing(
            {
                "version": 1,
                "items": {
                    jti: {
                        "label": "phone",
                        "username": "alice",
                        "issued_at": now,
                        "expires_at": now + exp_offset,
                        "revoked": False,
                    },
                },
            },
        )

    def test_registry_never_holds_token_text(self):
        self._seed()
        raw = main._pairing_path().read_text(encoding="utf-8")
        self.assertNotIn("eyJ", raw)
        self.assertNotIn("token", raw.replace("pairing_tokens", ""))

    def test_registry_file_is_private(self):
        self._seed()
        mode = main._pairing_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_expired_entries_are_pruned_on_read(self):
        self._seed(jti="old", exp_offset=-60)
        self.assertEqual(main._load_pairing()["items"], {})
        # 清理只发生在读路径，文件要等下一次写入才收缩。
        self.assertFalse(main._pairing_path().read_text(encoding="utf-8") == "{}")

    def test_item_view_redacts_token(self):
        view = main._pairing_item_view(
            {
                "label": "phone",
                "username": "alice",
                "issued_at": 1,
                "expires_at": 2,
                "revoked": True,
                "token": "secret",
            },
        )
        self.assertNotIn("token", view)
        self.assertTrue(view["revoked"])

    # ---- 按 jti 吊销 ----

    def test_revoke_jti_calls_core_helper(self):
        with mock.patch(
            "qwenpaw.app.auth._add_to_revocation_list",
        ) as helper:
            self.assertTrue(main._core_revoke_jti("abc", 123))
        helper.assert_called_once_with("abc", 123)

    def test_revoke_jti_degrades_when_core_lacks_helper(self):
        with mock.patch(
            "qwenpaw.app.auth._add_to_revocation_list",
            side_effect=AttributeError("gone"),
        ):
            self.assertFalse(main._core_revoke_jti("abc", 123))

    # ---- 访问校验 ----

    def test_pairing_requires_username(self):
        request = _request(headers={})
        with mock.patch.object(main, "_pairing_auth_enabled", return_value=True):
            with mock.patch.object(main, "_ssh_token_user", return_value=None):
                with self.assertRaises(HTTPException) as ctx:
                    main._require_pairing_access(request)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_pairing_accepts_state_user(self):
        request = _request(user="alice")
        self.assertEqual(main._require_pairing_access(request), "alice")

    def test_pairing_accepts_verified_token(self):
        request = _request({"authorization": "Bearer good"})
        with mock.patch.object(main, "_ssh_token_user", return_value="alice"):
            self.assertEqual(main._require_pairing_access(request), "alice")


    # ---- 改名与删除 ----

    def _item(self, revoked=False):
        return {
            "label": "phone",
            "username": "alice",
            "issued_at": 1,
            "expires_at": 2,
            "revoked": revoked,
        }

    def test_rename_updates_label(self):
        data = {"version": 1, "items": {"abc": self._item()}}
        self.assertEqual("主力手机", main._rename_pairing_item(data, "abc", " 主力手机 "))
        self.assertEqual("主力手机", data["items"]["abc"]["label"])

    def test_rename_rejects_blank_label(self):
        data = {"version": 1, "items": {"abc": self._item()}}
        with self.assertRaises(main.PairingEditError) as ctx:
            main._rename_pairing_item(data, "abc", "   ")
        self.assertEqual(400, ctx.exception.status)

    def test_rename_unknown_is_404(self):
        data = {"version": 1, "items": {}}
        with self.assertRaises(main.PairingEditError) as ctx:
            main._rename_pairing_item(data, "nope", "x")
        self.assertEqual(404, ctx.exception.status)

    def test_rename_revoked_is_rejected(self):
        data = {"version": 1, "items": {"abc": self._item(revoked=True)}}
        with self.assertRaises(main.PairingEditError) as ctx:
            main._rename_pairing_item(data, "abc", "x")
        self.assertEqual(409, ctx.exception.status)

    def test_delete_requires_revoked(self):
        data = {"version": 1, "items": {"abc": self._item()}}
        with self.assertRaises(main.PairingEditError) as ctx:
            main._delete_pairing_item(data, "abc")
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("abc", data["items"])

    def test_delete_revoked_removes_entry(self):
        data = {"version": 1, "items": {"abc": self._item(revoked=True)}}
        main._delete_pairing_item(data, "abc")
        self.assertEqual({}, data["items"])

    def test_delete_unknown_is_404(self):
        with self.assertRaises(main.PairingEditError) as ctx:
            main._delete_pairing_item({"version": 1, "items": {}}, "nope")
        self.assertEqual(404, ctx.exception.status)

    # ---- 长期令牌 ----

    def test_long_token_expires_at_the_fixed_moment(self):
        token = main._create_long_token("alice")
        exp, jti = main._token_claims(token)
        self.assertTrue(jti)
        self.assertEqual(main._long_expiry_epoch(), exp)


if __name__ == "__main__":
    unittest.main()