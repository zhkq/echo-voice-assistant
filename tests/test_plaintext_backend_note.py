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

## 2026-10-16 第二轮：**这条告警必须看"实际用于网络调用"的那个地址**

第一版取的是 `echo_server._base_url_from_settings(use_credentials=False)` —— 那个开关
**只看隐藏设置** `capabilityEchoServerUrl`。真机上那一栏要么是空的、要么还留着本机
`http://127.0.0.1:8900`（本机配对那一档的旧值），而**真正在用的**是配对文件
`{DATA}/backend.json` 里那台远端明文 http（`http://echoserver.zhkq.online:8900`，
2026-10-16 01:28 的凭据备份可以逐字对上）→ 判据落在"空/回环"上 → **静默**：
告警部署了却一次都没出现。

所以 `AddressSourceTests` 这一组不再打桩解析器，而是把**两个来源**（隐藏设置、配对文件）
按真机那样摆好，走 `_base_url_from_settings()` 这条**真路** —— 它与
`client_from_settings()` / `EchoServerClient.base_url` / `backend_endpoint()` 是同一份解析。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import boot                                                    # noqa: E402
from app import db                                                     # noqa: E402
from app.capabilities import credentials                               # noqa: E402
from app.capabilities import echo_server                               # noqa: E402
from app.capabilities import pairing                                   # noqa: E402

#: 真机上的「本机配对文件」（`capabilityLocalPairPath` 那份 `local-pair.json`）写的就是这个。
_LOCAL_PAIR_URL = "http://127.0.0.1:8900"


class PlaintextBackendNoteTests(unittest.TestCase):
    """判据本身：https / 回环 / 空 放过，其余 `http://` 告警（把解析器换成替身）。"""

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


class AddressSourceTests(unittest.TestCase):
    """地址**从哪来**：必须走"实际用于网络调用"的那条路（真机缺口，见模块 docstring）。

    这里不碰这台机器真实的库与凭据：`echo_server` 里那两个来源（`_setting` / `_creds`）
    都被换成替身，走的是**真的** `_base_url_from_settings()`。
    """

    def setUp(self):
        self._orig = (echo_server._setting, echo_server._creds,
                      pairing.local_pair_state, echo_server._base_url_from_settings)
        self._notes = list(db._STARTUP_NOTES)
        db._STARTUP_NOTES.clear()

    def tearDown(self):
        (echo_server._setting, echo_server._creds, pairing.local_pair_state,
         echo_server._base_url_from_settings) = self._orig
        db._STARTUP_NOTES[:] = self._notes

    def _machine(self, *, setting="", paired_url=""):
        """把两个来源按真机那样摆好：隐藏设置 + 配对文件（`{DATA}/backend.json`）。

        顺带把「本机配对文件」（`local-pair.json`）也摆成真机那样 —— 它写的是
        `http://127.0.0.1:8900`，而**网络调用根本不看它**；昨晚就是这一份把人带沟里的。
        """
        def _get(key, default=None):
            return setting if key == "capabilityEchoServerUrl" else default
        echo_server._setting = _get
        c = (credentials.BackendCredentials(base_url=paired_url, client_id="cli-1",
                                           secret="sec", server_name="gpu-office")
             if paired_url else None)
        echo_server._creds = lambda: c
        pairing.local_pair_state = lambda: {
            "found": True, "path": r"D:\ECHO\backend\state\local-pair.json",
            "baseUrl": _LOCAL_PAIR_URL, "expired": False, "expiresAt": 0.0, "message": ""}
        return c

    def test_the_local_pair_file_does_not_hide_a_remote_plaintext_backend(self):
        """**昨晚的真实现场**：本机配对文件是 127.0.0.1，实际用的却是远端明文 http。

        旧实现传了 `use_credentials=False`（只看隐藏设置），这台机器上那是**空串**
        → 告警被静默掉。这条用例在旧实现上必红。
        """
        self._machine(setting="", paired_url="http://echoserver.zhkq.online:8900")
        note = boot.backend_security_note()
        self.assertIn("明文 HTTP", note, "实际用的就是明文 http，不许静默")
        self.assertIn("echoserver.zhkq.online:8900", note)

    def test_an_https_pairing_is_quiet(self):
        """同一个来源、换成 https（TLS 落地之后就是这台机器的现状）→ 放过。"""
        self._machine(setting="", paired_url="https://echoserver.zhkq.online:8900")
        self.assertEqual("", boot.backend_security_note())

    def test_a_machine_that_really_only_talks_to_its_own_loopback_is_quiet(self):
        """真的只连本机回环（设置空 + 配对文件就是 127.0.0.1）→ **静默**（保留原判据）。"""
        self._machine(setting="", paired_url=_LOCAL_PAIR_URL)
        self.assertEqual("", boot.backend_security_note())

    def test_the_setting_is_what_actually_gets_used_so_it_wins(self):
        """设置里填的那一栏就是**实际会用的**地址（`_base_url_from_settings` 的顺序：
        显式配置优先，其次配对时的那个）。所以判据必须跟着它，不能反过来。"""
        self._machine(setting="http://10.0.0.9:8900",
                      paired_url="https://echoserver.zhkq.online:8900")
        note = boot.backend_security_note()
        self.assertIn("10.0.0.9:8900", note)

    def test_it_asks_for_the_address_the_way_real_calls_do(self):
        """接线判据：**不许**再传 `use_credentials=False`（那会让真机上的配对地址读不到）。"""
        seen = []

        def spy(*a, **kw):
            seen.append(kw)
            return "http://echoserver.zhkq.online:8900"

        echo_server._base_url_from_settings = spy        # tearDown 会还原真的那个
        note = boot.backend_security_note()
        self.assertIn("明文 HTTP", note)
        self.assertEqual(1, len(seen), "地址只该解析一次")
        self.assertIsNot(seen[0].get("use_credentials"), False,
                         "又只看隐藏设置了：真机上配对文件里的地址永远读不到（2026-10-16）")


if __name__ == "__main__":
    unittest.main()
