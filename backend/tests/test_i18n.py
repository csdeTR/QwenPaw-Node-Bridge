# -*- coding: utf-8 -*-
"""运行时语言判定与译文回退。当前只有中文。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import i18n  # noqa: E402  pylint: disable=wrong-import-position


class ResolveLangTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_dir = temp.name
        env = mock.patch.dict(
            os.environ,
            {"QWENPAW_NODE_BRIDGE_DATA_DIR": self.data_dir},
        )
        env.start()
        self.addCleanup(env.stop)
        console = mock.patch.object(i18n, "_console_language", return_value=None)
        console.start()
        self.addCleanup(console.stop)

    def test_defaults_to_chinese(self):
        self.assertEqual("zh", i18n.resolve_lang(None))

    def test_query_language_has_priority(self):
        request = SimpleNamespace(
            query_params={"lang": "zh-CN"},
            headers={"accept-language": "en-US"},
        )
        self.assertEqual("zh", i18n.resolve_lang(request))

    def test_accept_language_is_parsed(self):
        request = SimpleNamespace(
            query_params={},
            headers={"accept-language": "zh-Hans-CN,zh;q=0.9,en;q=0.8"},
        )
        self.assertEqual("zh", i18n.resolve_lang(request))

    def test_unknown_language_falls_back(self):
        request = SimpleNamespace(
            query_params={},
            headers={"accept-language": "fr-FR"},
        )
        self.assertEqual("zh", i18n.resolve_lang(request))


class BridgeLanguageTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_dir = temp.name
        env = mock.patch.dict(
            os.environ,
            {"QWENPAW_NODE_BRIDGE_DATA_DIR": self.data_dir},
        )
        env.start()
        self.addCleanup(env.stop)
        console = mock.patch.object(i18n, "_console_language", return_value=None)
        console.start()
        self.addCleanup(console.stop)

    def _write(self, payload):
        path = Path(self.data_dir) / "bridge_settings.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_bridge_language_precedes_request(self):
        self._write({"language": "en"})
        request = SimpleNamespace(
            query_params={"lang": "zh"},
            headers={"accept-language": "zh-CN"},
        )
        with mock.patch.object(i18n, "SUPPORTED", ("zh", "en")):
            self.assertEqual("en", i18n.resolve_lang(request))

    def test_bad_file_falls_back_to_request(self):
        (Path(self.data_dir) / "bridge_settings.json").write_text(
            "{not json",
            encoding="utf-8",
        )
        request = SimpleNamespace(query_params={"lang": "zh"}, headers={})
        self.assertEqual("zh", i18n.resolve_lang(request))

    def test_unsupported_bridge_language_is_ignored(self):
        self._write({"language": "fr"})
        self.assertEqual("zh", i18n.resolve_lang(None))


class TranslationTest(unittest.TestCase):
    def test_missing_translation_returns_source(self):
        self.assertEqual("原文", i18n.t("原文", "zh"))

    def test_registered_translation_is_used(self):
        i18n.TRANSLATIONS.setdefault("en", {})["原文"] = "source"
        try:
            self.assertEqual("source", i18n.t("原文", "en"))
        finally:
            i18n.TRANSLATIONS["en"].pop("原文", None)

    def test_formatting_is_optional(self):
        i18n.TRANSLATIONS.setdefault("en", {})["数量 {count}"] = "count {count}"
        try:
            self.assertEqual("count 3", i18n.t("数量 {count}", "en", count=3))
        finally:
            i18n.TRANSLATIONS["en"].pop("数量 {count}", None)


if __name__ == "__main__":
    unittest.main()
