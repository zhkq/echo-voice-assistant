# -*- coding: utf-8 -*-
"""管理面（设计 §8.4）：**独立端口上的控制台**。

这一组用例盯四件事：

1. **进不来就不给看**：没登录一律 401；口令错不给"用户名不存在 vs 口令不对"的区分
   （那等于送一个枚举账号的接口）；失败多了要退避。
2. **与能力面严格隔离**：管理面 app 上没有 `/v1/*`，能力面 app 上没有 `/admin/*`。
   设计说这靠**两个端口**来保证 —— 那就有两条会红的用例钉着，而不是一句承诺。
3. **写面有七道闸**（2026-09-25 用户要求"发授权"等写动作上管理面）：
   会话、同站 `Origin`/`Referer`、非简单请求标志头、CSRF、二次确认、秘密只回显一次、
   每个动作（含失败）落审计。原来那条"管理面一个写端点都不许有"的用例改成了
   **写端点逐个登记 + 逐条实测**（`WRITE_ENDPOINTS` / `WriteGuardTests`）——
   加端点是允许的，但"忘了加防护"必须红。
4. **只读那半边不许被一起锁死**：`/overview` 这些照旧只需要登录（不要求写请求那几个头）。

写用例的通用做法：**从 `openapi()` 现读写端点**（`_AdminCase.write_requests`），
而不是在测试里另抄一份清单 —— 抄的那份迟早与真实路由表漂开，
而"新加的写端点没被覆盖到"恰恰是最该被自动发现的事。
"""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                          # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

from server import admin as admin_mod                                # noqa: E402
from server import engines, errors, main as server_main              # noqa: E402
from server import routes as routes_mod                              # noqa: E402
from server import settings as settings_mod                          # noqa: E402
from server import store as store_mod                                # noqa: E402
from server import auth as auth_mod                                  # noqa: E402
from tests.test_server_contract import FAKE_SPECS, _fake_loader, _wav_bytes  # noqa: E402


#: 管理面的写端点（**登记表**，不是白名单开关）。
#:
#: 它存在的意义：加一个写端点时必须在这里添一行 —— 而改动这一行的那个 diff
#: 会逼人回答一次"它的防护是什么"（设计 §8.4 那七道闸）。
#: `login` 是**唯一**免 CSRF 的写端点（登录时还没有会话），所以它天然豁免中间件；
#: `logout` 不豁免（它是改状态的动作，而且此时会话已经在了）。
WRITE_ENDPOINTS = {
    "/admin/api/login",
    "/admin/api/logout",
    "/admin/api/pairing-codes",
    "/admin/api/pairing-codes/{code_id}",
    "/admin/api/clients/{client_id}/disable",
    "/admin/api/clients/{client_id}/enable",
    "/admin/api/clients/{client_id}/revoke",
    "/admin/api/clients/{client_id}/scopes",
    "/admin/api/clients/{client_id}/quota",
    "/admin/api/clients/{client_id}/rotate-secret",
}


def _cfg(tmp):
    cfg = settings_mod.load()
    cfg.raw["tmp"]["root"] = os.path.join(tmp, "tmp")
    cfg.raw["server"]["state_root"] = os.path.join(tmp, "state")
    cfg.raw["auth"]["db"] = os.path.join(tmp, "auth.db")
    # 换令牌那条路要一个密钥（`Auth.key` 没配就抛 auth_misconfigured）。
    # 32 字节以上，免得 PyJWT 每次都告警把输出弄脏。
    cfg.raw["auth"]["enabled"] = True
    cfg.raw["auth"]["mode"] = "jwt"
    cfg.raw["auth"]["jwt_secret"] = "0123456789abcdef0123456789abcdef"
    cfg.raw["models"]["specs"] = FAKE_SPECS
    return cfg


class PasswordTests(unittest.TestCase):
    def test_a_password_verifies_and_a_wrong_one_does_not(self):
        h = admin_mod.hash_password("correct horse")
        self.assertTrue(admin_mod.verify_password("correct horse", h))
        self.assertFalse(admin_mod.verify_password("Correct Horse", h))
        self.assertFalse(admin_mod.verify_password("", h))

    def test_the_stored_form_has_no_plaintext(self):
        h = admin_mod.hash_password("hunter2-very-secret")
        self.assertNotIn("hunter2", h)
        self.assertTrue(h.startswith("scrypt$"), h)

    def test_two_hashes_of_the_same_password_differ(self):
        """每次都要新盐 —— 否则"两个人口令一样"从库上就能看出来。"""
        self.assertNotEqual(admin_mod.hash_password("same"), admin_mod.hash_password("same"))

    def test_a_garbage_stored_value_is_false_not_an_exception(self):
        for bad in ("", "plaintext", "scrypt$only-one", "scrypt$!!$!!", "argon2id$x$y"):
            with self.subTest(bad=bad):
                self.assertFalse(admin_mod.verify_password("x", bad))

    def test_generated_passwords_are_long_enough_and_unique(self):
        a, b = admin_mod.new_password(), admin_mod.new_password()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 16)


class SessionStoreTests(unittest.TestCase):
    def test_create_get_drop(self):
        s = admin_mod.SessionStore()
        sess = s.create("ops")
        self.assertTrue(s.get(sess["token"]))
        self.assertEqual(s.count(), 1)
        s.drop(sess["token"])
        self.assertIsNone(s.get(sess["token"]))

    def test_an_expired_session_is_gone(self):
        s = admin_mod.SessionStore(ttl_s=0.01)
        sess = s.create("ops")
        time.sleep(0.05)
        self.assertIsNone(s.get(sess["token"]))

    def test_disabling_a_user_kills_his_sessions(self):
        """禁用要**立刻**生效 —— 不能等会话自然过期（8 小时）。"""
        s = admin_mod.SessionStore()
        s.create("ops")
        s.create("ops")
        s.create("other")
        self.assertEqual(s.drop_user("ops"), 2)
        self.assertEqual(s.count(), 1)

    def test_tokens_are_unpredictable(self):
        s = admin_mod.SessionStore()
        a, b = s.create("x"), s.create("x")
        self.assertNotEqual(a["token"], b["token"])
        self.assertNotEqual(a["csrf"], b["csrf"])
        self.assertGreaterEqual(len(a["token"]), 32)


