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
import builtins
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
from server import limits as limits_mod                              # noqa: E402
from server import ops as ops_mod                                    # noqa: E402
from server import perfmon as perfmon_mod                            # noqa: E402
from server import routes as routes_mod                              # noqa: E402
from server import settings as settings_mod                          # noqa: E402
from server import store as store_mod                                # noqa: E402
from server import auth as auth_mod                                  # noqa: E402
from tests.test_server_contract import FAKE_SPECS, _fake_loader, _wav_bytes  # noqa: E402
from tests.test_perfmon import _FakeHost                              # noqa: E402


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
    # 「性能」页签的两个写动作（开始 / 停止记录）。**采集器全在内存里**，
    # 但"开始记录"仍然是一个改状态的动作，所以它与其它写动作走**同一套闸门**
    # （会话 + 同站 Origin + X-ECHO-Admin + CSRF + 审计），没有新开免检端点。
    "/admin/api/perf/start",
    "/admin/api/perf/stop",
    # 运行参数（总并发 / 每客户端并发 / 队列上限）。2026-09-29 用户要求
    # "并发上限由管理员在管理面上配" —— 它同样是改状态的动作，走同一套闸门 + 审计
    # （action = `limits-set`）。**没有一个字是免检的**：见 `LimitsConsoleTests`。
    "/admin/api/limits",
    # 改**自己**的口令（2026-09-29 用户要求："页面上还是要提供修改密码功能"）。
    # 它动的是"进这扇门的凭据"，所以除了那七道闸还自带两道锁：
    # ① **必须带当前口令**（`current`，见 `admin_mod.change_admin_password`）——
    #    一次会话劫持不足以改掉口令；② **只能改会话里的那一个账号**
    #    （请求体里的 `username` 只用来**拒绝**）。新增 / 删除 / 禁用管理员、
    # 改**别人**的口令仍然只在命令行 —— 见 `PasswordChangeConsoleTests`。
    "/admin/api/password",
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
        self.admin = self.make_admin_app()
        self.ac = TestClient(self.admin, base_url=self.ADMIN_BASE)
        self.csrf = ""

    def make_admin_app(self):
        """造管理面 app。子类可以覆盖它来注入替身 —— 「性能」页签那个采集器
        就是靠这里注入的（于是那些用例**不会真的去调 `nvidia-smi`**）。"""
        return admin_mod.create_admin_app(self.cfg, self.state)

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


