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
import fnmatch
import os
import pathlib
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_PS1 = os.path.join(ROOT, "scripts", "install.ps1")
INSTALL_BAT = os.path.join(ROOT, "scripts", "install.bat")
INSTALL_ALL = os.path.join(ROOT, "scripts", "install-all.ps1")
SKILL_MD = os.path.join(ROOT, ".dsh", "skills", "echo-install", "SKILL.md")
SH = os.path.join(ROOT, ".dsh", "skills", "echo-install", "scripts",
                  "echo-install-components.sh")
PS1_COMPONENTS = os.path.join(ROOT, ".dsh", "skills", "echo-install", "scripts",
                              "echo-install-components.ps1")
COMPONENTS_PY = os.path.join(ROOT, "app", "components.py")


def _read(path, encoding="utf-8"):
    with open(path, encoding=encoding, newline="") as fh:
        return fh.read()


def _strip_bash_comments(text):
    """去掉整行注释 —— 否则「注释里提到 declare -A」会被当成真用了它。"""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def _mac_shell_scripts():
    mac_dir = os.path.join(ROOT, "mac")
    if not os.path.isdir(mac_dir):
        return []
    return [os.path.join(mac_dir, n) for n in sorted(os.listdir(mac_dir))
            if n.endswith(".sh")]


_PS1_MAP_LINE = re.compile(
    r"^\s*'([^']+)'\s*=\s*@\{\s*pip\s*=\s*@\(([^)]*)\)\s*;\s*"
    r"model\s*=\s*'([^']+)'\s*;\s*stt\s*=\s*'([^']+)'\s*"
    r"(?:;\s*module\s*=\s*'([^']+)')?"
    # module 之后允许零或多个附加键（如 sensevoice 的 `; skeleton = $true`）：
    # 表是会长字段的，解析器不该因为多了个字段就静默漏掉整条引擎。
    r"(?:;\s*[A-Za-z_]\w*\s*=\s*[^;}]+)*\s*\}", re.M)


def _parse_ps1_engine_map():
    """把 Windows 组件安装器里的 $ENGINE_MAP 解析成 {引擎: {pip, model, stt, module}}。"""
    text = _read(PS1_COMPONENTS)
    out = {}
    for engine, pip_part, model, stt, module in _PS1_MAP_LINE.findall(text):
        out[engine] = {"pip": re.findall(r"'([^']+)'", pip_part),
                       "model": model, "stt": stt, "module": module or ""}
    if not out:
        raise AssertionError("$ENGINE_MAP 一条都没解析出来 —— 格式变了？")
    return out


def _parse_bash_case_map(path, fn_name):
    """把 bash 里 `fn() { case "$1" in  pattern) echo "值" ;; ... esac }` 解析成 {模式: 值}。"""
    text = _read(path)
    body = re.search(r"^%s\(\) \{(.*?)^\}" % re.escape(fn_name), text, re.S | re.M)
    if not body:
        raise AssertionError(f"找不到 bash 函数 {fn_name}()")
    out = {}
    for patterns, value in re.findall(r'^\s*([A-Za-z0-9_|*.-]+)\)\s*echo "([^"]*)"\s*;;',
                                      body.group(1), re.M):
        for p in patterns.split("|"):
            if p and p != "*":
                out[p] = value
    if not out:
        raise AssertionError(f"{fn_name}() 里一条 case 分支都没解析出来")
    return out


def _lookup_case_arm(mapping, engine):
    """在 bash case 表里查引擎：先精确匹配，再按 glob（mac 那边 whisper 档写成 `whisper-*`）。"""
    if engine in mapping:
        return mapping[engine]
    for pattern, value in mapping.items():
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(engine, pattern):
            return value
    return ""


def _component_model_ids():
    """app/components.py 里所有 model_id="..." —— 模型 id 的权威清单。"""
    return set(re.findall(r'model_id="([^"]+)"', _read(COMPONENTS_PY)))


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


class LowDiskGuardTests(unittest.TestCase):
    """磁盘建议值不能把非交互安装挡死（2026-10-28 同事升级事故）。

    现场：D: 只剩 8.7GB（建议 >=12GB），install-all 传 `-Silent` 调 install.ps1；
    `Ask-YesNo '继续使用此盘？' $false` 在 Silent 下取默认 N → 直接 exit 1。
    升级只覆盖代码（-SkipRuntime），8.7GB 完全够用；12GB 只是全量安装的建议值。
    """

    def test_silent_install_warns_but_keeps_going_on_low_disk(self):
        text = _read(INSTALL_PS1)
        block = re.search(r"\$need = 12GB(.*?)Info \(\"安装目录", text, re.S)
        self.assertIsNotNone(block, "install.ps1 的磁盘检查块不见了")
        body = block.group(1)
        self.assertIn("$script:Silent -or $script:DryRun", body,
                      "Silent/DryRun 必须走“只警告继续”的分支，不能再按默认 N 退出")
        self.assertIn("非交互安装", body)
        # 交互模式仍然要问（默认 N）；这条防止把安全提示也一起删掉
        self.assertIn("elseif (-not (Ask-YesNo '继续使用此盘？' $false))", body)


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
        """`echo-core\\` 是 2026-09-30 起 kit 里的代码目录名；老包叫 `ECHO\\`，两个都要提。

        为什么必须提老名字：同一个技能也随**离线最小包**（`build_min_kit.py`，包里仍是
        `ECHO\\`）发出去 —— 只写新名字，那份包里的 agent 就会说"找不到 install.ps1"。
        """
        self.assertIn(r"echo-core\scripts\install.ps1", self.text,
                      "技能文档没告诉 agent install.ps1 的实际位置")
        self.assertIn(r"ECHO\scripts", self.text,
                      "技能文档丢了老包（ECHO\\）的名字 —— 离线最小包还在用它")

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


