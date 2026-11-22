# -*- coding: utf-8 -*-
"""敏感端点访问策略：来源网段、两侧受信任地址、反代与文件权限。"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402  pylint: disable=wrong-import-position


class SourceClassTest(unittest.TestCase):
    def test_private_and_mesh_ranges(self):
        for host in ("10.0.0.5", "172.20.0.1", "192.168.1.1", "127.0.0.1", "100.64.0.1"):
            self.assertTrue(main._is_private_client(host), host)
        for host in ("fd00::1", "fe80::1", "::1"):
            self.assertTrue(main._is_private_client(host), host)
        self.assertFalse(main._is_private_client("8.8.8.8"))

    def test_extra_cidrs(self):
        with mock.patch.dict(
            os.environ,
            {"QWENPAW_BRIDGE_TRUSTED_CIDRS": "203.0.113.0/24,bogus"},
        ):
            self.assertTrue(main._is_private_client("203.0.113.9"))
            self.assertFalse(main._is_private_client("8.8.8.8"))


class TrustedAddressTest(unittest.TestCase):
    def setUp(self):
        self.trusted = mock.patch.object(
            main,
            "_load_trusted_addresses",
            return_value=[
                {"host": "chat.example.com", "source_cidr": "8.8.8.0/24"},
                {"host": "nomatch.example.com", "source_cidr": None},
            ],
        )
        self.trusted.start()
        self.addCleanup(self.trusted.stop)

    def test_host_and_source_must_both_match(self):
        headers = {"host": "chat.example.com"}
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=False):
            self.assertTrue(
                main._sensitive_access_allowed(
                    token=None,
                    client_host="8.8.8.9",
                    headers=headers,
                ),
            )
            self.assertFalse(
                main._sensitive_access_allowed(
                    token=None,
                    client_host="1.1.1.1",
                    headers=headers,
                ),
            )

    def test_entry_without_source_does_not_allow_public(self):
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=False):
            self.assertFalse(
                main._sensitive_access_allowed(
                    token=None,
                    client_host="8.8.8.9",
                    headers={"host": "nomatch.example.com"},
                ),
            )

    def test_private_source_is_always_allowed(self):
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=False):
            self.assertTrue(
                main._sensitive_access_allowed(
                    token=None,
                    client_host="10.0.0.5",
                    headers={"host": "anything.example.com"},
                ),
            )

    def test_auth_enabled_requires_token(self):
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=True):
            with mock.patch.object(main, "_ssh_token_user", return_value=None):
                self.assertFalse(
                    main._sensitive_access_allowed(
                        token=None,
                        client_host="10.0.0.5",
                        headers={},
                    ),
                )
            with mock.patch.object(main, "_ssh_token_user", return_value="alice"):
                self.assertTrue(
                    main._sensitive_access_allowed(
                        token="good",
                        client_host="10.0.0.5",
                        headers={},
                    ),
                )


class ProxyTest(unittest.TestCase):
    def test_forwarded_headers_without_opt_in_fail_closed(self):
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=False):
            with mock.patch.dict(os.environ, {"QWENPAW_BRIDGE_TRUST_PROXY": ""}):
                self.assertFalse(
                    main._sensitive_access_allowed(
                        token=None,
                        client_host="127.0.0.1",
                        headers={
                            "host": "chat.example.com",
                            "x-forwarded-for": "8.8.8.8",
                        },
                    ),
                )

    def test_forwarded_client_is_used_when_opted_in(self):
        with mock.patch.object(main, "_ssh_auth_enabled", return_value=False):
            with mock.patch.dict(os.environ, {"QWENPAW_BRIDGE_TRUST_PROXY": "1"}):
                with mock.patch.object(
                    main,
                    "_load_trusted_addresses",
                    return_value=[
                        {"host": "chat.example.com", "source_cidr": "8.8.8.0/24"},
                    ],
                ):
                    self.assertTrue(
                        main._sensitive_access_allowed(
                            token=None,
                            client_host="127.0.0.1",
                            headers={
                                "host": "chat.example.com",
                                "x-forwarded-for": "8.8.8.9",
                            },
                        ),
                    )


class ValidateAddressesTest(unittest.TestCase):
    def test_normalizes_and_accepts(self):
        items = main._validate_trusted_addresses(
            {"items": [{"host": "HTTPS://Chat.Example.com:8443/x", "source_cidr": "8.8.8.0/24"}]},
        )
        self.assertEqual(
            [{"host": "chat.example.com", "source_cidr": "8.8.8.0/24"}],
            items,
        )

    def test_rejects_invalid_cidr_and_duplicates(self):
        with self.assertRaises(ValueError):
            main._validate_trusted_addresses(
                {"items": [{"host": "a.example.com", "source_cidr": "not-a-cidr"}]},
            )
        with self.assertRaises(ValueError):
            main._validate_trusted_addresses(
                {
                    "items": [
                        {"host": "a.example.com", "source_cidr": "8.8.8.0/24"},
                        {"host": "a.example.com", "source_cidr": "1.1.1.0/24"},
                    ],
                },
            )


class FileModeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {"QWENPAW_NODE_BRIDGE_DATA_DIR": self.temp_dir.name},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_credential_files_are_0600_and_dir_0700(self):
        main._save_ssh_settings({"host": "h"})
        main._save_trusted_addresses([{"host": "h", "source_cidr": None}])
        main._write_inline_key("key-material")
        root = Path(self.temp_dir.name)
        self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
        for name in ("ssh_settings.json", "trusted_addresses.json", "ssh_key"):
            self.assertEqual(
                0o600,
                stat.S_IMODE((root / name).stat().st_mode),
                name,
            )


class SelfHeadersTest(unittest.TestCase):
    def test_does_not_forward_trusted_header(self):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-qwenpaw-trusted-host", b"1")],
            "query_string": b"",
            "client": ("8.8.8.8", 12345),
        }
        headers = main._self_headers(Request(scope))
        self.assertNotIn("x-qwenpaw-trusted-host", headers)


if __name__ == "__main__":
    unittest.main()