class WildcardAdminListenWriteTests(_AdminCase):
    """管理面绑**通配地址**时（容器里必须这样，见 `server/compose.yaml`），同站判定仍要过。

    容器方案的全部前提就是这一条：进程绑 `0.0.0.0:8901`（绑回环的话宿主与 `ssh -L`
    都进不来），而浏览器访问的是 `http://127.0.0.1:8901`。如果 `allowed_origins()`
    依赖 `admin_listen` 的**字面 host**，这里就会 403 —— 而现象是
    "管理页面能打开、一按按钮就失败"，很难联想到是监听地址写法的问题。

    同时钉住反面：通配监听**不许**把白名单放宽成"谁的 Origin 都收"（那才是真正的
    安全削弱），这一点由 `Origin: http://gpu-01:8901` 与 `http://evil.example:8901`
    两条实测挡住。
    """

    def setUp(self):
        super().setUp()
        # 与容器里等价：`ECHO_ADMIN_LISTEN=0.0.0.0:8901`（写配置 / 写环境变量是同一件事）
        self.cfg.raw["server"]["admin_listen"] = "0.0.0.0:8901"

    def test_a_write_with_the_loopback_origin_goes_through(self):
        self.login()
        r = self.ac.post("/admin/api/pairing-codes",
                         json={"name": "容器里发的授权", "scopes": "asr"},
                         headers=self.wheaders())
        self.assertEqual(r.status_code, 200, r.text)
        code = (r.json().get("pairingCode") or {}).get("url") or ""
        self.assertIn("echo://pair?", code)
        self.assertIn("issue-pairing-code", [a for _who, a, _t in self.audit_rows()],
                      "写动作必须留下一条审计（操作者是登录的管理员名）")

    def test_the_wildcard_listen_does_not_widen_the_origin_whitelist(self):
        self.login()
        for origin in ("http://evil.example:8901",     # DNS-rebinding 的正面拦截
                       "http://gpu-01:8901",           # 本机真实网卡地址，**不是**回环
                       "http://127.0.0.1:9999"):       # 同主机、别的端口
            with self.subTest(origin=origin):
                r = self.ac.post("/admin/api/pairing-codes", json={"name": "x"},
                                 headers=self.wheaders(origin=origin))
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn("Origin", r.json()["detail"])

    def test_the_login_page_is_reachable_and_still_needs_a_login(self):
        """页面本身公开（否则没法登录），但数据端点照旧 401。"""
        self.assertEqual(self.ac.get("/admin/").status_code, 200)
        self.assertEqual(self.ac.get("/admin/api/overview").status_code, 401)


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
                     "/admin/api/me", "/admin/api/admins", "/admin/api/pairing-codes",
                     "/admin/api/perf/state", "/admin/api/perf/points"):
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

    def test_the_pending_list_holds_only_codes_that_still_work(self):
        """**"待用 = 真正可用"**：三张码（未用的 / 已过期的 / 已用掉的）→ 表里只剩第一张。

        这是 2026-09-29 用户实测的那个 bug 的判据：他把「已经配对成功过」的码当成
        `office-2060s` 那张，而它带着「已过期」的徽章挂在「待用」表里 ——
        标题与内容自相矛盾。两个条件分开的坏法都会在这里红：

        * 查询漏了"过期"那一半 → 第二张会出现在结果里；
        * 消费没有真的落库（比如哪天改成软删除却忘了在查询里带条件）→ 第三张会出现。
        """
        self.login()
        live = self.wpost("/admin/api/pairing-codes",
                          {"name": "能用的", "ttlSeconds": 600}).json()["pairingCode"]
        expired = self.wpost("/admin/api/pairing-codes",
                             {"name": "过期的", "ttlSeconds": 60}).json()["pairingCode"]
        used = self.wpost("/admin/api/pairing-codes",
                          {"name": "用掉的", "ttlSeconds": 600}).json()["pairingCode"]
        store = self.state.auth.store
        # 把第二张推成"1 秒前刚过期"：删掉再以负 TTL 存回同一张码（不碰 store 的内部锁）
        store.delete_pairing_code(expired["id"])
        store.put_pairing_code(expired["id"], -1, created_by="t", name="过期的")
        self.assertEqual(len(store.pairing_codes()), 3, "库里应当确实有三行")
        # 第三张走真实的免凭据端点兑掉 —— 消费必须真的落库（不是只在这里打桩）
        first = self.client.post("/v1/pair", json={"code": used["code"]})
        self.assertEqual(first.status_code, 200, first.text)

        d = self.ac.get("/admin/api/pairing-codes").json()
        self.assertEqual([c["id"] for c in d["codes"]], [live["id"]], d["codes"])
        row = d["codes"][0]
        self.assertEqual(row["name"], "能用的")
        self.assertFalse(row["expired"])
        self.assertGreaterEqual(row["remainingSeconds"], 1,
                                "列出来的码一定还能用，剩余秒数不该是 0")

    def test_a_consumed_code_leaves_the_list_at_once_and_cannot_be_used_twice(self):
        """回归「只能用一次」：`/v1/pair` 用掉码的那一瞬之后 ——
        列表里立刻没有它，而且**第二次兑换必须失败**。

        两个断言必须成对：只断言"列表里没了"的话，一个"把码从表里挪走但没作废"的
        坏改法也能过；只断言"第二次失败"的话，一张还挂在"待用"表里的码也能过。
        """
        self.login()
        pc = self.wpost("/admin/api/pairing-codes",
                        {"name": "只能用一次", "ttlSeconds": 600}).json()["pairingCode"]
        before = self.ac.get("/admin/api/pairing-codes").json()["codes"]
        self.assertEqual([c["id"] for c in before], [pc["id"]], before)

        first = self.client.post("/v1/pair", json={"code": pc["code"]})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["clientId"].startswith("cli-"))

        after = self.ac.get("/admin/api/pairing-codes").json()["codes"]
        self.assertEqual(after, [], "用掉的码还留在「待用」表里：%s" % (after,))
        again = self.client.post("/v1/pair", json={"code": pc["code"]})
        self.assertEqual(again.status_code, 401, again.text)
        # 失败的那一次不该把行"写回来"，也不该再长出新的待用行
        self.assertEqual(self.ac.get("/admin/api/pairing-codes").json()["codes"], [])

    def test_the_expiry_boundary_is_one_second_each_way(self):
        """过期边界：差 1 秒过 / 差 1 秒没过。判据是**同一个函数**，不靠 sleep。

        后半段还钉住「清理不会误删还能用的行」：`store.sweep_pairing_codes()` 的
        边界必须与列表判据一致（都用 `<`），否则"下一次发码顺手清理"会删掉一张
        列表上还写着能用的码。
        """
        self.login()
        store = self.state.auth.store
        now = time.time()
        store.put_pairing_code("expired-1s", -1, created_by="t", name="过期 1 秒")
        store.put_pairing_code("live-1s", 1, created_by="t", name="还有 1 秒")
        rows = ops_mod.pending_pairing_codes(store, now=now)
        self.assertEqual([r["id"] for r in rows], ["live-1s"], rows)
        self.assertEqual(rows[0]["remainingSeconds"], 1)
        self.assertFalse(rows[0]["expired"])
        # 恰好到期的那一瞬仍然算"能用" —— 与 `Auth.redeem()` 的判据一字不差（`< now`）
        self.assertFalse(auth_mod.pairing_code_expired({"expires_at": now}, now))
        self.assertTrue(auth_mod.pairing_code_expired({"expires_at": now - 1}, now))
        self.assertFalse(auth_mod.pairing_code_expired({"expires_at": now + 1}, now))
        # 清理：只删"严格已过期"的，不误删边界上那张还能用的
        store.put_pairing_code("at-boundary", 0, created_by="t", name="正好到期")
        store.sweep_pairing_codes(now=now)
        left = sorted(r["code_hash"] for r in store.pairing_codes())
        self.assertNotIn("expired-1s", left, "过期的行没有被清掉")
        self.assertIn("at-boundary", left, "清理误删了正好到期（仍然能用）的那一行")
        self.assertIn("live-1s", left, "清理误删了还能用的那一行")
        # 端点那一侧同样只剩能用的：过期的绝不出现（这里是"差 1 秒过"的那张）
        d = self.ac.get("/admin/api/pairing-codes").json()
        self.assertNotIn("expired-1s", [c["id"] for c in d["codes"]], d["codes"])

    def test_a_consumed_code_leaves_an_audit_row_instead_of_a_pending_row(self):
        """用掉的码**不留在这张表里**；留痕在审计里（谁发的码 / 什么名字 / 换了哪个客户端）。"""
        self.login()
        pc = self.wpost("/admin/api/pairing-codes",
                        {"name": "留痕的", "scopes": "asr",
                         "note": "ops"}).json()["pairingCode"]
        out = self.client.post("/v1/pair", json={"code": pc["code"]}).json()
        hit = [r for r in self.audit_rows() if r[1] == auth_mod.PAIR_REDEEM_ACTION]
        self.assertEqual(len(hit), 1, self.audit_rows())
        admin, _action, target = hit[0]
        self.assertIn("ops", admin, "审计里没写清是谁发的这张码：%s" % (hit[0],))
        self.assertIn("留痕的", target)
        self.assertIn(out["clientId"], target)

    def test_an_expired_code_is_not_listed_and_a_new_issue_reaps_it(self):
        """过期的码**不进"待用"表**，而库里那一行由下一次发码顺手清掉。

        2026-09-29 之前这里断言的是"被**标记**为已过期"（表里留着、徽章写「已过期」）——
        那正是用户报的 bug：标题说「待用」、内容说「已过期」。现在它不列出来了。

        这里用"删掉再以负 TTL 存回同一张码"把它推进过去，而不是 sleep 60 秒：
        用的是 store 的公开方法，不去碰它的内部锁。
        """
        self.login()
        old = self.wpost("/admin/api/pairing-codes", {"ttlSeconds": 60}).json()["pairingCode"]
        store = self.state.auth.store
        store.delete_pairing_code(old["id"])
        store.put_pairing_code(old["id"], -1, created_by="t", name="过期的")
        self.assertEqual([r["code_hash"] for r in store.pairing_codes()], [old["id"]],
                         "库里应当确实有这一行（否则下面那条'被清掉'就没有意义）")
        d = self.ac.get("/admin/api/pairing-codes").json()
        self.assertEqual(d["codes"], [], "过期的码被列进了「待用」表")
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