class _AdminCase(unittest.TestCase):
    """一个装了管理员账号的 app（假引擎、临时库）。"""

    #: 管理面测试用的 base_url。**必须是回环地址**：写请求的同站校验拿它当 `Host`，
    #: 而 `Host: testserver`（TestClient 的默认值）不是本站 —— 那会把每个写用例
    #: 都变成 403，而且是"测出来的 403"而不是"真的拦住了"。
    ADMIN_BASE = "http://127.0.0.1:8901"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-admin-")
        self.cfg = _cfg(self.tmp)
        store = store_mod.Store(self.cfg.get("auth.db"))
        store.upsert_admin("ops", admin_mod.hash_password("good-pass"))
        store.upsert_admin("off", admin_mod.hash_password("good-pass"))
        store.set_admin_disabled("off", True)
        store.close()
        # ⚠️ `create_app()` 会调 `install_paths_seam()` —— 那是**进程内全局**的
        # monkey-patch（装完之后整个进程的 `paths.*` 都改读服务端配置）。不在用例里
        # 还原，**排在后面的测试文件**会红，而它报的是 paths 的默认值，现象完全
        # 联想不到是这里留下的状态。这条以前真踩过（见 AGENTS.md），
        # 而且有一条护栏用例专门扫"起了真 app 的测试文件有没有还原"
        # （`PathsSeamTests.test_every_test_that_starts_a_real_app_restores_the_seam`）——
        # 我这一版第一遍就是被它抓住的。
        from app import paths
        self._paths_seam = getattr(paths, "_settings_get", None)
        self.addCleanup(self._restore_seam)
        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": _fake_loader}):
            self.cap = server_main.create_app(self.cfg)
        self.client = TestClient(self.cap)
        self.client.__enter__()
        self.addCleanup(self._down)
        self.state = self.cap.state.echo
        self.admin = admin_mod.create_admin_app(self.cfg, self.state)
        self.ac = TestClient(self.admin, base_url=self.ADMIN_BASE)
        self.csrf = ""

    def _restore_seam(self):
        from app import paths
        if self._paths_seam is None:
            try:
                delattr(paths, "_settings_get")
            except AttributeError:
                pass
        else:
            paths._settings_get = self._paths_seam

    def _down(self):
        try:
            self.client.__exit__(None, None, None)
        except Exception:
            pass

    def login(self, user="ops", password="good-pass"):
        r = self.ac.post("/admin/api/login", json={"username": user, "password": password})
        if r.status_code == 200:
            self.csrf = r.json().get("csrf") or ""
        return r

    # ---- 写请求的公共头（缺一个就该被拒，所以它们是显式拼出来的）----

    def wheaders(self, *, csrf=True, header=True, origin="http://127.0.0.1:8901"):
        h = {}
        if csrf:
            h["X-CSRF-Token"] = self.csrf
        if header:
            h[admin_mod.WRITE_HEADER] = "1"
        if origin:
            h["Origin"] = origin
        return h

    def wpost(self, path, payload=None, **kw):
        return self.ac.post(path, json=(payload if payload is not None else {}),
                            headers=self.wheaders(**kw))

    def wdelete(self, path, payload=None, **kw):
        return self.ac.request("DELETE", path, json=(payload if payload is not None else {}),
                               headers=self.wheaders(**kw))

    def write_requests(self):
        """写端点 → 可以直接打的一次请求：`(method, 路径模板, 可打的 URL)`。

        **从 `openapi()` 现读**（不是另抄一份清单）：新加的写端点会自动进这一组，
        于是"新端点忘了加防护"当场红。
        """
        out = []
        for path, methods in sorted((self.admin.openapi().get("paths") or {}).items()):
            if not path.startswith("/admin/api") or path == "/admin/api/login":
                continue
            for method in sorted(methods):
                if method in ("get", "head", "options"):
                    continue
                real = path.replace("{client_id}", "cli-nope").replace("{code_id}", "deadbeef")
                out.append((method.upper(), path, real))
        return out

    def db_state(self):
        """库里"看得见的状态"快照（客户端 / 配对码）。用来断言**没有副作用**。"""
        store = self.state.auth.store
        return ([(r["client_id"], r["token_version"], int(r["disabled"]), r["scopes"],
                  float(r["daily_audio_minutes"])) for r in store.clients()],
                sorted(r["code_hash"] for r in store.pairing_codes()))

    def audit_rows(self):
        """审计行（谁 / 做了什么 / 对谁）。**不排序依赖**：时间戳可能撞在同一刻。"""
        return [(r["admin"], r["action"], r["target"])
                for r in self.state.auth.store.recent_audit(200)]

    def cap_headers(self, client_id="cli-1"):
        """造一个客户端并给它一个真令牌（能力面开着鉴权，未鉴权的请求**不记账**）。"""
        store = self.state.auth.store
        if store.client(client_id) is None:
            store.upsert_client(client_id, "测试机",
                                auth_mod.hash_secret("s", client_id), scopes="asr")
            self.state.auth.cache.forget(client_id)
        row = store.client(client_id)
        token, _ = auth_mod.issue_token(row, self.state.auth.key, 3600)
        return {"Authorization": "Bearer " + token, "Content-Type": "audio/wav"}

    def call_asr(self):
        return self.client.post("/v1/asr", content=_wav_bytes(seconds=1.0),
                                headers=self.cap_headers())


