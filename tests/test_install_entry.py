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
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_PS1 = os.path.join(ROOT, "scripts", "install.ps1")
INSTALL_BAT = os.path.join(ROOT, "scripts", "install.bat")
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
    r"(?:;\s*module\s*=\s*'([^']+)'\s*)?\}", re.M)


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


class EngineTableAlignmentTests(unittest.TestCase):
    """两个安装器（Windows .ps1 / macOS .sh）的引擎表必须彼此一致，且被 app 认。

    为什么值得钉：这一表把「用户选的能力」翻译成三样东西 —— pip 依赖、模型 id、写进
    `sttModel` 的值。三样各自都有权威出处（`app/components.py` 的 `model_id`、
    `app/audio/stt.py` 的 `resolve_engine`/`WHISPER_MODELS`）。表一旦漂了，症状是
    「装完了但转写报错」或者「下了一个 app 不认识的模型 id」，而且两边平台各错各的。
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROOT)
        from app.audio import stt as stt_mod
        cls.stt = stt_mod
        cls.ps1 = _parse_ps1_engine_map()
        cls.sh_pip = _parse_bash_case_map(SH, "engine_pip")
        cls.sh_model = _parse_bash_case_map(SH, "engine_model")
        cls.sh_stt = _parse_bash_case_map(SH, "engine_stt")
        cls.sh_module = _parse_bash_case_map(SH, "engine_module")

    def test_both_installers_cover_the_same_engines(self):
        self.assertEqual(sorted(self.ps1), sorted(self.sh_stt),
                         "两个安装器认识的引擎集合不一致")

    def test_pip_deps_agree(self):
        for engine, info in self.ps1.items():
            self.assertEqual(sorted(info["pip"]),
                             sorted(_lookup_case_arm(self.sh_pip, engine).split()),
                             f"{engine} 的 pip 依赖两边不一致")

    def test_model_ids_agree(self):
        for engine, info in self.ps1.items():
            # mac 那边 whisper 档写成 echo "$1"（就是引擎名本身），等价于 ps1 的 model
            got = _lookup_case_arm(self.sh_model, engine)
            if got == "$1":
                got = engine
            self.assertEqual(info["model"], got, f"{engine} 的模型 id 两边不一致")

    def test_stt_values_agree(self):
        for engine, info in self.ps1.items():
            self.assertEqual(info["stt"], _lookup_case_arm(self.sh_stt, engine),
                             f"{engine} 写进 sttModel 的值两边不一致")

    def test_python_authority_table_agrees_with_both_installers(self):
        """**权威表在 `app/install_state.py`**，两个安装器是它的镜像 —— 三者必须逐项一致。

        为什么把权威放在 python 侧：安装状态/自检/面板横幅都由 ECHO 自己算（`install_state.py`），
        技能脚本在"还没装好 ECHO"时就得知道引擎→依赖的映射，所以只能各留一份镜像。
        镜像与权威漂了，症状是"技能装的东西 app 不认"，或者"面板说还缺、其实已经装了"。
        """
        sys.path.insert(0, ROOT)
        from app import install_state
        specs = install_state.ENGINE_SPECS
        self.assertEqual(sorted(specs), sorted(self.ps1),
                         "app/install_state.py 与安装器认识的引擎集合不一致")
        for engine, spec in specs.items():
            self.assertEqual(spec["model"], self.ps1[engine]["model"], f"{engine} 模型 id")
            self.assertEqual(spec["stt"], self.ps1[engine]["stt"], f"{engine} sttModel 值")
            self.assertEqual(spec["module"], self.ps1[engine]["module"], f"{engine} pip 模块名")
            self.assertEqual(spec["module"], _lookup_case_arm(self.sh_module, engine),
                             f"{engine} 的 bash 镜像与权威表不一致")

    def test_model_ids_exist_in_the_component_catalog(self):
        known = _component_model_ids()
        for engine, info in self.ps1.items():
            self.assertIn(info["model"], known,
                          f"{engine} 用的模型 id 不在 app/components.py 里：{info['model']}")

    def test_stt_values_are_accepted_by_the_app(self):
        """装出来的设置，app 必须认得 —— 不认的话会静默回落，用户看到「选了却不生效」。"""
        for engine, info in self.ps1.items():
            value = info["stt"]
            got_engine, got_model = self.stt.resolve_engine(value)
            if engine.startswith("whisper-"):
                self.assertEqual("whisper", got_engine, f"{engine} 应解析成 whisper")
                self.assertIn(value, self.stt.WHISPER_MODELS,
                              f"{value} 不是合法的 whisper 档名")
            elif engine == "sherpa":
                self.assertEqual(("sherpa", ""), (got_engine, got_model))
            elif engine == "sensevoice":
                self.assertEqual("sensevoice", got_engine)
            elif engine == "qwen3asr":
                self.assertEqual("qwen3asr", got_engine)
            else:
                self.fail(f"测试没覆盖这个引擎的分类：{engine}")


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
    """标准版本地永久安装的"三条路"契约（2026-09-22 晚补）。

    为什么值得钉：这条路径坏掉时**表面看不出来** —— 只是"冷启动慢两分钟"，
    而慢的原因在日志里只表现为 npm 的一串 ETARGET。要保证的最低限度：
    两个平台的安装器都走**同一个**助手脚本、助手真的实现了"从 npx 缓存复制"那条路、
    失败时给的是**可执行的**下一步，且技能文档写清了这条坑。
    """

    PS1_HELPER = os.path.join(ROOT, ".dsh", "skills", "echo-install", "scripts",
                              "harness-install-local.ps1")
    SH_HELPER = os.path.join(ROOT, ".dsh", "skills", "echo-install", "scripts",
                             "harness-install-local.sh")

    def test_both_platforms_delegate_to_the_helper(self):
        self.assertIn("harness-install-local.ps1", _read(PS1_COMPONENTS),
                      "Windows 组件安装器没走本地安装助手（又各自实现一遍了？）")
        self.assertIn("harness-install-local.sh", _read(SH),
                      "mac 组件安装器没走本地安装助手")

    def test_helpers_are_twins(self):
        """两个助手必须都在，且都实现同样的三件事：缓存路径、完整性自检、可覆盖版本。"""
        for path, tag in ((self.PS1_HELPER, "ps1"), (self.SH_HELPER, "sh")):
            self.assertTrue(os.path.isfile(path), f"{tag} 助手脚本不在：{path}")
            text = _read(path)
            self.assertIn("_npx", text, f"{tag} 助手没有「从 npx 缓存复制」那条路")
            self.assertIn("node-pty", text, f"{tag} 助手缺完整性自检（半残包要拦下）")
            self.assertIn("0.1.5-rc.2", text, f"{tag} 助手的默认版本漂了")

    def test_version_and_cache_only_are_overridable(self):
        """registry 哪天又坏在别的版本上时，要能一行参数换版本 / 离线只走缓存。"""
        self.assertIn("DshVersion", _read(PS1_COMPONENTS), "Windows 侧缺 -DshVersion")
        self.assertIn("--dsh-version", _read(SH), "mac 侧缺 --dsh-version")
        self.assertIn("FromCache", _read(self.PS1_HELPER))
        self.assertIn("--from-cache", _read(self.SH_HELPER))

    def test_pinning_the_absolute_path_is_opt_in(self):
        """默认**不写** harnessCommand（交给 ECHO 自动解析），要钉死得显式加开关。

        为什么默认不写（2026-09-22 晚定）：写死 node 绝对路径后，node 一升级
        （WorkBuddy / nvm 换版本目录）那条路径就失效、harness 起不来，而
        `harnessCommand` 是隐藏设置，用户很难自己找到并改回来。
        ECHO 的 `command()` 本来就会在"出厂值 + 本地入口存在"时直连本地入口，
        node 也由 `node_dirs()` 探测（与技能里的 Resolve-NodeDir 同一批目录）。
        """
        ps1 = _read(PS1_COMPONENTS)
        sh = _read(SH)
        self.assertIn("[switch]$PinHarnessCommand", ps1, "Windows 侧没有钉死开关")
        self.assertIn("and $PinHarnessCommand", ps1, "Windows 侧默认没走'不写'那条（开关没参与判断）")
        self.assertIn("--pin-harness-command", sh, "mac 侧没有钉死开关")
        self.assertIn('[ "$PIN_HARNESS_COMMAND" -eq 1 ]', sh, "mac 侧默认没走'不写'那条")
        self.assertIn("--pin-harness-command", _read(SKILL_MD), "技能没写清默认不写 / 怎么钉死")

    def test_skill_documents_the_registry_trap(self):
        text = _read(SKILL_MD)
        self.assertIn("ETARGET", text, "技能没写清 npm 装不下来这条及其绕法")
        self.assertIn("harness-install-local", text, "技能没告诉排障的人可以单独跑助手")


class InstallReportsTheRightHarnessState(unittest.TestCase):
    """安装器判「智能体就绪」必须看 **components 里的 harness**（2026-09-22 同事反馈的 P0）。

    症状：`-Agent harness` 装完，脚本白等 150 秒 → 结论里报「还差 1 项 智能体（harness）」
    → `EXITCODE=1`，而那一刻 `/api/status` 里 harness 明明是 online。
    根因：判据用的是**顶层 `dsh`** —— 那是 DSH **桌面版**适配器，选 harness 时它本来就该是
    offline，于是这个判断**恒为假**。两个平台的组件脚本犯了同一个错。
    """

    def test_ps1_judges_the_harness_component(self):
        code = "\n".join(ln for ln in _read(PS1_COMPONENTS).splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn("$st.dsh.online", code,
                         "又拿顶层 dsh 判 harness 了 —— 选标准版时恒为假，会把成功报成失败")
        self.assertIn("components", code, "要从 components 里取 harness 的状态")

    def test_sh_judges_the_harness_component(self):
        code = _strip_bash_comments(_read(SH))
        self.assertNotIn("dsh_online", code,
                         "mac 侧还留着读顶层 dsh 的判据 —— 与 Windows 是同一个 bug")
        self.assertIn("harness_state", code, "mac 侧没有取 harness 组件状态的判据")

    def test_wait_is_tiered_by_install_kind(self):
        """本地永久安装实测约 11 秒就起；150 秒是照 npx 时代定的，失败时白等两分半。"""
        ps1 = _read(PS1_COMPONENTS)
        self.assertIn("$local = [bool]$script:HarnessCommand", ps1, "Windows 侧没有按安装方式分档")
        self.assertIn("$Seconds = 45", ps1)
        sh = _read(SH)
        self.assertIn("limit=45", sh, "mac 侧没有按安装方式分档")
        self.assertIn("limit=150", sh)

    def test_selfcheck_marks_the_unselected_agent_as_skipped(self):
        """自检输出里 `dsh offline` 与 `harness online` 并排打印，人/agent 都容易读成「坏了」。"""
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


if __name__ == "__main__":
    unittest.main()
