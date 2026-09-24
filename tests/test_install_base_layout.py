# -*- coding: utf-8 -*-
"""安装根布局（3.0，`3.0-设计总览与组件关系.md` §2.2）：`{echoBase}` 下六个兄弟目录。

**为什么单独一个文件**：清点（2026-09-24）时发现**没有任何用例设过 `ECHO_BASE`** ——
开发树叫 `echo-dev`，所以整套用例跑的都是"老式扁平布局"那条回落分支。也就是说
新分支当时是**零覆盖**的：红与绿都不说明问题。这里补的就是那个底座。

布局（六个兄弟目录 + `dsh` 里再分两处）：

    {echoBase}/
    ├── echo-core/   代码（可整体覆盖）
    ├── data/        echo.db / logs / pid / token
    ├── models/      模型权重
    ├── dsh/
    │   ├── app/     DSH 标准版本体
    │   └── home/    DSH_HOME（skills/会话/settings）
    ├── meeting/     会议数据 ≡「会议空间」工作区
    └── aide/        指令数据 ≡「指令空间」工作区

两条铁律在本文件里各有用例盯着：
  * **L6**：可被整体覆盖的 `echo-core/` 里不许出现数据/模型/DSH/会议/指令；
  * **老装机不搬家**：没有安装根时，每个根都逐字等于 2.0 的路径。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import paths                                              # noqa: E402


class _BaseCase(unittest.TestCase):
    """把安装根指到一个临时目录（`ECHO_BASE`），并保证用例之间互不影响。

    **同时把设置读成空**：这台开发机的设置库里 `commandWorkspace` 是用户真改过的
    （`C:\\Users\\...\\Desktop\\临时`）。不隔离的话，"六个兄弟目录"那几条就会去读真实
    机器状态 —— 单跑绿、跟别的用例一起跑红，而报错完全指不到原因（这个坑本仓库踩过好几次）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-base-")
        self.addCleanup(self._cleanup)
        p = patch.dict(os.environ, {"ECHO_BASE": self.tmp}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(paths, "_settings_get", lambda name, d="": "")
        p2.start()
        self.addCleanup(p2.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class BaseDetectionTests(unittest.TestCase):
    def test_a_flat_tree_has_no_base(self):
        """**老装机（代码就在安装根下）必须没有安装根** —— 有的话所有根都会搬家。"""
        with patch.dict(os.environ, {"ECHO_BASE": ""}, clear=False):
            os.environ.pop("ECHO_BASE", None)
            with patch.object(paths, "echo_root", lambda: r"D:\ECHO"):
                self.assertEqual(paths.echo_base(), "")

    def test_a_tree_named_echo_core_is_self_describing(self):
        """布局自描述：代码目录叫 `echo-core`，父目录就是安装根（不用配任何东西）。"""
        with patch.dict(os.environ, {"ECHO_BASE": ""}, clear=False):
            os.environ.pop("ECHO_BASE", None)
            with patch.object(paths, "echo_root", lambda: r"D:\ECHO\echo-core"):
                self.assertEqual(paths.echo_base(), r"D:\ECHO")

    def test_the_env_var_wins(self):
        with patch.dict(os.environ, {"ECHO_BASE": r"E:\其他根"}, clear=False):
            with patch.object(paths, "echo_root", lambda: r"D:\ECHO\echo-core"):
                self.assertEqual(paths.echo_base(), r"E:\其他根")

    def test_a_developers_tree_is_not_mistaken_for_a_base(self):
        """开发树叫 `echo-dev`，绝不能被当成新布局（否则数据会跑到 `C:\\` 那一层）。"""
        with patch.dict(os.environ, {"ECHO_BASE": ""}, clear=False):
            os.environ.pop("ECHO_BASE", None)
            with patch.object(paths, "echo_root", lambda: r"C:\echo-dev"):
                self.assertEqual(paths.echo_base(), "")


class NewLayoutRootsTests(_BaseCase):
    def test_the_six_siblings(self):
        base = self.tmp
        self.assertEqual(paths.data_root(), os.path.join(base, "data"))
        self.assertEqual(paths.models_root(), os.path.join(base, "models"))
        self.assertEqual(paths.meetings_root(), os.path.join(base, "meeting"))
        self.assertEqual(paths.dsh_root(), os.path.join(base, "dsh", "app"))
        self.assertEqual(paths.dsh_home_root(), os.path.join(base, "dsh", "home"))
        self.assertEqual(paths.command_root(), os.path.join(base, "aide"))
        self.assertEqual(paths.base_dir("core"), os.path.join(base, "echo-core"))

    def test_the_agent_fallback_cwd_is_the_command_space(self):
        """agent 的兜底 cwd 必须是**指令空间**，不许落在可整体覆盖的 `echo-core` 里。

        2026-09-25 定：`agents/base` 原来把兜底 cwd 写成 `paths.echo_root()` —— 新布局下
        那就是 `echo-core`（升级即覆盖的代码目录）：agent 能直接改删代码，DSH 还会把代码
        当工作区内容索引。现在是 `paths.command_root()`（新布局 `{echoBase}/aide`、
        老装机 `{ECHO}/data/command`），与建命令会话用的工作区是同一个位置。
        """
        from app.agents.base import echo_workspace
        self.assertEqual(echo_workspace(), os.path.join(self.tmp, "aide"))
        code = os.path.normcase(os.path.join(self.tmp, "echo-core"))
        self.assertFalse(os.path.normcase(echo_workspace()).startswith(code + os.sep),
                         "兜底 cwd 又跑回 echo-core 里了")

    def test_every_root_is_under_the_base(self):
        for fn in (paths.data_root, paths.models_root, paths.meetings_root,
                   paths.dsh_root, paths.dsh_home_root, paths.command_root):
            with self.subTest(fn=fn.__name__):
                self.assertTrue(os.path.normcase(fn()).startswith(os.path.normcase(self.tmp)),
                                "%s 跑到安装根外面去了" % fn.__name__)

    def test_nothing_lands_in_the_replaceable_code_dir(self):
        """**L6**：`echo-core/` 是"升级时整体覆盖"的目录，六类东西一个都不许进去。"""
        code = os.path.normcase(os.path.join(self.tmp, "echo-core"))
        for fn in (paths.data_root, paths.models_root, paths.meetings_root,
                   paths.dsh_root, paths.dsh_home_root, paths.command_root):
            with self.subTest(fn=fn.__name__):
                p = os.path.normcase(fn())
                self.assertFalse(p == code or p.startswith(code + os.sep),
                                 "%s 落在 echo-core 里了（升级会把它冲掉）" % fn.__name__)

    def test_hf_home_matches_models_root(self):
        """HF_HOME 的兜底值必须**就是** `models_root()`。

        这是清点时排第一的风险：`modelinfo` 那行原来写死 `BASE_DIR/models`，
        新布局下 `BASE_DIR` 是 `echo-core` —— 权重下到 A、`models_root()` 去 B 找，
        huggingface 静默重下几 GB，而且模型落进了可覆盖的代码目录。

        做法：把 `HF_HOME` 清掉后**重新执行** `modelinfo` 的模块级代码（`reload`），
        再看它设成什么。不能只 import 一次就断言 —— 那个值在进程第一次 import 时就定了，
        与本用例设的 `ECHO_BASE` 无关（第一版就是这么错的：单跑绿、合跑红）。
        """
        import importlib
        import app.modelinfo as modelinfo
        saved = os.environ.pop("HF_HOME", None)
        try:
            importlib.reload(modelinfo)
            self.assertEqual(
                os.path.normcase(os.environ.get("HF_HOME", "")),
                os.path.normcase(paths.models_root()),
                "HF_HOME 与 models_root() 不一致：权重会下到一个地方、去另一个地方找")
        finally:
            if saved is not None:
                os.environ["HF_HOME"] = saved

    def test_the_meeting_space_is_the_meeting_dir(self):
        """**会议数据目录 ≡ 会议工作区**（3.0 把两条设置合成一条不变量）。"""
        self.assertEqual(paths.meeting_space_root(), paths.meetings_root())

    def test_a_configured_meeting_dir_drags_the_workspace_along(self):
        """改了「会议文件目录」，工作区**跟着走** —— 这正是原来会静默错位的那一处。"""
        target = os.path.join(self.tmp, "别处的会议")
        with patch.object(paths, "_settings_get",
                          lambda name: target if name == "meetingsDir" else ""):
            self.assertEqual(paths.meetings_root(), target)
            self.assertEqual(paths.meeting_space_root(), target)

    def test_an_old_custom_meeting_workspace_is_still_respected(self):
        """老装机上"只改过 meetingWorkspace"的用户：不许把他的会话搬走。"""
        old = os.path.join(self.tmp, "老工作区")
        with patch.object(paths, "_settings_get",
                          lambda name: old if name == "meetingWorkspace" else ""):
            self.assertEqual(paths.meeting_space_root(), old)

    def test_the_factory_workspace_values_mean_auto(self):
        """出厂值（老写法的 `{ECHO}/data/command` / 新写法的 `{ECHO_BASE}/aide`）都算"没配过"。"""
        for factory in ("", "{ECHO}/data/command", "{ECHO_BASE}/aide"):
            with self.subTest(value=factory):
                with patch.object(paths, "_settings_get", lambda name, v=factory: v):
                    self.assertEqual(paths.command_root(), os.path.join(self.tmp, "aide"))

    def test_a_placeholder_expands_to_the_base(self):
        # 占位符替换**只做字符串替换**（不规整分隔符）——`{ECHO_BASE}/aide` 展开后
        # 仍是 `/`。断言时统一 normpath，免得把"写法"当成"语义"来测。
        got = paths.expand("{ECHO_BASE}/aide")
        self.assertEqual(os.path.normpath(got), os.path.normpath(os.path.join(self.tmp, "aide")))


class LegacyLayoutTests(unittest.TestCase):
    """没有安装根时，每个根都必须**逐字**等于 2.0 的路径（老装机不搬家）。"""

    def setUp(self):
        os.environ.pop("ECHO_BASE", None)
        self.addCleanup(lambda: os.environ.pop("ECHO_BASE", None))

    def test_roots_fall_back_to_the_old_places(self):
        with patch.object(paths, "echo_root", lambda: r"D:\ECHO"), \
                patch.object(paths, "_settings_get", lambda name, d="": ""):
            self.assertEqual(paths.echo_base(), "")
            self.assertEqual(paths.data_root(), r"D:\ECHO\data")
            self.assertEqual(paths.meetings_root(), os.path.join(r"D:\ECHO\data", "meetings"))
            self.assertEqual(paths.models_root(), r"D:\ECHO\models")
            self.assertEqual(paths.dsh_root(), os.path.join(r"D:\ECHO", "harness", "dsh"))
            self.assertEqual(paths.dsh_home_root(), os.path.join(r"D:\ECHO\data", "harness"))
            self.assertEqual(paths.command_root(),
                             os.path.join(r"D:\ECHO", "data", "command"))

    def test_the_platform_default_still_applies(self):
        """老布局 + 没设 ECHO_DATA → 走平台默认值（macOS 落在 Application Support）。"""
        with patch.object(paths, "_platform_defaults",
                          lambda: {"dataDir": r"C:\Users\x\AppData\Roaming\ECHO"}), \
                patch.object(paths, "_settings_get", lambda name, d="": ""):
            self.assertEqual(paths.data_root(), r"C:\Users\x\AppData\Roaming\ECHO")

    def test_echo_data_env_still_wins(self):
        with patch.dict(os.environ, {"ECHO_DATA": r"E:\数据"}, clear=False):
            self.assertEqual(paths.data_root(), r"E:\数据")


class DshLocalEntryTests(_BaseCase):
    """DSH 本地入口的两个可能落点（新布局优先、老布局兜底）。

    ⚠️ **两个落点都要隔离**：这台开发机上 `{仓库}/harness/dsh/...` 是真的存在的
    （开发时真装过标准版），不隔离的话"空文件不算装好"那条会去找到真文件而误判。
    """

    def setUp(self):
        super().setUp()
        code = tempfile.mkdtemp(prefix="echo-code-")
        self.addCleanup(__import__("shutil").rmtree, code, True)
        p = patch.object(paths, "echo_root", lambda: code)
        p.start()
        self.addCleanup(p.stop)

    def _touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("// bin")
        return path

    def test_the_new_layout_wins(self):
        from app import harness_proc as hp
        new = self._touch(os.path.join(paths.dsh_root(), "node_modules", "@deepseek-ai",
                                       "dsh", "lib", "bin.js"))
        self.assertEqual(hp.local_entry(), new)

    def test_an_old_install_is_still_found(self):
        """升级上来的机器：代码是新布局，但 DSH 还是 2.0 时代装在 `{ECHO}/harness/dsh`。

        不认老落点的话，用户得为了一次升级重下 223 MB。
        """
        from app import harness_proc as hp
        old = self._touch(os.path.join(paths.echo_root(), hp.LOCAL_ENTRY_REL))
        self.assertEqual(hp.local_entry(), old)

    def test_an_empty_file_is_not_an_install(self):
        from app import harness_proc as hp
        p = os.path.join(paths.dsh_root(), "node_modules", "@deepseek-ai",
                         "dsh", "lib", "bin.js")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        self.assertEqual(hp.local_entry(), "")


if __name__ == "__main__":
    unittest.main()
