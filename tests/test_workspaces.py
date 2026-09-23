# -*- coding: utf-8 -*-
"""默认 DSH 分组（「会议空间」/「指令空间」）的建立与命名。

2026-09-23 需求：新机器装好就该在 DSH 侧栏看到这两个分组，而不是攒一堆「未分组」。
要成立得三件事一起做（见 app/workspaces.py 的说明）：目录存在 + workspace/create +
workspace/rename（DSH 的 create **只收目录**，名字由目录名派生）。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import workspaces
from app.agents.dsh_agent import DshAgent


class _FakeClient:
    """记录调用的假智能体客户端。

    `titles=None` 表示"DSH 里的名字就是我要的那个"（= rename 成功）；
    给 dict 则表示显式指定读回来的名字（用来构造"名字不对"的场景）。
    """

    def __init__(self, created=True, titles=None, boom=None):
        self.created = created
        self.titles = None if titles is None else dict(titles)
        self.boom = boom
        self.calls = []
        self.wids = {}
        self._intended = {}

    def ensure_workspace(self, path, title=""):
        self.calls.append(("ensure_workspace", path, title))
        if self.boom:
            raise RuntimeError(self.boom)
        wid = self.wids.setdefault(path, "ws-%d" % (len(self.wids) + 1))
        self._intended[wid] = title
        return wid, self.created

    def workspace_title(self, workspace_id):
        if self.titles is not None:
            return self.titles.get(workspace_id, "")
        return self._intended.get(workspace_id, "")


def _specs(tmp, meeting_title="会议空间", command_title="指令空间"):
    return [
        dict(key="meetingWorkspace", label="会议空间", title=meeting_title,
             path=os.path.join(tmp, "meetings"), what="会议"),
        dict(key="commandWorkspace", label="指令空间", title=command_title,
             path=os.path.join(tmp, "command"), what="指令"),
    ]


class SpaceSpecTests(unittest.TestCase):
    def test_settings_drive_paths_and_titles(self):
        table = {"meetingWorkspace": "{ECHO}/data/meetings",
                 "meetingWorkspaceTitle": "会议空间",
                 "commandWorkspace": "{ECHO}/data/command",
                 "commandWorkspaceTitle": "指令空间"}
        with patch.object(workspaces, "_setting", lambda k, d="": table.get(k, d)):
            specs = workspaces.space_specs()
        self.assertEqual(["会议空间", "指令空间"], [s["label"] for s in specs])
        self.assertEqual(["会议空间", "指令空间"], [s["title"] for s in specs])
        self.assertEqual("{ECHO}/data/meetings", specs[0]["path"])
        self.assertEqual("{ECHO}/data/command", specs[1]["path"])

    def test_title_for_path_uses_configured_title_for_default_spaces(self):
        table = {"meetingWorkspace": r"D:\ECHO\data\meetings",
                 "meetingWorkspaceTitle": "会议空间"}
        with patch.object(workspaces, "_setting", lambda k, d="": table.get(k, d)):
            self.assertEqual("会议空间", workspaces.title_for_path(r"D:\ECHO\data\meetings"))
            # 大小写/结尾斜杠容错
            self.assertEqual("会议空间", workspaces.title_for_path("d:\\echo\\data\\meetings\\"))

    def test_title_for_path_keeps_basename_for_other_dirs(self):
        with patch.object(workspaces, "_setting", lambda k, d="": ""):
            self.assertEqual("日常交互", workspaces.title_for_path(r"D:\work\日常交互"))
            self.assertEqual("", workspaces.title_for_path(""))

    def test_title_falls_back_to_factory_name_when_setting_blank(self):
        table = {"meetingWorkspace": r"D:\ECHO\data\meetings", "meetingWorkspaceTitle": ""}
        with patch.object(workspaces, "_setting", lambda k, d="": table.get(k, d)):
            self.assertEqual("会议空间", workspaces.title_for_path(r"D:\ECHO\data\meetings"))


class EnsureSpacesTests(unittest.TestCase):
    def test_creates_dir_and_asks_for_the_group_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient()
            with patch.object(workspaces, "space_specs", lambda: _specs(tmp)):
                report = workspaces.ensure_spaces(client=client, log=False)
            self.assertTrue(os.path.isdir(os.path.join(tmp, "meetings")))
            self.assertTrue(os.path.isdir(os.path.join(tmp, "command")))
        self.assertEqual([("ensure_workspace", os.path.join(tmp, "meetings"), "会议空间"),
                          ("ensure_workspace", os.path.join(tmp, "command"), "指令空间")],
                         client.calls)
        self.assertEqual(["created", "created"], [r["action"] for r in report])
        self.assertEqual([True, True], [r["title_ok"] for r in report])
        self.assertEqual("ws-1", report[0]["workspaceId"])

    def test_existing_workspace_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(created=False)
            with patch.object(workspaces, "space_specs", lambda: _specs(tmp)):
                report = workspaces.ensure_spaces(client=client, log=False)
        self.assertEqual(["kept", "kept"], [r["action"] for r in report])

    def test_wrong_title_is_reported_not_hidden(self):
        """改名没成（或用户自己起了名）必须如实标 title_ok=False —— 别自说自话。"""
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(created=False,
                                 titles={"ws-1": "meetings", "ws-2": "command"})
            with patch.object(workspaces, "space_specs", lambda: _specs(tmp)):
                report = workspaces.ensure_spaces(client=client, log=False)
        self.assertEqual([False, False], [r["title_ok"] for r in report])
        self.assertIn("meetings", report[0]["detail"])

    def test_unreadable_title_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(titles={})          # 读不回分组名
            with patch.object(workspaces, "space_specs", lambda: _specs(tmp)):
                report = workspaces.ensure_spaces(client=client, log=False)
        self.assertEqual([None, None], [r["title_ok"] for r in report])
        self.assertEqual(["created", "created"], [r["action"] for r in report])

    def test_unconfigured_space_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = [dict(key="commandWorkspace", label="指令空间", title="指令空间",
                          path="", what="指令")]
            client = _FakeClient()
            with patch.object(workspaces, "space_specs", lambda: empty):
                report = workspaces.ensure_spaces(client=client, log=False)
        self.assertEqual("skipped", report[0]["action"])
        self.assertEqual([], client.calls)

    def test_no_agent_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(workspaces, "space_specs", lambda: _specs(tmp)), \
                    patch.object(workspaces, "_client", lambda: (None, "harness 没起来")):
                report = workspaces.ensure_spaces(log=False)
        self.assertEqual(["no-agent", "no-agent"], [r["action"] for r in report])
        self.assertIn("harness 没起来", report[0]["detail"])

    def test_client_error_becomes_a_failed_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(boom="workspace/not-found")
            with patch.object(workspaces, "space_specs", lambda: _specs(tmp)):
                report = workspaces.ensure_spaces(client=client, log=False)
        self.assertEqual(["failed", "failed"], [r["action"] for r in report])
        self.assertIn("workspace/not-found", report[0]["detail"])


class _TitleAgent(DshAgent):
    """只测 `_apply_title`：不建连接，rpc 换成记录器。"""

    def __init__(self, current_title=""):
        self._current = current_title
        self.calls = []

    def workspace_title(self, workspace_id):
        return self._current

    def rpc(self, method, args=None, **kwargs):
        self.calls.append((method, args))
        return {"value": {}}

    @property
    def renames(self):
        return [c for c in self.calls if c[0] == "workspace/rename"]


class ApplyTitleTests(unittest.TestCase):
    def test_freshly_created_workspace_is_renamed(self):
        agent = _TitleAgent(current_title="meetings")      # 新建时名字=目录名
        with patch("app.agents.dsh_agent.db.add_log", lambda *a, **k: None):
            action = agent._apply_title("ws-1", r"D:\ECHO\data\meetings", "会议空间",
                                        just_created=True)
        self.assertEqual("renamed", action)
        self.assertEqual(1, len(agent.renames))
        req = agent.renames[0][1]["request"]
        self.assertEqual({"workspaceId": "ws-1", "title": "会议空间"}, req)

    def test_untouched_default_name_is_renamed_even_when_not_created(self):
        """老机器上工作区早就建过（名字还是目录名）—— 这正是要升级的那种。"""
        agent = _TitleAgent(current_title="meetings")
        with patch("app.agents.dsh_agent.db.add_log", lambda *a, **k: None):
            action = agent._apply_title("ws-1", r"D:\ECHO\data\meetings", "会议空间",
                                        just_created=False)
        self.assertEqual("renamed", action)

    def test_user_named_workspace_is_left_alone(self):
        agent = _TitleAgent(current_title="我自己的会")
        action = agent._apply_title("ws-1", r"D:\ECHO\data\meetings", "会议空间",
                                    just_created=False)
        self.assertEqual("user-named", action)
        self.assertEqual([], agent.renames)

    def test_already_named_is_a_no_op(self):
        agent = _TitleAgent(current_title="会议空间")
        self.assertEqual("unchanged",
                         agent._apply_title("ws-1", r"D:\ECHO\data\meetings", "会议空间"))
        self.assertEqual([], agent.renames)

    def test_rename_failure_is_swallowed_but_logged(self):
        class _Bad(_TitleAgent):
            def rpc(self, method, args=None, **kwargs):
                raise RuntimeError("gateway/bad-request")

        agent = _Bad(current_title="meetings")
        seen = []
        with patch("app.agents.dsh_agent.db.add_log",
                   lambda level, source, msg: seen.append((level, source, msg))):
            action = agent._apply_title("ws-1", r"D:\ECHO\data\meetings", "会议空间",
                                        just_created=True)
        self.assertEqual("rename-failed", action)
        self.assertEqual(1, len(seen))
        self.assertIn("会议空间", seen[0][2])


if __name__ == "__main__":
    unittest.main()