class PairingPageTests(_AdminCase):
    """页面上那两句话：这张表里**只有能用的**，以及用掉的码去哪儿看。

    文字测起来像是"测文案"，但这里测的是**判据的位置**：
    "只有能用的码"这句话必须与后端 `ops.pending_pairing_codes()` 的语义一致，
    而"用掉的码在审计里"是用户能自己回答"我那张码到底兑没兑"的唯一入口。
    """

    def _html(self):
        r = self.ac.get("/admin/")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_the_pending_card_claims_only_usable_codes(self):
        html = self._html()
        self.assertIn("待用的配对码（能用的 ", html,
                      "标题又成了含糊的「待用」：表里只有能用的码，就该这么说")
        # 不到 1 分钟如实报秒（`left()` 里那一条）—— 否则 5 秒的码会渲染成"1 分钟"，
        # 而 0 秒那一档渲染出来就是与"待用"自相矛盾的「已过期」徽章。
        self.assertIn('sec + " 秒"', html)

    def test_the_page_says_where_a_consumed_code_went(self):
        html = self._html()
        self.assertIn("pair-redeem", html)
        self.assertIn("pair:&lt;发放者&gt;", html)


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
        """账号的**增删改**（含改别人的口令）只在命令行 —— 面板最多只读展示清单。

        理由：改账号等于"改谁能进这扇门"，而面板本身就在这扇门里
        （一次会话劫持就能顺手把攻击者自己加成管理员）。
        2026-09-29 只开了**一个**口子：改**自己**的口令，而且必须带当前口令
        （`POST /admin/api/password`，见 `PasswordChangeConsoleTests`）——
        所以这里仍然钉着两件事：没有任何写端点落在 `/admins` 上；
        写端点里也没有 `{username}` 这种"指名道姓改谁"的占位符。
        """
        self.login()
        d = self.ac.get("/admin/api/admins").json()
        self.assertEqual([a["username"] for a in d["admins"]], ["off", "ops"])
        self.assertEqual([a["disabled"] for a in d["admins"]], [True, False])
        self.assertNotIn("password_hash", json.dumps(d, ensure_ascii=False))
        for _method, path, _real in self.write_requests():
            self.assertNotIn("/admins", path)
            self.assertNotIn("{username}", path,
                             "写端点不许按用户名指名改账号（那正是「改别人」的入口）")
        touched = [p for _m, p, _r in self.write_requests()
                   if "password" in p or "/admins" in p]
        self.assertEqual(touched, ["/admin/api/password"], touched)


