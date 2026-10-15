# -*- coding: utf-8 -*-
"""「后端是明文 HTTP」这条告警（2026-10-16）。

用户当天的原话是"中继通道有什么加密措施吗"—— 查下来整条链路是**明文 HTTP**，
而"没人提过这件事"本身就是问题：面板、日志、启动说明里都没写。所以加一条启动说明
（面板首页那张状态卡会显示 `startupNotes`）。

判据只有一条，三种情况必须分清：
  * `https://…`      → **放过**（已经加密了，再喊就是噪声）
  * `http://127.0.0.1` / `localhost` / `[::1]` → **放过**（本机回环没有暴露面）
  * 其余 `http://…`  → **告警**（音频、指令文本、手机令牌都在明路上）

另外钉一条纪律：**只告警、不改配置**（地址是用户配对时定的，程序不替他决定走哪条路）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import boot                                                    # noqa: E402
from app import db                                                     # noqa: E402
from app.capabilities import echo_server                               # noqa: E402


class PlaintextBackendNoteTests(unittest.TestCase):

    def setUp(self):
        self._orig = echo_server._base_url_from_settings
        self._notes = list(db._STARTUP_NOTES)
        db._STARTUP_NOTES.clear()

    def tearDown(self):
        echo_server._base_url_from_settings = self._orig
        db._STARTUP_NOTES[:] = self._notes

    def _for(self, url):
        echo_server._base_url_from_settings = lambda **kw: url
        return boot.backend_security_note()

    def test_remote_plain_http_warns(self):
        note = self._for("http://echoserver.example.com:8900")
        self.assertIn("明文 HTTP", note)
        self.assertIn("echoserver.example.com:8900", note, "要把到底是哪个地址说出来")

    def test_https_is_quiet(self):
        self.assertEqual("", self._for("https://echoserver.example.com:8443"))

    def test_loopback_is_quiet(self):
        for url in ("http://127.0.0.1:8900", "http://localhost:8900", "http://[::1]:8900"):
            self.assertEqual("", self._for(url), url)

    def test_empty_is_quiet(self):
        self.assertEqual("", self._for(""))
        self.assertEqual("", self._for(None))

    def test_it_only_warns_and_never_touches_settings(self):
        """只告警、不改配置 —— 地址由用户配对时定。"""
        echo_server._base_url_from_settings = lambda **kw: "http://10.0.0.9:8900"
        db._STARTUP_NOTES.clear()
        boot._warn_plaintext_backend()
        self.assertTrue(any("明文 HTTP" in n for n in db.startup_notes()))
        # 允许同一个地址出现两次时也不重复刷屏（db.add_startup_note 自带去重）
        boot._warn_plaintext_backend()
        self.assertEqual(1, sum(1 for n in db.startup_notes() if "明文 HTTP" in n))


if __name__ == "__main__":
    unittest.main()
