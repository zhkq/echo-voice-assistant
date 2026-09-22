# -*- coding: utf-8 -*-
"""DSH 不止一个家目录：桌面版 / 标准版 harness / 两个都有 / 两个都没有。

背景（2026-09-22 同事反馈"用户不一定两个都有"）
============================================

ECHO 原来把"ECHO AUTO 注册进 DSH"写死成**桌面版那一份**（`DSH_HOME` 或用户家目录下的
.dsh）。可向导默认推荐的就是标准版（`agentBackend=harness`），只装标准版时四处同时坏掉：

  * ECHO AUTO 注册不到（boot 那道闸只看桌面版适配器）；
  * 路由令牌读成空串（凭据在标准版家目录里）→ 令牌校验静默失效；
  * 组成员密钥（内网网关令牌这类）解析不到 → 模型组直接不可用；
  * 面板「添加成员」的候选一个都列不出来。

本文件用隔离目录把这四种装法都钉住：**写只写实际存在的家目录**，
绝不为没装/没初始化完的 DSH 造配置（D25 的老纪律）。

用 unittest 写（不是 pytest 风格）：门禁是 `python -m unittest discover -s tests -t .`。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml                                                     # noqa: E402

from app import boot                                            # noqa: E402
from app import llm_router                                      # noqa: E402
from app import router_admin                                    # noqa: E402

PROXY_PATH = ROOT / "dsh-failover" / "proxy.py"


class _Isolated(unittest.TestCase):
    """公用底座：把 llm_router 指到临时目录（家目录 / router config / homes.json）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echo-multihome-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        cfg = self.tmp / "router-config.json"
        cfg.write_text(json.dumps({
            "port": 18899,
            "groups": {"echo-auto": {
                "display_name": "ECHO AUTO",
                "context_window": 131072,
                "max_tokens": 32768,
                "members": [{"name": "内Flash", "base_url": "https://gw.example/v1",
                             "model": "DeepSeek-V4-Flash",
                             "credential": "INTRANET_TOKEN"}],
            }},
        }, ensure_ascii=False), encoding="utf-8")
        self.patch_obj(llm_router, "ROUTER_CONFIG", cfg)
        self.patch_obj(llm_router, "HOMES_FILE", self.tmp / "homes.json")

    # ---- 打桩小工具（patch.object + 自动收尾）----
    def patch_obj(self, obj, name, value):
        p = patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)
        return value

    def patch_attr(self, target, value):
        p = patch(target, value)
        p.start()
        self.addCleanup(p.stop)
        return value

    # ---- 造家目录 / 声明这台机器上有哪些 ----
    def make_home(self, name, token=""):
        home = self.tmp / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.yaml").write_text(
            "permission:\n  defaultPreset: danger-full-access\n", encoding="utf-8")
        creds = "version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-x\n"
        if token:
            creds += "  %s: %s\n" % (llm_router.TOKEN_REF, token)
        (home / ".credentials.yaml").write_text(creds, encoding="utf-8")
        return home

    def use_homes(self, desktop=None, harness=None):
        """声明"这台机器上有哪些家目录"（``None`` = 没有）。"""
        missing = self.tmp / "not-installed"
        self.patch_obj(llm_router, "DSH_HOME", desktop or missing)
        self.patch_obj(llm_router, "SETTINGS", (desktop or missing) / "settings.yaml")
        self.patch_obj(llm_router, "CREDENTIALS",
                       (desktop or missing) / ".credentials.yaml")
        self.patch_obj(llm_router, "harness_home", lambda: harness)