class LoginTests(_AdminCase):
    def test_the_page_is_served_without_login(self):
        """页面本身可以拿（它只是个空壳），**数据**才要登录。"""
        r = self.ac.get("/admin/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("管理面", r.text)
        self.assertNotIn("api_key", r.text)

    def test_every_data_endpoint_requires_a_login(self):
        for path in ("/admin/api/overview", "/admin/api/models", "/admin/api/clients",
                     "/admin/api/calls", "/admin/api/inventory", "/admin/api/me"):
            with self.subTest(path=path):
                r = self.ac.get(path)
                self.assertEqual(r.status_code, 401, r.text)
                self.assertEqual(r.json()["code"], "unauthorized")

    def test_a_wrong_password_is_refused_without_saying_which_part_was_wrong(self):
        """**不许区分**"没有这个用户"与"口令不对" —— 那等于送一个枚举账号的接口。

        判据不是"含某个词"，而是**两种失败的响应体逐字相同**（code / message / detail 都比）。
        这一条比我第一版写的字符串匹配强：文案改了它照样成立，而"两种失败不一样"永远会红。
        （`unauthorized(detail=…)` 把细节放在 `detail` 里、`message` 是固定文案 ——
        所以我第一版断言 `message` 里含"用户名或口令"必然失败。）
        """
        a = self.login(password="nope")
        b = self.login(user="nobody")
        self.assertEqual(a.status_code, 401)
        self.assertEqual(b.status_code, 401)
        self.assertEqual(a.json(), b.json(),
                         "两种失败的响应体不一样 —— 前端能据此判断账号是否存在")

    def test_the_detail_is_the_same_sentence_in_both_cases(self):
        r = self.login(password="nope")
        self.assertIn("用户名或口令", str(r.json().get("detail") or ""))

    def test_a_disabled_admin_cannot_log_in(self):
        r = self.login(user="off")
        self.assertEqual(r.status_code, 401)

    def test_a_successful_login_sets_a_cookie_and_returns_a_csrf_token(self):
        r = self.login()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["username"], "ops")
        self.assertTrue(body["csrf"])
        cookie = r.headers.get("set-cookie", "")
        self.assertIn(admin_mod.COOKIE_NAME, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie.replace("samesite", "SameSite"))

    def test_brute_force_is_throttled(self):
        """免凭据的登录端点**必须**防爆破（与 `/v1/pair` 同一个道理）。"""
        codes = [self.login(password="bad").status_code for _ in range(7)]
        self.assertIn(429, codes, codes)
        self.assertEqual(codes[0], 401)

    def test_logout_needs_the_csrf_token_and_drops_the_session(self):
        """登出也是**改状态**的动作：2026-09-25 起它同样受写请求那三道闸管。

        所以"没带 CSRF 令牌就登出"这条老断言还在，但请求现在得先过
        `Origin`/`X-ECHO-Admin` 那两关才轮得到 CSRF 被检查 —— 缺头也是 403，
        这正是写面默认拒绝的意思。
        """
        self.login()
        bad = self.ac.post("/admin/api/logout")
        self.assertEqual(bad.status_code, 403, "没带任何写请求的头也让它登出？")
        no_csrf = self.ac.post("/admin/api/logout",
                               headers=self.wheaders(csrf=False))
        self.assertEqual(no_csrf.status_code, 403)
        self.assertIn("CSRF", no_csrf.json()["detail"])
        ok = self.ac.post("/admin/api/logout", headers=self.wheaders())
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self.ac.get("/admin/api/overview").status_code, 401)

    def test_login_is_audited(self):
        self.login()
        store = self.state.auth.store
        rows = store.recent_audit(5)
        self.assertEqual([(r["admin"], r["action"]) for r in rows], [("ops", "login")])


class DataTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_overview_shape(self):
        self.call_asr()
        d = self.ac.get("/admin/api/overview").json()
        self.assertEqual(d["server"]["id"], self.cfg.get("server.id"))
        self.assertIn("uptimeSeconds", d["server"])
        self.assertIn("active", d["busy"])
        self.assertIn("models", d)
        self.assertIn("defaultMinutes", d["quota"])
        self.assertGreaterEqual(d["metrics"]["totalCalls"], 1)
        self.assertEqual(d["sessions"], 1)
        self.assertIn("dropped", d["calls"])

    def test_models_shape(self):
        d = self.ac.get("/admin/api/models").json()
        self.assertTrue(d["models"])
        row = d["models"][0]
        for key in ("id", "state", "slot", "resident", "maxConcurrency", "estVramMb"):
            self.assertIn(key, row)

    def test_clients_shape_and_per_client_totals(self):
        # 先造一个客户端并真调一次：这张表是**客户端的清单**，一个都没有时它本来就是空的
        self.call_asr()
        self.state.call_log.flush()
        d = self.ac.get("/admin/api/clients").json()
        self.assertTrue(d["clients"], d)
        row = d["clients"][0]
        for key in ("clientId", "scopes", "disabled", "calls", "audioMinutes",
                    "usedMinutesToday", "dailyAudioMinutes"):
            self.assertIn(key, row)

    def test_calls_shape(self):
        self.call_asr()
        self.state.call_log.flush()
        d = self.ac.get("/admin/api/calls").json()
        self.assertGreaterEqual(d["aggregate"]["total"], 1)
        self.assertTrue(d["recent"])
        row = d["recent"][0]
        self.assertIn("endpoint", row)
        # **元数据里没有内容**：那一行的键就是设计那十个
        from server.calls import CALL_FIELDS
        self.assertEqual(set(row.keys()), set(CALL_FIELDS))

    def test_inventory_lists_the_live_schema(self):
        d = self.ac.get("/admin/api/inventory").json()
        self.assertIn("clients", d["tables"])
        self.assertIn("calls", d["tables"])
        self.assertEqual(d["tables"]["calls"], list(d["tables"]["calls"]))
        self.assertIn("secret_hash", d["tables"]["clients"])
        self.assertEqual(sorted(d["whitelist"]), sorted(d["tables"].keys()))
        self.assertIn("没有内容", d["statement"])

    def test_me_returns_the_csrf_token_for_later_writes(self):
        d = self.ac.get("/admin/api/me").json()
        self.assertEqual(d["username"], "ops")
        self.assertTrue(d["csrf"])


class IsolationTests(_AdminCase):
    """两个 app 的路径集合**不许交叉**（设计 §8.4：靠两个端口隔离）。"""

    def _paths(self, app):
        out = set()
        for route in getattr(app, "routes", []):
            p = str(getattr(route, "path", "") or "")
            if p:
                out.add(p)
        out |= set((app.openapi().get("paths") or {}).keys())
        return out

    def test_the_admin_app_has_no_capability_routes(self):
        paths = self._paths(self.admin)
        leaked = sorted(p for p in paths if p.startswith("/v1"))
        self.assertEqual(leaked, [], "管理面暴露了能力面端点：%s" % leaked)

    def test_the_capability_app_has_no_admin_routes(self):
        paths = self._paths(self.cap)
        leaked = sorted(p for p in paths if p.startswith("/admin"))
        self.assertEqual(leaked, [], "能力面暴露了管理面端点：%s" % leaked)

    def test_every_write_endpoint_is_registered(self):
        """写端点**逐个登记**（`WRITE_ENDPOINTS`）—— 加一个就必须在这里添一行。

        这条取代了原来那条"管理面一个写端点都不许有"：2026-09-25 用户要求把
        "发授权"这类动作搬到面板上，所以判据从"不许有"变成"**每一个都在册**"。
        它不做安全断言（那由 `WriteGuardTests` 逐条实测），
        它的作用是让"顺手加个写端点"这件事在 diff 里显形。
        """
        admin_paths = set((self.admin.openapi().get("paths") or {}).keys())
        writes = set()
        for path in sorted(admin_paths):
            methods = set((self.admin.openapi()["paths"][path] or {}).keys())
            if methods & {"post", "put", "patch", "delete"}:
                writes.add(path)
        self.assertEqual(writes, WRITE_ENDPOINTS,
                         "写端点清单与登记表不一致（多的那一头要补理由，少的那头是登记表过期了）")
        # 能力面**一个写端点都没有多出来**：这条与上面那条是两件事，别合并
        cap_writes = set()
        for path, methods in (self.cap.openapi().get("paths") or {}).items():
            if set(methods) & {"post", "put", "patch", "delete"}:
                cap_writes.add(path)
        self.assertTrue(cap_writes <= {"/v1/asr", "/v1/diarize", "/v1/speaker/embed",
                                       "/v1/pair", "/v1/token"},
                        "能力面出现了白名单外的 POST：%s" % sorted(cap_writes))

    def test_a_capability_jwt_is_useless_on_the_admin_console(self):
        """能力面的客户端 JWT **认不到**管理面来 —— 两套身份，别串。"""
        from server import auth as auth_mod
        store = self.state.auth.store
        store.upsert_client("cli-1", "测试机", auth_mod.hash_secret("s", "cli-1"), scopes="asr")
        self.state.auth.cache.forget("cli-1")
        row = store.client("cli-1")
        token, _ = auth_mod.issue_token(row, self.state.auth.key, 3600)
        r = self.ac.get("/admin/api/overview", headers={"Authorization": "Bearer " + token})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "unauthorized")


