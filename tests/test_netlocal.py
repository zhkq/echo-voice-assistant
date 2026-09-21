# -*- coding: utf-8 -*-
"""``app/netlocal.py`` 的测试：本机回环调用必须绕过系统代理。

背景（2026-09-22 同事实测反馈 B2）：公司机器上设了 `http_proxy`，ECHO 探 `127.0.0.1:43199`
时被代理劫走 → harness 一直显示 idle、token 拿不到；而 `curl --noproxy 127.0.0.1` 返回 401。
这类故障**没有报错**，只有"服务看起来没起来"，所以必须有测试钉住。
"""
import os
import unittest
import urllib.request
from unittest.mock import patch

from app import netlocal


class IsLoopbackTests(unittest.TestCase):

    def test_loopback_forms(self):
        for url in ("http://127.0.0.1:43199/",
                    "http://127.0.0.1/",
                    "http://localhost:8970/api/status",
                    "http://LOCALHOST/",
                    "http://[::1]:18061/health",
                    "http://127.1.2.3:1/",          # 127.0.0.0/8 整个网段
                    "https://127.0.0.1:30205/x"):
            self.assertTrue(netlocal.is_loopback(url), url)

    def test_external_hosts_are_not_loopback(self):
        for url in ("https://pypi.org/simple/",
                    "https://www.modelscope.cn",
                    "https://hf-mirror.com",
                    "http://192.168.1.10:8970/",     # 局域网 ≠ 本机
                    "http://10.13.35.58/"):
            self.assertFalse(netlocal.is_loopback(url), url)

    def test_garbage_does_not_raise(self):
        for value in ("", "not a url", None, 12345):
            self.assertFalse(netlocal.is_loopback(value))


class OpenerChoiceTests(unittest.TestCase):

    def test_loopback_uses_the_direct_opener(self):
        self.assertIs(netlocal.pick("http://127.0.0.1:1/x"), netlocal._DIRECT_OPENER)

    def test_external_uses_the_default_opener(self):
        self.assertIs(netlocal.pick("https://pypi.org/simple/"), netlocal._DEFAULT_OPENER)

    def test_direct_opener_carries_no_proxy(self):
        """关键：无代理 opener 里不能有**任何非空代理** —— 否则等于没绕过。

        注意别断言"一定有 1 个 ProxyHandler"：CPython 的 ``build_opener(ProxyHandler({}))``
        会把 ProxyHandler 整个跳过（``add_handler`` 忽略只有 ``proxy_open`` 的处理器），
        所以"没有 ProxyHandler"也是正确结果。要守的是"没有非空代理"。
        """
        for h in netlocal._DIRECT_OPENER.handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                self.assertEqual({}, h.proxies, "回环 opener 不能带任何代理")
        self.assertIsNot(netlocal._DIRECT_OPENER, netlocal._DEFAULT_OPENER,
                         "两个 opener 必须是不同实例，否则谈不上按 URL 分派")


class NoProxyEnvTests(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_fills_both_keys_and_is_idempotent(self):
        self.assertTrue(netlocal.ensure_no_proxy_env(), "第一次应当有改动")
        self.assertIn("127.0.0.1", os.environ["NO_PROXY"])
        self.assertIn("localhost", os.environ["no_proxy"])
        self.assertFalse(netlocal.ensure_no_proxy_env(), "第二次不该重复写")

    def test_existing_entries_are_kept(self):
        os.environ["NO_PROXY"] = "example.com"
        netlocal.ensure_no_proxy_env()
        self.assertIn("example.com", os.environ["NO_PROXY"], "用户自己配的域名不能丢")
        self.assertIn("127.0.0.1", os.environ["NO_PROXY"])


class InstallBypassTests(unittest.TestCase):
    """全局开关：装一次，之后所有 urllib.request.urlopen 的回环调用都绕过代理。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
        netlocal.uninstall_loopback_bypass()
        self.addCleanup(netlocal.uninstall_loopback_bypass)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_install_is_idempotent_and_restorable(self):
        self.assertTrue(netlocal.install_loopback_bypass())
        self.assertFalse(netlocal.install_loopback_bypass(), "重复装应当是空操作")
        self.assertTrue(netlocal.uninstall_loopback_bypass())

    def test_loopback_call_goes_through_the_direct_opener(self):
        netlocal.install_loopback_bypass()
        with patch.object(netlocal._DIRECT_OPENER, "open", return_value="DIRECT") as direct, \
                patch.object(netlocal._DEFAULT_OPENER, "open", return_value="PROXIED") as default:
            self.assertEqual(urllib.request.urlopen("http://127.0.0.1:43199/"), "DIRECT")
            direct.assert_called_once()
            default.assert_not_called()

    def test_external_call_still_goes_through_the_normal_path(self):
        """外部地址必须照旧（企业网访问 PyPI/ModelScope 往往正需要代理）。"""
        netlocal.install_loopback_bypass()
        with patch.object(netlocal._DIRECT_OPENER, "open") as direct, \
                patch.object(netlocal, "_ORIGINAL_URLOPEN", return_value="NORMAL") as normal:
            self.assertEqual(urllib.request.urlopen("https://pypi.org/simple/"), "NORMAL")
            normal.assert_called_once()
            direct.assert_not_called()

    def test_request_objects_are_recognised_too(self):
        netlocal.install_loopback_bypass()
        req = urllib.request.Request("http://localhost:18061/health", method="GET")
        with patch.object(netlocal._DIRECT_OPENER, "open", return_value="DIRECT") as direct:
            self.assertEqual(urllib.request.urlopen(req), "DIRECT")
            direct.assert_called_once()


if __name__ == "__main__":
    unittest.main()
