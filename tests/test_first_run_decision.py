# -*- coding: utf-8 -*-
"""首次启用向导的**判据**：首次安装要弹、升级安装不弹（2026-10-10 用户口径）。

要解决的问题（用户原话）：
  * 「恢复初次启用向导，用户第一次使用 echo 前要完成本向导」；
  * 「升级或者重装时……**如果是升级安装，安装后面板直接进入工作状态**，
     如果是首次安装，才会进入初次启用向导」；
  * 「向导入口对于升级用户别保留在顶部，在设置里就行」。

判据为什么不能"看数据目录里有没有东西"：**全新装完、服务一起来就会建库写设置**，
于是刚装好的机器看起来也像老机器。唯一知道真相的是安装器（它自己判断了是装进
空目录还是覆盖已有目录）→ 由它写 `install-mode.json`，这里只读它。

**还不能拿 `declared()` 当判据**（2026-09-21 那次回归的镜像）：技能装完会写
`install-report.json`，可技能**不负责**配笔记库与三个技能 —— 所以"技能刚装完的
全新机器"**仍然要弹**。这一条单独钉一个用例。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import install_state, paths                              # noqa: E402


class FirstRunDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-firstrun-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data, exist_ok=True)
        self.dsh = os.path.join(self.tmp, "dsh", "home")
        self.meetings = os.path.join(self.tmp, "meeting")
        # 只隔离两个登记文件与探测用的根；设置项单独在需要时打桩
        self._p = [patch.object(install_state, "_data_root", lambda: self.data)]
        for p in self._p:
            p.start()
            self.addCleanup(p.stop)

    # ---------------------------------------------------------------- 登记文件本身

    def test_mode_roundtrip_and_defaults(self):
        self.assertEqual(install_state.install_mode(), "", "没登记过就该是空串（不猜）")
        install_state.save_install_mode(install_state.MODE_FRESH, note="t")
        self.assertEqual(install_state.install_mode(), "fresh")
        install_state.save_install_mode(install_state.MODE_UPGRADE)
        self.assertEqual(install_state.install_mode(), "upgrade")

    def test_mode_rejects_unknown_values(self):
        for bad in ("", "new", "FRESHH", "upgraded"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    install_state.save_install_mode(bad)

    def test_broken_or_wrong_shaped_file_reads_as_unregistered(self):
        for payload in ("{ not json", '{"mode": 123}', '["fresh"]', '{"mode": "weird"}'):
            with self.subTest(payload=payload):
                with open(install_state.mode_path(), "w", encoding="utf-8") as fh:
                    fh.write(payload)
                self.assertEqual(install_state.install_mode(), "")

    def test_first_run_done_needs_an_explicit_true(self):
        self.assertFalse(install_state.first_run_done(), "没写过 = 没走完")
        # 写坏了/写成别的东西 → 一律当"没走完"（宁可再弹一次，也别让新用户永远配不上库）
        for payload in ("{}", '{"done": false}', '{"done": "yes"}', "{ not json"):
            with self.subTest(payload=payload):
                with open(install_state.first_run_path(), "w", encoding="utf-8") as fh:
                    fh.write(payload)
                self.assertFalse(install_state.first_run_done())
        install_state.mark_first_run_done()
        self.assertTrue(install_state.first_run_done())

    # ---------------------------------------------------------------- 该不该弹

    def _onboard(self, mode="", done=False, existing=None):
        existing = {"dsh": False, "db": False, "rows": 0, "meetings": 0, "vault": ""} \
            if existing is None else existing
        if mode:
            install_state.save_install_mode(mode)
        if done:
            install_state.mark_first_run_done()
        with patch.object(install_state, "existing_content", lambda: existing):
            return install_state.onboarding()

    def test_fresh_install_should_onboard(self):
        got = self._onboard(mode="fresh")
        self.assertTrue(got["shouldOnboard"], got)
        self.assertEqual(got["mode"], "fresh")
        self.assertFalse(got["firstRunDone"])

    def test_fresh_install_after_the_wizard_should_not_onboard(self):
        self.assertFalse(self._onboard(mode="fresh", done=True)["shouldOnboard"])

    def test_upgrade_install_never_onboards(self):
        """**用户的核心要求**：升级安装直接进工作状态，入口只在设置里。"""
        for done in (False, True):
            with self.subTest(done=done):
                got = self._onboard(mode="upgrade", done=done)
                self.assertFalse(got["shouldOnboard"], got)
                self.assertIn("升级", got["reason"])

    def test_upgrade_wins_even_if_the_machine_looks_empty(self):
        """升级判据优先于"看起来像新机器" —— 装进已有目录但数据被清过也要听登记。"""
        got = self._onboard(mode="upgrade", existing={"dsh": False, "db": False,
                                                     "rows": 0, "meetings": 0, "vault": ""})
        self.assertFalse(got["shouldOnboard"])

    def test_unregistered_machine_is_judged_by_what_is_already_there(self):
        """老版本升上来的机器没有登记 → 用「**真内容**」兜底。

        ⚠️ 2026-10-11 用户实测把这条改了：原来 `dsh` / `db` **在不在**也算升级 ——
        但**刚装完的机器一样有库**（服务一起来就建 `echo.db`）和 DSH 家目录，
        于是全新安装被判成升级、**向导不弹**（用户原话：「用交付包装完，但没看到向导，
        所以没法走昨晚设计的三步」）。现在只认"里面有没有真东西"：
        `rows`（命令/会议行数）/ `meetings`（会议目录数）/ `vault`（配过笔记库）。
        """
        empty = {"dsh": False, "db": False, "rows": 0, "meetings": 0, "vault": ""}
        self.assertTrue(self._onboard(existing=empty)["shouldOnboard"], "空目录 = 全新")
        # **刚装完**：库在、家目录在，但里面都是空的 → 必须算全新（该进向导）
        for key in ("dsh", "db"):
            with self.subTest(key=key):
                ex = dict(empty)
                ex[key] = True
                got = self._onboard(existing=ex)
                self.assertTrue(got["shouldOnboard"],
                                "刚装完（只有 %s）该进向导：%s" % (key, got))
        # 有**真内容**才算升级
        for key, val in (("rows", 3), ("meetings", 3), ("vault", "D:\\vault")):
            with self.subTest(key=key):
                ex = dict(empty)
                ex[key] = val
                got = self._onboard(existing=ex)
                self.assertFalse(got["shouldOnboard"], "有 %s 就是升级：%s" % (key, got))

    def test_skill_install_does_not_suppress_onboarding(self):
        """**钉住 2026-09-21 那个反例**：技能装完（declared 为真）在全新机器上**照样要弹**。

        技能负责装模型/引擎，**不负责**配笔记库与那三个技能；拿 `declared()` 当首装判据，
        就会让新用户永远配不上这两样（正是本轮要修的问题）。
        """
        install_state.save_report({"components": ["stt-sherpa"]})
        self.assertTrue(install_state.declared(), "技能登记过")
        # 探测也要打桩：否则会读到**开发机自己**的库/会议，"全新机器"就不成立了
        empty = {"dsh": False, "db": False, "rows": 0, "meetings": 0, "vault": ""}
        with patch.object(install_state, "existing_content", lambda: empty):
            self.assertTrue(install_state.onboarding()["shouldOnboard"],
                            "全新机器 + 技能装完 → 仍要进首次启用向导")


class ExistingContentProbeTests(unittest.TestCase):
    """探测既有内容：**只读、绝不抛**（它是"这是升级"的旁证，也可能在坏环境里跑）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-existing-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        self.dsh = os.path.join(self.tmp, "dsh", "home")
        self.meetings = os.path.join(self.tmp, "meeting")
        os.makedirs(self.data, exist_ok=True)

    def _run(self, vault=""):
        class _S:
            def get(self, key, *a, **kw):
                return vault if key == "worklogVaultRoot" else ""

        with patch("app.paths.data_root", lambda: self.data), \
             patch("app.paths.dsh_home_root", lambda: self.dsh), \
             patch("app.paths.meetings_root", lambda: self.meetings), \
             patch("app.config.settings", _S()):
            return install_state.existing_content()

    def test_nothing_there_is_not_an_error(self):
        got = self._run()
        self.assertFalse(got["dsh"])
        self.assertFalse(got["db"])
        self.assertEqual(got["meetings"], 0)

    def test_it_sees_dsh_home_meetings_and_vault(self):
        os.makedirs(os.path.join(self.dsh, "skills"))
        os.makedirs(os.path.join(self.meetings, "2026-09-18_10-00-00"))
        os.makedirs(os.path.join(self.meetings, "2026-09-19_10-00-00"))

        got = self._run(vault=r"C:\somewhere\notes")

        self.assertTrue(got["dsh"], "有 DSH_HOME/skills 就算已有 DSH")
        self.assertEqual(got["meetings"], 2, "会议数要数出来（会议数据是事故级，必须能核对）")
        self.assertEqual(got["vault"], r"C:\somewhere\notes")

    def test_a_db_missing_our_tables_still_counts_as_existing(self):
        import sqlite3
        db = os.path.join(self.data, "echo.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE other (x)")     # 不是我们的表 → COUNT 会抛，但**不该炸**
        con.commit()
        con.close()

        got = self._run()

        self.assertTrue(got["db"])
        self.assertEqual(got["rows"], 0)