class WriteGuardTests(_AdminCase):
    """写请求的三道闸：会话 / 同站 `Origin`(`Referer`) / 非简单请求标志头（+ CSRF）。

    **每条都从 `openapi()` 现读端点逐个打一遍** —— 所以以后新增一个写端点，
    它会自动被这几条覆盖到；"新端点忘了加防护"不该靠人记得回来看测试文件。
    """

    def test_every_write_endpoint_refuses_without_a_session(self):
        for method, path, real in self.write_requests():
            with self.subTest(endpoint="%s %s" % (method, path)):
                r = self.ac.request(method, real, json={})
                self.assertEqual(r.status_code, 401, r.text)
                self.assertEqual(r.json()["code"], "unauthorized")

    def test_no_side_effect_when_not_logged_in(self):
        """**未登录的写请求一个字节都不许落库** —— 只断言 401 是不够的。"""
        before = self.db_state()
        for method, path, real in self.write_requests():
            self.ac.request(method, real,
                            json={"confirm": True, "scopes": "asr", "dailyAudioMinutes": 1})
        self.assertEqual(self.db_state(), before)
        # 未登录的请求**连审计都不写**：不知道"是谁"，写了只是给扫描器一个刷表的入口
        self.assertEqual(self.audit_rows(), [])

    def test_a_cross_site_origin_is_refused(self):
        """`Origin: http://evil.example` → 403。

        这一条就是 **DNS-rebinding 的正面拦截**：攻击者把域名解析到 127.0.0.1 时，
        浏览器认为这是**同站**请求、cookie 照发（`SameSite=Strict` 拦不住它），
        但 `Origin` 是攻击者的域名 —— 对不上本站，403。
        """
        self.login()
        for method, path, real in self.write_requests():
            with self.subTest(endpoint="%s %s" % (method, path)):
                r = self.ac.request(method, real, json={"confirm": True},
                                    headers=self.wheaders(origin="http://evil.example"))
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn("Origin", r.json()["detail"])

    def test_the_same_host_on_another_port_is_not_the_same_site(self):
        self.login()
        r = self.wpost("/admin/api/clients/cli-nope/disable",
                       origin="http://127.0.0.1:9999")
        self.assertEqual(r.status_code, 403, r.text)

    def test_no_origin_and_no_referer_is_refused(self):
        """两个都没有 → 403（**不猜**"看着像同源就当同源"，那正是 CSRF 的入口）。"""
        self.login()
        r = self.wpost("/admin/api/clients/cli-nope/disable", origin=None)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("缺失", r.json()["detail"])

    def test_a_referer_is_accepted_when_the_origin_is_absent(self):
        self.login()
        good = self.wheaders(origin=None)
        good["Referer"] = "http://127.0.0.1:8901/admin/"
        r = self.ac.post("/admin/api/clients/cli-nope/disable", json={}, headers=good)
        # 过了闸门才会走到"没有这个客户端"那句 —— 404 就是放行的证据
        self.assertEqual(r.status_code, 404, r.text)
        bad = self.wheaders(origin=None)
        bad["Referer"] = "http://evil.example/admin/"
        r2 = self.ac.post("/admin/api/clients/cli-nope/disable", json={}, headers=bad)
        self.assertEqual(r2.status_code, 403, r2.text)

    def test_the_non_simple_request_header_is_required(self):
        """缺 `X-ECHO-Admin` → 403。

        为什么要这个头（而不是"有 cookie 就行"）：跨站页面能发**简单请求**
        （`<form method=post>`），也能发 `<img>`/`<script>`，但它们**都发不出自定义头**。
        所以"必须有这个头"直接把"只靠受害者浏览器里的 cookie 就能提权"堵死。
        """
        self.login()
        for method, path, real in self.write_requests():
            with self.subTest(endpoint="%s %s" % (method, path)):
                r = self.ac.request(method, real, json={"confirm": True},
                                    headers=self.wheaders(header=False))
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn(admin_mod.WRITE_HEADER, r.json()["detail"])

    def test_the_csrf_token_is_required(self):
        self.login()
        r = self.wpost("/admin/api/clients/cli-nope/disable", csrf=False)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("CSRF", r.json()["detail"])

    def test_login_is_the_only_write_endpoint_without_the_gate(self):
        """登录**天然豁免**（那时还没有会话，谈不上 CSRF/Origin 之外的身份）——
        它仍然受失败退避保护（见 `LoginTests`）。"""
        r = self.ac.post("/admin/api/login",
                         json={"username": "ops", "password": "good-pass"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_rejected_write_is_audited_with_the_admin_name(self):
        """闸门挡回来的请求**也留痕**（操作者是登录的那个管理员名）。"""
        self.login()
        self.wpost("/admin/api/clients/cli-nope/disable", header=False)
        hit = [r for r in self.audit_rows() if r[1] == admin_mod.GUARD_ACTION]
        self.assertTrue(hit, self.audit_rows())
        self.assertEqual(hit[0][0], "ops")
        self.assertIn("/admin/api/clients/cli-nope/disable", hit[0][2])

    def test_unauthenticated_probes_do_not_grow_the_audit_table(self):
        """未登录的扫描器不许把审计表当免费存储用（不然它就是新的噪音源）。"""
        for _ in range(5):
            self.ac.post("/admin/api/clients/cli-1/revoke", json={"confirm": True})
        self.assertEqual(self.audit_rows(), [])


class ReadOnlyNotLockedTests(_AdminCase):
    """**只读那半边不许被一起锁死**（第八条要求）。

    写请求要三道闸，不代表 GET 也要 —— 把只读一起锁上，第一次用的人会以为
    "面板坏了"（尤其 `curl` / 脚本这条路）。
    """

    def test_read_endpoints_need_only_a_login(self):
        self.login()
        self.call_asr()          # 先有一个客户端，好让 /clients/{id} 有东西可看
        for path in ("/admin/api/overview", "/admin/api/models", "/admin/api/clients",
                     "/admin/api/clients/cli-1", "/admin/api/calls", "/admin/api/inventory",
                     "/admin/api/me", "/admin/api/admins", "/admin/api/pairing-codes"):
            with self.subTest(path=path):
                r = self.ac.get(path)                     # 没有任何写请求的头
                self.assertEqual(r.status_code, 200, r.text)

    def test_a_foreign_origin_on_a_read_is_not_a_problem(self):
        """只读端点**故意**不设 Origin 闸：读到的数据本来就在登录之后，
        而浏览器不会让跨站脚本读走响应（没有 CORS 头）。给 GET 加 Origin 校验
        只会把"用脚本拉一份清单"这件事弄坏，换不来任何东西。"""
        self.login()
        r = self.ac.get("/admin/api/overview", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_page_itself_is_still_public(self):
        self.assertEqual(self.ac.get("/admin/").status_code, 200)


class ConfirmTests(_AdminCase):
    """危险动作（撤销 / 轮换 secret / 作废码）必须带 `confirm`。

    前端弹窗挡的是"手滑"，后端这个字段挡的是"直接构造一个请求"。
    """

    DANGEROUS = (("POST", "/admin/api/clients/cli-1/revoke"),
                 ("POST", "/admin/api/clients/cli-1/rotate-secret"),
                 ("DELETE", "/admin/api/pairing-codes/deadbeef"))

    def test_missing_confirm_is_a_400_and_changes_nothing(self):
        self.login()
        self.call_asr()
        before = self.db_state()
        for method, path in self.DANGEROUS:
            with self.subTest(path=path):
                r = self.ac.request(method, path, json={}, headers=self.wheaders())
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("confirm", r.json()["detail"])
        self.assertEqual(self.db_state(), before, "400 却已经改了库")

    def test_a_wrong_confirm_value_is_a_400(self):
        self.login()
        self.call_asr()
        r = self.wpost("/admin/api/clients/cli-1/revoke", {"confirm": "cli-别的"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.state.auth.store.client("cli-1")["token_version"], 1)

    def test_confirm_accepts_the_target_id_or_true(self):
        self.login()
        self.call_asr()
        self.assertEqual(self.wpost("/admin/api/clients/cli-1/revoke",
                                    {"confirm": "cli-1"}).status_code, 200)
        self.assertEqual(self.wpost("/admin/api/clients/cli-1/revoke",
                                    {"confirm": True}).status_code, 200)
        self.assertEqual(self.wpost("/admin/api/clients/cli-1/revoke",
                                    {"confirm": "true"}).status_code, 200)
        self.assertEqual(self.state.auth.store.client("cli-1")["token_version"], 4)

    def test_a_missing_confirm_is_audited_as_a_failure(self):
        self.login()
        self.call_asr()
        self.assertEqual(self.wpost("/admin/api/clients/cli-1/revoke", {}).status_code, 400)
        hit = [r for r in self.audit_rows() if r[1] == "revoke.failed"]
        self.assertTrue(hit, self.audit_rows())
        self.assertEqual(hit[0][0], "ops")
        self.assertIn("confirm", hit[0][2])


class PairingCodeWriteTests(_AdminCase):
    """「发授权」：一次性明文配对串、库里只有哈希、用掉即失效、可作废。"""

    def test_issue_returns_a_one_time_pairing_string(self):
        self.login()
        r = self.wpost("/admin/api/pairing-codes",
                       {"name": "张三的办公本", "scopes": "asr,diarize",
                        "ttlSeconds": 3600, "note": "ops 发"})
        self.assertEqual(r.status_code, 200, r.text)
        pc = r.json()["pairingCode"]
        self.assertTrue(pc["code"], pc)
        self.assertTrue(pc["url"].startswith("echo://pair?host="), pc["url"])
        self.assertIn("code=" + pc["code"], pc["url"])
        self.assertAlmostEqual(pc["ttlSeconds"], 3600, delta=2)
        # scopes 在**唯一的**统一化函数里被规整过（逗号 → 空格），与命令行同一条路
        self.assertEqual(pc["scopes"], "asr diarize")
        self.assertEqual(pc["id"], auth_mod.hash_pairing_code(pc["code"]))

    def test_the_plaintext_is_only_a_hash_in_the_database(self):
        self.login()
        pc = self.wpost("/admin/api/pairing-codes",
                        {"name": "只存哈希的"}).json()["pairingCode"]
        blob = open(self.cfg.get("auth.db"), "rb").read()
        self.assertNotIn(pc["code"].encode(), blob, "配对码明文落库了")
        rows = self.state.auth.store.pairing_codes()
        self.assertEqual([r["code_hash"] for r in rows], [pc["id"]])
        self.assertNotEqual(rows[0]["code_hash"], pc["code"])

    def test_the_code_can_be_redeemed_exactly_once(self):
        """端到端：面板发出来的码，能力面 `/v1/pair` 真的能兑，而且**只能用一次**。"""
        self.login()
        pc = self.wpost("/admin/api/pairing-codes",
                        {"name": "面板发的", "scopes": "asr"}).json()["pairingCode"]
        first = self.client.post("/v1/pair", json={"code": pc["code"],
                                                   "clientName": "对端自报的名字"})
        self.assertEqual(first.status_code, 200, first.text)
        # 码上带的名字/scope **盖过**对端自报的（设计 §7.4）
        self.assertEqual(first.json()["name"], "面板发的")
        self.assertEqual(first.json()["scopes"], ["asr"])
        again = self.client.post("/v1/pair", json={"code": pc["code"]})
        self.assertEqual(again.status_code, 401, again.text)
        self.assertEqual(self.state.auth.store.pairing_codes(), [],
                         "用掉的码应当**用掉即删**（§8.5）")

    def test_the_list_shows_the_remaining_time_and_never_the_plaintext(self):
        self.login()
        pc = self.wpost("/admin/api/pairing-codes",
                        {"name": "待用的", "ttlSeconds": 600}).json()["pairingCode"]
        d = self.ac.get("/admin/api/pairing-codes").json()
        self.assertEqual(len(d["codes"]), 1, d)
        row = d["codes"][0]
        self.assertEqual(row["id"], pc["id"])
        self.assertEqual(row["name"], "待用的")
        self.assertGreater(row["remainingSeconds"], 500)
        self.assertLessEqual(row["remainingSeconds"], 600)
        self.assertFalse(row["expired"])
        self.assertNotIn(pc["code"], json.dumps(d, ensure_ascii=False),
                         "列表把明文带出来了")

    def test_ttl_is_honoured_and_out_of_range_is_a_400(self):
        """越界**报错不夹紧**：夹紧会让"我明明填了 30 天"变成"其实只发了 7 天"。"""
        self.login()
        ok = self.wpost("/admin/api/pairing-codes", {"ttlSeconds": 120}).json()["pairingCode"]
        self.assertAlmostEqual(ok["expiresAt"] - time.time(), 120, delta=5)
        for bad in (5, 8 * 24 * 3600, "nope", -1):
            with self.subTest(ttl=bad):
                r = self.wpost("/admin/api/pairing-codes", {"ttlSeconds": bad})
                self.assertEqual(r.status_code, 400, r.text)

    def test_an_expired_code_is_marked_and_a_new_issue_reaps_it(self):
        """过期的码会被下一次发码顺手清掉（`ops.issue_pairing_code` 里那一步）——
        否则"待用配对码"这个数字会永远比实际多。

        这里用"删掉再以负 TTL 存回同一张码"把它推进过去，而不是 sleep 60 秒：
        用的是 store 的公开方法，不去碰它的内部锁。
        """
        self.login()
        old = self.wpost("/admin/api/pairing-codes", {"ttlSeconds": 60}).json()["pairingCode"]
        store = self.state.auth.store
        store.delete_pairing_code(old["id"])
        store.put_pairing_code(old["id"], -1, created_by="t", name="过期的")
        d = self.ac.get("/admin/api/pairing-codes").json()
        self.assertEqual(len(d["codes"]), 1)
        self.assertTrue(d["codes"][0]["expired"])
        self.assertEqual(d["codes"][0]["remainingSeconds"], 0)
        fresh = self.wpost("/admin/api/pairing-codes", {"name": "新的"}).json()["pairingCode"]
        left = [r["code_hash"] for r in store.pairing_codes()]
        self.assertEqual(left, [fresh["id"]], "过期的码没有被顺手清掉")

    def test_revoking_a_pending_code_removes_it_and_stops_it_working(self):
        self.login()
        pc = self.wpost("/admin/api/pairing-codes",
                        {"name": "要作废的"}).json()["pairingCode"]
        r = self.wdelete("/admin/api/pairing-codes/" + pc["id"], {"confirm": pc["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.state.auth.store.pairing_codes(), [])
        self.assertEqual(self.client.post("/v1/pair",
                                          json={"code": pc["code"]}).status_code, 401)

    def test_revoking_an_unknown_code_is_a_404(self):
        self.login()
        r = self.wdelete("/admin/api/pairing-codes/deadbeef", {"confirm": "deadbeef"})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["code"], "pairing_code_not_found")

    def test_revoking_a_used_code_is_a_404_not_a_success(self):
        self.login()
        pc = self.wpost("/admin/api/pairing-codes", {"name": "用掉的"}).json()["pairingCode"]
        self.client.post("/v1/pair", json={"code": pc["code"]})
        r = self.wdelete("/admin/api/pairing-codes/" + pc["id"], {"confirm": pc["id"]})
        self.assertEqual(r.status_code, 404, r.text)


class ClientWriteTests(_AdminCase):
    """客户端管理：禁用/启用、撤销、改 scopes、改配额、轮换 secret。"""

    def test_disable_then_enable(self):
        self.login()
        self.assertEqual(self.call_asr().status_code, 200)
        r = self.wpost("/admin/api/clients/cli-1/disable")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["disabled"])
        blocked = self.call_asr()
        # 403 而不是 401：**认识你，但不许用**（设计 §7.5 ③）
        self.assertEqual(blocked.status_code, 403, blocked.text)
        back = self.wpost("/admin/api/clients/cli-1/enable")
        self.assertEqual(back.status_code, 200, back.text)
        self.assertTrue(back.json()["tokenStillValid"])
        self.assertEqual(self.call_asr().status_code, 200)

    def test_revoke_kills_the_token_on_the_very_next_request(self):
        """撤销**立刻**生效：管理面与能力面同进程、共用同一个鉴权缓存。

        （命令行那条路是另一个进程，靠 ≤5 秒的轮询发现 —— 两者不混为一谈，
        见 `test_server_contract` 的跨进程那条用例。）

        ⚠️ 这里必须**复用撤销前那个令牌**：`cap_headers()` 每次都按库里的新版本号
        重新签发一个，用它去断言"撤销生效"是自欺（新令牌本来就该有效）。
        """
        self.login()
        old_headers = self.cap_headers()
        self.assertEqual(self.client.post("/v1/asr", content=_wav_bytes(1.0),
                                          headers=old_headers).status_code, 200)
        r = self.wpost("/admin/api/clients/cli-1/revoke", {"confirm": "cli-1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["tokenVersion"], 2)
        after = self.client.post("/v1/asr", content=_wav_bytes(1.0), headers=old_headers)
        self.assertEqual(after.status_code, 401, after.text)
        self.assertEqual(after.json()["code"], "unauthorized")

    def test_set_scopes_takes_effect_on_the_next_request(self):
        self.login()
        self.assertEqual(self.call_asr().status_code, 200)
        r = self.wpost("/admin/api/clients/cli-1/scopes", {"scopes": "diarize"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["scopes"], "diarize")
        blocked = self.call_asr()             # 现在只剩 diarize，asr 不再给
        self.assertEqual(blocked.status_code, 403, blocked.text)
        self.wpost("/admin/api/clients/cli-1/scopes", {"scopes": "asr"})
        self.assertEqual(self.call_asr().status_code, 200)

    def test_scopes_can_be_cleared_to_means_unlimited(self):
        self.login()
        self.call_asr()
        r = self.wpost("/admin/api/clients/cli-1/scopes", {"scopes": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["scopes"], "")
        self.assertEqual(self.state.auth.store.client("cli-1")["scopes"], "")

    def test_missing_scopes_field_is_a_400(self):
        """**"没给这个字段"与"给了一个空串"是两件事**：前者是写错了请求，后者是"不限"。"""
        self.login()
        self.call_asr()
        r = self.wpost("/admin/api/clients/cli-1/scopes", {})
        self.assertEqual(r.status_code, 400, r.text)

    def test_set_quota_stores_the_limit(self):
        self.login()
        self.call_asr()
        r = self.wpost("/admin/api/clients/cli-1/quota", {"dailyAudioMinutes": 120})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.state.auth.store.client("cli-1")["daily_audio_minutes"], 120.0)
        self.assertFalse(r.json()["usedMinutesTodayCleared"])
        self.assertEqual(self.wpost("/admin/api/clients/cli-1/quota",
                                    {"dailyAudioMinutes": 0}).status_code, 200)
        self.assertEqual(self.state.auth.store.client("cli-1")["daily_audio_minutes"], 0.0)

    def test_a_bad_quota_is_a_400(self):
        self.login()
        self.call_asr()
        for bad in (-1, "很多", None):
            with self.subTest(bad=bad):
                r = self.wpost("/admin/api/clients/cli-1/quota", {"dailyAudioMinutes": bad})
                self.assertEqual(r.status_code, 400, r.text)

    def test_rotate_secret_reveals_it_once_and_never_again(self):
        self.login()
        self.call_asr()
        r = self.wpost("/admin/api/clients/cli-1/rotate-secret", {"confirm": "cli-1"})
        self.assertEqual(r.status_code, 200, r.text)
        secret = r.json()["secret"]
        self.assertTrue(secret)
        # ① 新 secret **真的能用**（换令牌）—— 不是打印了个假东西
        basic = base64.b64encode(("cli-1:" + secret).encode()).decode()
        ok = self.client.post("/v1/token", headers={"Authorization": "Basic " + basic})
        self.assertEqual(ok.status_code, 200, ok.text)
        # ② 旧 secret 立刻失效
        old = base64.b64encode(b"cli-1:s").decode()
        gone = self.client.post("/v1/token", headers={"Authorization": "Basic " + old})
        self.assertEqual(gone.status_code, 401, gone.text)
        # ③ 之后任何接口都不再回显它，也不再回显哈希
        listing = self.ac.get("/admin/api/clients").text
        detail = self.ac.get("/admin/api/clients/cli-1").text
        self.assertNotIn(secret, listing)
        self.assertNotIn(secret, detail)
        self.assertNotIn("secret_hash", listing + detail)
        self.assertNotIn(secret.encode(), open(self.cfg.get("auth.db"), "rb").read())

    def test_rotate_secret_kills_the_old_token_immediately(self):
        self.login()
        old_headers = self.cap_headers()
        self.assertEqual(self.client.post("/v1/asr", content=_wav_bytes(1.0),
                                          headers=old_headers).status_code, 200)
        self.wpost("/admin/api/clients/cli-1/rotate-secret", {"confirm": True})
        again = self.client.post("/v1/asr", content=_wav_bytes(1.0), headers=old_headers)
        self.assertEqual(again.status_code, 401, again.text)

    def test_rotate_secret_with_grace_also_hands_out_a_new_code(self):
        """宽限期那条路（例行轮换不打断客户端）与命令行 `--grace-hours` 同一条实现：
        同样顺带发一张配对码，好把新凭据交出去。"""
        self.login()
        self.call_asr()
        r = self.wpost("/admin/api/clients/cli-1/rotate-secret",
                       {"confirm": True, "graceHours": 24})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("pairingCode", body)
        self.assertTrue(body["pairingCode"]["url"].startswith("echo://pair?"))
        self.assertGreater(body["graceSeconds"], 0)
        self.assertEqual(len(self.state.auth.store.pairing_codes()), 1)

    def test_an_unknown_client_is_a_404_on_every_action(self):
        self.login()
        cases = (("disable", {}), ("enable", {}), ("revoke", {"confirm": True}),
                 ("scopes", {"scopes": "asr"}), ("quota", {"dailyAudioMinutes": 1}),
                 ("rotate-secret", {"confirm": True}))
        for action, payload in cases:
            with self.subTest(action=action):
                r = self.wpost("/admin/api/clients/cli-nope/" + action, payload)
                self.assertEqual(r.status_code, 404, r.text)
                self.assertEqual(r.json()["code"], "client_not_found")

    def test_the_detail_view_never_carries_a_hash(self):
        self.login()
        self.call_asr()
        r = self.ac.get("/admin/api/clients/cli-1")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["clientId"], "cli-1")
        for key in ("tokenVersion", "dailyAudioMinutes", "lastSeen", "secretRotatedAt"):
            self.assertIn(key, d)
        for word in ("secret_hash", "prev_secret_hash", "password_hash"):
            self.assertNotIn(word, r.text)

    def test_the_account_list_is_read_only(self):
        """管理员账号**只在命令行**改 —— 面板最多只读展示清单。

        理由：改账号等于"改谁能进这扇门"，而面板本身就在这扇门里
        （一次会话劫持就能顺手把攻击者自己加成管理员）。
        """
        self.login()
        d = self.ac.get("/admin/api/admins").json()
        self.assertEqual([a["username"] for a in d["admins"]], ["off", "ops"])
        self.assertEqual([a["disabled"] for a in d["admins"]], [True, False])
        self.assertNotIn("password_hash", json.dumps(d, ensure_ascii=False))
        for _method, path, _real in self.write_requests():
            self.assertNotIn("/admins", path)


class AuditTests(_AdminCase):
    """每个写操作（**含失败**）落一条审计，操作者是登录的管理员名。"""

    def test_every_write_action_leaves_a_row_with_the_admin_name(self):
        self.login()
        self.call_asr()
        pc = self.wpost("/admin/api/pairing-codes", {"name": "审计用"}).json()["pairingCode"]
        self.wpost("/admin/api/clients/cli-1/disable")
        self.wpost("/admin/api/clients/cli-1/enable")
        self.wpost("/admin/api/clients/cli-1/scopes", {"scopes": "asr"})
        self.wpost("/admin/api/clients/cli-1/quota", {"dailyAudioMinutes": 5})
        self.wpost("/admin/api/clients/cli-1/revoke", {"confirm": True})
        self.wpost("/admin/api/clients/cli-1/rotate-secret", {"confirm": True})
        self.wdelete("/admin/api/pairing-codes/" + pc["id"], {"confirm": pc["id"]})
        rows = self.audit_rows()
        seen = {(r[0], r[1]) for r in rows}
        for action in ("issue-pairing-code", "disable", "enable", "set-scopes",
                       "set-quota", "revoke", "rotate-secret", "delete-pairing-code"):
            self.assertIn(("ops", action), seen, seen)
        self.assertEqual({r[0] for r in rows}, {"ops"},
                         "审计里出现了非登录名的操作者：%s" % rows)
        self.assertNotIn("cli", {r[0] for r in rows})

    def test_a_failed_write_is_audited_with_the_reason(self):
        self.login()
        self.call_asr()
        self.assertEqual(self.wpost("/admin/api/clients/cli-1/revoke", {}).status_code, 400)
        hit = [r for r in self.audit_rows() if r[1] == "revoke.failed"]
        self.assertTrue(hit, self.audit_rows())
        self.assertEqual(hit[0][0], "ops")
        self.assertIn("cli-1", hit[0][2])
        self.assertIn("confirm", hit[0][2])
        # 失败了就**没有副作用**
        self.assertEqual(self.state.auth.store.client("cli-1")["token_version"], 1)

    def test_an_unknown_target_is_audited_as_a_failure(self):
        self.login()
        self.wpost("/admin/api/clients/cli-nope/disable")
        hit = [r for r in self.audit_rows() if r[1] == "disable.failed"]
        self.assertTrue(hit, self.audit_rows())
        self.assertIn("client_not_found", hit[0][2])

    def test_reads_are_not_audited(self):
        """只读的看**不算动作** —— 否则审计表会被刷新页面的手速刷满。"""
        self.login()
        self.ac.get("/admin/api/overview")
        self.ac.get("/admin/api/clients")
        self.ac.get("/admin/api/calls")
        self.assertEqual({r[1] for r in self.audit_rows()}, {"login"})


class SharedImplementationTests(_AdminCase):
    """第 7 条闸门：命令行与管理面**共用同一份实现**（`server/ops.py`）。

    判据不是"代码看起来一样"，而是**行为对得上**：同一个统一化、同一串配对串、
    同一批 store 方法。两处各写一遍时，漂移是必然的（命令行改了、网页那条不会改），
    所以这里用"同一件事从两个入口各做一遍、结果必须一致"把它钉住。
    """

    def test_the_cli_printer_and_the_api_return_byte_identical_urls(self):
        self.login()
        pc = self.wpost("/admin/api/pairing-codes",
                        {"name": "同一件事", "scopes": "asr"}).json()["pairingCode"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server_main._print_pairing(pc, self.cfg)
        line = [ln.strip() for ln in buf.getvalue().splitlines() if "echo://pair" in ln][0]
        self.assertEqual(line, pc["url"], "命令行与管理面拼出来的配对串不一样")

    def test_both_entrances_store_the_same_shape_of_row(self):
        from server import ops as ops_mod
        self.login()
        api = self.wpost("/admin/api/pairing-codes",
                         {"name": "A", "scopes": "asr,diarize",
                          "createdBy": "面板"}).json()["pairingCode"]
        cli = ops_mod.issue_pairing_code(self.cfg, self.state.auth, name="B",
                                         scopes="asr,diarize", created_by="cli")
        rows = {r["code_hash"]: r for r in self.state.auth.store.pairing_codes()}
        self.assertIn(api["id"], rows)
        self.assertIn(cli["id"], rows)
        self.assertEqual(rows[api["id"]]["scopes"], "asr diarize")
        self.assertEqual(rows[cli["id"]]["scopes"], "asr diarize")
        self.assertEqual(rows[api["id"]]["created_by"], "面板")
        self.assertEqual(rows[cli["id"]]["created_by"], "cli")
        # 同一台后端、同一段配置 → 配对串的前缀（host / fp）必须一致
        self.assertEqual(api["url"].split("code=")[0], cli["url"].split("code=")[0])

    def test_the_cli_side_uses_the_same_functions(self):
        """把"共用"这件事钉在**源码**上：`main._admin_cli` 里不该再出现
        `a.create_pairing_code(` / `a.revoke(` / `store.revoke(` 这些直连调用。
        """
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "server", "main.py"), encoding="utf-8").read()
        for forbidden in ("a.create_pairing_code(", "a.cache.revoke(", "a.set_scopes(",
                          "a.set_quota(", "a.set_disabled(", "a.rotate_secret("):
            self.assertNotIn(forbidden, src,
                             "命令行又绕过 ops 直连 store/auth 了：%s" % forbidden)


class AdminCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-admin-cli-")
        self.cfg_path = os.path.join(self.tmp, "server.yaml")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            fh.write("server: {id: admin-cli}\n"
                     "auth:\n  enabled: true\n  mode: jwt\n"
                     "  jwt_secret: '0123456789abcdef0123456789abcdef'\n"
                     "  db: '%s'\n"
                     "models: {specs: %s}\n" % (
                         os.path.join(self.tmp, "auth.db").replace("\\", "/"),
                         json.dumps(FAKE_SPECS)))

    def _run(self, *argv):
        buf = io.StringIO()
        with patch.object(server_main, "create_app") as _ca:
            import contextlib
            with contextlib.redirect_stdout(buf):
                rc = server_main.main(["--config", self.cfg_path] + list(argv))
        return rc, buf.getvalue()

    def test_new_admin_prints_the_password_once_and_stores_only_a_hash(self):
        rc, out = self._run("--new-admin", "ops")
        self.assertEqual(rc, 0, out)
        self.assertIn("只出现这一次", out)
        line = [ln.strip() for ln in out.splitlines() if ln.strip() and "已建" not in ln
                and "管理面板" not in ln][0]
        store = store_mod.Store(os.path.join(self.tmp, "auth.db"))
        self.addCleanup(store.close)
        row = store.admin("ops")
        self.assertIsNotNone(row)
        self.assertNotIn(line, row["password_hash"], "口令被明文存进去了")
        self.assertTrue(admin_mod.verify_password(line, row["password_hash"]),
                        "打印出来的口令验证不过 —— 那这次建号就是白建")

    def test_list_admins_says_when_there_are_none(self):
        rc, out = self._run("--list-admins")
        self.assertEqual(rc, 0)
        self.assertIn("还没有管理员账号", out)
        self._run("--new-admin", "ops")
        rc, out = self._run("--list-admins")
        self.assertIn("ops", out)

    def test_disable_and_delete_are_reported_cleanly(self):
        self._run("--new-admin", "ops")
        rc, out = self._run("--disable-admin", "ops")
        self.assertEqual(rc, 0)
        self.assertIn("已禁用", out)
        rc, out = self._run("--delete-admin", "ops")
        self.assertEqual(rc, 0)
        rc, out = self._run("--delete-admin", "ops")
        self.assertEqual(rc, 1)
        self.assertIn("没有这个管理员", out)
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
