# -*- coding: utf-8 -*-
"""智能体（DSH / CodeBuddy）自己那份配置的守卫测试（2026-09-19）。

背景：用户实测截图里，设置页「面板与服务」下的「DSH 服务地址」与上面「智能体」表格里
DSH 那一行显示的是同一个地址（那一行已经写着"API 可访问（http://127.0.0.1:43120）"），
原话："下面的 dsh 没必要吧，或者把端口挪上去"。

做法：这些键（`dshBaseUrl` / `agentCustomPath` / `agentCodebuddyEnabled`）归**智能体自己**，
在 `DEFAULTS` 里标 `hidden`（不占设置页分组），值由适配器声明（`settings_keys`）、
经 `GET /api/agents` 的 `settings` 字段下发给面板的展开区。
本文件钉住这条链路与几个容易漏的不变量。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                              # noqa: E402
from app import agents                                           # noqa: E402
from app.config import DEFAULTS, settings                        # noqa: E402


def _owner_map():
    """配置键 -> 声明它的智能体（适配器的 settings_keys）。"""
    owners = {}
    for cls in agents.specs():
        for key in (getattr(cls, "settings_keys", ()) or ()):
            owners.setdefault(key, []).append(cls.name)
    return owners


class AgentSettingsOwnershipTests(unittest.TestCase):
    def test_every_declared_key_exists_and_is_hidden(self):
        for key, owners in _owner_map().items():
            with self.subTest(key=key):
                self.assertIn(key, DEFAULTS, "适配器声明了不存在的配置键：%s" % key)
                meta = DEFAULTS[key]
                self.assertTrue(meta.get("hidden"),
                                "%s 应由智能体展开区承载（hidden），不该出现在设置页分组里" % key)
                self.assertEqual(meta["grp"], "agent",
                                 "%s 归属智能体分组（面板用 grp=agent 做回退判断）" % key)

    def test_secret_keys_are_declared_but_never_served(self):
        """`harnessToken` 这类密钥可以写在 settings_keys 里（适配器要用它连服务），
        但 `agent_settings()` 必须把它挡在接口之外 —— 面板永远看不到明文。"""
        from app import agents
    def test_secret_keys_are_declared_and_only_report_whether_set(self):
        """密钥（`harnessToken`）要能被面板编辑，但**值永不出接口**。

        2026-09-19 用户实测问"我在哪里配置 key"：原来这类键被整条跳过，于是错误提示让人
        "把 token 填到「harness 访问 token」里"，而面板上根本没有那一栏。
        现在的契约：行照发（面板渲染成密码框），但只带 `secret=True` + `hasValue`，`value` 恒为空。
        """
        secrets = [(k, o) for k, o in _owner_map().items() if DEFAULTS[k].get("secret")]
        self.assertTrue(secrets, "至少要有一个密钥项被这条用例覆盖到（harnessToken）")
        for key, owners in secrets:
            with self.subTest(key=key):
                self.assertTrue(any(key in (getattr(cls, "settings_keys", ()) or ())
                                    for cls in agents.specs()),
                                "%s 应由某个适配器声明" % key)
                declared = [cls for cls in agents.specs()
                            if key in (getattr(cls, "settings_keys", ()) or ())]
                for cls in declared:
                    rows = {s["key"]: s for s in agents.agent_settings(cls)}
                    self.assertIn(key, rows, "%s 应作为密钥行下发（面板需要那一栏）" % key)
                    row = rows[key]
                    self.assertTrue(row.get("secret"))
                    self.assertFalse(row.get("value"), "密钥的值不许出接口")
                    self.assertIn("hasValue", row, "面板要靠它显示「已配置/未配置」")

    def test_no_key_is_claimed_by_two_agents(self):
        for key, owners in _owner_map().items():
            with self.subTest(key=key):
                self.assertEqual(len(owners), 1, "%s 被多个智能体声明：%s" % (key, owners))

    def test_dsh_service_address_moved_out_of_the_panel_group(self):
        """它就是这条需求本身：不再是「面板与服务」里的一行。"""
        self.assertEqual(DEFAULTS["dshBaseUrl"]["grp"], "agent")
        self.assertTrue(DEFAULTS["dshBaseUrl"]["hidden"])
        self.assertIn("dshBaseUrl", _owner_map(), "DSH 适配器要声明它（否则面板拿不到值）")


class AgentSettingsPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-agent-")
        cls._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "test.db")
        db.init()
        settings.seed_defaults()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _agents(self):
        return {a["name"]: a for a in self.client.get("/api/agents").json()["agents"]}

    def test_each_agent_carries_its_own_settings(self):
        got = self._agents()
        dsh = {s["key"] for s in got["dsh"]["settings"]}
        self.assertEqual(dsh, {"dshBaseUrl"})
        cb = {s["key"] for s in got["codebuddy"]["settings"]}
        self.assertIn("agentCustomPath", cb)

    def test_values_are_readable_with_panel_metadata(self):
        row = {s["key"]: s for s in self._agents()["dsh"]["settings"]}["dshBaseUrl"]
        self.assertEqual(row["value"], settings.get("dshBaseUrl"))
        self.assertTrue(row["value"], "默认值不该是空的（否则面板展开区一片空白）")
        self.assertEqual(row["value_type"], "str")
        self.assertTrue(row["label"] and row["description"])

    def test_these_keys_stay_out_of_the_generic_settings_list(self):
        served = {s["key"] for s in self.client.get("/api/settings").json()["settings"]}
        for key in _owner_map():
            with self.subTest(key=key):
                self.assertNotIn(key, served, "%s 不该出现在设置页表单里" % key)

    def test_edits_round_trip_through_the_agents_api(self):
        r = self.client.put("/api/settings", json={"values": {"dshBaseUrl": "http://127.0.0.1:43999"}})
        self.assertEqual(r.status_code, 200, r.text)
        row = {s["key"]: s for s in self._agents()["dsh"]["settings"]}["dshBaseUrl"]
        self.assertEqual(row["value"], "http://127.0.0.1:43999")
        self.assertEqual(settings.get("dshBaseUrl"), "http://127.0.0.1:43999")


class AgentPanelWiringTests(unittest.TestCase):
    """面板侧：展开区的字段来自 agents 的 settings（不再靠 hidden 键的 settingsValue）。"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "app.js"), encoding="utf-8") as fh:
            cls.js = fh.read()

    def test_detail_renders_fields_from_the_agents_payload(self):
        """**意图**：展开区必须按后端给的 `settings` 逐项渲染（否则 DSH 地址 / CLI 路径没入口）。

        2026-09-25：设置区改版后这一行变成 `(cur.settings || []).filter(...).map(...)`
        （要跳过 harnessHome —— 它已经在上面单独成行），**行为没变**，变的是字面表达式。
        所以这里断言"确实拿 `cur.settings` 去遍历"，中间允许夹一层 filter；
        只钉死字面串会让每次无害重构都误报（这次就是）。
        """
        self.assertIn("(cur.settings || [])", self.js,
                      "展开区要按后端给的 settings 渲染（否则 DSH 地址/CLI 路径没入口）")
        self.assertRegex(self.js, r"\(cur\.settings \|\| \[\]\)[\s\S]{0,120}?\.map\(",
                         "必须真的遍历它逐项渲染（允许中间夹 filter 之类的筛选）")
        self.assertIn('data-agent-field="${esc(s.key)}"', self.js)

    def test_hidden_key_lookup_is_gone(self):
        """`settingsValue()` 读的是 /api/settings，而 hidden 键根本不在里面 —— 那个回填是坏的。"""
        self.assertNotIn("settingsValue", self.js)
        self.assertNotIn('settingsValue("agentCustomPath")', self.js)


if __name__ == "__main__":
    unittest.main()
