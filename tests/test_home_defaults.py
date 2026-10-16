# -*- coding: utf-8 -*-
"""把「专有名词与姓名纠错机制」内置进**默认安装 / 配置阶段**（2026-10-16 用户要求）。

先找事实，再动手（本文件钉的就是找到的那件事）
--------------------------------------------
用户当天在**稳定版 DSH 标准版**里说的话是：

    「记住京小智等近期的热点名词，常见的姓名，在指令、纪要、回顾等任务过程中都要进行纠错」
    「以后这类专有名词我通过指令告诉你就行吧」

那边把它落成了 **7 处**，其中"**每个会话自动加载**"的那一层是 `$DSH_HOME/AGENTS.md`
（家目录全局指令），另外还有**会议工作区**的 `{MEETINGS}/AGENTS.md` 与三个技能里的速查行；
真正的那张表（公司/平台名词 + 同事姓名）在用户**自己的笔记库**里。

于是"内置"被刻意限定成两件事：

  ① **机制**（先纠错再产出 / 权威表在哪 / 引用原话保留原样 / 绝不把两人合并 / 新误识回填）——
     随包分发，新机器装上就有 ⇒ 本文件的主要判据；
  ② **那张表** —— **不内置**（属于用户的笔记库；公开仓库不能带作者的个人与单位信息，
     与 `tests/test_skills_setup.py` 的隐私闸同源）⇒ :class:`PrivacyGateTests`。

一条硬红线：家目录的 `AGENTS.md` 是**用户自己的指令文件**，所以除了"不存在"与
"内容逐字等于我们自己写下的旧版本"，任何情况都**不许写**（:class:`NeverOverwritesTests`）。
"""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import paths                                          # noqa: E402
from app import skills_setup                                   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULTS = os.path.join(ROOT, ".dsh", "home-defaults")
HOME_SRC = os.path.join(DEFAULTS, "AGENTS.default.md")
MEETING_SRC = os.path.join(DEFAULTS, "meeting", "AGENTS.default.md")

#: 机制里**承重**的几句话（钉语义、不钉整篇文案 —— 以后改措辞不该把用例改成"改文档"）
LOAD_BEARING = ("术语与姓名纠错", "权威表", "正确写法", "保留原样",
                "绝不把不同的人合并", "新误识")
#: 家目录那份额外要有的一条（会议工作区那份用的是同一件事的另一种措辞）
HOME_ONLY = ("改名不改链接",)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


