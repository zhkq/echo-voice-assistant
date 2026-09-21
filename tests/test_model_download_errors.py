# -*- coding: utf-8 -*-
"""A4：模型下载失败要给出**能照着做**的人话（同事 2026-09-21 反馈）。

同事在公司网里下载模型，界面上只看到 `SSLError: certificate verify failed`，
于是自己去设了 `HF_HUB_VERIFY=0`（关掉 TLS 校验）—— 那不该是交付路径。
这里的约定是：源本身已经"先 ModelScope、失败再 HF"（`_snapshot`），
两个源都挂掉时**再附一句怎么办**，而不是把两段英文异常直接丢给用户。
"""

import ssl
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import app.modelinfo as mi


def _fake_module(name, exc):
    mod = types.ModuleType(name)

    def snapshot_download(*_a, **_kw):
        raise exc

    mod.snapshot_download = snapshot_download
    return mod


class ExplainTests(unittest.TestCase):
    def test_certificate_failure_points_at_mirror_not_disabling_tls(self):
        tip = mi.explain_download_error(
            "SSLError: HTTPSConnectionPool: certificate verify failed: self signed certificate")
        self.assertIn("证书", tip)
        self.assertIn("不要关校验", tip)          # 别再引导用户关 TLS

    def test_proxy_and_network_and_auth_are_distinguished(self):
        self.assertIn("代理", mi.explain_download_error("ProxyError: 407 Proxy Authentication Required"))
        self.assertIn("网络", mi.explain_download_error("Max retries exceeded with url /tmp"))
        self.assertIn("登录", mi.explain_download_error("401 Client Error: Unauthorized"))
        self.assertIn("拒绝", mi.explain_download_error("403 Client Error: Forbidden for url"))

    def test_unknown_error_says_nothing(self):
        self.assertEqual(mi.explain_download_error(""), "")
        self.assertEqual(mi.explain_download_error("KeyboardInterrupt"), "")

    def test_case_insensitive(self):
        self.assertTrue(mi.explain_download_error("CERTIFICATE_VERIFY_FAILED"))


class SnapshotFallbackTests(unittest.TestCase):
    def test_modelscope_success_does_not_touch_hf(self):
        mod = types.ModuleType("modelscope")
        calls = []

        def ok(ms_id, local_dir=None, allow_patterns=None):
            calls.append(ms_id)
            return local_dir

        mod.snapshot_download = ok
        with patch.dict(sys.modules, {"modelscope": mod}):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(mi._snapshot(ms_id="a/b", hf_id="c/d", local_dir=tmp), "modelscope")
        self.assertEqual(calls, ["a/b"])

    def test_both_fail_appends_actionable_tip(self):
        err = ssl.SSLError("certificate verify failed: self signed certificate")
        fakes = {"modelscope": _fake_module("modelscope", err),
                 "huggingface_hub": _fake_module("huggingface_hub", err)}
        with patch.dict(sys.modules, fakes):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError) as ctx:
                    mi._snapshot(ms_id="a/b", hf_id="c/d", local_dir=tmp)
        msg = str(ctx.exception)
        self.assertIn("ModelScope(a/b)", msg)          # 两个源的原因都留着，便于排障
        self.assertIn("HF(c/d)", msg)
        self.assertIn("怎么办", msg)
        self.assertIn("证书", msg)

    def test_no_source_at_all(self):
        with self.assertRaises(RuntimeError) as ctx:
            mi._snapshot()
        self.assertIn("没有可用的下载源", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
