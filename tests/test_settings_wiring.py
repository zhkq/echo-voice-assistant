# -*- coding: utf-8 -*-
"""设置接线验证（2026-09-19 设置菜单重设计）

用户的原话是：「整个设置菜单的分组情况和设置项是否为失效或者重复需要重新设计，
同时要**验证设置是否被代码正常读取**」。靠眼睛看面板验证不了这件事 —— 一个
「写进去没人读」的设置项和正常项在界面上长得一模一样。本文件把它变成可重复执行的检查：

  1. 读取审计（`scripts/audit-settings.py`）：每个非弃用项要么被 app/ 读到，要么
     写进确认名单（ACK_INDIRECT / ACK_PANEL），**名单本身也要防腐烂**；
  2. 往返：`/api/settings` 读得出、PUT 写进去、再读还是新值（隐藏项走 settings.get）；
  3. 分组合法：grp 必须在面板的分组表里；可见项**不许**落在「智能体」组
     （那一组由智能体表格整块渲染，普通项进去会被吞掉、在界面上彻底看不见）；
  4. 选项不是幻觉：`wakeEngine` / `ttsEngine` 的候选项必须有实现；
  5. 弃用项：不出现在 /api/settings、写入被拒收、老值按迁移规则搬到新开关上。

DB 重定向到临时目录，不碰真实 data/；PUT 的联动（重启唤醒、热重载路由）在测试里哑掉。
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app.db as db                                              # noqa: E402
from app.config import (CLEAR_SECRET, DEPRECATION_MIGRATIONS,    # noqa: E402
                        DEFAULTS, SETTING_ORDER, settings)


def _load_audit():
    """把 scripts/audit-settings.py 当模块载入（文件名带连字符，不能直接 import）。"""
    path = os.path.join(_ROOT, "scripts", "audit-settings.py")
    spec = importlib.util.spec_from_file_location("audit_settings", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(rel_path):
    with open(os.path.join(_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- 1. 审计

class SettingsAuditTests(unittest.TestCase):
    """每个设置项都要有「谁在读它」的答案；答不出来就得进确认名单，否则测试红。"""

    @classmethod
    def setUpClass(cls):
        cls.audit = _load_audit()
        keys, flags, hits = cls.audit.scan()
        cls.keys = keys
        cls.flags = flags
        cls.hits = hits
        cls.verdicts = {k: cls.audit.verdict(flags[k], hits[k]) for k in keys}

    def test_every_key_is_read_or_explicitly_acknowledged(self):
        rows = [(k, self.flags[k], self.hits[k], self.verdicts[k]) for k in self.keys]
        bad = self.audit._unacknowledged(rows)
        self.assertEqual([b[0] for b in bad], [],
                         "这些设置项没人读、也没写进确认名单：%s" % [b[0] for b in bad])

    def test_deprecated_keys_are_skipped_by_the_audit(self):
        deprecated = {k for k, m in DEFAULTS.items() if m.get("deprecated")}
        for key in deprecated:
            self.assertEqual(self.verdicts[key], "DEPRECATED", "%s 应判为已弃用" % key)

    def test_acknowledgement_lists_do_not_rot(self):
        """确认名单必须「正好对上」：名单里的项要仍然需要确认，非名单项不许需要确认。"""
        for key, reason in self.audit.ACK_INDIRECT.items():
            with self.subTest(key=key):
                self.assertIn(key, DEFAULTS, "确认名单里有不存在的键（名单腐烂了）")
                self.assertFalse(DEFAULTS[key].get("deprecated"))
                self.assertGreaterEqual(len(reason.strip()), 8, "确认名单必须写清谁在读它")
                self.assertEqual(self.verdicts[key], "OK-INDIRECT",
                                 "%s 已不再是间接读：请从 ACK_INDIRECT 删掉" % key)
        for key, reason in self.audit.ACK_PANEL.items():
            with self.subTest(key=key):
                self.assertIn(key, DEFAULTS)
                self.assertFalse(DEFAULTS[key].get("deprecated"))
                self.assertTrue(reason.strip())
                self.assertEqual(self.verdicts[key], "PANEL-READ",
                                 "%s 已不再是面板自用：请从 ACK_PANEL 删掉" % key)

    def test_no_setting_is_dead_or_write_only(self):
        bad = {k: v for k, v in self.verdicts.items() if v in ("DEAD", "PANEL-ONLY")}
        self.assertEqual(bad, {}, "存在死设置 / 只写没人读的设置：%s" % bad)


# ---------------------------------------------------------------- 2. 分组

class GroupingTests(unittest.TestCase):
    """分组合法性：面板按 grp 分组渲染，grp 写错 = 设置项从界面上消失。

    另有二级小节（`sub`）：它是**包含在**一级分组里的再分节，不是并列分组
    （用户 2026-09-19：「语音命令和 beep/command/speech 应该是包含不是并列」）。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("web", "app.js"))
        order = re.search(r"const SET_GROUP_ORDER = \[(.*?)\];", cls.js, re.S)
        names = re.search(r"const SET_GROUP_NAMES = \{(.*?)\};", cls.js, re.S)
        sub_order = re.search(r"const SET_SUB_ORDER = \[(.*?)\];", cls.js, re.S)
        sub_names = re.search(r"const SET_SUB_NAMES = \{(.*?)\};", cls.js, re.S)
        assert order and names and sub_order and sub_names, \
            "web/app.js 里的分组表被改得认不出来了"
        cls.order = re.findall(r'"([a-z]+)"', order.group(1))
        cls.names = dict(re.findall(r'([a-z]+):\s*"([^"]+)"', names.group(1)))
        cls.sub_order = re.findall(r'"([a-z]+)"', sub_order.group(1))
        cls.sub_names = dict(re.findall(r'([a-z]+):\s*"([^"]+)"', sub_names.group(1)))
        mk = re.search(r"const MODEL_KEYS = new Set\(\[(.*?)\]\);", cls.js, re.S)
        assert mk, "web/app.js 里的 MODEL_KEYS 被改得认不出来了"
        cls.model_keys = set(re.findall(r'"(\w+)"', mk.group(1)))

    def _visible(self):
        return {k: m for k, m in DEFAULTS.items()
                if not m.get("deprecated") and not m.get("hidden")}

    def test_every_group_has_a_title_and_a_place_in_the_order(self):
        """可见项的 grp 必须在面板分组表里（hidden 项归卡片/智能体表格，不参与分组渲染）。"""
        for key, meta in self._visible().items():
            with self.subTest(key=key):
                self.assertIn(meta["grp"], self.order,
                              "%s 的分组不在 SET_GROUP_ORDER 里" % key)
                self.assertIn(meta["grp"], self.names,
                              "%s 的分组没有中文标题" % key)

    def test_group_and_sub_titles_are_chinese(self):
        """标题一律中文（用户要求「别用英文」）—— 界面上不该出现 grp/sub 的英文键名。"""
        cjk = re.compile(r"[\u4e00-\u9fff]")
        for key, title in self.names.items():
            with self.subTest(kind="group", key=key):
                self.assertTrue(cjk.search(title), "分组标题不是中文：%s=%r" % (key, title))
        for key, title in self.sub_names.items():
            with self.subTest(kind="sub", key=key):
                self.assertTrue(cjk.search(title), "小节标题不是中文：%s=%r" % (key, title))

    def test_sub_sections_are_contained_not_parallel(self):
        """二级小节必须「包含在」某个分组里：不许有与分组同名的小节。"""
        grps = {m["grp"] for m in self._visible().values()}
        self.assertFalse(grps & set(self.sub_order),
                         "小节名 %s 同时被当成了一级分组（用户要求包含关系，不是并列）"
                         % sorted(grps & set(self.sub_order)))
        owners = {}
        for key, meta in self._visible().items():
            sub = meta.get("sub")
            if not sub:
                continue
            owners.setdefault(sub, set()).add(meta["grp"])
        for sub, grp_set in owners.items():
            with self.subTest(sub=sub):
                self.assertEqual(len(grp_set), 1,
                                 "小节 %s 出现在多个分组里：%s" % (sub, sorted(grp_set)))

    def test_every_sub_section_is_declared_named_and_used(self):
        declared = set(self.sub_order)
        used = set()
        for key, meta in self._visible().items():
            sub = meta.get("sub")
            if not sub:
                continue
            used.add(sub)
            with self.subTest(key=key):
                self.assertIn(sub, declared,
                              "%s 的小节 %r 没在 SET_SUB_ORDER 里（面板会排到末尾）" % (key, sub))
                self.assertIn(sub, self.sub_names, "%s 的小节没有中文标题" % key)
        self.assertEqual(declared - used, set(),
                         "声明了却没有任何设置项的小节（会留下空标题）：%s"
                         % sorted(declared - used))

    def test_api_rows_carry_the_sub_section(self):
        """面板靠 `sub` 字段决定分节 —— 它必须随 /api/settings 下发（并由面板渲染）。"""
        self.assertIn('s.sub', self.js, "面板要按 sub 分节")
        self.assertIn("renderGroupBody", self.js)
        self.assertIn("toggleSetSub", self.js, "二级小节要能各自折叠")

    def test_no_visible_setting_lands_in_the_agent_group(self):
        """「智能体」组的整块内容由智能体表格渲染 —— 普通项进去就再也看不见了。"""
        for key, meta in self._visible().items():
            self.assertNotEqual(meta["grp"], "agent",
                                "%s 会被智能体表格吞掉（该组只放 hidden 的智能体键）" % key)

    def test_model_tab_keys_share_the_model_group(self):
        """「能力」页签承载的那些项要能被整块回退显示。

        * 模型/引擎选择（sttModel 等 9 项）归 `model` 组 —— 页签挂了就在设置页整块出现；
        * `ttsEngine` 是例外：它是"朗读"的行为开关，分组仍属「语音命令 → 朗读与反馈」
          （回退时显示在原地、不搬家），只是编辑入口由能力页签接管。
        """
        cap_owned_elsewhere = {"ttsEngine"}
        self.assertTrue(cap_owned_elsewhere <= self.model_keys,
                        "ttsEngine 应由能力页签承载（加入 MODEL_KEYS）")
        for key in self.model_keys:
            self.assertIn(key, DEFAULTS, "%s 不在 DEFAULTS 里" % key)
            if key in cap_owned_elsewhere:
                self.assertEqual(DEFAULTS[key]["grp"], "voice",
                                 "%s 回退时应出现在「语音命令 → 朗读与反馈」里" % key)
                continue
            self.assertEqual(DEFAULTS[key]["grp"], "model",
                             "%s 在「能力」页签上，grp 应是 model（回退显示用）" % key)

    def test_order_field_is_total_and_unique(self):
        self.assertEqual(len(SETTING_ORDER), len(DEFAULTS))
        self.assertEqual(len(set(SETTING_ORDER.values())), len(DEFAULTS),
                         "渲染顺序必须唯一，否则同组顺序会不稳定")