class MacInstallBaseLayoutTests(unittest.TestCase):
    """mac 侧的脚本也要认安装根布局（同一条判据，三处一起守）。

    为什么值得单独钉：**"只在 Windows 上想到"这类漏改已经出现两次** ——
    `launch-desktop.ps1` 漏了（同事实测报 `venv missing`）、`startup.ps1`/`restart-echo.ps1`
    漏了两处（端口文件永远读不到）。mac 这边同样的三件东西（venv / data / 启动脚本）
    一旦落进 `echo-core`，后果是"升级整体覆盖代码"时把运行时和数据一起删掉（L6）。
    """

    def _text(self, name):
        with open(os.path.join(ROOT, "mac", name), encoding="utf-8") as fh:
            return fh.read()

    def test_each_runtime_script_computes_both_roots(self):
        for name in ("setup_mac.sh", "start_mac.sh", "stop_mac.sh"):
            text = self._text(name)
            with self.subTest(script=name):
                self.assertIn('basename "$CODE"', text,
                              "%s 没有'代码目录叫 echo-core 就上提一层'的判据" % name)
                self.assertIn('BASE="$CODE"', text, "%s 没有 BASE（安装根）" % name)

    def test_the_venv_is_created_in_the_install_base(self):
        self.assertIn('venv "$BASE/venv"', self._text("setup_mac.sh"),
                      "venv 建在 echo-core 里 → 升级覆盖代码会把运行时一起删掉")

    def test_the_running_scripts_use_the_base_venv_and_data(self):
        start = self._text("start_mac.sh")
        self.assertIn('"$BASE/venv/bin/python"', start)
        self.assertIn('"$BASE/data/logs"', start)
        self.assertNotIn("./venv/bin/python", start, "还在用代码目录里的 venv")
        stop = self._text("stop_mac.sh")
        self.assertIn('PID_FILE="$BASE/data/echo-mac.pid"', stop,
                      "pid 文件按代码目录找 → 停不掉真进程（或误判没在跑）")

    def test_the_installer_runs_the_path_probe_in_the_code_dir(self):
        """`from app import paths` 必须在**代码目录**里跑：安装根里没有 app 包。"""
        with open(SH, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('cd "$CODE" && "$PY" -c \'from app import paths', text,
                      "数据根探测跑在安装根里 → ImportError → 回落成错的目录")


class MacScriptPortabilityTests(unittest.TestCase):
    """mac 脚本必须能在**系统自带的 bash 3.2** 上跑（开发机是 5.x，测不出来）。"""

    def setUp(self):
        self.text = _read(SH)

    def test_shebang_uses_env_bash(self):
        self.assertTrue(self.text.startswith("#!/usr/bin/env bash"),
                        "mac 上 /bin/bash 是 3.2；用 env 找 bash 更稳")

    def test_no_bash4_only_constructs(self):
        code = _strip_bash_comments(self.text)
        banned = {
            "declare -A": "关联数组是 bash 4+（mac 自带 3.2 会报错）",
            "typeset -A": "关联数组是 bash 4+",
            "mapfile": "mapfile 是 bash 4+",
            "readarray": "readarray 是 bash 4+",
            "&>>": "&>> 追加重定向是 bash 4+",
        }
        for token, why in banned.items():
            self.assertNotIn(token, code, f"用了 {token}：{why}")
        self.assertIsNone(re.search(r"\$\{[^}]*(\^\^|,,)[^}]*\}", code),
                          "用了大小写转换（${x^^}/${x,,}）—— bash 4+ 才有")

    def test_no_crlf(self):
        """CRLF 的 .sh 在 mac 上会以 `$'\\r': command not found` 静默失败。"""
        for path in [SH] + _mac_shell_scripts():
            with open(path, "rb") as fh:
                data = fh.read()
            self.assertNotIn(b"\r\n", data, f"{os.path.basename(path)} 含有 CRLF")

    def test_tree_mode_survives_a_missing_venv_with_guidance(self):
        self.assertIn("setup_mac.sh", self.text,
                      "缺少 venv 时要明确告诉用户去跑 mac/setup_mac.sh")

    def test_data_root_is_asked_not_hardcoded(self):
        # mac 全新安装时数据根是 ~/Library/Application Support/ECHO，端口文件在那边
        self.assertIn("paths.data_root()", self.text,
                      "端口/数据根必须问应用自己的路径层，不能写死 <安装目录>/data")


class HarnessLocalInstallTests(unittest.TestCase):
    """标准版本地永久安装助手的历史契约（2026-09-22 补）。

    2026-11-03 起 **Windows 不再走这条路**（只保留官方桌面端自带的 dsh）；
    macOS 这轮不改，仍在使用 `.sh` 助手。助手脚本本身与它"从 npx 缓存复制"的
    三条路仍被下面的用例钉着（macOS 还在用），但**装机器不许再把它接回 Windows**。
    """

    PS1_HELPER = os.path.join(ROOT, ".dsh", "skills", "echo-install", "scripts",
                              "harness-install-local.ps1")
    SH_HELPER = os.path.join(ROOT, ".dsh", "skills", "echo-install", "scripts",
                             "harness-install-local.sh")

    def test_windows_uses_the_official_desktop_and_mac_keeps_the_local_helper(self):
        """2026-11-03：Windows 只有一套 DSH（官方桌面端自带的 dsh）。

        标准版 harness 的「npm 本地永久安装」已彻底退役 —— 组件安装器**不许**再调
        `harness-install-local.ps1`，改为复用 `harness-install-desktop.ps1` 保证官方桌面端
        已安装。macOS 这轮不改，仍走本地安装助手（`.sh`）。
        """
        win = _read(PS1_COMPONENTS)
        self.assertNotIn("harness-install-local.ps1", win,
                         "Windows 组件安装器还在调标准版本地安装助手（已退役）")
        self.assertIn("harness-install-desktop.ps1", win,
                      "Windows 组件安装器没有复用「确保官方桌面端已安装」的助手")
        self.assertIn("harness-install-local.sh", _read(SH),
                      "mac 组件安装器没走本地安装助手")

    def test_helpers_are_twins(self):
        """两个助手必须都在，且都实现同样的三件事：缓存路径、完整性自检、可覆盖版本。"""
        for path, tag in ((self.PS1_HELPER, "ps1"), (self.SH_HELPER, "sh")):
            self.assertTrue(os.path.isfile(path), f"{tag} 助手脚本不在：{path}")
            text = _read(path)
            self.assertIn("_npx", text, f"{tag} 助手没有「从 npx 缓存复制」那条路")
            self.assertIn("node-pty", text, f"{tag} 助手缺完整性自检（半残包要拦下）")
            # ⚠️ 这条断言 2026-10-08 改过：原来钉的是"默认版本必须是 0.1.5-rc.2"，
            # 而用户的要求正好相反 —— **别写死版本，装 npm 上的最新版**。
            # 现在钉的是"有一条自动取 latest 的路 + 一个兜底版本"，两个平台都要有。
            self.assertIn("dist-tags.latest", text,
                          f"{tag} 助手没有「自动取 npm latest」这条路（又写死版本了？）")
            self.assertIn("0.2.0-rc.2", text, f"{tag} 助手没了兜底版本")

    def test_default_version_is_resolved_not_hardcoded(self):
        """**2026-10-08 用户要求**：装最新版，别写死旧版本；同时要有升级入口。

        原状：`param(... $Version = '0.1.5-rc.2')` / `VERSION="0.1.5-rc.2"` 写死，
        而当时 npm 的 latest 已经是 0.2.0-rc.2 —— 新装机器永远拿到旧版。
        三件事必须都在两个平台上：
          ① 默认留空 → 查 `dist-tags.latest`；
          ② 查不到时有兜底版本（全新机器断网也不能整个失败）；
          ③ 有升级入口（`-Upgrade` / `--upgrade`），否则"装好了"那条快路径
             会把升级**静默吃掉**（这正是原来的病）。
        """
        ps1 = _read(self.PS1_HELPER)
        sh = _read(self.SH_HELPER)
        for text, tag in ((ps1, "ps1"), (sh, "sh")):
            self.assertIn("dist-tags.latest", text, f"{tag} 没有自动取 latest")
        self.assertIn("FallbackVersion", ps1, "ps1 没有兜底版本")
        self.assertIn("FALLBACK_VERSION", sh, "sh 没有兜底版本")
        # 默认值必须是空（留空 = 自动取最新），不能再写死一个具体版本当默认
        self.assertRegex(ps1, r"\$Version\s*=\s*''", "ps1 的 -Version 默认值不是空")
        self.assertRegex(sh, r'VERSION\s*=\s*""', "sh 的 VERSION 默认值不是空")
        # 升级入口
        self.assertIn("$Upgrade", ps1, "ps1 缺 -Upgrade（升级会被「已装好」快路径吃掉）")
        self.assertIn("--upgrade", sh, "sh 缺 --upgrade")
        self.assertIn("UPGRADE=1", sh, "sh 没解析 --upgrade")
        # 升级真的要能落地：删整树重装（npm 只按版本号判"已装"，不修残树）
        self.assertIn("删整树重装", ps1)

    def test_mac_installer_does_not_override_the_auto_resolved_version(self):
        """**mac 装机器不许把版本写死**（2026-10-08 真机事故）。

        mac 这轮不改，仍走本地 npm 安装助手：`DSH_VERSION` 默认必须为空，
        让助手自己去查 `dist-tags.latest`；否则会盖掉助手的默认，新装机器永远拿旧版。
        """
        sh = _read(SH)
        self.assertRegex(sh, r'DSH_VERSION\s*=\s*""',
                         "mac 装机器把 DSH_VERSION 默认写死了（会盖掉自动取 latest）")
        self.assertNotRegex(sh, r'DSH_VERSION\s*=\s*"0\.',
                            "mac 装机器的默认值又变成具体版本了")
        self.assertIn("--version", sh)
        self.assertIn("harness-install-local", sh)

    def test_windows_installer_no_longer_pins_a_dsh_version(self):
        """2026-11-03：Windows 的 DSH 只有官方桌面端这一套，版本随桌面端升级。

        安装器不再把任何 DSH 版本传给本地安装助手（那个助手在 Windows 侧已退役）；
        `-DshVersion` 只作为**退役参数**保留（旧命令行不因未知参数直接失败）。
        """
        ps1 = _read(PS1_COMPONENTS)
        self.assertNotIn("harness-install-local", ps1,
                         "Windows 还在走标准版本地安装（已退役）")
        self.assertIn("[string]$DshVersion", ps1,
                      "退役参数要保留，否则旧命令行/旧 install-all 会报未知参数")
        self.assertIn("已退役", ps1)

    def test_version_and_cache_only_are_overridable(self):
        """macOS 侧仍能一行参数换版本 / 离线只走缓存；Windows 侧这些入口已退役。"""
        self.assertIn("--dsh-version", _read(SH), "mac 侧缺 --dsh-version")
        self.assertIn("FromCache", _read(self.PS1_HELPER))
        self.assertIn("--from-cache", _read(self.SH_HELPER))
        self.assertIn("已退役", _read(PS1_COMPONENTS),
                      "Windows 侧退役的参数要明确说'已退役'，不能静默忽略")

    def test_windows_no_longer_writes_harness_command(self):
        """2026-11-03：不再写 `harnessCommand` / `harnessHome` 等 harness* 设置。

        DSH 的家目录固定是 `%USERPROFILE%\\.dsh`，程序入口固定是官方桌面端自带的
        `dsh.cmd`，ECHO 自己知道去哪找 —— 安装器只写 `agentBackend=dsh` 与
        `dshAutoStart=true`。mac 这轮不改，仍可选择钉死绝对路径。
        """
        ps1 = _read(PS1_COMPONENTS)
        code = "\n".join(ln for ln in ps1.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn("$values['harnessCommand']", code, "又在写 harnessCommand 了")
        self.assertNotIn("$values['harnessHome']", code, "又在写 harnessHome 了")
        self.assertNotIn("$values['agentHarnessEnabled']", code, "又在写 agentHarnessEnabled 了")
        self.assertIn("$values['dshAutoStart']", code, "没有写 dshAutoStart")
        self.assertIn("agentBackend'] = 'dsh'", code, "agentBackend 没固定写 dsh")
        sh = _read(SH)
        self.assertIn("--pin-harness-command", sh, "mac 侧没有钉死开关")
        self.assertIn('[ "$PIN_HARNESS_COMMAND" -eq 1 ]', sh, "mac 侧默认没走'不写'那条")

    def test_skill_documents_the_windows_dsh_retirement(self):
        """Windows 的标准版 harness / npm 那条路已退役，技能里不许再教它。"""
        text = _read(SKILL_MD)
        for retired in ("ETARGET", "koffi", "本地永久安装",
                        "SAFE_DELETE_BULK_CONFIRM_REQUIRED"):
            self.assertNotIn(retired, text,
                             f"技能里还留着已退役的 Windows npm 内容：{retired}")
        self.assertIn("只有一套 DSH", text, "技能没写清「只有一套 DSH」")
        self.assertIn("dshAutoStart", text, "技能没写清 dshAutoStart")
        self.assertIn("不需要 Node.js", text, "技能没写清不需要 Node.js")


class InstallReportsTheRightHarnessState(unittest.TestCase):
    """安装器判「智能体就绪」必须看 **components 里的 harness**（2026-09-22 同事反馈的 P0）。

    症状：`-Agent harness` 装完，脚本白等 150 秒 → 结论里报「还差 1 项 智能体（harness）」
    → `EXITCODE=1`，而那一刻 `/api/status` 里 harness 明明是 online。
    根因：判据用的是**顶层 `dsh`** —— 那是 DSH **桌面版**适配器，选 harness 时它本来就该是
    offline，于是这个判断**恒为假**。两个平台的组件脚本犯了同一个错。
    """

    def test_ps1_judges_the_dsh_component(self):
        code = "\n".join(ln for ln in _read(PS1_COMPONENTS).splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn("$st.dsh.online", code,
                         "又拿顶层 dsh 判智能体了 —— 组件状态要从 components 里取")
        self.assertIn("components", code, "要从 components 里取 dsh 的状态")

    def test_sh_judges_the_harness_component(self):
        code = _strip_bash_comments(_read(SH))
        self.assertNotIn("dsh_online", code,
                         "mac 侧还留着读顶层 dsh 的判据 —— 与 Windows 是同一个 bug")
        self.assertIn("harness_state", code, "mac 侧没有取 harness 组件状态的判据")

    def test_windows_waits_for_the_dsh_component_with_a_cap(self):
        """2026-11-03：Windows 只有一套 DSH —— 拉起后等 `/api/status` 的 components 里
        `dsh == online`，上限 180 秒；超时如实说"还在起"，不许无限等、也不许假装成功。

        mac 这轮不改，仍按安装方式分档（本地入口 45 秒 / npx 150 秒）。
        """
        ps1 = _read(PS1_COMPONENTS)
        self.assertIn("$Seconds = 180", ps1, "Windows 等 DSH 的上限应为 180 秒")
        self.assertIn("AddSeconds($Seconds)", ps1, "等待要用有上限的截止时间")
        self.assertIn("还在起", ps1, "超时要如实说'还在起'")
        self.assertIn("name -eq 'dsh'", ps1, "要从 components 里取 dsh 的状态")
        self.assertIn("/api/control/dsh/start", ps1, "没有调用开始 DSH 的接口")
        sh = _read(SH)
        self.assertIn("limit=45", sh, "mac 侧没有按安装方式分档")
        self.assertIn("limit=150", sh)

    def test_selfcheck_marks_the_unselected_agent_as_skipped(self):
        """自检输出里把"没选的智能体"印成 offline 会被读成「坏了」——标 skipped 并写清。"""
        self.assertIn("skipped", _read(PS1_COMPONENTS), "Windows 自检没标出未选中的智能体")
        self.assertIn("skipped", _read(SH), "mac 自检没标出未选中的智能体")


class NativeCommandStderrIsNotAnError(unittest.TestCase):
    """`$ErrorActionPreference='Stop'` + `2>&1 |` 会把 native 的**警告**升格成终止性错误。

    2026-09-22 同事实测：npm 明明装成功了（190 包、bin.js 就位），却因为一句
    `npm warn deprecated …` 跳进 catch、打印「npm 执行异常」。更糟的是警告若出现在
    **安装中途**，管道提前中断会留下半个 node_modules —— 正是「装残」那类事故的隐患。
    """

    HELPER = os.path.join(ROOT, ".dsh", "skills", "echo-install", "scripts",
                          "harness-install-local.ps1")

    def test_native_calls_downgrade_error_action_preference(self):
        text = _read(self.HELPER)
        self.assertIn("$ErrorActionPreference = 'Continue'", text,
                      "native 调用前没有临时降级 —— npm 的警告会被当成异常")
        self.assertIn("$LASTEXITCODE", text, "降级之后必须改看退出码判成败")

    def test_robocopy_is_guarded_too(self):
        """同一个坑在 robocopy 上也会咬人：异常会**绕过**「0-7 都算成功」那句判断。"""
        text = _read(self.HELPER)
        self.assertIn("$rcExit", text, "robocopy 没按退出码判成败")

    def test_npm_noise_is_suppressed(self):
        self.assertIn("--loglevel=error", _read(self.HELPER), "没压 npm 的 deprecated 噪音")

    def test_bash_side_needs_no_such_guard(self):
        """mac 只有 `set -u`（没有 `set -e`），stderr 不会升级成致命错误。

        记录这个差异，免得有人「为了对齐」给 bash 也加一层莫名其妙的包装。
        """
        self.assertNotIn("set -e", _read(HarnessLocalInstallTests.SH_HELPER))


class NpxCacheCanBeFilled(unittest.TestCase):
    """全新机器上 `_npx` 缓存是空的 —— 第 ③ 条兜底必须能自己把缓存填上。

    2026-09-22 同事就是这种情况：装之前刚清过缓存，于是「从 npx 缓存复制」无物可复制。
    """

    def test_both_helpers_can_fill_the_cache(self):
        ps1 = _read(HarnessLocalInstallTests.PS1_HELPER)
        sh = _read(HarnessLocalInstallTests.SH_HELPER)
        self.assertIn("Fill-NpxCache", ps1, "Windows 侧没有「先填缓存」这条路")
        self.assertIn("fill_cache", sh, "mac 侧没有「先填缓存」这条路")
        # 填缓存不能去占 43199（那是 ECHO 自己 harness 的端口）—— 只看代码，注释里提到无妨
        ps1_code = "\n".join(ln for ln in ps1.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("43199", ps1_code)
        self.assertNotIn("43199", _strip_bash_comments(sh))

    def test_fill_uses_a_command_that_exits_immediately(self):
        """用 `--package=<包> -- node --version` 把包装进缓存，而不是「起一次 web 再杀」：
        后者要挑空闲端口、要管进程回收，而这里跑完即退。"""
        for path in (HarnessLocalInstallTests.PS1_HELPER, HarnessLocalInstallTests.SH_HELPER):
            text = _read(path)
            self.assertIn("--package=@deepseek-ai/dsh@", text, f"{path} 的填缓存手法不对")

    def test_registry_claim_is_downgraded(self):
        """registry 那次事故已恢复（2026-09-22 晚复测 npm 584 包装成）——
        注释里再写「必然 ETARGET」会把以后排障的人带偏。"""
        for path in (HarnessLocalInstallTests.PS1_HELPER, HarnessLocalInstallTests.SH_HELPER,
                     PS1_COMPONENTS, SH, SKILL_MD):
            self.assertNotIn("必然 ETARGET", _read(path), f"{path} 还留着旧结论")


class AgentResumeNoteIsDocumented(unittest.TestCase):
    """给 agent 的一句话：执行环境中途回收进程时，直接重跑同一条命令（脚本幂等）。"""

    def test_skill_tells_agents_to_just_rerun(self):
        text = _read(SKILL_MD)
        self.assertIn("install-", text, "技能没提断点日志的位置")
        self.assertIn("幂等", text)
        self.assertIn("重跑", text)


class LauncherFailureIsNotInstallFailure(unittest.TestCase):
    """「有没有把 ECHO 拉起来」不能决定安装成败（2026-09-22 同事实测）。

    症状：在**没有控制台的沙箱**里（agent 自动装 ECHO 的环境）跑
    `install.ps1 -DestDir C:\\ECHO -Silent`，收尾那步拉起 console 程序失败，报
    `ERROR_NO_DATA (0x800700E8)`「管道正被关闭」；而脚本顶部是
    `$ErrorActionPreference = 'Stop'` → 它变成**终止性异常** → 被入口 catch 抓住 →
    一次**完全成功**的安装打印「安装中断」并 `exit 1`。
    这正是同事已经报过一次的「装好了却报失败」，换个地方又出现。

    而且 `-Silent` 下 `Ask-YesNo` 取默认值 `$true`，所以**技能那条命令必然走到这里**。
    """

    def test_no_bare_start_process_can_abort_the_install(self):
        code = "\n".join(ln for ln in _read(INSTALL_PS1).splitlines()
                        if not ln.lstrip().startswith("#"))
        self.assertEqual(code.count("Start-Process"), 1,
                         "拉起 ECHO 的进程创建只应留一处（包在 Start-EchoDetached 里）；"
                         "裸调用会以终止性异常把成功的安装判成失败")
        self.assertIn("function Start-EchoDetached", code)
        self.assertIn("-ErrorAction Stop", code, "要显式接住，不能指望外层 catch 兜")

    def test_failure_says_the_install_itself_is_fine(self):
        code = _read(INSTALL_PS1)
        self.assertIn("安装本身是好的", code,
                      "拉起失败时必须明确告诉用户：安装没问题，双击快捷方式即可")

    def test_silent_still_asks_to_launch(self):
        """记录这个前提 —— 它正是"沙箱里必然踩到"的原因。"""
        text = _read(INSTALL_PS1)
        self.assertIn("Ask-YesNo '现在启动 ECHO 并打开控制面板？' $true", text,
                      "启动那步的默认答案变了的话，这条前提要一起复核")
        self.assertIn("if ($script:Silent -or $script:DryRun) {", text,
                      "Ask-YesNo 在 Silent 下取默认值 —— 所以 -Silent 也会真去拉起")


class HarnessTreeSmokeTest(unittest.TestCase):
    """「装好了」的判据不能只看 bin.js 在不在（2026-09-22 同事反馈 3.2）。

    实测：`zod@4.6.5` 装着、`package.json` 也在，但整个 `v4/` 子目录缺失 ——
    `node bin.js web` 报 `ERR_MODULE_NOT_FOUND: …zod/v4/classic/external.js`。
    而旧自检只验「bin.js 存在且非空」+「node-pty 带 package.json/lib/index.js」，
    两条**全过**，于是"已装好"快路径把坏树当好的用，之后每次启动都失败；npm 又只按
    版本号认为它已装，后续修复轮也不会碰它（重跑只会说 changed N packages）。
    """

    def test_both_helpers_run_a_smoke_test(self):
        for path, tag in ((HarnessLocalInstallTests.PS1_HELPER, "ps1"),
                          (HarnessLocalInstallTests.SH_HELPER, "sh")):
            self.assertIn("--version", _read(path), "%s 助手没有冒烟测试" % tag)

    def test_fast_path_requires_the_smoke_test(self):
        """bin.js 在、但跑不起来时，**不许**走「已装好、跳过下载」那条路。"""
        ps1 = _read(HarnessLocalInstallTests.PS1_HELPER)
        self.assertIn("Test-HarnessTree $target).Count -eq 0", ps1,
                      "Windows 侧的快路径又只看 bin.js 了")
        sh = _strip_bash_comments(_read(HarnessLocalInstallTests.SH_HELPER))
        self.assertIn('tree_broken "$TARGET"', sh, "mac 侧的快路径又只看 bin.js 了")

    def test_a_broken_tree_is_reinstalled_not_reused(self):
        """判定残树后要**整树删掉重装** —— npm 不会去修缺失的子目录。"""
        for path, tag in ((HarnessLocalInstallTests.PS1_HELPER, "ps1"),
                          (HarnessLocalInstallTests.SH_HELPER, "sh")):
            self.assertIn("装残", _read(path), "%s 助手没说清残树要重装" % tag)


class BackendChoiceTests(unittest.TestCase):
    """安装流程**后面**那句「后端怎么来」（用户 2026-10-01 要的）。

    为什么钉它：这一步是唯一"装完客户端之后还要问人"的环节，也是"同事装一个包 → 零下载"
    这条路上后端那一半的入口。它坏了不会有任何报错 —— 只会**没人问**，于是每台新机器都在
    `capabilityPrivacy` / 配对 / 离线包上各走各的。

    契约（都是**文本断言**，与这份测试文件里其它安装脚本用例同一个手法）：
      ① 参数存在且默认 `ask`；
      ② 排在组件之后、收尾之前（那会儿服务已经起来，配对与「起本机后端」才立刻生效）；
      ③ **`-Yes` 不抑制它**（`-Yes` 免的是"装到哪个目录"，这一问是用户明确要的）；
      ④ 非交互（脚本化/CI）时**跳过**，不许静默替用户配对；
      ⑤ 两条路各自的落点：`/api/capability/pair` 与 `/api/capability/backend/start`；
      ⑥ **离线包优先**（`ECHO-backend-offline-*`），有就不下载；
      ⑦ 这一步**不许改退出码**（后端是可选项，装客户端本身是好的）。
    """

    @classmethod
    def setUpClass(cls):
        cls.text = _read(INSTALL_ALL)

    def test_the_switch_exists_and_defaults_to_asking(self):
        self.assertIn("[ValidateSet('ask', 'pair', 'local', 'skip')][string]$Backend = 'ask'",
                      self.text)
        self.assertIn("[string]$BackendPair = ''", self.text)
        self.assertIn("[string]$BackendDir = ''", self.text)

    def test_it_runs_after_the_components_and_before_the_summary(self):
        i_comp = self.text.index("$compExit = Invoke-ComponentsScript")
        i_back = self.text.index("Invoke-BackendStep -Dir $BackendDir")
        i_show = self.text.index("Show-Result -ComponentsExit $compExit")
        self.assertLess(i_comp, i_back, "后端那一步要排在组件**之后**")
        self.assertLess(i_back, i_show, "后端那一步要在收尾摘要**之前**（摘要里要有它的结论）")
        self.assertIn("$script:BackendSummary", self.text)

    def test_yes_does_not_suppress_this_one_question(self):
        """`-Yes` 免的是"装到哪个目录"；这一问是用户明确要求的一步，**照问**。

        判据：问不问只看"是不是交互控制台"（`[Console]::IsInputRedirected`），
        `$Backend -eq 'ask'` 那一段里**不许**出现 `$Yes`。
        """
        m = re.search(r"if \(\$mode -eq 'ask'\) \{(.*?)\n    \}", self.text, re.S)
        self.assertIsNotNone(m, "找不到问后端那一段")
        block = m.group(1)
        self.assertIn("Read-Host", block, "这一段就是那个问题本身")
        self.assertNotIn("$Yes", block, "-Yes 不该把这一问也免掉")

    def test_a_non_interactive_run_skips_instead_of_guessing(self):
        """脚本化/CI：没人能贴配对串 —— **静默改用户的配对才是真错**（模块头第 2 条纪律）。"""
        self.assertIn("IsInputRedirected", self.text)
        m = re.search(r"if \(\$mode -eq 'ask' -and -not \$interactive\) \{(.*?)\n    \}",
                      self.text, re.S)
        self.assertIsNotNone(m, "找不到非交互那一段")
        self.assertIn("$mode = 'skip'", m.group(1))

    def test_pairing_goes_through_the_same_endpoint_the_panel_uses(self):
        self.assertIn("/api/capability/pair", self.text)
        self.assertIn("base_url", self.text)
        self.assertIn("fingerprint", self.text)
        # 配对串的解析要与面板同一个口径（web/app.js::parsePairString）
        self.assertIn("ConvertFrom-EchoPairString", self.text)
        for key in ("'host'", "'url'", "'code'", "'fp'", "'fingerprint'"):
            self.assertIn(key, self.text, "配对串的键少了 %s" % key)
        # 从聊天/文档复制时带的引号/反引号要先去掉（否则连引号一起当地址）
        self.assertIn(".Trim('\"').Trim(\"'\").Trim('`')", self.text)

    def test_a_http_pairing_string_is_retried_once_over_https(self):
        """管理员那张串写 http、实际是 HTTPS 反代时，安装期也应当能配上。

        2026-10-21 真机：服务端发出来的是 `host=http://10.x:8900`，同事在安装器里
        怎么都配不上；把 host 改成 https 后在面板里成功了。安装器这一步与面板走
        同一个接口，所以只差"同一地址再试一次 https"（只升不降；401/403/429 不重试，
        那说明码已经用掉/被限流，换协议没有意义）。
        """
        self.assertIn("同一个地址再试一次 https", self.text)
        self.assertIn("$p.Url -match '^http://'", self.text)
        self.assertIn("-notin @(401, 403, 429)", self.text)

    def test_pairing_failures_say_which_kind_it_is(self):
        """"配对失败"四个字不够 —— 码过期 / 证书 / Docker 网桥 / 限流，下一步完全不同。"""
        for want in ("配对码无效 / 已被使用 / 已过期", "Docker 网桥",
                     "限流", "证书或配对串里的 fp="):
            self.assertIn(want, self.text, "配对失败的提示少了：%s" % want)

    def test_privacy_is_read_from_the_settings_list_and_written_inside_values(self):
        """`GET /api/settings` 是 `{settings: [{key,value}]}`；`PUT` 要包 `values`。

        这两处以前都写错了（读 `$st.capabilityPrivacy` 恒为 null、PUT 发扁平字典被 422），
        于是"配对后把「不出机」改成「内网」"从来没生效过 —— 远端后端会被自己的许可
        挡下，现象是"配好了却连不上"。
        """
        self.assertIn("$st.settings | Where-Object { $_.key -eq 'capabilityPrivacy' }", self.text)
        self.assertIn("values = @{ capabilityPrivacy = 'lan' }", self.text)

    def test_local_means_unpack_and_trigger_the_download_only_when_needed(self):
        """本机跑：解薄包 → **有离线包就直接复制启用**（不下载）→ 没有才触发下载。"""
        self.assertIn("ECHO-backend-offline-*.zip", self.text)
        self.assertIn("ECHO-backend-portable-*.zip", self.text)
        self.assertIn("/api/capability/backend/start", self.text)
        i_off = self.text.index("if ($off) {")
        i_thin = self.text.index("} elseif ($thin) {")
        self.assertLess(i_off, i_thin, "离线包那一档要**排在薄包之前**（有离线包就零下载）")
        self.assertIn("零下载", self.text)

    def test_it_uses_the_same_readiness_judgement_as_the_app(self):
        """"运行时能用"的判据与 `backend_env.check_server_deps` 同一条：解释器 + 能 import。

        只看 `runtime\\python.exe` 在不在，就会把"只装了半个运行时"报成就绪 ——
        那正是 2026-10-01 真机的那个 bug。
        """
        self.assertIn("import fastapi, uvicorn", self.text)
        self.assertIn("Test-BackendRuntimeUsable", self.text)

    def test_a_broken_backend_step_never_fails_the_install(self):
        """后端是可选项：这一步只 Warn/Info + 写摘要，**不许 exit**。"""
        body = self.text[self.text.index("function Invoke-BackendStep"):]
        # 2026-10-23: 升级模式那一整段（停服务/备份/版本）插在入口之前，
        # 它不属于后端这一步 —— 切到升级段的标题为止。
        body = body[:body.index("\n# ---------------------------------------------------------------- 升级模式")]
        self.assertNotIn("exit ", body, "后端那一步不许结束安装进程：%s"
                         % [ln.strip() for ln in body.splitlines() if "exit " in ln][:5])

    def test_the_launcher_passes_the_delivery_folder_along(self):
        """松的"装我.cmd + 几个 zip"形态：后端的两个 zip 就在脚本旁边，
        所以 `-BackendDir` 必须指向脚本自己那一层（否则找不到、只能靠 Python 侧去猜落点）。"""
        cmd = _read(os.path.join(ROOT, "delivery", "kit-install.cmd"), encoding="ascii")
        self.assertIn('-BackendDir "%HERE%"', cmd)
        self.assertIn("ECHO-backend-offline-*.zip", cmd,
                      "脚本里要说清它旁边该放哪两个 zip（那是交付契约）")


    def test_the_offline_pack_branch_also_starts_the_backend(self):
        """**解包 ≠ 能用**：离线包那条分支必须**也**触发起后端并等就绪。

        现场（2026-10-01 真机，用户装了新版）：选了"本机自己跑"，离线包解开了、运行时也就位了
        （日志写着「后端运行时已就位（**零下载**）」），但 `D:\\ECHO\\backend` 里**没有 server.yaml、
        没有 state/、连 backend.log 都没生成** —— 也就是说**根本没启动**。用户装完立刻转写：

            会议状态 error：没有可用的后端…

        （`capabilityMeetingAsrBackend` 只有 `echo-server`/`asr-provider` 两档 —— 会议转写**必须**
        有一个后端，客户端进程内的 sherpa 只服务语音助手那条路。所以"离线包已就位但没起"= 装完不能用。）

        判据（文本断言）：两条分支都走 `Start-LocalBackendNow`（它内部 `POST /api/capability/backend/start`
        并轮询 `GET /api/capability/backend` 直到 `job.running` 为假）。
        """
        self.assertIn("function Start-LocalBackendNow", self.text, "两条分支要共用一个'起并等就绪'的助手")
        self.assertIn("/api/capability/backend/start", self.text)
        self.assertIn("/api/capability/backend\" -f $port", self.text.replace("'", "\""),
                      "等就绪要轮询状态接口")
        calls = self.text.count("Start-LocalBackendNow ")
        self.assertGreaterEqual(calls, 3, "离线包（可用/不可用两条子路）与薄包都要调用它，实际 %d 处" % calls)
        i_off = self.text.index("if ($off) {")
        i_thin = self.text.index("} elseif ($thin) {")
        offline_part = self.text[i_off:i_thin]
        self.assertIn("Start-LocalBackendNow", offline_part,
                      "**离线包分支里必须有它** —— 少了这一下就是「装完就转写必然失败」")

    def test_it_waits_for_readiness_instead_of_pretending_success(self):
        """等就绪要有**上限**，超时就如实说"还在起"，不许假装成功（也不能无限等）。"""
        self.assertIn("AddSeconds(480)", self.text, "等待上限 8 分钟")
        self.assertIn("还在装/起", self.text)

class InstallPathNormalizationTests(unittest.TestCase):
    """安装路径的输入归一化（2026-10-21 同事反馈：输入 `C:\\echo` 报"路径有问题"）。

    真机最可能的形状是 Explorer「复制为路径」给的 `"C:\\ECHO"`，或者手输时带了尾反斜杠。
    两者都会让 `-Root "C:\\ECHO\\"` 变成"反斜杠把收尾引号转义掉、下一项被吞进值里"，
    于是脚本收到一个带引号/带别的参数的怪路径 —— 报错还看不出是转义问题。
    所以入口脚本必须先归一化（去引号、去尾反斜杠），再拼参数 / 再校验。
    """

    def test_the_launcher_strips_quotes_and_a_trailing_backslash(self):
        cmd = _read(os.path.join(ROOT, "delivery", "kit-install.cmd"), encoding="ascii")
        self.assertIn('set "ROOT=%ROOT:"=%"', cmd, "Explorer 复制的带引号路径要先去掉引号")
        self.assertIn('if "%ROOT:~-1%"=="\\" set "ROOT=%ROOT:~0,-1%"', cmd,
                      "尾反斜杠会把 -Root 后面的收尾引号转义掉")
        self.assertIn("reported a bad", cmd, "注释里要留下为什么（别让下一个人再踩）")

    def test_both_installers_trim_quotes_and_trailing_slashes_before_validating(self):
        for name, path in (("install-all.ps1", INSTALL_ALL), ("install.ps1", INSTALL_PS1)):
            with self.subTest(name=name):
                text = _read(path)
                self.assertIn(".Trim('\"')", text, "%s 没去引号" % name)
                self.assertIn("-replace '\\\\+$', ''", text, "%s 没去尾反斜杠" % name)
                self.assertIn("不要带引号", text, "%s 的报错要告诉人正确形状" % name)

    def test_the_fast_path_fails_early_when_the_install_root_cannot_be_created(self):
        """C:\\ 根目录在受管电脑上往往不能写 —— 要当场说清，别等到装运行时才报。"""
        for name, path in (("install-all.ps1", INSTALL_ALL), ("install.ps1", INSTALL_PS1)):
            with self.subTest(name=name):
                text = _read(path)
                self.assertIn("创建不了安装目录", text)
        self.assertIn("New-Item -ItemType Directory -Force -Path $script:TargetRoot",
                      _read(INSTALL_ALL))


class RequirementsFilesAreLocaleSafe(unittest.TestCase):
    """pip `-r` 读的清单文件：**含非 ASCII 就必须在第一两行有 PEP263 编码声明**。

    现场（2026-10-01 真机，第一次真跑"装运行时"那一步才炸）：

        pip install -r server/requirements.txt
        UnicodeDecodeError: 'gbk' codec can't decode byte 0xab in position 17

    根因：那份文件是「**UTF-8 中文注释 + 没有 BOM、没有 coding 声明**」，而 pip 的
    `_internal/utils/encoding.py::auto_decode()` 在既没 BOM 也没 cookie 时**按 locale 解码**
    （中文 Windows = cp936）→ 撞上 UTF-8 字节就崩。报错完全看不出是编码问题，
    看着像"包坏了"或"网不通"，很容易被引去查错地方。

    为什么这条值得专门钉：`requirements-core.txt` 早就有那行声明，**只有 `server/` 这份漏了** ——
    典型的一份文件一处漏，而它恰好在"另一条路"上（后端运行时），平时跑不到。
    """

    #: 这些目录里的清单不是"我们要 pip 的"（第三方/产物）
    SKIP = ("venv/", ".venv/", "node_modules/", "dist/", "_offline-cache/", ".git/")
    #: 走目录时**直接剪掉**的名字（与上面同源，但用于 `os.walk` 的 dirnames 剪枝）
    SKIP_DIRS = ("venv", ".venv", "node_modules", "dist", "_offline-cache", ".git")

    def _files(self):
        # 2026-10-09 修：原来用 `Path.rglob` —— 它**在走目录的过程中**碰到断链
        # （Windows Junction 指向已被删掉的目标）会直接抛 `FileNotFoundError`，
        # 把整条门禁带红。而"门禁崩在 walk 到一个毁掉的 node_modules 链上"与
        # "清单文件有没有编码声明"毫无关系（典型**红的地方不是坏的地方**）。
        # 现场：把标准版 harness 从 0.1.5-rc.2 换成 0.2.0-rc.2 之后，
        # `data/harness/profiles/**/node_modules` 里留下 83 个断链，这条用例就崩了。
        # 改成 `os.walk` + `onerror`（吞掉不可读目录）并**剪掉**本来就不看的目录：
        # 既robust，又不必真进 node_modules 那几万个小文件。
        out = []
        root = pathlib.Path(ROOT)
        for dirpath, dirnames, filenames in os.walk(str(root), onerror=lambda _e: None):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for fn in filenames:
                low = fn.lower()
                if not low.endswith(".txt"):
                    continue
                if "requirements" not in low and not low.startswith("constraints"):
                    continue
                path = pathlib.Path(dirpath) / fn
                rel = path.relative_to(root).as_posix()
                if any(s in rel + "/" for s in self.SKIP):
                    continue
                out.append((path, rel))
        return sorted(set(out), key=lambda x: x[1])

    def test_any_non_ascii_requirements_file_declares_its_encoding(self):
        checked, bad = [], []
        for path, rel in self._files():
            raw = path.read_bytes()
            if not any(b > 127 for b in raw):
                continue
            checked.append(rel)
            head = raw.decode("utf-8", "ignore").splitlines()[:2]
            if not any("coding" in ln and ("=" in ln or ":" in ln) for ln in head):
                bad.append(rel)
        self.assertGreaterEqual(len(checked), 3,
                                "只扫到 %d 个含中文的清单文件，扫描范围可能配错了" % len(checked))
        self.assertEqual(bad, [], "这些清单含非 ASCII 却没有 PEP263 声明"
                                  "（中文 Windows 上 pip 会按 cp936 解码并崩）：%s" % bad)


class OfflineRuntimeAbiTests(unittest.TestCase):
    """交付安装器必须**判运行时 ABI**（2026-10-09 真机事故：同事的 kit 卡在 `[3/9]`）。

    现场：`ERROR: Could not find a version that satisfies the requirement PyYAML>=6.0
    (from versions: none)`。而包里那份 `pyyaml-6.0.3-cp311-cp311-win_amd64.whl`
    **是完整可用的**（zip 能开、`METADATA` 写着 `Name: PyYAML / Version: 6.0.3`）。
    真因是 **pip 对 ABI 不匹配的轮子静默忽略** —— 那台机器上跑 pip 的解释器不是 CPython 3.11。

    旧脚本有两条路会走到这一步，而且**都不说真相**、反而报"多半是 bundle\\wheels 里缺 wheel"：

      ① 复用已有的 `runtime-core` **不看版本**；
      ② 候选里的 `python` / `python3` **没有版本约束** → 建出 3.12/3.13 的运行时被直接采信，
         于是包里那份 3.11.9 嵌入包**永远轮不到**。

    门禁里没有 Windows 真机，所以这里是**文本断言**（与 `BackendChoiceTests` 同一手法）。
    """

    @classmethod
    def setUpClass(cls):
        cls.text = _read(INSTALL_ALL)

    def test_the_abi_judgement_is_311_and_64bit(self):
        self.assertIn("function Test-RuntimeAbi", self.text, "没有 ABI 判据函数")
        self.assertIn("$script:WantPyMinor = 11", self.text, "判据必须钉 3.11")
        # 探针本身要量位宽（win_amd64 轮子在 32 位解释器上一样被忽略）
        self.assertIn("calcsize", self.text, "ABI 探针要同时量位宽")
        body = self.text[self.text.index("function Test-RuntimeAbi"):]
        body = body[:body.index("\nfunction ")]
        self.assertIn("[int]$Matches[4] -eq 64", body)

    def test_reusing_an_existing_runtime_goes_through_the_gate(self):
        i = self.text.index("$py = Test-RuntimePython $rcDir")
        block = self.text[i:i + 900]
        self.assertIn("Test-RuntimeAbi $py", block,
                      "复用已有的 runtime-core 必须先判 ABI —— 2026-10-09 事故的头一条")
        self.assertIn("Move-BadRuntimeAside", block, "ABI 不对的要搬开，别让它下一次又被复用")

    def test_each_venv_candidate_is_checked_too(self):
        i = self.text.index("foreach ($cand in @(@('py', @('-3.11'))")
        block = self.text[i:i + 1400]
        self.assertIn("Test-RuntimeAbi $made", block,
                      "`py -3.11` 建完也要过闸（版本钉死≠一定对：还可能是 32 位）")

    def test_the_bundled_3119_embed_comes_before_an_unpinned_python(self):
        """顺序就是判据（2026-10-09 重排）：**无版本约束的 `python` / `python3` 必须排在
        包里那份 3.11.9 之后**。

        低版本机器（3.9/3.10/2.7）上，unpinned 候选要么建出 ABI 不对的运行时（被闸枪毙、
        白建十来秒），要么根本没有 `venv` 模块；Microsoft Store 那个 python 桩甚至会弹出
        应用商店。而包里的嵌入包**永远是对的** —— 先试它，低版本机器就**根本不会去试**。
        """
        i_embed = self.text.index("③ 用包里的 python.org 嵌入包")
        i_unpinned = self.text.index("foreach ($cand in @(@('python', @()), @('python3', @())))")
        self.assertLess(i_embed, i_unpinned, "嵌入包必须排在无版本约束的 python 候选之前")
        block = self.text[i_unpinned:i_unpinned + 1400]
        self.assertIn("Test-RuntimeAbi $made", block,
                      "兜底那一级也要过 ABI 闸（薄包场景下它是唯一的路）")

    def test_the_failure_message_names_the_real_cause_first(self):
        """失败时**先判 ABI、再怪 wheel**：那句"多半是缺 wheel"曾把人引去查错地方。"""
        i = self.text.index("核心依赖安装失败")
        block = self.text[i:i + 1600]
        self.assertIn("Test-RuntimeAbi $Py", block, "失败分支里要先判 ABI")
        self.assertNotIn("多半是 bundle\\wheels 里缺 wheel", block,
                         "那句误导性提示已经删掉（真因多半是 ABI 不匹配，不是缺 wheel）")

    def test_the_probe_cannot_be_mangled_by_powershell(self):
        """探针里**不许有双引号**：WinPS 5.1 把参数交给原生命令时会吃掉它们
        （实测 `print("x")` 传过去变成 `print(x)` → SyntaxError）——那样这个闸会**永远判失败**，
        比没有闸更糟。2026-10-09 我自己先写了个带引号的探针，验活时真撞上了。
        """
        head = "$script:RuntimeAbiProbe = '"
        i = self.text.index(head) + len(head)
        probe = self.text[i:self.text.index("'", i)]
        self.assertNotIn('"', probe, "探针里有双引号 —— WinPS 5.1 会把它吃掉")
        self.assertIn("calcsize", probe, "探针要量位宽")


class UpgradeModeTests(unittest.TestCase):
    """2026-10-23 用户口径：**升级只更新核心代码**。

    界面 + 核心功能（代码）可以换；数据库/设置、会议、已有模型权重、后端与手表配置、
    智能体配置都不能碰。DSH 可以问一句要不要升级，但 agent 配置一个字不改。

    这一组用例钉的是"行为边界"，不是措辞：真跑一次升级不在门禁里（会起服务/停服务），
    所以断言落在**升级分支必须经过/必须绕开的那几个入口**上。
    """

    def setUp(self):
        self.text = _read(INSTALL_ALL)
        self.comp = _read(PS1_COMPONENTS)

    def test_it_detects_upgrade_before_touching_the_tree(self):
        self.assertIn("$script:IsUpgrade = ($installMode -eq 'upgrade')", self.text)
        # 探测在入口、早于 Install-TreeAndShortcuts（覆盖代码）
        i_mode = self.text.index("$installMode = Invoke-InstallModeProbe")
        i_copy = self.text.index("Install-TreeAndShortcuts", i_mode)
        self.assertLess(i_mode, i_copy, "安装模式必须在覆盖代码之前判出来")

    def test_it_stops_echo_gracefully_and_refuses_while_recording(self):
        self.assertIn("/api/control/echo/stop", self.text)
        self.assertIn("meeting.active", self.text)
        self.assertIn("正在录音", self.text)
        # 兜底**不许**退回旧的 stop.ps1：它会顺手停掉后端（用户口径：后端不要碰），
        # 而且旧装那份不认 -KeepBackend，未知参数会让这一步直接失败。
        self.assertIn("手动关闭 ECHO", self.text)
        self.assertNotIn("& powershell -NoProfile -ExecutionPolicy Bypass -File $stop", self.text)

    def test_it_does_not_touch_models_or_settings(self):
        self.assertIn("-KeepSettings", self.text)
        self.assertIn("-SkipModels", self.text)
        self.assertIn("-SkipPip", self.text)
        # 升级时引擎列表必须清空 —— 组件脚本据此不写 sttModel / meetingSttModel
        anchor = self.text.index("$installMode = Invoke-InstallModeProbe")
        i_up = self.text.index("if ($script:IsUpgrade) {", anchor)
        block = self.text[i_up:i_up + 1200]
        self.assertIn("$engineList = @()", block)

    def test_it_keeps_the_backend_untouched(self):
        # 升级分支里只读状态、不调用 Invoke-BackendStep
        i = self.text.index("# 升级**不问后端、不碰后端**")
        i_end = self.text.index("} else {", i)
        block = self.text[i:i_end]
        self.assertIn("保持现有配置", block)
        self.assertNotIn("Invoke-BackendStep", block)

    def test_it_reconciles_core_deps_before_starting_the_service(self):
        """2026-11-02 同事升级事故：11-01 的包给启动链加了硬依赖 `cryptography`，
        而升级路径按设计不装依赖（组件 -SkipPip；install.ps1 见 runtime 已存在直接 return）
        → `python -m app.main` 启动即 ModuleNotFoundError → 安装器空等 90 秒后只报
        "本实例没起来"。所以升级**必须**在拉服务之前对一次基础依赖账，并且失败时
        把服务日志尾部摊开（不许再只有一句"本实例没起来"）。"""
        self.assertIn("function Sync-CoreDeps", self.text)
        i_fn = self.text.index("function Start-EchoOwnService")
        i_call = self.text.index("Sync-CoreDeps", i_fn)
        i_start = self.text.index("后台启动：", i_fn)
        self.assertLess(i_call, i_start, "依赖对账必须在拉起服务之前")
        self.assertIn("requirements-core.txt", self.text)
        self.assertIn("echo-server.log.err", self.text, "失败分支要打印服务日志尾部")
        self.assertIn("cryptography", self.text, "探针要覆盖新加的硬依赖")

    def test_it_backs_up_the_database_before_overwriting(self):
        self.assertIn(".bak-before-upgrade-", self.text)
        self.assertIn("Backup-UpgradeDatabase", self.text)

    def test_dsh_is_optional_and_only_the_program_is_updated(self):
        self.assertIn("Ask-UpgradeDsh", self.text)
        self.assertIn("$UpgradeDsh", self.comp)
        self.assertIn("-Upgrade", self.comp)  # 传给 harness-install-desktop（重新下载官方桌面端）
        # 2026-10-31：客户端已不再写 sttModel（没有本地 STT 引擎）；KeepSettings 仍要生效。
        self.assertIn("if ($KeepSettings)", self.comp)
        self.assertNotIn("$values['sttModel']", self.comp)

    def test_the_launcher_recognises_an_existing_install(self):
        cmd = _read(os.path.join(ROOT, "delivery", "kit-install.cmd"), encoding="ascii")
        self.assertIn("Found an existing ECHO", cmd)
        self.assertIn('if not defined EXISTING set "ARGS=%ARGS% -Yes"', cmd,
                      "升级要交互（问 DSH），全新安装才 -Yes")

    def test_the_readme_explains_what_upgrade_keeps(self):
        text = _read(os.path.join(ROOT, "delivery", "kit-readme-win.md"))
        self.assertIn("已经装过 ECHO", text)
        self.assertIn("不会动", text)
        self.assertIn("模型权重", text)
        self.assertIn("智能体配置", text)


class DshInstallLogicTests(unittest.TestCase):
    """2026-11-03 真机三个安装阻断的回归（用户逐条指出）。

    1. "是否已装 DSH"的判据太粗暴：`<安装根>\\dsh\\app` 空目录（install.ps1 自己
       会建、老标准版卸载后也可能剩）被当成"已装 DSH"。
    2. 判断与动作不一致：升级路径里"不升级 DSH"会把 Agent 设成 none → 组件那一步
       根本不检查 dsh，装完 ECHO 没有引擎。
    3. 数组 splat 把命名参数按位置绑定 → helper 一条命令都没执行：
       `$dargs = @('-LogFile', $x); & $helper @dargs` 是错的，必须哈希表。
    """

    @classmethod
    def setUpClass(cls):
        cls.install = _read(INSTALL_PS1)
        cls.all = _read(INSTALL_ALL)
        cls.comp = _read(PS1_COMPONENTS)
        cls.desktop = _read(os.path.join(
            ROOT, ".dsh", "skills", "echo-install", "scripts",
            "harness-install-desktop.ps1"))
        cls.skill = _read(SKILL_MD)

    def test_install_ps1_stops_creating_the_retired_dsh_dirs(self):
        """install.ps1 不许再建 dsh\\app / dsh\\home —— 空目录是误判的源头。"""
        self.assertNotIn("'dsh\\app', 'dsh\\home'", self.install)
        self.assertIn("foreach ($d in @('data', 'models', 'meeting', 'aide'))",
                      self.install)

    def test_upgrade_detection_ignores_empty_install_dirs(self):
        """升级时"已装 DSH"必须看真东西（官方 CLI / 官方家目录），不看空目录。"""
        self.assertNotIn("Join-Path $script:TargetRoot 'dsh\\app'", self.all)
        self.assertIn("$officialCli", self.all)
        self.assertIn("resources\\runtime\\cli\\bin\\dsh.cmd", self.all)
        self.assertIn("$dshHome", self.all)

    def test_upgrade_never_silently_skips_the_dsh_check(self):
        """不升级 DSH 也必须按 Agent=dsh 跑校验；以前设 none 就完全不管了。"""
        anchor = self.all.index("if ($script:IsUpgrade) {", self.all.index("$installMode = Invoke-InstallModeProbe"))
        block = self.all[anchor:anchor + 2000]
        self.assertIn("$Agent = 'dsh'", block)
        self.assertNotIn("$Agent = 'none'", block)
        self.assertNotIn("$Agent = 'harness'", block)

    def test_components_helper_uses_a_hashtable_splat(self):
        """数组 splat = 位置绑定 → helper 的 -LogFile 落到 -InstallDir 上直接失败。"""
        # 注释里会解释"以前用错成什么样"，所以只扫代码行。
        code = "\n".join(ln for ln in self.comp.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertIn("$dargs = @{ LogFile =", code)
        self.assertNotIn("$dargs = @(", code)
        self.assertIn("& $desktopHelper @dargs", code)

    def test_desktop_installer_does_not_shadow_the_args_automatic(self):
        """`$args` 是 PowerShell 自动变量；当普通数组用会踩坑（真机安装命令没执行）。"""
        self.assertIsNone(re.search(r"(?m)^\s*\$args\s*=", self.desktop),
                          "不要把自动变量 $args 当普通数组赋值")
        self.assertIn("$installerArgs = @('/S')", self.desktop)
        self.assertIn("-ArgumentList $installerArgs", self.desktop)

    def test_desktop_detection_requires_an_installed_desktop_root(self):
        """只看 dsh.cmd 文件在不在太粗暴：必须是已装桌面端（有 DeepSeek Harness.exe）。"""
        self.assertIn("Get-InstallRoot $full", self.desktop)
        self.assertIn("treating as NOT installed", self.desktop)

    def test_running_check_distinguishes_gui_from_echo_headless_dsh(self):
        """GUI 与 ECHO 的无头 dsh 同名 exe；只按进程名会把引擎当成桌面版在运行。"""
        self.assertIn("Get-RunningKinds", self.desktop)
        self.assertIn("dsh-desktop-host", self.desktop)
        self.assertIn("web --port", self.desktop)
        self.assertNotIn("Get-Process -Name 'DeepSeek Harness'", self.desktop)
        # 升级时要明确要求先停 ECHO（无头引擎占着程序文件）
        self.assertIn("Stop ECHO first", self.desktop)

    def test_version_probe_has_a_hard_timeout(self):
        """坏掉的 CLI 可能挂住；`& $cmd --version` 没有超时会把安装器一起挂死。"""
        self.assertIn("WaitForExit(20000)", self.desktop)
        self.assertIn("Start-Process -FilePath $cmd -ArgumentList '--version'", self.desktop)

    def test_custom_install_dir_is_verified_under_that_dir(self):
        """装自定义目录失败时，不能拿 %LOCALAPPDATA% 里的旧安装冒充成功。"""
        self.assertIn("Find-CliUnderRoot $target", self.desktop)
        self.assertIn("custom install dir did not produce a usable CLI", self.desktop)

    def test_missing_engine_in_offline_or_upgrade_mode_tries_online_install(self):
        """清掉所有 dsh 后重装：升级/离线模式只认已装会直接卡死；引擎刚需，
        应探测下载地址并尝试联网补装官方桌面端（断开网则快速失败给手装指引）。"""
        code = "\n".join(ln for ln in self.comp.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertIn("Test-OfficialDownloadReachable", code)
        self.assertIn("$dargs.ContainsKey('NoDownload')", code)
        self.assertIn("desktop(retry)", code)
        self.assertIn("已尝试联网补装", code)


if __name__ == "__main__":
    unittest.main()
