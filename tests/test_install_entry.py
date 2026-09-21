# -*- coding: utf-8 -*-
"""安装入口的守卫：来源怎么被找到、运行时怎么降级、以及"装完了"不许是句空话。

背景（2026-09-21 同事实测反馈）
------------------------------
技能 ``echo-install`` 第 3 节①让 agent 跑 ``scripts\\install.ps1``，可那个脚本
**就在主包里面**（``ECHO\\scripts\\install.ps1``）—— 资料夹里没有散着的它，agent 于是
回报"没给 install.ps1"。顺着这条线又挖出三颗雷，全部实测复现过：

  1. 外层工具包 ``ECHO-kit-*.zip`` 名字也匹配 ``ECHO-*.zip`` 且**比主包新**，
     ``Find-DeliveryZip`` 按时间取最新会选中它（解出来没有 manifest.json）；
  2. 运行时准备里 ``& uv venv ...`` 直连：uv 把进度写到 **stderr**，而脚本顶部是
     ``$ErrorActionPreference='Stop'`` —— 那条 stderr 变成终止性错误，安装当场 FATAL，
     "降级到 python.org 嵌入包"那条路根本没机会跑；
  3. uv 建的 venv **不带 pip**，基础依赖一个都没装上，而安装器只 Warn 一句、最后照样
     打印"安装完成！" —— 同事拿到的是一个起不来的 ECHO。

为什么这里只做"源码扫描"而不用真跑一遍 PowerShell：单测套件要在 ubuntu/macos 的 CI 上
也跑（``static`` job），那些 runner 上没有 Windows PowerShell，真跑会把 CI 弄红。
所以本文件钉的是**契约的存在性**，端到端由 ``scripts/check-windows.ps1`` 与人工冒烟负责。
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


class ExtractedPackSourceTests(unittest.TestCase):
    """发给同事的资料夹是"已解开的主包 + 技能"，安装器要认得这种来源。"""

    def test_install_ps1_has_a_tree_source_path(self):
        text = _read(INSTALL_PS1)
        self.assertIn("$script:TreeSource", text)
        self.assertIn("按【已解开的包目录】安装", text,
                      "场景 C（已解开的包目录）不见了 —— 同事的资料夹就是这种形态")

    def test_tree_mode_copies_nothing_when_already_in_place(self):
        text = _read(INSTALL_PS1)
        self.assertIn("来源就是安装目录，无需复制", text)


class RuntimeFallbackTests(unittest.TestCase):
    """运行时准备这条路必须"能降级、且不假装成功"。"""

    def setUp(self):
        self.text = _read(INSTALL_PS1)

    def test_uv_and_py_go_through_invoke_native(self):
        # uv/py 把进度写到 stderr，脚本顶部又是 EAP=Stop：直接 & 调用会变成终止性错误，
        # 于是降级链断在第一级（2026-09-21 实测 FATAL: Using CPython 3.11.15）。
        self.assertNotIn("& $uv.Source venv", self.text)
        self.assertNotIn("& py -3.11 -m venv", self.text)
        self.assertIn("Invoke-Native $uv.Source @('venv'", self.text)

    def test_repairs_a_pip_less_runtime(self):
        # uv venv 默认不带 pip：必须能补上（ensurepip），而不是让后面 pip install 直接失败
        self.assertIn("function Assert-Pip", self.text)
        self.assertIn("ensurepip", self.text)

    def test_missing_core_deps_are_fatal_not_a_warning(self):
        # 旧行为：只 Warn 一句就继续，最后还打印"安装完成"
        self.assertIn("基础依赖安装失败", self.text)
        self.assertNotIn("基础依赖安装返回非零", self.text)

    def test_self_check_actually_imports_the_core_deps(self):
        # 有 python.exe ≠ 能用；以 import 为准
        self.assertIn("import fastapi, uvicorn", self.text)


class SkillPointsAtTheRealEntryTests(unittest.TestCase):
    """技能文档必须把入口指到**资料夹里的真实路径**，而不是凭空假设的路径。"""

    def setUp(self):
        self.text = _read(SKILL_MD)

    def test_says_where_install_ps1_actually_is(self):
        self.assertIn(r"ECHO\scripts\install.ps1", self.text,
                      "技能文档没告诉 agent install.ps1 的实际位置")

    def test_says_the_program_is_already_unpacked(self):
        self.assertIn("已经解开", self.text,
                      "技能文档没说明资料夹里的 ECHO\\ 是已解开的主程序（不会再解压）")

    def test_keeps_the_zip_only_fallback(self):
        # 只拿到 zip 的人也要有活路：文档里保留解一层的退化路径
        self.assertIn("Expand-Archive", self.text)

    def test_does_not_use_the_bare_relative_path_again(self):
        self.assertNotIn(r"-File scripts\install.ps1", self.text,
                         "又出现了资料夹相对路径的 install.ps1（它不在那儿）")

    def test_does_not_use_psscriptroot_for_the_component_script(self):
        # 命令是在 agent 自己的终端里内联跑的，那时 $PSScriptRoot 是空的
        self.assertNotIn(r"$PSScriptRoot\scripts\echo-install-components.ps1", self.text)
        self.assertIn(r"$skill\scripts\echo-install-components.ps1", self.text)


if __name__ == "__main__":
    unittest.main()
