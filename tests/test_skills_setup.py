# -*- coding: utf-8 -*-
"""随包技能的**安装**（2026-10-11 用户指出的坑）。

现场：随包的技能跟着**代码树**走（`<代码目录>/.dsh/skills/`），而 ECHO 的 agent 是从
**`DSH_HOME/skills/`** 读技能的（标准版 harness 是 `{echoBase}/dsh/home`，老布局
`{DATA}/harness`；桌面版是 `~/.dsh`）—— **两个不同的目录**。于是新装的机器上，连随包的
`meeting-record` 其实都**没生效**（"新用户安装后没有 skill"的根因）。

两条铁律各有一条用例：**只补缺、绝不覆盖**；**只装随包清单**。
外加一条**隐私闸**：随包的技能里不许出现作者的个人/单位信息（这是公开仓库）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import skills_setup                                    # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SourceTests(unittest.TestCase):
    def test_source_is_derived_not_hardcoded(self):
        """源目录从本文件位置推（`<code>/.dsh/skills`）—— 不写任何绝对路径。"""
        root = skills_setup.code_skills_root()
        self.assertEqual(os.path.normcase(os.path.join(_ROOT, ".dsh", "skills")),
                         os.path.normcase(root))
        for name in skills_setup.SHIPPED:
            with self.subTest(name=name):
                self.assertTrue(os.path.isdir(os.path.join(root, name)),
                                "随包清单里的 %s 在仓库里不存在（包会打不全）" % name)


class PrivacyGateTests(unittest.TestCase):
    """随包技能里**不许**出现作者的个人/单位信息（公开仓库，git 历史删不掉）。"""

    #: 命中任意一个就说明"把作者的东西发出去了"
    FORBIDDEN = ("笔记-2026", "zhkq", "OneDrive", "C:\\Users", "C:/Users",
                 "经理例会", "创新工作室", "生产经营分析会", "专题汇报会", "项目评审会",
                 "绩效", "RPA", "机房", "巡检", "王晋刚")

    def test_shipped_skills_carry_no_personal_or_org_markers(self):
        root = skills_setup.code_skills_root()
        for name in skills_setup.SHIPPED:
            src = os.path.join(root, name)
            for dp, dn, fn in os.walk(src):
                for f in fn:
                    p = os.path.join(dp, f)
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    for bad in self.FORBIDDEN:
                        with self.subTest(skill=name, file=f, marker=bad):
                            self.assertNotIn(
                                bad, text,
                                "随包技能 %s/%s 里出现了 %r —— 公开仓库不能带这些"
                                % (name, os.path.relpath(p, src), bad))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-skills-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.homes = [os.path.join(self.tmp, "home-a"), os.path.join(self.tmp, "home-b")]

    def _install(self, homes=None):
        with patch.object(skills_setup, "target_homes", lambda: list(homes or self.homes)):
            return skills_setup.install()

    def test_it_installs_every_shipped_skill_into_every_home(self):
        got = self._install()
        self.assertTrue(got["ok"], got)
        self.assertEqual([], got["errors"])
        self.assertEqual([], got["absent"])
        for home in self.homes:
            for name in skills_setup.SHIPPED:
                with self.subTest(home=home, name=name):
                    self.assertTrue(os.path.isfile(os.path.join(home, "skills", name, "SKILL.md")))

    def test_it_is_idempotent_and_never_overwrites(self):
        """**铁律**：目标已存在就跳过 —— 用户自己调过的同名技能一个字都不许动。"""
        mine = os.path.join(self.homes[0], "skills", "daily-review")
        os.makedirs(mine)
        with open(os.path.join(mine, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("MINE — 我自己改过的，别覆盖\n")

        first = self._install()
        self.assertIn("daily-review@%s" % self.homes[0], first["skipped"],
                      "已存在的必须报成 skipped")
        self.assertIn("daily-review@%s" % self.homes[1], first["installed"],
                      "别的家目录该装的照装")
        with open(os.path.join(mine, "SKILL.md"), encoding="utf-8") as fh:
            self.assertEqual("MINE — 我自己改过的，别覆盖\n", fh.read(),
                             "用户的技能被覆盖了 —— 这条是硬红线")

        second = self._install()
        self.assertEqual([], second["installed"], "第二次应当全都跳过（幂等）")
        self.assertEqual(len(skills_setup.SHIPPED) * 2, len(second["skipped"]))

    def test_a_missing_shipped_skill_is_reported_not_swallowed(self):
        """清单里有、代码树里没有 = **包没打全** —— 要吵出来，不能静默少装一个。"""
        with patch.object(skills_setup, "SHIPPED", tuple(skills_setup.SHIPPED) + ("no-such-skill",)):
            got = self._install()
        self.assertIn("no-such-skill", got["absent"])
        self.assertFalse(got["ok"], "少装一个就不该报 ok")

    def test_status_is_read_only_and_says_what_is_missing(self):
        before = os.listdir(self.tmp)
        with patch.object(skills_setup, "target_homes", lambda: list(self.homes)):
            st = skills_setup.status()
        self.assertEqual(before, os.listdir(self.tmp), "status() 不该写盘")
        self.assertEqual(list(skills_setup.SHIPPED), st["shipped"])
        self.assertEqual(sorted(skills_setup.SHIPPED), st["missing"], "一个都没装时全是缺")
        self.assertEqual(2, len(st["homes"]))

    def test_a_write_failure_does_not_stop_the_others(self):
        """一个家目录写不进去，别的照装 —— 别一错全停。"""
        bad = os.path.join(self.tmp, "bad-home")
        with open(bad, "w", encoding="utf-8") as fh:      # 拿**文件**当家目录 → makedirs 必失败
            fh.write("x")
        got = self._install(homes=[bad, self.homes[1]])
        self.assertTrue(any("bad-home" in e for e in got["errors"]), got)
        self.assertTrue(os.path.isfile(os.path.join(self.homes[1], "skills", "meeting-record", "SKILL.md")),
                        "另一个家目录必须照装")


class InstallerWiringTests(unittest.TestCase):
    """安装器必须①写下安装模式、②把随包技能装进去（2026-10-11）。

    脚本逻辑是 PowerShell、跑起来要真装一遍，所以按仓库惯例断言**脚本契约**
    （`tests/test_install_entry.py` 是同一套做法）。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_ROOT, "scripts", "install-all.ps1"), encoding="utf-8-sig") as fh:
            cls.ps = fh.read()

    def test_it_probes_and_records_the_install_mode(self):
        self.assertIn("function Invoke-InstallModeProbe", self.ps)
        self.assertIn("install-mode.json", self.ps, "要写 <数据根>\\install-mode.json")
        self.assertIn("$null = Invoke-InstallModeProbe", self.ps,
                      "**装之前**就要探测（事后分不出全新还是升级）")
        # 两种模式都要判，且升级必须由"已有内容"推出来
        self.assertIn("'upgrade'", self.ps)
        self.assertIn("'fresh'", self.ps)

    def test_the_mode_file_is_written_without_a_bom(self):
        """PS 5.1 的 `Set-Content -Encoding UTF8` 会写 BOM，而 ECHO 读的是严格 utf-8。

        这里钉住"用 .NET 写、显式 UTF8Encoding($false)"这条 —— 少一个 `$false` 就多一个 BOM。
        """
        self.assertIn("UTF8Encoding($false)", self.ps)
        self.assertNotIn("Set-Content -Path $modeFile", self.ps)

    def test_it_installs_the_shipped_skills_once_the_service_is_up(self):
        self.assertIn("function Invoke-SkillsSetup", self.ps)
        self.assertIn("/api/skills/setup", self.ps)
        # 必须在服务起来**之后**（端口是那时才有的），所以顺序上要晚于 Start-EchoOwnService
        self.assertLess(self.ps.index("Start-EchoOwnService"),
                        self.ps.index("Invoke-SkillsSetup"),
                        "技能那一步要在服务起来之后")
        # 用户自己的同名技能不许被覆盖 —— 这里只是把"保留"如实报出来
        self.assertIn("skipped", self.ps)


if __name__ == "__main__":
    unittest.main()