class _Env(unittest.TestCase):
    """把"家目录 / 会议工作区"换成临时目录 —— 用例绝不碰真实的 DSH 家目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-home-defaults-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.homes = [os.path.join(self.tmp, "home-a"), os.path.join(self.tmp, "home-b")]
        self.ws = os.path.join(self.tmp, "meeting")

    @contextlib.contextmanager
    def env(self, homes=None, legacy_layout=False):
        """把"家目录 / 会议工作区"换成临时目录 —— 用例绝不碰真实的 DSH 家目录与会议目录。

        **两个接缝都要打桩**：`target_homes`（家目录）与 `echo_base` + `meetings_root`
        （会议工作区）。少桩一个，跑一遍用例就会在开发机的真实 `data/meetings/` 里留文件
        —— `tests/test_skills_setup.py` 就是只桩了前者（见下 `LegacyLayoutTests` 的说明）。
        """
        base = "" if legacy_layout else self.tmp
        with patch.object(skills_setup, "target_homes", lambda: list(homes or self.homes)), \
             patch("app.paths.echo_base", lambda: base), \
             patch("app.paths.meetings_root", lambda: self.ws):
            yield

    def home_file(self, home):
        return os.path.join(home, "AGENTS.md")

    def ws_file(self):
        return os.path.join(self.ws, "AGENTS.md")

    def _snapshot(self, folder):
        """目录里的**文件名 → 字节 + mtime**（幂等判据：一个字节、一纳秒都不许动）。"""
        out = {}
        for name in sorted(os.listdir(folder)):
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                out[name] = (_read_bytes(p), os.stat(p).st_mtime_ns)
        return out


class ShippedDefaultTests(_Env):
    """① 默认配置里**确实有**这个机制（而且落在 agent 真会读的那个文件名上）。"""

    def test_the_source_templates_exist_and_carry_the_mechanism(self):
        for path, what in ((HOME_SRC, "家目录"), (MEETING_SRC, "会议工作区")):
            with self.subTest(path=os.path.relpath(path, ROOT)):
                self.assertTrue(os.path.isfile(path), "随包模板不在：%s" % path)
                text = _read(path)
                self.assertIn(skills_setup.BUILTIN_MARK, text,
                              "%s 模板第一行要有内置标记（面板/报告靠它区分'你改过'）" % what)
                for phrase in LOAD_BEARING:
                    self.assertIn(phrase, text, "%s 模板少了承重句：%r" % (what, phrase))
        home_text = _read(HOME_SRC)
        for phrase in HOME_ONLY:
            self.assertIn(phrase, home_text, "家目录模板少了承重句：%r" % phrase)

    def test_install_lands_it_where_the_agent_actually_reads_it(self):
        """落点是 `$DSH_HOME/AGENTS.md`（每个会话自动加载）与 `{MEETINGS}/AGENTS.md`。"""
        with self.env():
            got = skills_setup.install()
        self.assertTrue(got["ok"], got)
        files = got["files"]
        self.assertEqual(sorted(files["installed"]),
                         sorted(["agents-md@%s" % h for h in self.homes]
                                + ["meeting-agents-md@%s" % self.ws]), files)
        self.assertEqual([], files["absent"], "包没打全")
        self.assertEqual([], files["errors"], files)
        for home in self.homes:
            with self.subTest(home=home):
                self.assertEqual(_read(HOME_SRC), _read(self.home_file(home)),
                                 "写进家目录的内容必须与随包模板一字不差")
        self.assertEqual(_read(MEETING_SRC), _read(self.ws_file()))

    def test_status_says_what_is_there_and_is_read_only(self):
        with self.env():
            before = self._snapshot(self.tmp)
            missing = skills_setup.status_files()
            self.assertEqual(before, self._snapshot(self.tmp), "status 不许写盘")
            self.assertEqual(["agents-md", "meeting-agents-md"], missing["missing"],
                             "一个都没装时两个都是缺")
            self.assertEqual(2, len(missing["packed"]), "随包模板应当都能读到")

            skills_setup.install_files()
            st = skills_setup.status_files()
            self.assertEqual([], st["missing"])
            self.assertEqual({"ok"}, {r["state"] for r in st["entries"]})

    def test_status_distinguishes_user_copies_from_ours(self):
        """只读状态要能说清"这份是谁的"：我们那份（ok）/ 你改过的（edited）/ 你自己的（user）。"""
        with self.env():
            skills_setup.install_files()
            with open(self.home_file(self.homes[0]), "a", encoding="utf-8") as fh:
                fh.write("\n- 我加的一条\n")
            with open(self.home_file(self.homes[1]), "w", encoding="utf-8") as fh:
                fh.write("# 我自己写的 AGENTS.md\n")
            states = {(r["name"], r["home"]): r["state"]
                      for r in skills_setup.status_files()["entries"]}
            self.assertEqual("edited", states[("agents-md", self.homes[0])])
            self.assertEqual("user", states[("agents-md", self.homes[1])])
            self.assertEqual("ok", states[("meeting-agents-md", self.ws)])


class IdempotenceTests(_Env):
    """② 幂等：重复安装不重写、不产生第二份、不动 mtime。"""

    def test_second_install_changes_nothing(self):
        with self.env():
            first = skills_setup.install_files()
            self.assertEqual(3, len(first["installed"]))
            before = {e["target"]: (_read_bytes(e["target"]),
                                    os.stat(e["target"]).st_mtime_ns) for e in first["entries"]}
            second = skills_setup.install_files()
        self.assertTrue(second["ok"], second)
        self.assertEqual([], second["installed"], "第二次不该再'新装'")
        self.assertEqual([], second["updated"], "第二次不该再'升级'")
        self.assertEqual(3, len(second["unchanged"]), "第二次应当全是'已是当前默认'")
        for e in second["entries"]:
            with self.subTest(target=e["target"]):
                self.assertEqual(before[e["target"]],
                                 (_read_bytes(e["target"]), os.stat(e["target"]).st_mtime_ns),
                                 "幂等：一个字节、一个 mtime 都不该动")

    def test_it_never_leaves_a_second_copy_or_a_half_written_file(self):
        with self.env():
            skills_setup.install_files()
            skills_setup.install_files()
            names = sorted(os.listdir(self.homes[0]))
            self.assertEqual(["AGENTS.md"], names,
                             "家目录里只该有一个 AGENTS.md（没有 .1 / .bak / .echo-tmp）")
            self.assertEqual(["AGENTS.md"], sorted(os.listdir(self.ws)))


class NeverOverwritesTests(_Env):
    """③ **不覆盖用户已有 / 自己改过的那份** —— 这条是硬红线。"""

    def test_a_file_the_user_wrote_is_left_alone(self):
        mine = "# 我自己的指令文件\n\n- 别动我\n"
        os.makedirs(self.homes[0])
        with open(self.home_file(self.homes[0]), "w", encoding="utf-8") as fh:
            fh.write(mine)
        with self.env():
            got = skills_setup.install_files()
        self.assertIn("agents-md@%s" % self.homes[0], got["kept"], "用户自己那份必须报成 kept")
        self.assertEqual(mine, _read(self.home_file(self.homes[0])), "用户文件被覆盖了")
        self.assertIn("agents-md@%s" % self.homes[1], got["installed"], "别的家目录该装的照装")

    def test_our_default_is_kept_once_the_user_edits_it(self):
        """**最容易做错的一种**：文件是我们写的（标记还在），但用户加了话 —— 也不许动。"""
        with self.env():
            skills_setup.install_files()
            mine = self.home_file(self.homes[0])
            with open(mine, "a", encoding="utf-8") as fh:
                fh.write("\n- 我们公司口径：XX 才对\n")
            edited = _read(mine)
            got = skills_setup.install_files()
        self.assertIn("agents-md@%s" % self.homes[0], got["kept"])
        self.assertIn("agents-md@%s" % self.homes[1], got["unchanged"], "没改的那份仍是未变")
        self.assertEqual(edited, _read(mine), "用户在我们那份上加的话被抹掉了")

    def test_only_our_own_previous_version_is_upgraded(self):
        """升级**只**认"逐字等于我们写下的旧版本"；差一个字符就当用户改过。"""
        with self.env(homes=[self.homes[0]]):
            for old, expect in (("OLD v0\n", "updated"),
                                ("OLD v0\n- 用户加的一行\n", "kept")):
                with self.subTest(old=old):
                    os.makedirs(self.homes[0], exist_ok=True)
                    with open(self.home_file(self.homes[0]), "w", encoding="utf-8") as fh:
                        fh.write(old)
                    with patch.dict(skills_setup.PREVIOUS_DEFAULTS,
                                    {"agents-md": ("OLD v0\n",)}, clear=False):
                        got = skills_setup.install_files()
                    entry = [e for e in got["entries"] if e["name"] == "agents-md"][0]
                    self.assertEqual(expect, entry["action"], entry)
                    if expect == "updated":
                        self.assertEqual(_read(HOME_SRC), _read(self.home_file(self.homes[0])))

    def test_a_crlf_copy_still_counts_as_ours(self):
        """只差换行（Windows 上很容易被别的工具改成 CRLF）**不算**用户改过 —— 否则永远升不了。"""
        with self.env(homes=[self.homes[0]]):
            os.makedirs(self.homes[0])
            with open(self.home_file(self.homes[0]), "w", encoding="utf-8", newline="") as fh:
                fh.write(_read(HOME_SRC).replace("\n", "\r\n"))
            got = skills_setup.install_files()
        entry = [e for e in got["entries"] if e["name"] == "agents-md"][0]
        self.assertEqual("unchanged", entry["action"], entry)

    def test_one_unwritable_target_does_not_stop_the_others(self):
        """目标读不了（这里拿**目录**当那个文件）时：如实报 error，别的家目录照装。"""
        os.makedirs(self.homes[0])
        os.makedirs(self.home_file(self.homes[0]))       # 同名**目录** → 读它必抛 OSError
        with self.env():
            got = skills_setup.install_files()
        self.assertFalse(got["ok"], "有一项没装成就不能说 ok")
        self.assertTrue(any(self.homes[0] in e for e in got["errors"]), got)
        err = [e for e in got["entries"] if e["action"] == "error"]
        self.assertEqual(1, len(err), got)
        self.assertIn("agents-md@%s" % self.homes[1], got["installed"], "别的家目录必须照装")


class LegacyLayoutTests(_Env):
    """老布局（扁平树，`echo_base()` 为空）：会议目录是**纯数据目录**，不往里塞指令文件。

    这条闸是**实测**逼出来的：既有用例 `tests/test_skills_setup.py` 只隔离 `target_homes`，
    没有隔离会议目录 —— 没有这道闸时，单单跑那些既有用例就会在开发机真实的
    `data/meetings/` 里写下一个 `AGENTS.md`（"测试碰真实状态"，仓库反复记过的事故）。
    """

    def test_flat_layout_does_not_touch_the_meetings_dir(self):
        with self.env(legacy_layout=True):
            got = skills_setup.install_files()
        self.assertEqual([], [e for e in got["entries"] if e["name"] == "meeting-agents-md"],
                         "老布局不该动会议目录")
        self.assertEqual(2, len(got["installed"]), "家目录那两份照装")
        self.assertFalse(os.path.exists(self.ws), "会议目录连建都不该建")

    def test_three_zero_layout_does_get_the_workspace_file(self):
        with self.env():
            got = skills_setup.install_files()
        self.assertEqual(1, len([e for e in got["entries"] if e["name"] == "meeting-agents-md"]))
        self.assertTrue(os.path.isfile(self.ws_file()))


class PackagingTests(unittest.TestCase):
    """④ 打包 / 安装链里**确实带上**它，而且一步都不少。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "scripts", "build-package.ps1"),
                  encoding="utf-8-sig") as fh:
            cls.pack = fh.read()
        with open(os.path.join(ROOT, "scripts", "install-all.ps1"),
                  encoding="utf-8-sig") as fh:
            cls.ps = fh.read()

    def test_the_main_package_whitelist_covers_the_defaults(self):
        """模板住在 `.dsh/home-defaults/`，而主包白名单里有 `.dsh` —— 不额外登记也不会漏。"""
        self.assertIn("'.dsh'", self.pack, "主包白名单不再带 .dsh 了")
        self.assertTrue(os.path.normcase(DEFAULTS).startswith(
            os.path.normcase(os.path.join(ROOT, ".dsh"))),
            "模板必须住在主包白名单覆盖的 .dsh/ 里")
        # 白名单是"目录级"的，所以真正要守的是：文件确实在树里（少了就是包打不全）
        for p in (HOME_SRC, MEETING_SRC):
            self.assertTrue(os.path.isfile(p), "模板不在树里：%s" % p)

    def test_the_installer_goes_through_the_existing_entry_point(self):
        """安装器走既有入口 `/api/skills/setup`（不做第二套搬运工），并把结果**如实报出来**。"""
        self.assertIn("function Invoke-SkillsSetup", self.ps)
        self.assertIn("/api/skills/setup", self.ps)
        self.assertIn("$r.files", self.ps, "说明文件那一段没印出来 = 往用户家目录写东西却不吭声")
        self.assertIn("$r.files.kept", self.ps, "'保留了你那份' 必须报出来")
        self.assertLess(self.ps.index("Start-EchoOwnService"),
                        self.ps.index("Invoke-SkillsSetup"),
                        "这一步要在服务起来之后（端口那时才有）")

    def test_the_installer_script_keeps_its_utf8_bom(self):
        """含中文的 .ps1 掉 BOM = WinPS 5.1 按 ANSI 解析（见 AGENTS.md 那次事故）。"""
        with open(os.path.join(ROOT, "scripts", "install-all.ps1"), "rb") as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "install-all.ps1 的 UTF-8 BOM 掉了")

    def test_defaults_root_is_derived_from_the_path_seam(self):
        """路径只由 `app/paths.py` 推导（D29），业务代码不许自己拼 —— 也不新增 `.dsh` 字面量。"""
        self.assertEqual(os.path.normcase(DEFAULTS),
                         os.path.normcase(paths.shipped_home_defaults_root()))
        self.assertEqual(paths.shipped_home_defaults_root(), skills_setup.defaults_root())

    def test_the_templates_are_not_named_AGENTS_md(self):
        """**踩过的坑**：源文件一叫 `AGENTS.md`，DSH 就把"模板"当成本仓库的活指令加载（实测过）。"""
        for p in (HOME_SRC, MEETING_SRC):
            self.assertNotEqual("AGENTS.md", os.path.basename(p))
            self.assertTrue(os.path.basename(p).endswith(".default.md"))


class PrivacyGateTests(unittest.TestCase):
    """随包默认里**不许**出现作者的个人 / 单位信息（与 `tests/test_skills_setup.py` 的隐私闸同源）。

    机制内置了，**那张表没有** —— 表属于用户的笔记库。这一条防的是"顺手把表也抄进包"。
    """

    FORBIDDEN = ("周克勤", "京小智", "金小智", "联通小智", "通cloud", "通通代理", "TongClaw",
                 "多模态能力共享中心", "金慧云", "徐锐", "沈松", "季涛", "黄继涛", "田英杰",
                 "郭涵川", "王晋刚", "彭帆", "笔记-2026", "zhkq", "OneDrive",
                 "C:\\Users", "C:/Users", "经理例会", "创新工作室")

    def test_the_shipped_default_carries_no_personal_markers(self):
        for path in (HOME_SRC, MEETING_SRC):
            text = _read(path)
            for bad in self.FORBIDDEN:
                with self.subTest(file=os.path.basename(path), marker=bad):
                    self.assertNotIn(bad, text,
                                     "随包默认里出现了 %r —— 公开仓库不能带这些（表在用户笔记库里）"
                                     % bad)


if __name__ == "__main__":
    unittest.main()