# ---------------------------------------------------------------- 四种装法
class HomeDiscoveryTests(_Isolated):

    def test_all_four_installations(self):
        cases = [(True, False, ["desktop"]), (False, True, ["harness"]),
                 (True, True, ["desktop", "harness"]), (False, False, [])]
        for desktop, harness, expect in cases:
            with self.subTest(desktop=desktop, harness=harness):
                d = self.make_home("desktop-home", "T-desktop") if desktop else None
                h = self.make_home("harness-home", "T-harness") if harness else None
                self.use_homes(desktop=d, harness=h)
                self.assertEqual([x["kind"] for x in llm_router.dsh_homes()], expect)

    def test_a_directory_without_settings_is_not_a_home(self):
        """只有目录、没有 settings.yaml = 那台 DSH 还没初始化过：不算家目录。"""
        empty = self.tmp / "half-initialized"
        empty.mkdir()
        self.use_homes(desktop=empty, harness=None)
        self.assertEqual(llm_router.dsh_homes(), [])
        ok, detail = llm_router.sync()
        self.assertFalse(ok)
        self.assertIn("没找到 DSH 家目录", detail)
        self.assertFalse((empty / "settings.yaml").exists(),
                         "不许替没初始化完的 DSH 造配置")

    def test_nothing_is_created_when_no_dsh_is_installed(self):
        self.use_homes(desktop=None, harness=None)
        ok, detail = llm_router.sync()
        self.assertFalse(ok)
        self.assertIn("没找到 DSH 家目录", detail)
        self.assertFalse((self.tmp / "not-installed").exists())