class PasswordChangeConsoleTests(_AdminCase):
    """改**自己**的口令（2026-09-29 用户要求："页面上还是要提供修改密码功能"）。

    这一组的重心不是"回了个 200"，而是四条：

    1. 闸门与其它写动作**完全一致**（未登录 401；错 Origin / 缺 `X-ECHO-Admin` /
       缺 CSRF → 403），而且被挡回来时**口令一个字节都没动**；
    2. **必须带当前口令**（`current` 错 → 403），且**只能改会话里那一个账号**
       （请求体里指着别人 → 400）—— 这两条就是"一次会话劫持不足以改口令"的落点；
    3. 成功之后**新口令能登录、旧口令不能** —— 这是这条功能唯一有意义的判据
       （只断言 200 的话，端点回 200 却没落库、或者落了个错的哈希，都会绿）；
    4. 审计（成功 `password-change` / 失败 `password-change.failed` 带中文原因）、
       以及"**其它会话失效、当前会话保留**"（既有手段：`SessionStore.drop_user`）。

    **没做**（也刻意不做）：新建 / 删除 / 禁用管理员、改**别人**的口令 ——
    仍然只在命令行。这条边界由 `ClientWriteTests.test_the_account_list_is_read_only`
    与 `PasswordPageTests` 一起钉着。
    """

    NEW = "brand-new-pass"

    def _hash(self, user="ops"):
        return self.state.auth.store.admin(user)["password_hash"]

    def _change(self, payload=None, **kw):
        body = {"current": "good-pass", "new": self.NEW}
        body.update(payload or {})
        return self.wpost("/admin/api/password", body, **kw)

    def _login_with(self, password, user="ops"):
        """另开一个客户端（自己的 cookie 罐）打一次登录 —— 走**真的**登录那条路。"""
        fresh = TestClient(self.admin, base_url=self.ADMIN_BASE)
        return fresh.post("/admin/api/login", json={"username": user, "password": password})

    def test_the_gates_are_the_same_as_every_other_write(self):
        before = self._hash()
        r = self.ac.post("/admin/api/password",
                         json={"current": "good-pass", "new": self.NEW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["code"], "unauthorized")
        self.login()
        for kw, want in (({"origin": "http://evil.example"}, "Origin"),
                         ({"header": False}, admin_mod.WRITE_HEADER),
                         ({"csrf": False}, "CSRF")):
            with self.subTest(gate=sorted(kw)):
                r = self._change(**kw)
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn(want, r.json()["detail"])
        self.assertEqual(self._hash(), before, "被闸门挡回来的请求居然改了口令")
        self.assertFalse(admin_mod.verify_password(self.NEW, self._hash()))
        self.assertEqual(self._login_with(self.NEW).status_code, 401)

    def test_a_wrong_current_password_is_refused_and_changes_nothing(self):
        """`current` 不对 → **403**（不是 400），口令不变。

        为什么是 403 而不是 400：请求本身没有任何毛病（那两个字段都在、都合法），
        **是身份不够** —— 这里正是"必须提供当前口令"那道闸门的落点，
        会话被劫持时攻击者就卡在这一步。说成 400（"请求不合法"）会把原因指错。
        """
        self.login()
        before = self._hash()
        for bad in ({"current": "wrong-pass"}, {"current": ""}, {"current": None}):
            with self.subTest(current=bad["current"]):
                r = self._change(bad)
                self.assertEqual(r.status_code, 403, r.text)
                self.assertEqual(r.json()["code"], "forbidden")
                self.assertIn("当前口令", r.json()["detail"])
                self.assertEqual(self._hash(), before)
        self.assertEqual(self._login_with("good-pass").status_code, 200)
        self.assertEqual(self._login_with(self.NEW).status_code, 401)

    def test_the_new_password_must_follow_the_policy(self):
        """空 / 太短 / 太长 / 与当前口令相同 → 400 + **中文原因**，口令不变。"""
        self.login()
        before = self._hash()
        cases = (({"new": ""}, "不能为空"),
                 ({"new": "short"}, "至少 8 位"),
                 ({"new": "x" * 201}, "最多 200 位"),
                 ({"new": "good-pass"}, "不能与当前口令相同"),
                 ({"new": None}, "不能为空"))
        for bad, want in cases:
            with self.subTest(bad=bad):
                r = self._change(bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["code"], "bad_request")
                self.assertIn(want, r.json()["detail"])
                self.assertIn("请求不合法", r.json()["message"])
                self.assertEqual(self._hash(), before, "400 却改了口令")
        # `new` 字段**整个没给**（与上面"给了空串/null"是两件事）
        r = self.ac.post("/admin/api/password", json={"current": "good-pass"},
                         headers=self.wheaders())
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("不能为空", r.json()["detail"])
        self.assertEqual(self._hash(), before, "400 却改了口令")

    def test_the_minimum_length_boundary_is_exactly_the_documented_one(self):
        """页面上写"至少 8 位"——那就得真是 8（7 位拒、8 位收）。"""
        self.login()
        self.assertEqual(admin_mod.MIN_PASSWORD_LEN, 8)
        self.assertEqual(admin_mod.MAX_PASSWORD_LEN, 200)
        self.assertEqual(self._change({"new": "7chars!"}).status_code, 400)
        self.assertEqual(self._change({"new": "8chars!!"}).status_code, 200)

    def test_only_the_logged_in_admin_can_be_targeted(self):
        """请求体里指着**别人** → 400，一口回绝；那个人的口令一个字节都没动。

        页面没有"改谁"这个入口（见 `PasswordPageTests`），所以这条路径只可能是
        手搓的请求 —— 它必须被**明说**拒绝，而不是被静默忽略：
        静默忽略会在某次"顺手支持一下 username 字段"的重构里变成真的改别人。
        """
        store = self.state.auth.store
        store.upsert_admin("second", admin_mod.hash_password("second-pass"))
        second_before = store.admin("second")["password_hash"]
        self.login()
        r = self._change({"username": "second"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("只能改当前登录的这个管理员", r.json()["detail"])
        self.assertIn("second", r.json()["detail"])
        self.assertEqual(store.admin("second")["password_hash"], second_before)
        self.assertTrue(admin_mod.verify_password("second-pass",
                                                 store.admin("second")["password_hash"]))
        self.assertFalse(admin_mod.verify_password(self.NEW,
                                                  store.admin("second")["password_hash"]))
        self.assertTrue(admin_mod.verify_password("good-pass", self._hash()),
                        "拒绝必须发生在任何写之前 —— 连自己那份也不许动")
        # 写上**自己**的名字是无害的（改的仍然是自己），这也说明它确实只认会话
        self.assertEqual(self._change({"username": "ops"}).status_code, 200)
        self.assertTrue(admin_mod.verify_password(self.NEW, self._hash()))

    def test_the_new_password_logs_in_and_the_old_one_does_not(self):
        """**这条功能的全部意义**：改完新口令进得来、旧口令进不来。"""
        self.login()
        r = self._change()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["username"], "ops")
        self.assertNotIn(self.NEW, r.text, "响应里出现了口令明文")
        row = self.state.auth.store.admin("ops")
        self.assertTrue(row["password_hash"].startswith("scrypt$"),
                        "存的还是明文/别的格式：%s" % row["password_hash"])
        self.assertTrue(admin_mod.verify_password(self.NEW, row["password_hash"]))
        self.assertFalse(admin_mod.verify_password("good-pass", row["password_hash"]))
        # 真的走一遍登录那条路（不是只比哈希）
        self.assertEqual(self._login_with("good-pass").status_code, 401)
        good = self._login_with(self.NEW)
        self.assertEqual(good.status_code, 200, good.text)
        self.assertTrue(good.json()["csrf"])

    def test_other_sessions_die_and_the_current_one_survives(self):
        """口令一变，**别的登录会话立刻失效**，当前这个留着。

        这里没有"会话版本号"那种机制（会话整个在 `SessionStore` 的进程内存里，
        见模块开头），所以用的是**既有手段** `drop_user(username, keep=当前令牌)`。
        留当前这个是有意的：刚改完就把自己踢回登录页，用户会以为改失败了。
        """
        self.login()
        others = []
        for _ in range(2):
            c = TestClient(self.admin, base_url=self.ADMIN_BASE)
            self.assertEqual(c.post("/admin/api/login",
                                    json={"username": "ops",
                                          "password": "good-pass"}).status_code, 200)
            self.assertEqual(c.get("/admin/api/me").status_code, 200)
            others.append(c)
        r = self._change()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["sessionsRevoked"], 2)
        for c in others:
            self.assertEqual(c.get("/admin/api/me").status_code, 401, "别的会话还活着")
        self.assertEqual(self.ac.get("/admin/api/me").status_code, 200, "把自己也踢下线了")

    def test_the_change_and_a_refusal_are_audited_without_the_password(self):
        self.login()
        self._change({"current": "wrong-pass"})
        self._change()
        rows = self.audit_rows()
        ok = [r for r in rows if r[1] == "password-change"]
        self.assertTrue(ok, rows)
        self.assertEqual({r[0] for r in ok}, {"ops"})
        self.assertEqual({r[2] for r in ok}, {"ops"}, "审计的 target 要写明改的是谁")
        failed = [r for r in rows if r[1] == "password-change.failed"]
        self.assertTrue(failed, rows)
        self.assertIn("当前口令不对", failed[0][2])
        dump = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn(self.NEW, dump, "审计里出现了新口令明文")
        self.assertNotIn("good-pass", dump, "审计里出现了口令明文")

    def test_repeated_wrong_current_passwords_are_throttled(self):
        """"当前口令"不许当在线爆破口：同一来源失败 5 次后退避（429）。

        这条退避与**登录**那条各算各的（`app.state.pwd_throttle`）——
        改口令时把当前口令记错，不该顺手把登录也一起锁住。
        """
        self.login()
        codes = [self._change({"current": "nope-%d" % i}).status_code for i in range(6)]
        self.assertEqual(codes[:5], [403] * 5, codes)
        self.assertEqual(codes[5], 429, codes)
        self.assertEqual(self._login_with("good-pass").status_code, 200,
                         "改口令的退避把登录也锁住了")
        self.assertTrue(admin_mod.verify_password("good-pass", self._hash()))

    def test_the_self_service_note_and_the_policy_come_from_the_backend(self):
        """「只能改自己 + 增删仍只在命令行」的说法与位数策略**只有后端一份**。

        页面是**渲染**它（`PasswordPageTests` 钉住渲染那一段），不是另抄一遍：
        两处各写一次必然漂开，而"页面上的承诺"漂开是最贵的一种 bug。
        """
        self.login()
        d = self.ac.get("/admin/api/admins").json()
        self.assertEqual(d["self"], "ops")
        self.assertEqual(d["passwordPolicy"]["minLength"], admin_mod.MIN_PASSWORD_LEN)
        self.assertEqual(d["passwordPolicy"]["maxLength"], admin_mod.MAX_PASSWORD_LEN)
        self.assertEqual((admin_mod.MIN_PASSWORD_LEN, admin_mod.MAX_PASSWORD_LEN), (8, 200))
        self.assertIn("命令行", d["note"])
        self.assertIn("--new-admin", d["note"])
        self.assertIn("当前口令", d["note"])
        for want in ("新增 / 删除 / 禁用管理员请用命令行", "谁能进门", "当前口令"):
            with self.subTest(want=want):
                self.assertIn(want, d["selfServiceNote"])


class PasswordPageTests(_AdminCase):
    """页面上的「修改口令」：表单在**账号那张卡**里，右上角另有一个入口。

    为什么放账号卡里（而不是单独一个页签）：账号相关的**说明**与**出口**同处一地 ——
    "增删仍只在命令行、只能改自己、必须带当前口令"这几句就在表单旁边，
    改一处不会漏另一处；右上角那个入口负责"不用找"（它跳到这张卡并聚焦当前口令框）。

    这一组是**静态**判据（页面文本 + 接线），不是真浏览器验证 ——
    仓库里没有前端工具链，页面是自包含的一份 HTML（见 `_page()`）。
    """

    def _html(self):
        r = self.ac.get("/admin/")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_the_form_lives_in_the_account_card_and_is_wired_to_the_api(self):
        html = self._html()
        for want in ('id="pwCard"', 'id="pwCur"', 'id="pwNew"', 'id="pwNew2"',
                     'id="btnPwSave"', "accountsCard", "bindPassword",
                     'wpost("/admin/api/password"', "修改我的口令"):
            with self.subTest(want=want):
                self.assertIn(want, html)

    def test_the_three_fields_are_password_inputs(self):
        """三个输入框都是 `type="password"`（不把口令显示在屏幕上）。"""
        html = self._html()
        for field in ("pwCur", "pwNew", "pwNew2"):
            with self.subTest(field=field):
                idx = html.index('id="%s"' % field)
                tag = html[idx:html.index(">", idx) + 1]
                self.assertIn('type="password"', tag)
                self.assertIn("autocomplete=", tag)

    def test_the_two_entries_are_checked_on_the_client_before_sending(self):
        """两次输入不一致 → **不发请求**（提示语在前，`wpost` 在后）。"""
        html = self._html()
        self.assertIn("两次输入的新口令不一致", html)
        self.assertLess(html.index("两次输入的新口令不一致"),
                        html.index('wpost("/admin/api/password"'))
        # 后端给的中文原因原样显示（不是"HTTP 403"了事）
        self.assertIn('fail("修改失败：" + e.message)', html)

    def test_the_right_hand_entry_jumps_to_the_card(self):
        """右上角那个入口 = 跳到账号卡 + 把光标放进「当前口令」。"""
        html = self._html()
        self.assertIn('id="btnPwd"', html)
        self.assertIn('activateTab("inventory")', html)
        self.assertIn('$("#pwCur")', html)

    def test_the_page_states_the_minimum_from_the_backend_policy(self):
        """位数写明在页面上，而且**数字来自后端**（页面不另抄一份）。"""
        html = self._html()
        self.assertIn("P.minLength", html)
        self.assertIn("至少", html)
        self.login()
        self.assertEqual(self.ac.get("/admin/api/admins").json()["passwordPolicy"]["minLength"],
                         admin_mod.MIN_PASSWORD_LEN)

    def test_the_two_sentences_about_who_may_change_accounts_are_rendered(self):
        """「只能改自己」「新增/删除仍只在命令行」两句都在卡片里显示（后端给、页面渲染）。"""
        html = self._html()
        self.assertIn("acc.note", html)
        self.assertIn("acc.selfServiceNote", html)
        # 卡片标题原来写的是「本面板只读」—— 现在开了改口令，那句话必须改掉（不能骗人）
        self.assertNotIn("管理员账号（本面板只读）", html)

    def test_the_password_never_touches_browser_storage_or_the_url(self):
        html = self._html()
        for bad in ("localStorage.setItem", "sessionStorage.setItem",
                    "indexedDB.open", "document.cookie", "password="):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, html)


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


