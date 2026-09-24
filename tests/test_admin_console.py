# -*- coding: utf-8 -*-
"""管理面（设计 §8.4）：**独立端口上的只读控制台**。

这一组用例盯三件事：

1. **进不来就不给看**：没登录一律 401；口令错不给"用户名不存在 vs 口令不对"的区分
   （那等于送一个枚举账号的接口）；失败多了要退避。
2. **与能力面严格隔离**：管理面 app 上没有 `/v1/*`，能力面 app 上没有 `/admin/*`。
   设计说这靠**两个端口**来保证 —— 那就有两条会红的用例钉着，而不是一句承诺。
3. **只读**：管理面**没有任何写端点**（写动作走命令行，那条路直接开库、不多开一条 HTTP 入口）。
   这条是"加写动作之前先想清楚"的闸：真要加，这条用例会红，加的人必须在提交里说明理由。
"""
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
from tests.test_server_contract import FAKE_SPECS, _fake_loader, _wav_bytes  # noqa: E402


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
        self.ac = TestClient(self.admin)

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
        return r

    def cap_headers(self, client_id="cli-1"):
        """造一个客户端并给它一个真令牌（能力面开着鉴权，未鉴权的请求**不记账**）。"""
        from server import auth as auth_mod
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
        r = self.login()
        csrf = r.json()["csrf"]
        bad = self.ac.post("/admin/api/logout")
        self.assertEqual(bad.status_code, 403, "没带 CSRF 令牌也让它登出？")
        ok = self.ac.post("/admin/api/logout", headers={"X-CSRF-Token": csrf})
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

    def test_the_admin_api_has_no_write_endpoints(self):
        """**只读**。真有写端点进来，这条会红 —— 加的人必须在提交里说明理由。"""
        admin_paths = set((self.admin.openapi().get("paths") or {}).keys())
        writes = []
        for path in sorted(admin_paths):
            methods = set((self.admin.openapi()["paths"][path] or {}).keys())
            if methods & {"post", "put", "patch", "delete"}:
                writes.append((path, sorted(methods)))
        # 允许的例外只有一个：登录与登出（它们不算"改服务端状态"）
        allowed = {"/admin/api/login", "/admin/api/logout"}
        got = [w for w in writes if w[0] not in allowed]
        self.assertEqual(got, [], "管理面出现了写端点：%s" % got)

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
