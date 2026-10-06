# -*- coding: utf-8 -*-
"""**方案 1：本机自动配对**（2026-09-28，见 `docs/3.0-设计总览与组件关系.md` §6.6）。

ECHO 拆成"客户端 + 能力后端"两段之后，"本机跑得动"的实质就是**本机装了一个后端**。
那种情况下不该让人抄配对码（配对串是给**另一台**机器准备的）。这一层钉四件事：

1. **后端真的会写**：启动时把 `{state_root}/local-pair.json` 写出来（原子写、本用户可读），
   而且里面那张码是**真码** —— 拿去兑换能换到 clientId/secret。
2. **文件里没有凭据**：只有一次性配对码。secret 只落在客户端那一侧。
3. **客户端走的是同一条配对实现**：`pair_local()` 内部调 `pair()`，
   所以产物（`{DATA}/backend.json`、凭据信封、TLS 指纹固定）与手工粘配对串**逐字一致**。
4. **失败说人话**：文件不在 → 列出看过的位置并提示可用配对串；码过期 → 说清"让后端重启一次"。

服务端那一半**必须把 `server.state_root` 与 `auth.db` 都指到临时目录**：不指的话
`localpair.publish()` 会去写开发机上真实的那份状态目录（AGENTS.md 里"测试动真实状态"
是同一形状的事故）。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import pairing                                     # noqa: E402
from app.capabilities.pairing import PairingError                        # noqa: E402
from server import auth as auth_mod                                      # noqa: E402
from server import localpair                                            # noqa: E402
from server import settings as settings_mod                              # noqa: E402


class _ServerCase(unittest.TestCase):
    """一个**状态全在临时目录里**的后端配置 + 鉴权。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="echo-localpair-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.cfg = settings_mod.load()
        self.cfg.raw["server"]["state_root"] = self.tmpdir
        self.cfg.raw["auth"]["db"] = os.path.join(self.tmpdir, "auth.db")
        self.store = auth_mod.open_store(self.cfg)
        self.auth = auth_mod.Auth(self.cfg, self.store)