class PerfConsoleTests(_AdminCase):
    """「性能」页签（用户 2026-09 要求：**登录后按「开始记录」才采，只放内存，用图表看**）。

    采集器**整个是替身**（`tests/test_perfmon.py::_FakeHost`）—— 这个类里
    没有一处会真的去调 `nvidia-smi` / 读 `/proc` / 用 psutil。
    `background=False`：采不采完全由用例自己驱动（没有 sleep、没有超时抖动），
    "1 秒一个点"那条节奏由 `tests/test_perfmon.py::BackgroundThreadTests` 管。
    """

    def make_admin_app(self):
        self.host = _FakeHost()
        self.perf = perfmon_mod.PerfMonitor(
            sampler=self.host,
            server_sampler=lambda: {"active": 1, "maxConcurrent": 2,
                                    "vramUsedMb": 5123.0, "vramBudgetMb": 20480.0},
            interval_s=1.0, window_s=60.0, viewer_ttl_s=10.0, background=False)
        return admin_mod.create_admin_app(self.cfg, self.state, perf=self.perf)

    def _sample(self, count, start=100.0):
        """像页面在轮询那样驱动采集（`touch` = 有人在看，`tick` = 采一个点）。"""
        for i in range(count):
            self.perf.touch(start + i)
            self.perf.tick(now=start + i)

    def test_the_two_writes_are_gated_like_every_other_write(self):
        """未登录 401；错 Origin / 缺 `X-ECHO-Admin` / 缺 CSRF → 403（与既有闸门一致）。"""
        for path in ("/admin/api/perf/start", "/admin/api/perf/stop"):
            with self.subTest(path=path, gate="no-session"):
                r = self.ac.post(path, json={})
                self.assertEqual(r.status_code, 401, r.text)
        self.login()
        for path in ("/admin/api/perf/start", "/admin/api/perf/stop"):
            with self.subTest(path=path, gate="origin"):
                r = self.wpost(path, origin="http://evil.example")
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn("Origin", r.json()["detail"])
            with self.subTest(path=path, gate="header"):
                r = self.wpost(path, header=False)
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn(admin_mod.WRITE_HEADER, r.json()["detail"])
            with self.subTest(path=path, gate="csrf"):
                r = self.wpost(path, csrf=False)
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn("CSRF", r.json()["detail"])
        self.assertFalse(self.perf.recording, "被闸门挡回来的请求居然开始了记录")
        self.assertEqual(self.host.calls, 0, "被闸门挡回来的请求居然采了点")

    def test_start_records_and_the_sampler_feeds_the_points(self):
        self.login()
        r = self.wpost("/admin/api/perf/start")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["recording"])
        self.assertTrue(r.json()["sampling"])
        self.assertEqual(r.json()["intervalSeconds"], 1.0)
        self.assertEqual(r.json()["capacity"], 60)
        self._sample(4, start=1.0)
        self.assertEqual(self.host.calls, 4)
        got = self.ac.get("/admin/api/perf/points").json()
        self.assertEqual(got["count"], 4)
        self.assertTrue(got["recording"])
        row = got["series"][-1]
        self.assertEqual(row["gpuPercent"], 40.0)          # 打桩的假数据确实进了点
        self.assertEqual(row["gpuMemUsedMb"], 400.0)
        self.assertEqual(row["vramUsedMb"], 5123.0)        # 后端自报
        self.assertEqual(row["active"], 1.0)               # 在途请求数

    def test_points_are_incremental_and_stop_freezes_the_count(self):
        self.login()
        self.perf.start(now=100.0)
        self._sample(5, start=100.0)
        head = self.ac.get("/admin/api/perf/points").json()
        self.assertEqual([p["seq"] for p in head["series"]], [1, 2, 3, 4, 5])
        self.assertEqual(head["nextSince"], 5)
        again = self.ac.get("/admin/api/perf/points?since=%s" % head["nextSince"]).json()
        self.assertEqual(again["series"], [], "拿了 nextSince 再取一次不该又是全量")
        self.assertEqual(again["nextSince"], 5)
        self._sample(2, start=105.0)
        inc = self.ac.get("/admin/api/perf/points?since=5").json()
        self.assertEqual([p["seq"] for p in inc["series"]], [6, 7])
        self.assertEqual(inc["nextSince"], 7)
        stop = self.wpost("/admin/api/perf/stop")
        self.assertEqual(stop.status_code, 200, stop.text)
        self.assertFalse(stop.json()["recording"])
        self._sample(20, start=200.0)
        after = self.ac.get("/admin/api/perf/points").json()
        self.assertEqual(after["samples"], 7, "停止之后还在长点")
        self.assertEqual(self.host.calls, 7)

    def test_the_ring_window_caps_the_buffer(self):
        """**缓存上限**：窗口 60 秒（60 点）里塞 70 个点 → 缓冲不超过 60（覆盖最老的）。"""
        self.login()
        started = self.wpost("/admin/api/perf/start", {"windowSeconds": 60})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["capacity"], 60)
        self._sample(70, start=1000.0)
        state = self.ac.get("/admin/api/perf/state").json()
        self.assertEqual(state["samples"], 60)
        self.assertEqual(state["totalSampled"], 70)
        self.assertEqual(state["dropped"], 10)
        self.assertEqual(state["firstSeq"], 11)
        self.assertEqual(state["lastSeq"], 70)
        self.assertEqual(self.ac.get("/admin/api/perf/points").json()["count"], 60)

    def test_an_out_of_range_window_is_a_400_and_starts_nothing(self):
        self.login()
        for bad in ({"windowSeconds": 10}, {"windowSeconds": 99999}, {"windowSeconds": "abc"}):
            with self.subTest(bad=bad):
                r = self.wpost("/admin/api/perf/start", bad)
                self.assertEqual(r.status_code, 400, r.text)
        self.assertFalse(self.perf.recording, "400 却已经开始了记录")

    def test_a_bad_since_is_a_400(self):
        self.login()
        r = self.ac.get("/admin/api/perf/points?since=abc")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("since", r.json()["detail"])

    def test_the_two_reads_are_not_cacheable(self):
        """读数是"每一秒都不一样"的：被任何一层缓存住都会表现成「图冻住了」。"""
        self.login()
        for path in ("/admin/api/perf/state", "/admin/api/perf/points"):
            with self.subTest(path=path):
                r = self.ac.get(path)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_start_and_stop_are_audited(self):
        self.login()
        self.wpost("/admin/api/perf/start")
        self.wpost("/admin/api/perf/stop")
        actions = [a for _who, a, _t in self.audit_rows()]
        self.assertIn("perf-start", actions)
        self.assertIn("perf-stop", actions)

    def test_the_whole_cycle_makes_no_file_write(self):
        """**不落盘**（需求 + 只读根文件系统的环境约束）。

        注意这条断言的范围：它盯的是**采样数据**这条路 —— 从按开始到取点，
        一个写文件的调用都不许有。写动作的**审计**行是既有机制（落的是 `admin_audit`，
        不是性能数据），它与这里无关。
        """
        writes = []
        real_open = builtins.open

        def spy_open(file, mode="r", *args, **kw):
            if any(ch in str(mode) for ch in "wax+"):
                writes.append(("open", str(file), str(mode)))
            return real_open(file, mode, *args, **kw)

        def boom(name):
            def fn(*a, **kw):
                writes.append((name, str(a[:1]), ""))
                raise AssertionError("性能这条路不许调用 %s" % name)
            return fn

        self.login()
        with patch.object(builtins, "open", spy_open), \
                patch.object(os, "replace", boom("os.replace")), \
                patch.object(os, "remove", boom("os.remove")), \
                patch.object(os, "rename", boom("os.rename")):
            self.wpost("/admin/api/perf/start")
            self._sample(5, start=100.0)
            self.ac.get("/admin/api/perf/points")
            self.ac.get("/admin/api/perf/state")
            self.wpost("/admin/api/perf/stop")
        self.assertEqual(writes, [], "性能这条路上出现了写文件调用：%s" % writes)