# ---------------------------------------------------------------- 3. 往返读写

def _probe_value(meta, current):
    """给某个设置项造一个「与当前值不同」的探测值（按 value_type）。"""
    vt = meta["value_type"]
    if vt == "bool":
        return (not bool(current))
    if vt == "int":
        return int(current or 0) + 7
    if vt == "float":
        return round(float(current or 0) + 0.25, 4)
    if vt == "list":
        return ["__probe__"]
    text = str(current or "")
    return (text + "__probe__") if text else "__probe__"


@contextmanager
def _quiet_side_effects():
    """PUT /api/settings 的联动在测试里哑掉：重启唤醒监听、热重载路由进程、写
    dsh-failover/config.json 都与「设置能不能读回来」无关，真做会污染仓库与进程。"""
    with patch("app.runtime.stop_wake"), patch("app.runtime.start_wake"), \
            patch("app.router_admin.apply_settings", lambda updated: (True, "")):
        yield


class RoundTripTests(unittest.TestCase):
    """每一项都要能写进去、读回来（面板与 API 用的就是这条路径）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-wiring-")
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
        cls.served = {s["key"]: s for s in cls.client.get("/api/settings").json()["settings"]}

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _served_now(self):
        return {s["key"]: s for s in self.client.get("/api/settings").json()["settings"]}

    def test_api_serves_exactly_the_visible_keys(self):
        visible = {k for k, m in DEFAULTS.items()
                   if not m.get("deprecated") and not m.get("hidden")}
        self.assertEqual(set(self.served), visible,
                         "面板看得到的项 / 看不到的项要对得上（只有 hidden/deprecated 才隐藏）")

    def test_every_visible_row_carries_the_metadata_the_panel_needs(self):
        for key, row in self.served.items():
            with self.subTest(key=key):
                self.assertTrue(row.get("label"), "%s 没有标题" % key)
                self.assertTrue(row.get("grp"), "%s 没有分组" % key)
                self.assertIn(row.get("value_type"), ("str", "int", "float", "bool", "list"))
                self.assertIsInstance(row.get("order"), int,
                                      "%s 没有 order（面板无法按声明顺序渲染）" % key)
                if DEFAULTS[key].get("secret"):
                    self.assertTrue(row.get("secret"))
                    self.assertEqual(row.get("value"), "", "密钥永不回显")

    def test_every_setting_round_trips(self):
        """全部 86 项（含隐藏/密钥）写一遍、读一遍。"""
        for key, meta in DEFAULTS.items():
            if meta.get("deprecated"):
                continue
            with self.subTest(key=key):
                want = _probe_value(meta, settings.get(key))
                with _quiet_side_effects():
                    r = self.client.put("/api/settings", json={"values": {key: want}})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(settings.get(key), want,
                                 "%s 写进去没读回来（%r != %r）" % (key, settings.get(key), want))
                if key == "apiAuthEnabled":
                    # 打开它以后所有接口都要 Bearer 令牌（这是它的作用）——后面的读检查会被
                    # 401 挡掉，所以这里验证完"写入生效/匿名被拒"就把它关回去。关回去走配置层：
                    # 走接口同样会 401（本地面板此时也拿不到令牌），这正是这一项的双刃性。
                    self.assertEqual(self.client.get("/api/settings").status_code, 401,
                                     "打开鉴权后匿名请求必须被拒")
                    settings.update({key: False})
                    self.assertFalse(settings.get(key))
                    continue
                if meta.get("hidden"):
                    continue
                row = self._served_now().get(key)
                self.assertIsNotNone(row, "%s 从 /api/settings 里消失了" % key)
                if meta.get("secret"):
                    self.assertEqual(row["value"], "", "密钥要遮罩")
                    self.assertTrue(row["hasValue"],
                                    "遮罩后要有 hasValue 告诉界面库里其实有值")
                else:
                    self.assertEqual(row["value"], want, "%s 接口读回的值不对" % key)

    def test_secrets_can_be_cleared_explicitly(self):
        key = "providerLlmApiKey"
        with _quiet_side_effects():
            self.client.put("/api/settings", json={"values": {key: "sk-probe-123"}})
        self.assertEqual(settings.get(key), "sk-probe-123")
        payload = self.client.get("/api/providers/config").json()
        row = {s["key"]: s for s in payload["settings"]}[key]
        self.assertTrue(row["hasValue"])
        self.assertEqual(row["value"], "")
        self.assertNotIn("sk-probe-123", json.dumps(payload))
        with _quiet_side_effects():
            self.client.put("/api/settings", json={"values": {key: CLEAR_SECRET}})
        self.assertEqual(settings.get(key), "", "清除哨兵要真的清空")

    def test_empty_secret_does_not_wipe_the_stored_one(self):
        """面板整批回传时密钥是空串 —— 空串只能解释成「不改」。"""
        key = "providerAsrApiKey"
        with _quiet_side_effects():
            self.client.put("/api/settings", json={"values": {key: "sk-keep-me"}})
            self.client.put("/api/settings", json={"values": {key: "", "sttLanguage": "en"}})
        self.assertEqual(settings.get(key), "sk-keep-me")


# ---------------------------------------------------------------- 4. 弃用与迁移

class DeprecationTests(unittest.TestCase):
    """弃用 = 不再展示 + 写入拒收 + 老值按规则搬家（不许静默改变行为）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-dep-")
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

    def test_deprecated_keys_are_invisible_and_read_only(self):
        for key in settings.deprecated_keys():
            with self.subTest(key=key):
                cleaned = settings.update({key: "whatever"})
                self.assertNotIn(key, cleaned, "%s 已弃用，写入必须被拒收" % key)
        served = {s["key"] for s in self.client.get("/api/settings").json()["settings"]}
        for key in settings.deprecated_keys():
            self.assertNotIn(key, served)

    def test_worklog_off_moves_to_the_single_switch(self):
        """老配置 worklogMode=off ⇒ 归档总开关关掉（行为不变，开关只剩一个）。"""
        settings.update({"worklogEnabled": True})
        db.set_setting("worklogMode", "off")
        settings._cache = None
        settings.seed_defaults()
        self.assertFalse(settings.get("worklogEnabled"))
        from app import worklog
        ok, why = worklog.ready()
        self.assertFalse(ok)
        self.assertIn("未启用", why)

    def test_provider_tts_values_move_to_tts_engine(self):
        from app.config import _offline_tts_engine
        settings.reset()
        db.set_setting("providerTts", "edge-tts")
        settings._cache = None
        settings.seed_defaults()
        self.assertEqual(settings.get("ttsEngine"), "edge-tts",
                         "选过在线朗读的用户要搬到 ttsEngine=edge-tts")
        db.set_setting("providerTts", "local-tts")
        settings._cache = None
        settings.seed_defaults()
        self.assertEqual(settings.get("ttsEngine"), _offline_tts_engine(),
                         "选过离线朗读的用户要搬到本平台离线引擎（不能搬到 auto：auto 会出网）")

    def test_migration_rules_point_at_live_keys(self):
        for key, rules in DEPRECATION_MIGRATIONS.items():
            with self.subTest(key=key):
                self.assertTrue(DEFAULTS[key].get("deprecated"),
                                "%s 没被标成弃用却配了迁移规则" % key)
                for _trigger, target, _value in rules:
                    self.assertIn(target, DEFAULTS)
                    self.assertFalse(DEFAULTS[target].get("deprecated"),
                                     "%s 的迁移目标不能是另一个弃用项" % key)