class LocalPairFileTests(_ServerCase):
    def test_publish_writes_a_usable_code_and_no_credentials(self):
        body = localpair.publish(self.cfg, self.auth)
        target = os.path.join(self.tmpdir, localpair.FILENAME)
        self.assertTrue(os.path.isfile(target), "文件没写到 state_root 里")
        with open(target, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["code"], body["code"])
        self.assertTrue(on_disk["baseUrl"].startswith("http://127.0.0.1:"),
                        "同机客户端应该走回环，而不是网卡地址：%s" % on_disk["baseUrl"])
        self.assertTrue(on_disk["url"].startswith("echo://pair?"), "整串必须与命令行同源")
        self.assertGreater(float(on_disk["expiresAt"]), time.time())

        # **文件里不许有凭据**（只有一次性配对码；secret 只在客户端那侧）
        with open(target, encoding="utf-8") as f:
            text = f.read()
        for forbidden in ("secret", "clientId", "client_id", "token"):
            self.assertNotIn(forbidden, text, "本机配对文件里出现了 %r —— 那不是它该放的东西"
                             % forbidden)

        # 那张码是**真码**：拿去兑换能换到 clientId/secret
        got = self.auth.redeem(str(on_disk["code"]), "本机自动配对")
        self.assertTrue(got.get("clientId") and got.get("secret"),
                        "写进文件的码兑换不出凭据：%s" % got)

    def test_the_code_is_one_shot(self):
        """用掉即删（既有语义）—— 所以文件是"配一次就够"，不是长期凭据。"""
        body = localpair.publish(self.cfg, self.auth)
        self.auth.redeem(body["code"], "本机自动配对")
        with self.assertRaises(Exception):
            self.auth.redeem(body["code"], "第二次")

    def test_restarting_the_backend_does_not_pile_up_pairing_codes(self):
        """**每次启动叫一次 publish，不等于每次发一张新码**（2026-10-06 修）。

        现场（用户报的）：本机客户端一旦判定"已配对到本机回环地址"就**跳过配对** ——
        它压根不来兑换这张码。而原来每次后端启动都发一张新的，于是每启动一次就多一张
        没人用的码，管理面「待用配对码」那页跟着涨（本机实测 dev 攒到 16 张）。
        那页按设计是"与实际库一致"的自证页，所以这不是纯显示问题。

        判据**同时看两个面**：文件里的明文码不变，且**库里行数不增**。
        只看明文会漏掉"每次发新码但文件总是最后一张"的实现。
        """
        first = localpair.publish(self.cfg, self.auth)
        n1 = len(self.store.pairing_codes())
        for _ in range(3):                      # 再"启动"三次
            again = localpair.publish(self.cfg, self.auth)
            self.assertEqual(again["code"], first["code"], "又发了一张新码 —— 会越积越多")
        self.assertEqual(len(self.store.pairing_codes()), n1,
                         "配对码行数涨了：每次启动都在发新码")
        self.assertEqual(n1, 1, "本机自动配对同一时刻只该有 1 张码")

    def test_a_redeemed_code_is_replaced_by_a_fresh_one(self):
        """码被兑换走后**必须补一张新的** —— 那时客户端是真的需要它。

        复用不能变成"永远指着同一张已经不存在的码"：客户端凭据损坏/被清时会真的来兑换，
        那时若文件里是一张查无此码的死码，本机自动配对就彻底断了。
        """
        first = localpair.publish(self.cfg, self.auth)
        self.auth.redeem(first["code"], "本机自动配对")
        second = localpair.publish(self.cfg, self.auth)
        self.assertNotEqual(second["code"], first["code"], "兑换后没有换新码")
        self.assertEqual(len(self.store.pairing_codes()), 1)
        # 新码必须仍然能用（不是随便回了个字符串）
        got = self.auth.redeem(second["code"], "本机自动配对")
        self.assertTrue(got.get("clientId"), "换出来的新码兑换不了：%s" % got)

    def test_ttl_comes_from_the_server_setting(self):
        self.cfg.raw["auth"]["local_pair_ttl_s"] = 123.0
        body = localpair.publish(self.cfg, self.auth)
        self.assertAlmostEqual(float(body["expiresAt"]) - time.time(), 123.0, delta=5.0)
        self.assertFalse(localpair.expired(body))
        self.assertTrue(localpair.expired(body, now=time.time() + 200))

    def test_the_file_follows_the_auth_db_directory(self):
        """**测试隔离靠这条**：`auth.db` 显式给了就写在它旁边。

        不这么做的话，每个进 lifespan 的用例都会往开发机真实的
        `{ECHO}/data/server-state/` 里写一张新的配对码（见 `_state_dir` 的说明）。
        """
        cfg = settings_mod.load()
        other = tempfile.mkdtemp(prefix="echo-localpair-authdb-")
        self.addCleanup(shutil.rmtree, other, True)
        cfg.raw["auth"]["db"] = os.path.join(other, "auth.db")
        cfg.raw["server"]["state_root"] = self.tmpdir      # 故意与 auth.db 不同
        self.assertEqual(os.path.dirname(localpair.path(cfg)), other)

    def test_read_and_clear(self):
        localpair.publish(self.cfg, self.auth)
        self.assertIsInstance(localpair.read(self.cfg), dict)
        self.assertTrue(localpair.clear(self.cfg))
        self.assertIsNone(localpair.read(self.cfg))
        self.assertFalse(localpair.clear(self.cfg), "重复删不该报成功")

    def test_file_is_owner_only_on_posix(self):
        if os.name != "posix":
            self.skipTest("Windows 上没有 POSIX 权限位（靠用户目录 ACL）")
        localpair.publish(self.cfg, self.auth)
        mode = os.stat(os.path.join(self.tmpdir, localpair.FILENAME)).st_mode & 0o777
        self.assertEqual(mode, 0o600, "本机配对文件必须只有本用户可读（实际 %o）" % mode)

    def test_loopback_even_when_listening_on_wildcard(self):
        self.cfg.raw["server"]["listen"] = "0.0.0.0:8900"
        self.assertIn("127.0.0.1:8900", localpair.local_base_url(self.cfg))
        self.cfg.raw["server"]["local_pair_base_url"] = "http://127.0.0.1:9999"
        self.assertEqual(localpair.local_base_url(self.cfg), "http://127.0.0.1:9999")

    def test_the_local_file_ignores_the_advertised_host(self):
        """**本机形态不需要「对外公布地址」**（2026-09-30 用户定的口径）。

        同一份配置可能既服务本机、又服务同事（那时 `server.advertised_host` 填的是
        局域网 IP）。本机这条路的地址**必须**是回环 —— 它跟"同事能不能连到这台机器的
        局域网 IP"是两个不相干的问题。这里同时钉 `baseUrl` 与串里的 `host=`：
        只钉前者的话，串仍会带着一个本机**不该走**的网卡地址（排障时会误导人）。
        """
        self.cfg.raw["server"]["advertised_host"] = "10.100.0.24"
        body = localpair.publish(self.cfg, self.auth)
        self.assertTrue(body["baseUrl"].startswith("http://127.0.0.1:"),
                        "本机客户端该走回环：%s" % body["baseUrl"])
        self.assertIn("host=http://127.0.0.1:", body["url"],
                      "本机配对串里也不该出现局域网地址：%s" % body["url"])
        self.assertNotIn("10.100.0.24", body["url"])