class PerfPageTests(_AdminCase):
    """「性能」页签的页面：**4 张图，一行 2 个、共 2 行**（需求指名要 2×2）。"""

    def _html(self):
        r = self.ac.get("/admin/")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_there_are_exactly_four_charts(self):
        html = self._html()
        for cid in ("chart-gpu", "chart-vram", "chart-cpu", "chart-ram"):
            with self.subTest(chart=cid):
                self.assertIn('id="%s"' % cid, html)
        self.assertEqual(html.count("<svg"), 4, "性能页签要正好 4 张折线图")

    def test_the_four_charts_are_a_two_column_grid(self):
        compact = self._html().replace(" ", "")
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", compact,
                      "4 张图必须是「一行 2 个、两行」的栅格")

    def test_the_tab_and_the_controls_are_on_the_page(self):
        html = self._html()
        self.assertIn('data-tab="perf"', html)
        self.assertIn("/admin/api/perf/start", html)
        self.assertIn("/admin/api/perf/stop", html)
        self.assertIn("/admin/api/perf/points", html)
        self.assertIn("?since=", html)                    # 增量取点
        self.assertIn("mergePoints", html)
        self.assertIn("setInterval(refreshPerf", html)     # 1.5 秒轮询

    def test_it_stops_polling_when_the_page_goes_away(self):
        html = self._html()
        self.assertIn('addEventListener("pagehide", stopPerfPoll)', html)
        self.assertIn("visibilitychange", html)

    def test_the_page_still_has_no_external_frontend_dependency(self):
        """服务端页面**不进前端工具链**：图表是内联 SVG，不是 chart.js/echarts。"""
        html = self._html()
        for bad in ("//cdn", "chart.js", "echarts", "unpkg", "jsdelivr", "type=\"module\""):
            with self.subTest(dep=bad):
                self.assertNotIn(bad, html)


