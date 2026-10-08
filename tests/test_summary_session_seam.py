# -*- coding: utf-8 -*-
"""纪要会话的**路径接缝**守卫（2026-10-08 真实事故）。

事故形状：
  * `_summary_session()` 从库里拿到本场登记的会话后**直接复用**，从不校验它的 cwd；
  * 于是"用另一棵树的代码 + 这棵树的数据根"跑一次（我自己就这样）之后，那个会话
    被建在 `C:\\echo-dev\\data\\meetings`，**库里那条注册也被改写成它**；
  * 此后每次生成纪要都复用它 —— 用户在 DSH 侧栏里永远看到"纪要会话不在会议分组里"，
    而且**重生成多少次都不会好**。

判据（两条）：
  1. cwd 与当前会议工作区**不一致** → 必须重建，不能复用；
  2. cwd 一致 / 探测不到 → 保持复用（别把能用的会话丢掉）。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                                 # noqa: E402
import app.meeting as meeting                                       # noqa: E402


class _FakeClient:
    name = "harness"

    def __init__(self, sessions):
        self._sessions = sessions
        self.created = []
        self.cleared = []

    def list_sessions(self):
        return list(self._sessions)

    def has_workspaces(self):
        return True

    def ensure_workspace(self, path, title=""):
        return "ws-1", False

    def create_session(self, cwd=None, workspace_id=None):
        sid = "session-new-%d" % (len(self.created) + 1)
        self.created.append({"cwd": cwd, "workspace_id": workspace_id, "sid": sid})
        return sid

    def ensure_session(self, kind, name=""):
        self.created.append({"kind": kind, "name": name, "sid": "session-ensure"})
        return "session-ensure"

    def touch_meeting_session(self, *a, **k):
        pass


class SummarySessionPathSeamTests(unittest.TestCase):
    WS = r"D:\ECHO\meeting"
    OLD = r"C:\echo-dev\data\meetings"

    def setUp(self):
        # 清掉进程内缓存，免得上一个用例的会话漏进来
        with meeting._MEETING_SESSIONS_LOCK:
            meeting._MEETING_SESSIONS.clear()
        self.addCleanup(meeting._MEETING_SESSIONS.clear)

    def _run(self, registered_sid, sessions):
        client = _FakeClient(sessions)
        # `ws` 在 `_summary_session` 里来自 `_paths.meeting_space_root()`（函数内
        # `from app import paths as _paths`），所以打桩要打在 **app.paths** 上。
        import app.paths as paths_mod
        with patch.object(paths_mod, "meeting_space_root", lambda: self.WS), \
                patch.object(db, "get_meeting_session",
                             lambda mid, agent=None: {"session_id": registered_sid}), \
                patch.object(db, "touch_meeting_session", lambda *a, **k: None), \
                patch.object(db, "add_log", lambda *a, **k: None):
            return meeting._summary_session(client, 62), client

    def test_a_session_in_the_wrong_workspace_is_rebuilt(self):
        """cwd 与当前会议工作区不一致 → 重建（**这条是事故的回归**）。"""
        sid, client = self._run("session-stale",
                                [{"sessionId": "session-stale", "cwd": self.OLD}])
        self.assertNotEqual(sid, "session-stale",
                            "错工作区的会话被复用了 —— 用户会一直看到它在别的分组里")
        self.assertTrue(client.created, "没有重建会话")
        self.assertEqual(client.created[0].get("workspace_id"), "ws-1",
                         "重建时要**走工作区**（用 cwd 建会落到未分组）")

    def test_a_session_in_the_right_workspace_is_reused(self):
        sid, client = self._run("session-good",
                                [{"sessionId": "session-good", "cwd": self.WS}])
        self.assertEqual(sid, "session-good", "同工作区的会话应当复用（别每次新建）")
        self.assertFalse(client.created, "不该新建")

    def test_case_and_separator_differences_still_count_as_the_same(self):
        """路径比较要归一化大小写/分隔符（Windows 上 `D:\\ECHO\\meeting` 有多种写法）。"""
        sid, _client = self._run("session-good",
                                 [{"sessionId": "session-good",
                                   "cwd": self.WS.lower().replace("\\", "/")}])
        self.assertEqual(sid, "session-good")

    def test_unknown_session_is_reused_not_rebuilt(self):
        """后端不认识这个会话（探测不到 cwd）→ 保持旧行为，别把能用的会话丢掉。"""
        sid, client = self._run("session-x", [])
        self.assertEqual(sid, "session-x")
        self.assertFalse(client.created)


if __name__ == "__main__":
    unittest.main()
