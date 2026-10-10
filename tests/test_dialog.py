# -*- coding: utf-8 -*-
"""原生选择器的契约（2026-10-11 用户要的「浏览…」按钮）。

**一条真弹窗都没有** —— 用例只喂假 runner。要钉住的是那四种结局各自的形状，
以及"**取消不是失败**"和"**什么都不许抛**"这两条：
面板要靠这个返回值决定"用这个路径"还是"让用户手敲"，判错了就会变成一句莫名其妙的红字。
"""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import dialog                                             # noqa: E402


def _runner(rc=0, out=b"", err=b""):
    calls = {}

    def run(argv, env, timeout):
        calls["argv"] = argv
        calls["env"] = env
        calls["timeout"] = timeout
        return rc, out, err

    run.calls = calls
    return run


class PickFolderTests(unittest.TestCase):
    def test_a_chosen_folder_comes_back_as_ok(self):
        r = _runner(0, b"D:\\OneDrive\\\xe7\xac\x94\xe8\xae\xb0")
        got = dialog.pick_folder(runner=r)
        self.assertTrue(got["ok"], got)
        self.assertFalse(got["cancelled"])
        self.assertEqual("D:\\OneDrive\\笔记", got["path"], "中文路径不许被编解码弄坏")

    def test_cancel_is_not_a_failure(self):
        """用户点了取消 —— 这是**正常结局**，前端该继续让他手敲。"""
        got = dialog.pick_folder(runner=_runner(0, b""))
        self.assertFalse(got["ok"])
        self.assertTrue(got["cancelled"], got)
        self.assertNotIn("reason", got, "取消不该带 reason（那不是错误）")

    def test_timeout_says_timeout(self):
        def boom(argv, env, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)
        got = dialog.pick_folder(timeout=7, runner=boom)
        self.assertEqual("timeout", got["reason"], got)
        self.assertFalse(got["cancelled"])

    def test_no_graphical_session_fails_gracefully(self):
        got = dialog.pick_folder(runner=_runner(1, b"", b"cannot show dialog"))
        self.assertEqual("no-gui", got["reason"], got)
        self.assertIn("dialog", got["message"])

    def test_an_unexpected_error_never_escapes(self):
        """接口层：**任何**异常都不许漏出去（漏出去就是 500）。"""
        def boom(argv, env, timeout):
            raise RuntimeError("powershell 不见了")
        got = dialog.pick_folder(runner=boom)
        self.assertEqual("error", got["reason"], got)
        self.assertIn("RuntimeError", got["message"])

    def test_unsupported_platform_is_graceful(self):
        with patch.object(dialog, "supported", lambda: False):
            got = dialog.pick_folder(runner=_runner(0, b"X"))
        self.assertEqual("unsupported", got["reason"], got)
        self.assertIn("粘贴路径", got["message"])

    def test_it_hides_the_console_and_passes_params_as_json(self):
        """① 不能顺带闪一个控制台窗口；② 参数走环境变量（JSON），不许拼进脚本里。"""
        r = _runner(0, b"C:\\vault")
        dialog.pick_folder(title="选笔记库", initial="D:\\one", runner=r)
        self.assertEqual("powershell", r.calls["argv"][0])
        self.assertIn("-STA", r.calls["argv"], "FolderBrowserDialog 需要 STA")
        self.assertNotIn("选笔记库", " ".join(r.calls["argv"]),
                         "标题不许拼进命令行（中文/引号会出问题）")
        import json
        payload = json.loads(r.calls["env"]["ECHO_PICK_JSON"])
        self.assertEqual("选笔记库", payload["title"])
        self.assertEqual("D:\\one", payload["initial"])


class DialogApiWiringTests(unittest.TestCase):
    """接口层：**只有回环能给**，且返回原样透传选择器的结果。"""

    @classmethod
    def setUpClass(cls):
        import io
        with io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "app", "api.py"), encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_the_endpoint_exists_and_is_loopback_only(self):
        self.assertIn('/dialog/pick-folder', self.src)
        self.assertIn("def api_pick_folder", self.src)

    def test_non_loopback_callers_are_refused(self):
        """非回环一律 403（在服务器上弹窗是错的；这条判据只有一处 `_is_loopback_call`）。"""
        i = self.src.index("def api_pick_folder")
        body = self.src[i:i + 1200]
        self.assertIn("_is_loopback_call(request)", body, "必须复用那唯一一处判据")
        self.assertIn("403", body)

    def test_it_delegates_to_the_dialog_module(self):
        i = self.src.index("def api_pick_folder")
        body = self.src[i:i + 1200]
        self.assertIn("dialog.pick_folder", body)

    def test_the_endpoint_really_runs(self):
        """把端点**真的跑一遍**（选择器换成替身，一个窗口都不弹）：

        * 回环 → 200，且原样透传选择器的结果；
        * 非回环 → 403（在别人连过来的会话里弹窗是错的）。
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        import app.api as api_mod
        from tests.auth_off import api_auth_off

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        sentinel = {"ok": False, "cancelled": True}

        # ⚠️ 这台开发机配过手机 → `apiAuthEnabled=true`，而裸 TestClient 的对端是
        # `"testclient"`（判不出回环）→ 会在**回环判据之前**被 `optional_auth` 拦成 401。
        # 这里按仓库的统一做法**在内存里**遮掉那一项（不写库），见 tests/auth_off.py。
        with api_auth_off():
            with patch.object(api_mod, "_is_loopback_call", lambda request: True), \
                    patch.object(dialog, "pick_folder", lambda **kw: sentinel):
                r = client.post("/api/dialog/pick-folder", json={"title": "选笔记库"})
            self.assertEqual(200, r.status_code, r.text)
            self.assertEqual(sentinel, r.json())

            with patch.object(api_mod, "_is_loopback_call", lambda request: False):
                r = client.post("/api/dialog/pick-folder", json={})
            self.assertEqual(403, r.status_code, r.text)


if __name__ == "__main__":
    unittest.main()
