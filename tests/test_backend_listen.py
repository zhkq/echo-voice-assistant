# -*- coding: utf-8 -*-
"""tests/test_backend_listen.py — 「开启局域网访问」是**配置**，不是手工命令行（用户 2026-10-05 要求）。

用户原话："把开启局域网访问变成配置"。

为什么这件事值得钉死：在它之前，想让同事连你这台 GPU 只能**手工**用 CLI
（`server.main --listen 0.0.0.0:8900`）起后端 —— 而 `backend_setup.configure()` 每次启动都会用
`127.0.0.1` 重写 `server.yaml`，于是 ECHO 一重启（或面板上点一下「起本机后端」）就会**再起一个
回环的**，与手工那个**并存**（Windows 上 `127.0.0.1` 与 `0.0.0.0` 能同时绑在 8900 上），
变成两个进程抢同一份模型/显卡（2026-10-05 当场发生过一次）。

三条硬指标：
  1. 设置 `capabilityBackendListen` 存在、**隐藏**、默认 `127.0.0.1`（默认即最安全）；
  2. 它只影响 `listen:` 的**地址**；`admin_listen` **永远**是回环（管理面不对外是设计）；
  3. 端口**不变**：仍是出厂 `DEFAULT_PORT`（用户明确说过不改端口 —— 端口一改，"一处权威 =
     生成的 yaml"那条判据会与客户端设置打架）。
"""
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import backend_proc, backend_setup   # noqa: E402
from app.config import settings               # noqa: E402


def _read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class SettingTests(unittest.TestCase):

    def test_the_setting_exists_and_is_hidden_and_safe_by_default(self):
        src = _read("app", "config.py")
        self.assertIn('"capabilityBackendListen"', src)
        blk = src[src.index('"capabilityBackendListen"'):]
        blk = blk[:blk.index("value_type=")]
        self.assertIn("hidden=True", blk, "这种能把自己暴露到局域网的开关要藏在隐藏设置里")
        self.assertIn('value="127.0.0.1"', blk, "默认必须是只本机")
        self.assertIn("局域网", blk, "说明书里要说清它意味着什么")
        self.assertIn("配对码", blk, "也要说清开了之后谁能用你的显卡")
        # 真值来源：只断言"有这个键、是个非空字符串" —— **不要**断言活值等于默认值：
        # 那是**用户状态**（开发机上早就被设成 0.0.0.0 了），钉它会让用例跟着本机漂移。
        live = settings.get("capabilityBackendListen")
        self.assertIsInstance(live, str)
        self.assertTrue(live.strip(), "这一项不该是空串（空串等于没配，会落回默认）")


class ListenHostTests(unittest.TestCase):

    def _with(self, value):
        with mock.patch.object(settings, "get",
                               side_effect=lambda k, d=None: value if k == "capabilityBackendListen" else d):
            return backend_setup.listen_host()

    def test_default_is_loopback(self):
        self.assertEqual("127.0.0.1", self._with(""))
        self.assertEqual("127.0.0.1", self._with("   "))

    def test_it_takes_the_address_and_ignores_any_port_written_with_it(self):
        self.assertEqual("0.0.0.0", self._with("0.0.0.0"))
        self.assertEqual("192.168.1.170", self._with("192.168.1.170"))
        # 与命令行 --listen 同形（host:port）也要能吃下 —— 但端口仍用出厂那个
        self.assertEqual("0.0.0.0", self._with("0.0.0.0:8900"))
        self.assertEqual("192.168.1.170", self._with("192.168.1.170:9999"))

    def test_it_never_raises(self):
        with mock.patch.object(settings, "get", side_effect=RuntimeError("boom")):
            self.assertEqual("127.0.0.1", backend_setup.listen_host())


class RenderConfigTests(unittest.TestCase):
    """`render_config` 是**纯函数**（不读不写盘），正好用来钉 yaml 的内容。"""

    def test_default_stays_loopback(self):
        text = backend_setup.render_config()
        self.assertIn('listen: "127.0.0.1:%d"' % backend_proc.DEFAULT_PORT, text)
        self.assertIn('admin_listen: "127.0.0.1:%d"' % backend_proc.DEFAULT_ADMIN_PORT, text)

    def test_lan_bind_changes_only_the_capability_face(self):
        text = backend_setup.render_config(bind_host="0.0.0.0")
        self.assertIn('listen: "0.0.0.0:%d"' % backend_proc.DEFAULT_PORT, text,
                      "能力面要按配置绑到 0.0.0.0（局域网可达）")
        self.assertIn('admin_listen: "127.0.0.1:%d"' % backend_proc.DEFAULT_ADMIN_PORT, text,
                      "**管理面永远只绑回环** —— 这是设计，不许跟着一起放开")

    def test_the_port_is_never_taken_from_the_setting(self):
        """填 `0.0.0.0:9999` 也只换地址：端口仍是出厂那个（用户明确说不改端口）。"""
        with mock.patch.object(settings, "get",
                               side_effect=lambda k, d=None: "0.0.0.0:9999"
                               if k == "capabilityBackendListen" else d):
            host = backend_setup.listen_host()
        text = backend_setup.render_config(bind_host=host)
        self.assertIn('listen: "0.0.0.0:%d"' % backend_proc.DEFAULT_PORT, text)
        self.assertNotIn("9999", text, "端口不许从这一项里被改掉")

    def test_configure_writes_the_configured_bind(self):
        """**行为**钉子：真正写盘那条路必须按配置写。

        为什么不能用"源码里有没有 `bind_host=listen_host()`"来钉（我第一版就是那样，结果
        **漏掉了真 bug**）：2026-10-05 那行被插到了调用**外面**，成了"给局部变量 bind_host
        赋值一个元组"（`x=...,` 在 Python 里合法），于是配置**完全不生效**，
        而语法检查 / 导入 / 字符串断言**全都绿**。所以这里真写一次、真读一次。
        """
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="echo-cfg-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfgfile = os.path.join(tmp, "server.yaml")
        with mock.patch.object(backend_setup, "config_path", lambda: cfgfile), \
                mock.patch.object(backend_setup, "backend_root", lambda: tmp), \
                mock.patch.object(backend_setup, "state_root", lambda: os.path.join(tmp, "state")), \
                mock.patch.object(backend_setup, "tmp_root", lambda: os.path.join(tmp, "tmp")), \
                mock.patch.object(backend_setup, "cache_root", lambda: os.path.join(tmp, "cache")), \
                mock.patch.object(backend_setup, "ensure_secret", lambda: ("s3cret", "")), \
                mock.patch.object(settings, "get",
                                  side_effect=lambda k, d=None: "0.0.0.0"
                                  if k == "capabilityBackendListen" else d):
            backend_setup.configure()
        text = io.open(cfgfile, encoding="utf-8").read()
        self.assertIn('listen: "0.0.0.0:%d"' % backend_proc.DEFAULT_PORT, text,
                      "configure() 写盘时必须把配置传进 render_config（否则就是设了不生效）")
        self.assertIn('admin_listen: "127.0.0.1:%d"' % backend_proc.DEFAULT_ADMIN_PORT, text,
                      "管理面不受这一项影响")


if __name__ == "__main__":
    unittest.main()