if __name__ == "__main__":
    unittest.main()


class EmptyInstallIsNotAnUpgradeTests(unittest.TestCase):
    """**刚装完的机器不能被判成升级**（2026-10-11 用户实测：装完没看到向导）。

    根因：兜底判据原来只看 `data/echo.db` **在不在** —— 而服务一起来就会建库，
    于是全新安装被误判成升级、向导不弹。判据必须看**里面有没有真东西**。
    """

    def _onboard(self, *, rows=0, meetings=0, vault="", db=True, dsh=True):
        empty = {"dsh": dsh, "db": db, "rows": rows, "meetings": meetings, "vault": vault}
        with patch.object(install_state, "install_mode", lambda path="": ""), \
                patch.object(install_state, "first_run_done", lambda path="": False), \
                patch.object(install_state, "existing_content", lambda: dict(empty)):
            return install_state.onboarding()

    def test_an_empty_db_is_not_an_old_machine(self):
        got = self._onboard()                      # 库在、家目录在，但里面是空的
        self.assertTrue(got["shouldOnboard"], got)
        self.assertIn("全新", got["reason"])

    def test_real_data_still_means_upgrade(self):
        for kw in ({"rows": 3}, {"meetings": 1}, {"vault": "D:\\vault"}):
            with self.subTest(**kw):
                self.assertFalse(self._onboard(**kw)["shouldOnboard"],
                                 "有真数据 / 配过笔记库 → 按升级处理（用户口径）")


if __name__ == "__main__":
    unittest.main()
