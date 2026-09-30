# -*- coding: utf-8 -*-
"""``app/backend_setup.py``（批 1c：写配置 → 起 → 等配对文件 → 配对）的用例。

卫生规则（照 ``tests/test_backend_proc.py`` / ``tests/test_backend_pairing.py``）
---------------------------------------------------------------------------
这一批会**写文件**也**会写设置**，所以三处都必须打桩，一处漏掉就会碰到开发机的真实状态：

* ``backend_proc.backend_root`` / ``backend_setup.backend_root`` → 临时目录
  （配置文件、state/tmp/cache 全在里面）；
* ``backend_pid._logs_dir`` → 临时目录（pid 与两份日志）；
* ``cred.credentials_path`` → 临时目录（**绝不碰真实的 `{DATA}/backend.json`**），
  以及 ``backend_setup.remember_pair_path`` → 记录器（**绝不碰真实的 settings 库**）。

只有 `SettingPathTests` 会走**真的** `remember_pair_path`（要验它的三种分支），
那里的设置对象也换成了假的。
"""
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

from app import backend_pid, backend_proc, backend_ready, backend_setup  # noqa: E402
from app.capabilities import credentials as cred
from app.capabilities import pairing
from app.capabilities.credentials import BackendCredentials


class _SetupCase(unittest.TestCase):
    """公共隔离：临时后端家 + 临时 pid/日志 + 临时凭据 + 设置写入记录器。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="echo-backend-setup-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for mod in (backend_proc, backend_setup):
            p = patch.object(mod, "backend_root", lambda: self.root)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(backend_pid, "_logs_dir",
                         lambda: os.path.join(self.root, "logs"))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(cred, "credentials_path",
                         lambda: os.path.join(self.root, "backend.json"))
        p.start()
        self.addCleanup(p.stop)
        self.settings_written = []
        # 真实现先留一份：`SettingPathTests` 要验它的分支，而模块属性已被换成记录器
        self._real_remember_pair_path = backend_setup.remember_pair_path
        p = patch.object(backend_setup, "remember_pair_path", self._remember)
        p.start()
        self.addCleanup(p.stop)
        self.models = os.path.join(self.root, "models")
        os.makedirs(self.models, exist_ok=True)

    def _remember(self, target):
        self.settings_written.append(target)
        return True, "已把本机配对文件路径设为 %s" % target

    # ---- 便捷方法 ----

    def configure(self, **kw):
        kw.setdefault("models_root_path", self.models)
        return backend_setup.configure(**kw)

    def config(self):
        with open(backend_setup.config_path(), "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh.read())

    def write_config_text(self, text):
        with open(backend_setup.config_path(), "w", encoding="utf-8") as fh:
            fh.write(text)


class ConfigContentTests(_SetupCase):
    def test_loopback_and_local_pair_are_written(self):
        """写出来的 yaml：只绑回环 + local_pair: true + 状态/临时/模型指对地方。"""
        ok, detail, info = self.configure()
        self.assertTrue(ok, detail)
        cfg = self.config()
        self.assertEqual(cfg["server"]["listen"], "127.0.0.1:8900")
        self.assertEqual(cfg["server"]["admin_listen"], "127.0.0.1:8901")
        self.assertIs(cfg["server"]["local_pair"], True)
        self.assertEqual(cfg["server"]["state_root"], backend_setup.state_root())
        self.assertEqual(cfg["tmp"]["root"], backend_setup.tmp_root())
        self.assertEqual(cfg["models"]["root"], self.models)
        self.assertEqual(cfg["models"]["device"], "cuda")
        self.assertIs(cfg["auth"]["enabled"], True)
        self.assertEqual(cfg["auth"]["mode"], "jwt")
        self.assertEqual(len(str(cfg["auth"]["jwt_secret"])), 64)

        # **只绑回环**这一条要能一眼验：整个文件里不许出现通配地址
        with open(backend_setup.config_path(), "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("0.0.0.0", raw)

        # 目录真建出来了（后端要求 state/tmp 可写；cache 是子进程的 HOME）
        for d in (backend_setup.state_root(), backend_setup.tmp_root(),
                  backend_setup.cache_root()):
            self.assertTrue(os.path.isdir(d), d)

        # 本机配对文件路径自动写进设置（不用问人）
        self.assertEqual(self.settings_written, [backend_setup.pair_file_path()])
        self.assertEqual(info["pairFile"], backend_setup.pair_file_path())
        self.assertEqual(info["baseUrl"], "http://127.0.0.1:8900")

    def test_configure_is_idempotent_and_backs_up_hand_edits(self):
        """再写一次内容不变（不产生备份）；手工改过则先备份再覆盖。"""
        self.assertTrue(self.configure()[0])
        first = open(backend_setup.config_path(), "r", encoding="utf-8").read()
        self.assertTrue(self.configure()[0])
        self.assertEqual(open(backend_setup.config_path(), "r",
                              encoding="utf-8").read(), first)
        self.assertEqual([p for p in os.listdir(self.root) if ".bak-" in p], [])

        hand = first.replace("vram_budget_mb: 0", "vram_budget_mb: 9999")
        self.write_config_text(hand)
        ok, detail, info = self.configure()
        self.assertTrue(ok, detail)
        self.assertIn("备份", detail)
        self.assertTrue(info.get("backup"), info)
        self.assertEqual(open(info["backup"], "r", encoding="utf-8").read(), hand)
        self.assertEqual(self.config()["server"]["vram_budget_mb"], 0)

    def test_a_settings_write_failure_is_reported_not_fatal(self):
        """设置写不进去（例如库里还没建表）→ **不中断**，但要在人话里说出来。

        理由：那条设置只影响面板按钮自己找不找得到文件，而本机配对用的是算出来的路径。
        """
        with patch.object(backend_setup, "remember_pair_path",
                          lambda target: (False, "写不进设置 capabilityLocalPairPath：no such table")):
            ok, detail, info = self.configure()
        self.assertTrue(ok, detail)
        self.assertIn("写不进设置", detail)
        self.assertIs(info["settingOk"], False)

    def test_a_missing_model_library_is_reported_not_fatal(self):
        """模型库还不存在时**如实说**（权重一批的事），不算配置失败。"""
        missing = os.path.join(self.root, "nope-models")
        ok, detail, _ = backend_setup.configure(models_root_path=missing)
        self.assertTrue(ok, detail)
        self.assertIn("还不存在", detail)


class SecretIsNeverRegeneratedTests(_SetupCase):
    def test_jwt_secret_is_reused_not_regenerated(self):
        """反复写配置，jwt_secret 一个字符都不许变（它是所有客户端凭据的根）。"""
        self.assertTrue(self.configure()[0])
        first = self.config()["auth"]["jwt_secret"]
        self.assertTrue(first)
        for _ in range(3):
            self.assertTrue(self.configure()[0])
            self.assertEqual(self.config()["auth"]["jwt_secret"], first)

        # 手工改过配置（哪怕只改了别处）也**不许**换密钥
        text = open(backend_setup.config_path(), "r", encoding="utf-8").read()
        self.write_config_text(text.replace(first, "hand-written-secret"))
        self.assertTrue(self.configure()[0])
        self.assertEqual(self.config()["auth"]["jwt_secret"], "hand-written-secret")

    def test_secret_file_is_used_when_the_config_is_gone(self):
        """配置被删了、但 `jwt-secret.txt` 还在 → 沿用文件里那份。"""
        with open(backend_setup.secret_path(), "w", encoding="utf-8") as fh:
            fh.write("secret-from-file")
        ok, detail, _ = self.configure()
        self.assertTrue(ok, detail)
        self.assertEqual(self.config()["auth"]["jwt_secret"], "secret-from-file")

    def test_fallback_parse_keeps_the_secret_without_yaml(self):
        """没有 yaml 库（或文件半坏）时，兜底解析也要能把密钥抠出来。"""
        text = 'auth:\n  mode: "jwt"\n  jwt_secret: "abc123"\n'
        self.write_config_text(text)
        self.assertEqual(backend_setup.existing_secret(), "abc123")
        self.assertEqual(backend_setup._secret_from_text(text), "abc123")


class SettingPathTests(_SetupCase):
    """走**真的** `remember_pair_path`（设置对象换成假的）—— 三种分支都要对。"""

    class _FakeSettings:
        def __init__(self, value=""):
            self.value = value
            self.updates = []

        def get(self, key, default=None):
            return self.value

        def update(self, mapping):
            self.updates.append(dict(mapping))
            self.value = mapping.get("capabilityLocalPairPath", self.value)

    def _remember(self, current, target):
        fake = self._FakeSettings(current)
        # 这个用例要的是**真实现**（模块属性已被 setUp 换成记录器，所以用留底那份）
        with patch("app.config.settings", fake):
            ok, note = self._real_remember_pair_path(target)
        return ok, note, fake

    def test_empty_setting_is_filled(self):
        ok, note, fake = self._remember("", "/tmp/x/local-pair.json")
        self.assertTrue(ok, note)
        self.assertEqual(fake.value, "/tmp/x/local-pair.json")
        self.assertIn("已把本机配对文件路径设为", note)

    def test_same_path_is_left_alone(self):
        target = os.path.join(self.root, "state", "local-pair.json")
        ok, note, fake = self._remember(target, target)
        self.assertTrue(ok, note)
        self.assertEqual(fake.updates, [])
        self.assertIn("已是", note)

    def test_a_real_hand_written_path_wins(self):
        mine = os.path.join(self.root, "elsewhere.json")
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write("{}")
        ok, note, fake = self._remember(mine, "/tmp/ours/local-pair.json")
        self.assertTrue(ok, note)
        self.assertEqual(fake.updates, [], "手工填过且文件还在 → 不许覆盖")
        self.assertIn("沿用", note)

    def test_a_stale_hand_written_path_is_replaced(self):
        ok, note, fake = self._remember(os.path.join(self.root, "gone.json"),
                                        "/tmp/ours/local-pair.json")
        self.assertTrue(ok, note)
        self.assertEqual(fake.value, "/tmp/ours/local-pair.json")
        self.assertIn("已不存在", note)


class PairSkipsTests(_SetupCase):
    def _stub_creds(self, base_url):
        p = patch.object(cred, "load", lambda: BackendCredentials(
            base_url=base_url, client_id="cli-1", secret="shh"))
        p.start()
        self.addCleanup(p.stop)

    def test_skip_when_already_paired_to_the_same_loopback(self):
        """同一个回环地址 → 跳过（否则每点一次就在服务端多一个客户端）。"""
        self._stub_creds("http://127.0.0.1:8900")
        with patch.object(pairing, "pair_local",
                          side_effect=AssertionError("不该再配一次")):
            ok, detail = backend_setup.pair_if_needed()
        self.assertTrue(ok, detail)
        self.assertIn("跳过", detail)

    def test_a_different_pairing_is_not_overwritten(self):
        """已配对到**别的**后端 → 不覆盖（除非调用方明确 replace）。"""
        self._stub_creds("http://gpu-01:8900")
        ok, detail = backend_setup.pair_if_needed()
        self.assertFalse(ok, detail)
        self.assertIn("不是本机后端", detail)

        os.makedirs(backend_setup.state_root(), exist_ok=True)
        with open(backend_setup.pair_file_path(), "w", encoding="utf-8") as fh:
            fh.write("{}")
        seen = {}

        def _pair_local(**kw):
            seen.update(kw)
            return BackendCredentials(base_url="http://127.0.0.1:8900",
                                      client_id="cli-2", secret="s2", server_name="本机")

        with patch.object(pairing, "pair_local", _pair_local):
            ok, detail = backend_setup.pair_if_needed(replace=True)
        self.assertTrue(ok, detail)
        self.assertIn("已连上本机后端", detail)
        self.assertEqual(seen.get("path"), backend_setup.pair_file_path())

    def test_missing_pair_file_is_reported(self):
        ok, detail = backend_setup.pair_if_needed()
        self.assertFalse(ok, detail)
        self.assertIn("还不存在", detail)

    def _touch_pair_file(self):
        os.makedirs(backend_setup.state_root(), exist_ok=True)
        with open(backend_setup.pair_file_path(), "w", encoding="utf-8") as fh:
            fh.write("{}")

    def test_it_retries_while_the_server_is_still_binding(self):
        """配对文件出现时服务可能还没 bind（uvicorn 先 lifespan 后绑端口）→ 连不上要重试。

        这不是"顺手加重试"：真机冒烟实测过这个窗口（文件写好了、8900 还没监听，
        立刻配对拿到的是 `WinError 10061 拒绝连接`）。
        """
        self._touch_pair_file()
        calls = []

        def _pair_local(**kw):
            calls.append(kw)
            if len(calls) < 3:
                raise pairing.PairingError("连不上 127.0.0.1:8900（拒绝连接）", code="offline")
            return BackendCredentials(base_url="http://127.0.0.1:8900",
                                      client_id="cli-9", secret="s", server_name="本机")

        with patch.object(pairing, "pair_local", _pair_local), \
                patch.object(backend_setup, "PAIR_FILE_POLL", 0.01):
            ok, detail = backend_setup.pair_if_needed(ready_timeout=3.0)
        self.assertTrue(ok, detail)
        self.assertEqual(len(calls), 3, "该在窗口内重试到服务能连上")
        self.assertIn("已连上本机后端", detail)

    def test_a_non_network_failure_is_not_retried(self):
        """配对码过期/文件坏了不重试：重试只是把同一句话重复二十遍。"""
        self._touch_pair_file()
        calls = []

        def _pair_local(**kw):
            calls.append(kw)
            raise pairing.PairingError("本机配对码已过期", code="expired")

        with patch.object(pairing, "pair_local", _pair_local), \
                patch.object(backend_setup, "PAIR_FILE_POLL", 0.01):
            ok, detail = backend_setup.pair_if_needed(ready_timeout=3.0)
        self.assertFalse(ok, detail)
        self.assertEqual(len(calls), 1, "非网络类失败不该重试")
        self.assertIn("过期", detail)


class WaitForPairFileTests(_SetupCase):
    def test_it_returns_as_soon_as_the_file_appears(self):
        self.assertFalse(os.path.exists(backend_setup.pair_file_path()))
        os.makedirs(backend_setup.state_root(), exist_ok=True)
        with open(backend_setup.pair_file_path(), "w", encoding="utf-8") as fh:
            fh.write("{}")
        ok, detail = backend_setup.wait_for_pair_file(timeout=0.1)
        self.assertTrue(ok, detail)
        self.assertIn("local-pair.json", detail)

    def test_a_timeout_says_where_to_look(self):
        ok, detail = backend_setup.wait_for_pair_file(timeout=0.05)
        self.assertFalse(ok, detail)
        self.assertIn("没等到", detail)
        self.assertIn("backend.log", detail)

    def test_a_dead_backend_fails_fast(self):
        """后端起来就崩 → 立刻如实说，而不是让人干等满超时。"""
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
        with open(backend_pid.pid_path(), "w", encoding="utf-8") as fh:
            fh.write("999999")                      # 这个 pid 不存在
        t0 = time.monotonic()
        ok, detail = backend_setup.wait_for_pair_file(timeout=30.0)
        elapsed = time.monotonic() - t0
        self.assertFalse(ok, detail)
        self.assertIn("立刻退出", detail)
        self.assertLess(elapsed, 5.0, "该快速失败，实际等了 %.1f 秒" % elapsed)


class LaunchTests(_SetupCase):
    def test_launch_needs_a_runtime_and_a_config(self):
        ok, detail = backend_setup.launch()
        self.assertFalse(ok, detail)
        self.assertIn("runtime", detail)          # 还没装运行时

        with patch.object(backend_proc, "python_exe", lambda: "/fake/python"):
            ok, detail = backend_setup.launch()
        self.assertFalse(ok, detail)
        self.assertIn("配置文件", detail)          # 有解释器但还没 configure

    def test_launch_builds_the_module_command(self):
        self.assertTrue(self.configure()[0])
        seen = {}

        def _spawn(argv, cwd="", env=None, ports=None):
            seen.update({"argv": list(argv), "cwd": cwd, "env": dict(env or {}),
                         "ports": tuple(ports or ())})
            return True, "已启动后端 pid=1"

        with patch.object(backend_proc, "python_exe", lambda: "/fake/python"), \
                patch.object(backend_proc, "spawn", _spawn):
            ok, detail = backend_setup.launch()
        self.assertTrue(ok, detail)
        self.assertEqual(seen["argv"][1:3], ["-m", "server.main"])
        self.assertIn("--config", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--config") + 1],
                         backend_setup.config_path())
        self.assertEqual(seen["cwd"], self.root)
        self.assertEqual(seen["ports"], (backend_proc.DEFAULT_PORT,
                                         backend_proc.DEFAULT_ADMIN_PORT))
        # 缓存与家目录指到可写目录（缺了它模型下载会失败）
        self.assertEqual(seen["env"]["MODELSCOPE_CACHE"], backend_setup.cache_root())
        self.assertEqual(seen["env"]["HOME"], backend_setup.cache_root())


class StartSequenceTests(_SetupCase):
    def test_a_failed_configure_stops_the_sequence(self):
        """第一步失败就返回：不启动、不配对、不多写任何设置。"""
        with patch.object(backend_setup, "configure",
                          lambda **kw: (False, "写不了配置", {"path": "x"})), \
                patch.object(backend_setup, "launch",
                             side_effect=AssertionError("不该启动")):
            res = backend_setup.start(fetch_runtime=False)
        self.assertFalse(res["ok"])
        self.assertEqual([s["name"] for s in res["steps"]], ["configure"])
        self.assertEqual(self.settings_written, [])

    def test_already_running_counts_as_success(self):
        """幂等：后端上次起的、还在跑**不该**报错，而要继续等文件、配对、再过就绪自测。"""
        calls = []
        with patch.object(backend_setup, "configure",
                          lambda **kw: (True, "配置已写好", {"path": "c", "pairFile": "p"})), \
                patch.object(backend_setup, "launch",
                             lambda **kw: (False, "ECHO 起的后端已经在跑（pid=7），不再起第二个")), \
                patch.object(backend_setup, "wait_for_pair_file",
                             lambda **kw: (calls.append("wait") or (True, "文件在了"))), \
                patch.object(backend_setup, "pair_if_needed",
                             lambda **kw: (calls.append("pair") or (True, "已连上本机后端"))), \
                patch.object(backend_ready, "probe",
                             lambda url, **kw: (calls.append("ready") or
                                                {"ok": True, "state": "ok",
                                                 "headline": "三层都过了"})):
            res = backend_setup.start(fetch_runtime=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual([s["name"] for s in res["steps"]],
                         ["configure", "launch", "pair-file", "pair", "ready"])
        self.assertEqual(calls, ["wait", "pair", "ready"])
        self.assertIn("三层都过", res["message"])
        self.assertTrue(res["ready"]["ok"])

    def test_the_ready_step_can_be_turned_off(self):
        """`ready_probe=False` 时不起就绪自测（老调用方与只验前四步的用例用得上）。"""
        with patch.object(backend_setup, "configure",
                          lambda **kw: (True, "配置已写好", {})), \
                patch.object(backend_setup, "launch", lambda **kw: (True, "起了")), \
                patch.object(backend_setup, "wait_for_pair_file",
                             lambda **kw: (True, "文件在了")), \
                patch.object(backend_setup, "pair_if_needed",
                             lambda **kw: (True, "已配对")):
            res = backend_setup.start(ready_probe=False, fetch_runtime=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual([s["name"] for s in res["steps"]],
                         ["configure", "launch", "pair-file", "pair"])

    def test_a_failed_ready_probe_fails_the_flow_with_its_own_sentence(self):
        """就绪自测没过 → 整条流程按失败收口，并**原样用那句结论**（health 绿 ≠ 能用）。"""
        headline = "模型就绪，但真实自测失败：模型没就绪（这一档正是那个坑）"
        with patch.object(backend_setup, "configure",
                          lambda **kw: (True, "配置已写好", {})), \
                patch.object(backend_setup, "launch", lambda **kw: (True, "起了")), \
                patch.object(backend_setup, "wait_for_pair_file",
                             lambda **kw: (True, "文件在了")), \
                patch.object(backend_setup, "pair_if_needed",
                             lambda **kw: (True, "已配对")), \
                patch.object(backend_ready, "probe",
                             lambda url, **kw: {"ok": False, "state": "asr-failed",
                                                "headline": headline}):
            res = backend_setup.start(fetch_runtime=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["message"], headline)
        self.assertEqual(res["steps"][-1]["name"], "ready")
        self.assertIs(res["steps"][-1]["ok"], False)


if __name__ == "__main__":
    unittest.main()