# ---------------------------------------------------------------- 5. 候选项 / 面板接线

class OptionAndPanelWiringTests(unittest.TestCase):
    """选项必须有实现；面板自己的设置项要真的接上（2026-09-19 修掉的两个漏接线）。"""

    def test_wake_engine_options_are_implemented(self):
        from app.audio import wake
        opts = set(DEFAULTS["wakeEngine"]["options"])
        self.assertEqual(opts, set(wake.ENGINE_LABELS),
                         "面板能选的唤醒方式必须都有实现（不许再出现 openwakeword 这种幽灵项）")
        self.assertEqual(wake.engine_label(DEFAULTS["wakeEngine"]["value"]),
                         wake.ENGINE_LABELS[DEFAULTS["wakeEngine"]["value"]])
        self.assertEqual(wake.engine_label("openwakeword"), wake.ENGINE_LABELS["sherpa"],
                         "历史配置里的未知值要按默认实现算，不能让唤醒起不来")

    def test_wake_engine_selects_a_real_implementation(self):
        """后端必须真的读 wakeEngine（它曾经只有面板读）。"""
        from app.audio.wake import WakeListener

        class _Settings(object):
            def __init__(self, value):
                self.value = value

            def settings_get(self, key, default=None):
                return {"wakeEngine": self.value}.get(key, default)

        for value, want in (("kws", "kws"), ("sherpa", "stream"), ("stream", "stream"),
                            ("openwakeword", "stream")):
            with self.subTest(wakeEngine=value):
                listener = WakeListener.__new__(WakeListener)
                listener.settings_get = _Settings(value).settings_get
                with patch.object(WakeListener, "_make_kws_detector", lambda self: "kws"), \
                        patch.object(WakeListener, "_make_stream_detector", lambda self: "stream"):
                    self.assertEqual(WakeListener._make_detector(listener), want)

    def test_tts_engine_options_are_implemented(self):
        from app.audio import tts
        opts = set(DEFAULTS["ttsEngine"]["options"])
        known = {"auto", "edge-tts", "off"} | tts._offline_engine_ids()
        self.assertTrue(opts <= known,
                        "ttsEngine 候选项里有没实现的：%s" % sorted(opts - known))

    def test_panel_auto_refresh_is_wired_to_the_polling(self):
        """panelAutoRefresh 曾经是 DEAD（面板写死 2 秒）；现在它必须驱动轮询。"""
        js = _read(os.path.join("web", "app.js"))
        self.assertIn('settingByKey("panelAutoRefresh")', js)
        self.assertIn("_panelRefreshDue", js)
        self.assertIn("_panelRefreshSeconds", js)
        self.assertIn("if (!_panelRefreshDue()) return;", js)

    def test_tts_has_exactly_one_selector(self):
        """TTS 只有一个开关：能力页签里那个 `ttsEngine` 下拉（providerTts 已弃用）。"""
        js = _read(os.path.join("web", "app.js"))
        self.assertIn("data-provider-kind", js)
        self.assertIn("data-tts-engine", js, "TTS 的选择走 ttsEngine 这一个开关")
        self.assertNotIn('data-provider-kind="tts"', js, "不许再给 TTS 放第二个 provider 下拉")
        self.assertNotIn("providerTts", js, "弃用项不该被面板引用")

    def test_capability_page_covers_every_capability_kind(self):
        """能力页签按"能力种类"渲染，并且每类都能看到实现与组件两件事。"""
        js = _read(os.path.join("web", "app.js"))
        for token in ("const CAP_KINDS", "capKindCard", "capProviderBlock", "capCompTable",
                      "renderCapEnv", "function loadCapabilities"):
            with self.subTest(token=token):
                self.assertIn(token, js)
        self.assertIn('CAP_KINDS = ["asr", "tts"]', js,
                      "能力页签只放转写与朗读；语言模型在「模型路由」页签里")
        self.assertIn("capCompsOf", js, "组件按 kind 归到对应能力卡里")

    def test_language_model_block_lives_in_the_model_router_tab(self):
        """语言模型（用哪个实现 + 在线服务）整合到「模型路由」页签（2026-09-19 用户要求）。

        理由：LLM 的"用哪个实现"和路由的上游配置是同一件事 —— ECHO AUTO 的成员本来就在
        那个页签里配，分两处只会互相找不着。
        """
        js = _read(os.path.join("web", "app.js"))
        html = _read(os.path.join("web", "index.html"))
        self.assertIn('id="rtLlmHost"', html)
        self.assertLess(html.index('id="view-failover"'), html.index('id="rtLlmHost"'),
                        "语言模型块要在「模型路由」页签里")
        self.assertLess(html.index('id="rtLlmHost"'), html.index('id="rtBadge"'),
                        "它排在通道设置之前（先选用哪个，再配通道）")
        self.assertIn("function loadRouterLlm", js)
        self.assertIn('capProviderBlock("llm")', js, "复用同一套渲染（状态/出网图标/在线服务字段）")
        self.assertIn("loadRouterLlm();", js.split("async function loadRouter()")[1][:1200],
                      "切到模型路由页签时要一并渲染语言模型块")
        self.assertNotIn('title: "语言模型"', js, "能力页签不该再有语言模型卡")
        # 两个页签共用同一份 provider 数据（不许各拉一遍），改完实现只刷当前那一页
        self.assertIn("function ensureProviderData", js)
        self.assertIn("refreshAfterProviderChange", js)

    def test_capability_card_shows_status_only_once(self):
        """能力卡的状态只显示一次（2026-09-19 用户看截图指出：下拉下面的附属、两种实现都是重复）。

        * 状态并进下拉选项文字（`capOptLabel`：名字 + 出网/本地 · 就绪）；
        * 不再有"当前 XXX · 已就绪"这种附属行，也不再有单列一遍"两种实现"的列表
          （那两处与下拉选项、顶部概览条是同一份信息的第 2/3 份拷贝）。
        """
        js = _read(os.path.join("web", "app.js"))
        for token in ("function capOptLabel", "function capTtsOptionLabel"):
            self.assertIn(token, js, "状态要并进下拉选项里")
        self.assertNotIn("当前 <b>", js, "下拉下面不该再挂一行「当前 XXX」")
        self.assertNotIn("cap-prov-state", js, "不该再单列一遍各实现的状态")
        self.assertNotIn("cap-prov-row", js, "下拉行不再需要标签行容器")

    def test_egress_warning_is_an_icon_with_a_tooltip(self):
        """出网提醒＝黄色三角图标 + 悬停 title（2026-09-19 用户要求：别占一整行）。"""
        js = _read(os.path.join("web", "app.js"))
        css = _read(os.path.join("web", "app.css"))
        self.assertIn("function capEgress(", js, "出网判断要单独成函数（图标与文案共用一份判据）")
        self.assertIn("function capEgressIcon(", js)
        self.assertIn('class="cap-egress"', js)
        self.assertIn('title="${esc(tip)}"', js, "说明文字要进 title（悬停浮出）")
        self.assertIn(".cap-egress", css, "图标样式（含 cursor:help）")
        self.assertIn("cursor: help", css)
        # 原来那种占一整行的 "⚠ 数据会出网：…" 与 "数据不出本机" 都不该再作为行渲染
        self.assertNotIn("⚠ 数据会出网", js)
        self.assertNotIn("数据不出本机", js)

    def test_router_settings_keys_match_the_router_group(self):
        """`ROUTER_KEYS`（被「模型路由」页签接管的键）必须与 `grp="router"` 一一对应。"""
        js = _read(os.path.join("web", "app.js"))
        m = re.search(r"const ROUTER_KEYS = new Set\(\[(.*?)\]\);", js, re.S)
        self.assertIsNotNone(m, "app.js 里应有 ROUTER_KEYS")
        declared = set(re.findall(r'"(\w+)"', m.group(1)))
        want = {k for k, meta in DEFAULTS.items()
                if meta.get("grp") == "router" and not meta.get("deprecated")}
        self.assertEqual(declared, want, "路由参数集合与 config 的 router 组漂移了")

    def test_router_settings_live_in_the_model_router_tab(self):
        """「设置 → 模型路由」那一组整合进顶部「模型路由」页签（2026-09-19 用户要求）。"""
        js = _read(os.path.join("web", "app.js"))
        html = _read(os.path.join("web", "index.html"))
        for token in ('id="rtSetHost"', 'id="rtSetSave"'):
            self.assertIn(token, html, "index.html 缺少 %s" % token)
        self.assertLess(html.index('id="view-failover"'), html.index('id="rtSetHost"'),
                        "路由参数要在「模型路由」页签里")
        self.assertIn("async function loadRouterSettings", js)
        self.assertIn("loadRouterSettings();", js.split("async function loadRouter()")[1][:900],
                      "切到模型路由页签时要渲染路由参数")
        self.assertIn("rows.map(renderSettingRow)", js, "复用设置页的行渲染（样式一致）")
        self.assertIn("_rtTabOk && ROUTER_KEYS.has(s.key)", js,
                      "设置页要把路由参数过滤掉（回退条件由 _rtTabOk 管）")
        self.assertIn("routerHintRow", js, "设置页要给一句指路")
        self.assertIn("async function ensureSettings", js, "两个页签共用一份设置数据")

    def test_component_list_is_rows_not_a_cramped_table(self):
        """组件清单用逐条行、按钮文字短（2026-09-19 用户："布局不太好看，按钮字太多"）。

        表格在窄边条里会把「用途」列压成竖排汉字、按钮也竖着排；行式布局下：
        名称+状态一行、体积+用途一行、动作一行（宽屏时动作挪到右侧）。
        """
        js = _read(os.path.join("web", "app.js"))
        css = _read(os.path.join("web", "app.css"))
        self.assertIn('class="cap-comps"', js)
        self.assertIn('class="cap-comp', js)
        self.assertNotIn("cap-table", js, "不该再渲染表格")
        self.assertNotIn(".cap-table", css, "表格样式应已删除（避免死样式）")
        for token in (".cap-comps", ".cap-comp-main", ".cap-comp-meta", ".cap-comp-acts"):
            with self.subTest(token=token):
                self.assertIn(token, css)
        self.assertIn(".cap-comp-acts .btn { white-space: nowrap; }", css,
                      "按钮不许竖排（窄边条实测会被挤成一列字）")

    def test_model_action_labels_stay_short(self):
        """按钮/链接文字要短（完整含义进 title）——「复制下载命令/复制目标路径」都太长。"""
        js = _read(os.path.join("web", "app.js"))
        block = js.split("function modelActions")[1].split("\n}")[0]
        for token in ('data-msdl=', "重新下载", '"下载"', "复制命令", "复制路径", ">链接</a>", "title="):
            with self.subTest(token=token):
                self.assertIn(token, block)
        for gone in ("复制目标路径</button>", "复制下载命令</button>"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, block, "长按钮文字应改成短标签 + title（title 里保留原名）")

    def test_ready_items_downgrade_their_actions(self):
        """已就绪的项不再摆显眼的下载按钮（用户："已就绪的还用保留下载吗"）。

        两档层级：未就绪 → 「下载」主按钮（`.btn.mini`）；已就绪 → 小文字链接
        （`.act-link`：灰字、悬停才亮，但仍可点「重新下载」）。
        """
        js = _read(os.path.join("web", "app.js"))
        css = _read(os.path.join("web", "app.css"))
        block = js.split("function modelActions")[1].split("\n}")[0]
        self.assertIn('ready ? "act-link" : "btn mini"', block, "按 ready 分两档类名")
        self.assertIn('data-force="${ready ? "1" : "0"}"', block)
        self.assertIn(".act-link {", css)
        self.assertIn(".act-link:hover", css)

    def test_pip_components_offer_a_download_command_button(self):
        """pip 类组件的动作叫「下载命令」，复制的是后端拼好的整条命令（用户 2026-09-19 指定）。"""
        js = _read(os.path.join("web", "app.js"))
        block = js.split("function capCompActions")[1].split("\n}")[0]
        self.assertIn('c.command_label || "下载命令"', block, "标签默认就是「下载命令」")
        self.assertNotIn(">复制说明</button>", block, "标签已改名")
        self.assertIn('data-mcopy="${esc(c.command)}"', block,
                      "复制的是后端给的 command（带解释器路径），不是 how 说明")
        self.assertIn("cmd 与 PowerShell 都行，在哪个目录执行都行", block,
                      "悬停要说清在哪执行、用哪个终端")
        # 没有 command 的组件（如 DSH 只装客户端）不给按钮 → how 必须在行里显示出来
        self.assertIn("c.how", js.split("function capCompTable")[1].split("\n}")[0])

    def test_engine_labels_do_not_wrap_vertically(self):
        """「命令转写 / 会议转写」在窄边条里被压成竖排两行 → 加 nowrap + 窄屏各占一行。"""
        css = _read(os.path.join("web", "app.css"))
        m = re.search(r"\.cap-engine-row label\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("white-space: nowrap", m.group(1))

    def test_command_button_label_comes_from_the_manifest(self):
        """「下载命令 / 复制启动命令」的标签由清单给（独立 harness 那条是"启动命令"）。"""
        js = _read(os.path.join("web", "app.js"))
        block = js.split("function capCompActions")[1].split("\n}")[0]
        self.assertIn("c.command_label", block)
        self.assertIn('c.command_label || "下载命令"', block, "缺省仍是「下载命令」")

    def test_secret_agent_key_gets_a_password_field(self):
        """智能体展开区里的密钥（harness 访问 token）要能填 —— 密码框 + 留空 = 不改。

        2026-09-19 用户实测问"我在哪里配置 key"：提示语让人填 token，面板上却没那一栏。
        """
        js = _read(os.path.join("web", "app.js"))
        block = js.split("function agentDetailHtml")[1].split("\nfunction ")[0]
        self.assertIn("if (s.secret)", block, "密钥行要有单独分支")
        self.assertIn('type="password"', block)
        self.assertIn('data-secret="1"', block)
        self.assertIn("已配置（留空 = 不改）", block, "占位符要说清留空不改")
        self.assertIn("未配置", block)

    def test_agent_switch_also_enables_the_product(self):
        """智能体开关即单选：选中时要把该产品的启用开关一起打开，避免自相矛盾。"""
        js = _read(os.path.join("web", "app.js"))
        self.assertIn("target.configKey", js)


if __name__ == "__main__":
    unittest.main()
