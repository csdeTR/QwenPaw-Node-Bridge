# -*- coding: utf-8 -*-
"""SSH 设置与凭据的最小单测。

这些用例不启动服务端，只验证最容易出错的纯逻辑：脱敏、空白保留、切换来源时删除
内联私钥、目标校验，以及能力清单与 manifest 一致。
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


class SshSettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {"QWENPAW_NODE_BRIDGE_DATA_DIR": self.temp_dir.name},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        main._delete_inline_key()

    def tearDown(self):
        main._delete_inline_key()

    def _write_key_file(self) -> Path:
        path = Path(self.temp_dir.name) / "id_ed25519"
        path.write_text("-----BEGIN TEST KEY-----\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_masked_hides_secrets(self):
        main._save_ssh_settings(
            {
                "host": "127.0.0.1",
                "port": 22,
                "user": "csdetr",
                "auth": "password",
                "password": "secret",
                "passphrase": "phrase",
            },
        )
        masked = main._masked_ssh_settings()
        self.assertNotIn("password", masked)
        self.assertNotIn("passphrase", masked)
        self.assertTrue(masked["password_configured"])
        self.assertTrue(masked["passphrase_configured"])
        self.assertTrue(masked["configured"])

    def test_blank_password_preserves_existing(self):
        main._save_ssh_settings(
            {
                "host": "127.0.0.1",
                "port": 22,
                "user": "csdetr",
                "auth": "password",
                "password": "secret",
            },
        )
        main._apply_ssh_settings({"auth": "password", "password": ""})
        self.assertEqual(main._load_ssh_settings()["password"], "secret")

    def test_switch_to_file_deletes_inline_key(self):
        main._write_inline_key("KEY")
        key_path = self._write_key_file()
        main._save_ssh_settings(
            {
                "host": "127.0.0.1",
                "port": 22,
                "user": "csdetr",
                "auth": "key",
                "key_source": "inline",
            },
        )
        main._apply_ssh_settings(
            {"auth": "key", "key_source": "file", "key_path": str(key_path)},
        )
        self.assertIsNone(main._read_inline_key())
        self.assertTrue(main._masked_ssh_settings()["key_configured"])

    def test_inline_key_requires_content(self):
        with self.assertRaises(ValueError):
            main._apply_ssh_settings(
                {
                    "host": "127.0.0.1",
                    "port": 22,
                    "user": "csdetr",
                    "auth": "key",
                    "key_source": "inline",
                },
            )

    def test_invalid_target_rejected(self):
        with self.assertRaises(ValueError):
            main._apply_ssh_settings({"port": 0, "user": "csdetr"})
        with self.assertRaises(ValueError):
            main._apply_ssh_settings({"port": 22, "user": ""})
        with self.assertRaises(ValueError):
            main._apply_ssh_settings(
                {
                    "host": "127.0.0.1",
                    "port": 22,
                    "user": "csdetr",
                    "auth": "key",
                    "key_source": "file",
                    "key_path": "/definitely/not/a/key",
                },
            )

    def test_credential_not_available_without_auth(self):
        with mock.patch(
            "qwenpaw.app.auth.is_auth_enabled",
            return_value=False,
        ):
            self.assertFalse(main._ssh_auth_enabled())

    @staticmethod
    def _http_request(headers=None, query=b"", client=("127.0.0.1", 50000)):
        raw_headers = [
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        ]
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": raw_headers,
            "query_string": query,
            "client": client,
        }
        return Request(scope)

    def test_http_auth_rejects_empty_bearer_when_enabled(self):
        request = self._http_request({"authorization": "Bearer "})
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                main._require_ssh_access(request)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_http_auth_accepts_valid_token_when_enabled(self):
        request = self._http_request({"authorization": "Bearer good"})
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=True):
            with mock.patch.object(main, "_ssh_token_user", return_value="user"):
                main._require_ssh_access(request)

    def test_http_auth_off_allows_private_client(self):
        request = self._http_request(client=("10.0.0.5", 50000))
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=False):
            main._require_ssh_access(request)

    def test_http_auth_off_rejects_public_client(self):
        request = self._http_request(client=("8.8.8.8", 50000))
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=False):
            with self.assertRaises(HTTPException) as ctx:
                main._require_ssh_access(request)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_http_auth_off_allows_trusted_header(self):
        request = self._http_request(
            headers={"x-qwenpaw-trusted-host": "1"},
            client=("8.8.8.8", 50000),
        )
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=False):
            main._require_ssh_access(request)

    def test_access_helper_shares_policy_with_ws(self):
        headers = {"x-qwenpaw-trusted-host": "1"}
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=False):
            self.assertTrue(
                main._ssh_access_allowed(
                    token=None,
                    client_host="8.8.8.8",
                    headers=headers,
                ),
            )
            self.assertFalse(
                main._ssh_access_allowed(
                    token=None,
                    client_host="8.8.8.8",
                    headers={},
                ),
            )

    def test_http_token_reads_query(self):
        request = self._http_request(query=b"token=good")
        self.assertEqual(main._http_token(request), "good")

    def test_http_token_prefers_bearer(self):
        request = self._http_request(
            {"authorization": "Bearer header-token"},
            query=b"token=query-token",
        )
        self.assertEqual(main._http_token(request), "header-token")

    def test_manifest_and_code_capabilities_match(self):
        manifest = json.loads(
            (ROOT_DIR / "plugin.json").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            tuple(manifest["meta"]["capabilities"]),
            tuple(main.CAPABILITIES),
        )
        self.assertEqual(manifest["entry"]["frontend"], "ui/index.js")
        self.assertTrue((ROOT_DIR / "ui" / "index.js").is_file())


if __name__ == "__main__":
    unittest.main()