class LimitsConsoleTests(_AdminCase):
    """「运行参数」：并发上限由管理员在页面上配（2026-09-29 用户要求）。

    这一组盯五件事：

    1. **与其它写动作同一套闸门**：未登录 401；错 Origin / 缺 `X-ECHO-Admin` / 缺 CSRF → 403，
       而且**被挡回来时值一个都没变**（只断言状态码是不够的）；
    2. 保存 → 读回一致（含"来源 = 管理面"与取值范围）；
    3. 越界 / 非数 → **400 + 中文原因**，值不变、库不动；
    4. **热生效**：保存完**同一个进程**的闸门立刻按新值判（判据是"真的被 503 顶回来"）；
    5. **持久化**：重开库 + 一份全新 cfg 之后仍然生效（= 重启后还在）。
    """

    def _limits(self):
        r = self.ac.get("/admin/api/limits")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _save(self, payload):
        return self.wpost("/admin/api/limits", payload)

    def _saved_rows(self):
        return {r["name"]: r["value"] for r in self.state.auth.store.limit_overrides()}

    def test_the_read_needs_a_login(self):
        r = self.ac.get("/admin/api/limits")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["code"], "unauthorized")

    def test_every_gate_refuses_and_the_values_do_not_move(self):
        def now():
            return (self.cfg.max_concurrent, self.cfg.per_client_concurrent,
                    int(self.cfg.get("limits.queue_max", -1)))

        before = now()
        # 未登录 → 401（先认身份，再判"这次请求像不像本站浏览器发的"）
        r = self.ac.post("/admin/api/limits", json={"maxConcurrent": 9})
        self.assertEqual(r.status_code, 401, r.text)
        self.login()
        for kw in ({"origin": "http://evil.example"}, {"header": False}, {"csrf": False}):
            with self.subTest(gate=sorted(kw)):
                r = self.wpost("/admin/api/limits", {"maxConcurrent": 9}, **kw)
                self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(now(), before, "被闸门挡回来的请求居然改了生效值")
        self.assertEqual(self._saved_rows(), {}, "被闸门挡回来的请求居然落了库")

    def test_the_factory_value_is_six_and_says_where_it_comes_from(self):
        self.login()
        got = self._limits()
        self.assertEqual(got["limits"]["maxConcurrent"], 6)
        self.assertEqual(got["limits"]["perClientConcurrent"], 1)
        self.assertEqual(got["limits"]["queueMax"], 0)
        self.assertEqual(got["source"]["maxConcurrent"], "default")
        self.assertEqual(got["ranges"]["maxConcurrent"], [1, 64])
        self.assertEqual(got["ranges"]["perClientConcurrent"], [1, 64])
        self.assertEqual(got["ranges"]["queueMax"], [0, 100])
        self.assertEqual(got["priority"], "admin")
        self.assertIn("管理面配置", got["priorityNote"])

    def test_save_then_read_back(self):
        self.login()
        r = self._save({"maxConcurrent": 9, "perClientConcurrent": 2, "queueMax": 3})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["limits"]["maxConcurrent"], 9)
        self.assertEqual(body["source"]["maxConcurrent"], "admin")
        again = self._limits()
        self.assertEqual(again["limits"],
                         {"maxConcurrent": 9, "perClientConcurrent": 2, "queueMax": 3})
        self.assertEqual(again["source"]["perClientConcurrent"], "admin")
        self.assertEqual(again["updatedBy"], "ops")
        self.assertTrue(again["updatedAt"] > 0)

    def test_a_partial_save_leaves_the_other_values_alone(self):
        """只改总并发时，另两个不许被"顺手重置成默认"。"""
        self.login()
        self.assertEqual(self._save({"maxConcurrent": 10}).status_code, 200)
        got = self._limits()
        self.assertEqual(got["limits"]["maxConcurrent"], 10)
        self.assertEqual(got["limits"]["perClientConcurrent"], 1)
        self.assertEqual(got["limits"]["queueMax"], 0)
        self.assertEqual(got["source"]["queueMax"], "default")

    def test_out_of_range_is_a_400_with_a_reason_and_changes_nothing(self):
        self.login()
        before = self._limits()["limits"]
        for bad, want in (({"maxConcurrent": 0}, "总并发"),
                          ({"maxConcurrent": 65}, "总并发"),
                          ({"maxConcurrent": "abc"}, "总并发"),
                          ({"maxConcurrent": ""}, "总并发"),
                          ({"perClientConcurrent": 7}, "每客户端并发"),
                          ({"queueMax": -1}, "队列上限"),
                          ({"queueMax": 101}, "队列上限")):
            with self.subTest(bad=bad):
                r = self._save(bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["code"], "bad_request")
                self.assertIn(want, r.json()["detail"])
                self.assertIn("请求不合法", r.json()["message"])
                self.assertEqual(self._limits()["limits"], before, "400 却改了值")
                self.assertEqual(self._saved_rows(), {}, "400 却落了库")

    def test_a_hot_change_is_seen_by_the_gate_without_a_restart(self):
        """**热生效**：保存之后，同一个进程的闸门立刻按新值判。

        判据不是"库里读得到"，而是**真的被 503 顶回来** —— 只测 store 的话，
        "执行的地方在启动时缓存了上限"这种错会照样绿。
        """
        self.login()
        self.assertEqual(self._save({"maxConcurrent": 1, "perClientConcurrent": 1}).status_code,
                         200)
        self.assertEqual(self.state.admission.snapshot()["maxConcurrent"], 1)
        with self.state.admission.hold("someone-else"):
            r = self.call_asr()
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(r.json()["code"], "server_busy")
        # 放开之后立刻又能用：变的是上限，不是把服务锁死了
        after = self.call_asr()
        self.assertEqual(after.status_code, 200, after.text)
        # `/v1/capabilities` 也是现读（客户端据此判断"要不要等"）
        self.assertEqual(self.client.get("/v1/capabilities").json()["limits"]["maxConcurrent"], 1)

    def test_the_value_survives_a_restart(self):
        """持久化：**重开库 + 一份全新 cfg**（= 重启进程）之后，值仍然生效。"""
        self.login()
        self.assertEqual(self._save({"maxConcurrent": 11, "perClientConcurrent": 2,
                                     "queueMax": 4}).status_code, 200)
        reopened = store_mod.Store(self.cfg.get("auth.db"))
        self.addCleanup(reopened.close)
        fresh = settings_mod.load()
        fresh.raw["auth"]["db"] = self.cfg.get("auth.db")
        self.assertEqual(limits_mod.apply_stored(fresh, reopened),
                         {"max_concurrent": 11, "per_client_concurrent": 2, "queue_max": 4})
        self.assertEqual(fresh.max_concurrent, 11)
        self.assertEqual(fresh.per_client_concurrent, 2)
        self.assertEqual(int(fresh.get("limits.queue_max")), 4)

    def test_the_admin_value_beats_the_environment_variable(self):
        """**优先级钉在接口上**：env 在场面时管理面保存的值仍然赢，且如实提示。

        为什么必须钉：`server/compose.yaml` 默认就设了 `ECHO_MAX_CONCURRENT`，
        若 env 优先，页面上改成别的值会**静默不生效** —— 后人只会以为"改了没用"。
        """
        self.login()
        with patch.dict(os.environ, {"ECHO_MAX_CONCURRENT": "2"}, clear=False):
            self.assertEqual(self._save({"maxConcurrent": 6}).status_code, 200)
            got = self._limits()
            self.assertEqual(got["limits"]["maxConcurrent"], 6, "env 把管理面的值盖住了")
            self.assertEqual(got["source"]["maxConcurrent"], "admin")
            self.assertEqual(got["envValues"]["maxConcurrent"], 2,
                             "env 仍然要**如实报出来**，不是装作没有")
            self.assertTrue(any("ECHO_MAX_CONCURRENT=2" in n for n in got["notes"]),
                            got["notes"])
            self.assertEqual(self.state.admission.snapshot()["maxConcurrent"], 6)

    def test_the_save_is_audited_with_the_admin_name(self):
        self.login()
        self._save({"maxConcurrent": 8})
        hit = [r for r in self.audit_rows() if r[1] == "limits-set"]
        self.assertTrue(hit, self.audit_rows())
        self.assertEqual(hit[0][0], "ops")
        self.assertIn("maxConcurrent=8", hit[0][2])

    def test_a_rejected_save_is_audited_with_the_reason(self):
        """越界也要留痕（与其它写动作一致：失败的动作名带 `.failed`）。"""
        self.login()
        self._save({"maxConcurrent": 999})
        hit = [r for r in self.audit_rows() if r[1] == "limits-set.failed"]
        self.assertTrue(hit, self.audit_rows())
        self.assertIn("maxConcurrent=999", hit[0][2])


