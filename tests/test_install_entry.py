# -*- coding: utf-8 -*-
"""安装入口的守卫：交付包怎么被找到，以及技能文档别再把入口指错。

背景（2026-09-21 同事实测反馈）
------------------------------
技能 ``echo-install`` 第 3 节①让 agent 跑 ``scripts\\install.ps1``，可那个脚本
**就在主包 zip 里面**（``ECHO\\scripts\\install.ps1``）—— 资料目录里没有散着的它，
agent 于是回报"没给 install.ps1"。

同一流程里还有第二颗雷：外层工具包 ``ECHO-kit-*.zip`` 的名字也匹配 ``ECHO-*.zip``，
而且**往往比主包更新**（先打主包、后压工具包），``Find-DeliveryZip`` 按修改时间取最新
就会选中它 —— 解出来没有 ``manifest.json``。

为什么这里只做"源码扫描"而不用真跑一遍 PowerShell：单测套件要在 ubuntu/macos 的 CI 上
也跑（``static`` job），那些 runner 上没有 Windows PowerShell，真跑会把 CI 弄红。
所以本文件钉的是**契约的存在性**（排除列表里有工具包模式、文档给的是包内路径），
真正的端到端由 ``scripts/check-windows.ps1`` 与人工冒烟负责。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_PS1 = os.path.join(ROOT, "scripts", "install.ps1")
INSTALL_BAT = os.path.join(ROOT, "scripts", "install.bat")
SKILL_MD = os.path.join(ROOT, ".dsh", "skills", "echo-install", "SKILL.md")


def _read(path, encoding="utf-8"):
    with open(path, encoding=encoding, newline="") as fh:
        return fh.read()


class DeliveryZipDiscoveryTests(unittest.TestCase):
    """交付包发现逻辑必须把**工具包**排除掉。"""

    def test_install_ps1_excludes_the_outer_kit_zip(self):
        text = _read(INSTALL_PS1)
        marker = "foreach ($p in @("
        start = text.index(marker)
        patterns = text[start:text.index(")", start)]
        for needed in ("*-offline-*", "*-component-*", "*-kit-*"):
            self.assertIn(needed, patterns,
                          f"Find-DeliveryZip 的排除列表少了 {needed}：{patterns!r}")

    def test_install_bat_excludes_the_outer_kit_zip(self):
        # install.bat 是 GBK（wscript/cmd 按系统 ANSI 读），不能当 UTF-8 读
        text = _read(INSTALL_BAT, encoding="gbk")
        line = [ln for ln in text.splitlines() if "findstr" in ln and "-offline-" in ln]
        self.assertEqual(len(line), 1, "找不到 install.bat 里那行 findstr 选择逻辑")
        self.assertIn('"-kit-"', line[0], f"install.bat 也要排除工具包：{line[0]!r}")


class SkillPointsAtTheRealEntryTests(unittest.TestCase):
    """技能文档必须把入口指到**包内**，而不是资料目录里的相对路径。"""

    def setUp(self):
        self.text = _read(SKILL_MD)

    def test_says_install_ps1_lives_inside_the_main_zip(self):
        self.assertIn(r"ECHO\scripts\install.ps1", self.text,
                      "技能文档没告诉 agent install.ps1 在解开的包里的哪个位置")

    def test_tells_the_agent_to_unpack_first(self):
        self.assertIn("Expand-Archive", self.text,
                      "技能第 3 节①少了「先解开主包」这一步 —— 同事就卡在这里")

    def test_does_not_use_the_bare_relative_path_again(self):
        self.assertNotIn(r"-File scripts\install.ps1", self.text,
                         "又出现了资料目录相对路径的 install.ps1（它不在那儿）")

    def test_does_not_use_psscriptroot_for_the_component_script(self):
        # 命令是在 agent 自己的终端里内联跑的，那时 $PSScriptRoot 是空的
        self.assertNotIn(r"$PSScriptRoot\scripts\echo-install-components.ps1", self.text)
        self.assertIn(r"$skill\scripts\echo-install-components.ps1", self.text)


if __name__ == "__main__":
    unittest.main()