# ---------------------------------------------------------------- 注册
class RegistrationTests(_Isolated):

    def test_registers_every_existing_home(self):
        for which in ("desktop-only", "harness-only", "both"):
            with self.subTest(which=which):
                made = {}
                if which in ("desktop-only", "both"):
                    made["桌面版"] = self.make_home("desktop-home", "T-desktop")
                if which in ("harness-only", "both"):
                    made["标准版"] = self.make_home("harness-home")
                self.use_homes(desktop=made.get("桌面版"), harness=made.get("标准版"))

                ok, detail = llm_router.sync()
                self.assertTrue(ok, detail)
                for label, home in made.items():
                    doc = yaml.safe_load((home / "settings.yaml").read_text(encoding="utf-8"))
                    route = doc["llm-pi-ai"]["providers"]["echo-auto"]
                    self.assertEqual(route["baseURL"], "http://127.0.0.1:18899")
                    self.assertTrue(route["models"], "%s 的模型列表是空的" % label)
                    self.assertTrue(llm_router._read_token(home / ".credentials.yaml"),
                                    "%s 没拿到令牌" % label)
                tokens = {llm_router._read_token(h / ".credentials.yaml")
                          for h in made.values()}
                self.assertEqual(len(tokens), 1, "两个家目录的令牌必须一致（路由只认一个）")
                payload = json.loads((self.tmp / "homes.json").read_text(encoding="utf-8"))
                self.assertEqual(len(payload["credentials"]), len(made),
                                 "homes.json 要列出每个家目录的凭据库，路由进程照它找密钥")

    def test_existing_token_is_reused_and_unified(self):
        """两个家目录令牌不一致时以**先存在的**（桌面版）为基准，别把原值换掉。"""
        d = self.make_home("desktop-home", "T-desktop")
        h = self.make_home("harness-home", "T-harness")
        self.use_homes(desktop=d, harness=h)
        ok, detail = llm_router.sync()
        self.assertTrue(ok, detail)
        self.assertEqual(llm_router._read_token(d / ".credentials.yaml"), "T-desktop")
        self.assertEqual(llm_router._read_token(h / ".credentials.yaml"), "T-desktop")

    def test_router_token_comes_from_whichever_home_exists(self):
        """只装标准版时令牌必须读得到 —— 原来在这里读成空串，校验静默失效。"""
        h = self.make_home("harness-home", "T-harness")
        self.use_homes(desktop=None, harness=h)
        self.assertEqual(llm_router.router_token(), "T-harness")

    def test_home_without_credentials_is_skipped_with_a_reason(self):
        """只有 settings.yaml、没有凭据库 = 那台 DSH 还没初始化完：跳过并说清楚。

        硬写进去就是 MISSING_CREDENTIAL —— 模型摆在列表里却选不动，比不出现更糟。
        """
        h = self.make_home("harness-home")
        (h / ".credentials.yaml").unlink()
        self.use_homes(desktop=None, harness=h)
        ok, detail = llm_router.sync()
        self.assertFalse(ok)
        self.assertIn("凭据库", detail)
        doc = yaml.safe_load((h / "settings.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("echo-auto", ((doc.get("llm-pi-ai") or {}).get("providers") or {}))

    def test_a_half_initialized_home_does_not_block_the_other(self):
        d = self.make_home("desktop-home", "T-desktop")
        h = self.make_home("harness-home")
        (h / ".credentials.yaml").unlink()
        self.use_homes(desktop=d, harness=h)
        ok, detail = llm_router.sync()
        self.assertTrue(ok, detail)
        self.assertIn("跳过", detail)
        doc = yaml.safe_load((d / "settings.yaml").read_text(encoding="utf-8"))
        self.assertIn("echo-auto", doc["llm-pi-ai"]["providers"])

    def test_second_sync_is_a_no_op(self):
        """第二次注册不许再写盘、更不该留备份。

        `off:` 被 YAML 1.1 读成布尔键这个坑会让"已是最新"永远判假 —— 每次启动白写一遍，
        历史上家目录里的上百份 .bak-echo-auto-* 就有它一份功劳。
        """
        d = self.make_home("desktop-home", "T-desktop")
        self.use_homes(desktop=d, harness=None)
        ok, detail = llm_router.sync()
        self.assertTrue(ok, detail)
        before = (d / "settings.yaml").read_bytes()
        backups = list(d.glob("settings.yaml.bak-echo-auto-*"))

        ok2, detail2 = llm_router.sync()
        self.assertTrue(ok2, detail2)
        self.assertIn("已是最新", detail2)
        self.assertEqual((d / "settings.yaml").read_bytes(), before, "内容没变就不该改写")
        self.assertEqual(list(d.glob("settings.yaml.bak-echo-auto-*")), backups,
                         "幂等的一轮不该产生备份文件")

    def test_status_lists_each_home_separately(self):
        d = self.make_home("desktop-home", "T-desktop")
        h = self.make_home("harness-home")
        self.use_homes(desktop=d, harness=h)
        llm_router.sync()
        st = llm_router.status()
        self.assertEqual([x["kind"] for x in st["homes"]], ["desktop", "harness"])
        self.assertTrue(all(x["registered"] for x in st["homes"]))
        self.assertTrue(st["registered"])

        # 只注册了一处时，逐家目录的状态要能看出来（面板据此说"标准版还没注册"）
        (h / "settings.yaml").write_text("permission:\n  defaultPreset: full\n",
                                         encoding="utf-8")
        st2 = llm_router.status()
        self.assertTrue(st2["registered"])
        self.assertEqual({x["kind"]: x["registered"] for x in st2["homes"]},
                         {"desktop": True, "harness": False})


# ---------------------------------------------------------------- 会话存档也按家目录找
class SessionStoreTests(_Isolated):
    """会议归档要读会话存档判断权限档位 —— 存档在**哪个 DSH 家目录**里都要找得到。"""

    def _seed_store(self, home, session_id, sandbox):
        store = home / "storages" / "session_projcache" / "sessions"
        store.mkdir(parents=True, exist_ok=True)
        (store / (session_id + ".json")).write_text(json.dumps({
            "record": {"rows": {"permissions": {"val": {"sandbox": sandbox}}}},
        }), encoding="utf-8")

    def test_reads_the_store_from_the_harness_home(self):
        from app import worklog
        h = self.make_home("harness-home")
        self._seed_store(h, "session-abc", "danger-full-access")
        self.use_homes(desktop=None, harness=h)
        self.assertEqual(worklog.session_access("session-abc"), "full")

    def test_reads_the_store_from_the_desktop_home(self):
        from app import worklog
        d = self.make_home("desktop-home")
        self._seed_store(d, "session-xyz", "workspace-write")
        self.use_homes(desktop=d, harness=None)
        self.assertEqual(worklog.session_access("session-xyz"), "restricted")

    def test_unknown_when_nothing_is_found(self):
        from app import worklog
        self.use_homes(desktop=None, harness=None)
        self.assertEqual(worklog.session_access("session-none"), "unknown")
        self.assertEqual(worklog.session_access(""), "unknown")


# ---------------------------------------------------------------- 面板候选
class PanelCandidateTests(_Isolated):

    def test_candidates_see_the_harness_home(self):
        """只装标准版时，「添加成员」也要列得出模型（配置在它的家目录里）。"""
        h = self.make_home("harness-home")
        (h / "settings.yaml").write_text(
            "permission:\n  defaultPreset: danger-full-access\n"
            "llm-pi-ai:\n"
            "  providers:\n"
            "    intranet-gw:\n"
            "      displayName: 内网网关\n"
            "      apiKeyEnv: INTRANET_TOKEN\n"
            "      api: openai-completions\n"
            "      baseURL: https://gw.example/v1\n"
            "      models:\n"
            "      - id: DeepSeek-V4-Flash\n"
            "        name: 内网 Flash\n",
            encoding="utf-8")
        self.use_homes(desktop=None, harness=h)
        keys = {c["key"] for c in router_admin.candidates()}
        self.assertIn("intranet-gw::DeepSeek-V4-Flash", keys)

    def test_credential_refs_are_merged_across_homes(self):
        """凭据 ref 分住在两个家目录里时，"有没有密钥"的提示不能只看一份。"""
        d = self.make_home("desktop-home", "T-desktop")
        h = self.make_home("harness-home")
        (h / ".credentials.yaml").write_text(
            "version: 1\nrefs:\n  INTRANET_TOKEN: abc\n", encoding="utf-8")
        self.use_homes(desktop=d, harness=h)
        refs = router_admin._cred_refs()
        self.assertLessEqual({"DEEPSEEK_API_KEY", "INTRANET_TOKEN"}, refs)


# ---------------------------------------------------------------- boot 那道闸
class _Agent:
    def __init__(self, ok, why):
        self._ok, self._why = ok, why

    def available(self, probe=False):
        return self._ok, self._why


class BootGateTests(_Isolated):

    def _gate(self, table):
        self.patch_attr("app.agents.names", lambda: list(table))
        self.patch_attr("app.agents.get_agent", lambda name=None: table[name])
        return boot._agent_dsh_available()

    def test_harness_only_machine_passes(self):
        """只装标准版（向导默认）时也要放行，否则它的家目录永远等不到注册。"""
        ok, why = self._gate({"dsh": _Agent(False, "连不上 DSH Desktop"),
                              "harness": _Agent(True, "独立 harness 运行中（43199）")})
        self.assertTrue(ok)
        self.assertIn("harness", why)

    def test_desktop_only_machine_passes(self):
        ok, why = self._gate({"dsh": _Agent(True, "API 可访问（43120）"),
                              "harness": _Agent(False, "标准版 harness 没在运行")})
        self.assertTrue(ok)
        self.assertIn("可访问", why)

    def test_neither_machine_passes(self):
        ok, why = self._gate({"dsh": _Agent(False, "没在跑"),
                              "harness": _Agent(False, "没在跑")})
        self.assertFalse(ok)
        self.assertIn("没在跑", why)


# ---------------------------------------------------------------- 路由进程找凭据
def _load_proxy():
    """把 dsh-failover/proxy.py 当独立模块加载（与 test_mac_sidebar 同一套做法）。

    必须先塞进 ``sys.modules``：模块里有 ``@dataclass``，而 dataclasses 要能从
    ``sys.modules`` 里找到本模块（否则报 NoneType has no attribute __dict__）。
    """
    name = "echo_failover_proxy_ut"
    spec = importlib.util.spec_from_file_location(name, PROXY_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


class ProxyCredentialTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echo-multihome-proxy-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.proxy = _load_proxy()
        self.addCleanup(self.proxy._HOMES_CACHE.update,
                        {"mtime": "init", "paths": None})
        self.env = patch.dict(os.environ, {"ECHO_DSH_HOMES": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _homes_file(self, paths):
        path = self.tmp / "homes.json"
        path.write_text(json.dumps({"credentials": [str(p) for p in paths]},
                                   ensure_ascii=False), encoding="utf-8")
        return path

    def _creds(self, name, ref, value):
        path = self.tmp / name
        path.write_text("refs:\n  %s: %s\n" % (ref, value), encoding="utf-8")
        return path

    def test_resolves_member_secrets_from_any_home(self):
        """内网网关令牌只写在标准版家目录里时，路由进程也要找得到。"""
        d = self._creds("desktop.yaml", "DEEPSEEK_API_KEY", "dsk")
        h = self._creds("harness.yaml", "INTRANET_TOKEN", "itok")
        self.proxy.HOMES_FILE = self._homes_file([d, h])
        self.assertEqual(self.proxy.resolve_credential("INTRANET_TOKEN"), "itok")
        self.assertEqual(self.proxy.resolve_credential("DEEPSEEK_API_KEY"), "dsk")

    def test_homes_file_is_hot_reloaded(self):
        """家目录是后来才出现的（面板里选中标准版）—— 路由进程不重启也要跟上。"""
        first = self._creds("only-desktop.yaml", "TOK_A", "aaa")
        second = self._creds("now-harness.yaml", "TOK_B", "bbb")
        homes_file = self._homes_file([first])
        self.proxy.HOMES_FILE = homes_file

        self.assertEqual(self.proxy.resolve_credential("TOK_A"), "aaa")
        homes_file.write_text(json.dumps({"credentials": [str(second)]}), encoding="utf-8")
        stamp = time.time() + 5
        os.utime(homes_file, (stamp, stamp))          # 明确改 mtime，别靠时钟精度
        self.assertEqual(self.proxy.resolve_credential("TOK_B"), "bbb")
        self.assertEqual(self.proxy.resolve_credential("TOK_A"), "")

    def test_env_then_desktop_home_fallback(self):
        """没有 homes.json 时：先用启动时的环境变量，再退回桌面版那一份（老行为）。"""
        d = self._creds("desktop.yaml", "FROM_DESKTOP", "d")
        h = self._creds("harness.yaml", "FROM_HARNESS", "h")
        self.proxy.HOMES_FILE = self.tmp / "absent.json"

        with patch.dict(os.environ, {"ECHO_DSH_HOMES": os.pathsep.join(
                [str(self.tmp / "desktop"), str(self.tmp / "harness")])}):
            # 环境变量给的是**家目录**，凭据库名由路由自己拼 —— 这里按同名文件放好
            (self.tmp / "desktop").mkdir(exist_ok=True)
            (self.tmp / "harness").mkdir(exist_ok=True)
            shutil.copyfile(d, self.tmp / "desktop" / ".credentials.yaml")
            shutil.copyfile(h, self.tmp / "harness" / ".credentials.yaml")
            self.proxy._HOMES_CACHE.update({"mtime": "init", "paths": None})
            self.assertEqual(self.proxy.resolve_credential("FROM_HARNESS"), "h")

        self.proxy._HOMES_CACHE.update({"mtime": "init", "paths": None})
        self.proxy.CRED_YAML = d
        self.assertEqual(self.proxy.resolve_credential("FROM_DESKTOP"), "d")

    def test_env_override_still_wins(self):
        f = self._creds("creds.yaml", "TOK_X", "from-file")
        self.proxy.HOMES_FILE = self._homes_file([f])
        with patch.dict(os.environ, {"FAILOVER_TOK_X": "from-env"}):
            self.assertEqual(self.proxy.resolve_credential("TOK_X"), "from-env")


class FailoverSpawnTests(unittest.TestCase):
    """ECHO 拉起路由进程时要把"有哪些家目录"告诉它（只传位置，不传密钥）。"""

    def test_spawn_receives_the_home_list(self):
        from app import failover_proxy

        self.tmp = Path(tempfile.mkdtemp(prefix="echo-multihome-spawn-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        fake_homes = [{"home": self.tmp / "h1"}, {"home": self.tmp / "h2"}]

        p1 = patch.object(llm_router, "dsh_homes", lambda: fake_homes)
        p2 = patch.object(llm_router, "HOMES_FILE", self.tmp / "homes.json")
        p3 = patch.object(failover_proxy, "_last_launch", 0.0)
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)
        calls = {"n": 0}
        captured = {}

        def online(timeout=1.0):
            calls["n"] += 1
            return calls["n"] > 1              # 第一次探测"不在"，拉起之后"在"

        def fake_popen(argv, **kw):
            captured.update(kw)
            return object()

        p4 = patch.object(failover_proxy, "proxy_online", online)
        p5 = patch.object(failover_proxy.subprocess, "Popen", fake_popen)
        for p in (p4, p5):
            p.start()
            self.addCleanup(p.stop)

        ok, detail = failover_proxy.ensure_running()
        self.assertTrue(ok, detail)
        self.assertEqual(captured["env"]["ECHO_DSH_HOMES"],
                         os.pathsep.join([str(self.tmp / "h1"), str(self.tmp / "h2")]))
        self.assertEqual(captured["env"]["ECHO_DSH_HOMES_FILE"],
                         str(self.tmp / "homes.json"))


if __name__ == "__main__":
    unittest.main()