class LimitsPageTests(_AdminCase):
    """「运行参数」卡在页面上（与 4 张图同一个页签，**不进前端工具链**）。"""

    def _html(self):
        r = self.ac.get("/admin/")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_the_card_and_its_three_fields_are_on_the_page(self):
        """三个输入框是**按后端给的范围动态生成**的（`id="lim-<api 字段名>"`），
        所以这里钉的是那段生成逻辑 + 三个字段名都真的被发出去。"""
        html = self._html()
        for want in ("/admin/api/limits", 'id="btnLimits"', 'id="lim-${esc(api)}"',
                     "limitsCard", "bindLimits", "已保存并生效",
                     "maxConcurrent", "perClientConcurrent", "queueMax"):
            with self.subTest(want=want):
                self.assertIn(want, html)

    def test_the_card_sits_in_the_perf_tab_above_the_charts(self):
        """放在「性能」页签里、4 张图上方：并发上限本来就是性能参数，
        调它要看的就是旁边那两张图（在途请求数 / GPU 利用率）。"""
        html = self._html()
        self.assertIn('data-tab="perf"', html)
        self.assertIn("运行参数", html)
        self.assertLess(html.index("${limitsCard(lim)}"),
                        html.index("${tpl ? tpl.innerHTML"))

    def test_the_page_says_the_queue_limit_has_no_executor_yet(self):
        """**不骗人**：v1 不排队，队列上限现在只影响 capabilities 里宣告的 queueMax ——
        这句话由后端给（`queueNote`），页面必须把它显示出来，不能只画一个输入框。"""
        html = self._html()
        self.assertIn("d.queueNote", html)          # 页面确实渲染了那句提示
        self.login()
        self.assertIn("queueNote", self.ac.get("/admin/api/limits").text)


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