class LocalPairClientTests(unittest.TestCase):
    """客户端侧：找文件 → 交给既有 `pair()`；失败说人话。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="echo-localpair-cli-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.path = os.path.join(self.tmpdir, localpair.FILENAME)

    def _write(self, **over):
        body = {"source": "local", "baseUrl": "http://127.0.0.1:8900", "code": "ABCD1234",
                "fingerprint": "sha256:deadbeef", "expiresAt": time.time() + 3600}
        body.update(over)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        return body

    def test_candidates_put_the_explicit_setting_first(self):
        with patch.dict(os.environ, {"ECHO_LOCAL_PAIR_PATH": "/tmp/env-local-pair.json"}):
            with patch.object(pairing, "_local_pair_setting", lambda: "/tmp/explicit.json"):
                cands = pairing.local_pair_candidates()
        self.assertEqual(cands[0], os.path.abspath("/tmp/explicit.json"))
        self.assertIn(os.path.abspath("/tmp/env-local-pair.json"), cands)
        self.assertEqual(len(cands), len(set(cands)), "候选里有重复")

    def test_missing_file_says_what_it_looked_at(self):
        with self.assertRaises(PairingError) as cm:
            pairing.pair_local(path=os.path.join(self.tmpdir, "nope.json"))
        self.assertEqual(cm.exception.code, "absent")
        self.assertIn("没找到本机后端的配对文件", cm.exception.message)
        self.assertIn("配对串", cm.exception.message, "要给出退路：手工配对串")

    def test_broken_file_is_not_silently_ignored(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        with self.assertRaises(PairingError) as cm:
            pairing.pair_local(path=self.path)
        self.assertIn("读不出来", cm.exception.message)

    def test_expired_code_points_at_restarting_the_backend(self):
        self._write(expiresAt=time.time() - 10)
        with self.assertRaises(PairingError) as cm:
            pairing.pair_local(path=self.path)
        self.assertEqual(cm.exception.code, "expired")
        self.assertIn("过期", cm.exception.message)
        self.assertIn("重启", cm.exception.message)

    def test_it_reuses_the_one_pairing_path(self):
        """**不许有第二套配对实现**：必须调既有的 `pair()`，且把 baseUrl/code/fp 原样传下去。"""
        body = self._write()
        seen = {}

        def fake_pair(base_url, code, client_name="", **kw):
            seen.update(base_url=base_url, code=code, client_name=client_name, **kw)
            return "CREDS"

        with patch.object(pairing, "pair", fake_pair):
            out = pairing.pair_local(path=self.path, save=False)
        self.assertEqual(out, "CREDS")
        self.assertEqual(seen["base_url"], body["baseUrl"])
        self.assertEqual(seen["code"], body["code"])
        self.assertEqual(seen["cert_fingerprint"], body["fingerprint"],
                         "配对串里的指纹必须传下去 —— 那是防中间人的那一步")
        self.assertFalse(seen["save"], "save 要能透传（用例不落盘）")

    def test_state_view_never_leaks_the_code(self):
        self._write()
        with patch.object(pairing, "local_pair_candidates", lambda: [self.path]):
            view = pairing.local_pair_state()
        self.assertTrue(view["found"])
        self.assertFalse(view["expired"])
        self.assertEqual(view["baseUrl"], "http://127.0.0.1:8900")
        self.assertNotIn("code", view)
        self.assertNotIn("ABCD1234", json.dumps(view))


class LocalPairSwitchTests(unittest.TestCase):
    """**它是部署开关，默认关**（2026-09-28）—— 这一条同时是"测试不写真实状态"的保险。

    `localpair.publish()` 会往鉴权库里发一张配对码。如果 lifespan 无条件写它，
    每个进 lifespan 的用例都会：① 往开发机真实的状态目录写文件；② 让"库里只有我刚发的
    那一张码"这类断言多出一行。所以开关放在**调用点**（lifespan），默认关，
    由交付路径（compose / 后端包）打开。
    """

    def test_default_is_off_and_the_example_config_says_so(self):
        from server import settings as settings_mod
        self.assertIs(settings_mod.DEFAULTS["server"]["local_pair"], False,
                      "默认必须是关的：lifespan 里那次发布只在交付路径上开")
        self.assertEqual(settings_mod.DEFAULTS["auth"]["local_pair_ttl_s"], 7 * 24 * 3600)
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "server", "echo-server.example.yaml")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("local_pair:", text, "示例配置要写出这个开关（否则没人知道有它）")
        self.assertIn("local_pair_ttl_s:", text)

    def test_env_can_turn_it_on(self):
        from server import settings as settings_mod
        with patch.dict(os.environ, {"ECHO_LOCAL_PAIR": "1"}):
            cfg = settings_mod.load()
        self.assertTrue(cfg.get("server.local_pair"))

    def test_the_shipped_deploy_paths_turn_it_on(self):
        """方案 1 的交付路径必须打开它 —— 否则"本机自动配对"在真机上等于没做。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("server/compose.yaml",
                    "delivery/backend-cu126/compose.yaml",
                    "delivery/backend-cu118/compose.yaml"):
            with self.subTest(compose=rel):
                with open(os.path.join(root, rel), encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("ECHO_LOCAL_PAIR", text,
                              "%s 没打开本机自配对 —— 方案 1 的用户还是得抄配对码" % rel)


if __name__ == "__main__":
    unittest.main()
