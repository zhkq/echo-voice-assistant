# -*- coding: utf-8 -*-
"""交付档位（能力路由 §1.1/§1.2、施工顺序 step -1）：**默认档真的不含 torch 吗？**

这一组用例回答的是"**办公本装不上**"那件事有没有被真正解决。它不测某个函数返回什么，
而是把**交付时的承诺**变成会红的东西：

1. **默认档的依赖清单里没有 torch 系**（铁律 L1）。这条是整个瘦身的定义 ——
   §1.3 列的"装不上 / 启动分钟级 / WinError 127 / 诡异崩溃"五类问题**全部**来自 torch 系，
   所以"不含 torch"不是优化，是把故障面整体搬走。
2. **默认档必须有 sherpa-onnx**（铁律 L3：唤醒与命令转写必须有本机后端）。
   这条盯着一次真实事故（2026-09-23）：模型文件齐全、面板报"已就绪"，但 runtime-core 里
   没装这个包 —— 每次语音指令在转写处抛 `ModuleNotFoundError`，面板一声不响。
   根因就是"准备运行时的时候没装它"，所以它必须是**默认档的硬要求**，不是可选组件。
3. **档位表与实际清单不许漂**：`profiles.json` 说的 requirements / 组件，必须与盘上的
   文件、与 `components/offline-pack.json` 对得上；**每个组件正好属于一个档**（没有孤儿）。
4. **体积是可加的**：声明的大小必须等于"组件之和 + 额外项"，否则那个数字迟早变成口号。

**不断言"真机装完 600 MB"** —— 那要一台干净机器实测，本机做不了（步骤见 §1.2）。
这里钉的是**可加的数字与依赖清单**：能把口径钉住，就已经能挡住"悄悄把 torch 加回来"。
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES = os.path.join(ROOT, "components", "profiles.json")
OFFLINE_PACK = os.path.join(ROOT, "components", "offline-pack.json")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _package_names(text: str):
    """依赖清单里的**包名**（去掉注释、版本约束、extras）。"""
    out = set()
    for raw in str(text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[; ]", line, 1)[0].strip().lower()
        if name:
            out.add(name)
    return out


class ProfileTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = _load(PROFILES)
        cls.profiles = {p["id"]: p for p in cls.doc["profiles"]}
        cls.pack = {c["id"]: c for c in _load(OFFLINE_PACK)}

    def test_the_two_profiles_exist(self):
        self.assertEqual(sorted(self.profiles), ["default", "offline"])

    def test_every_profile_has_the_fields_the_installer_needs(self):
        for pid, p in self.profiles.items():
            with self.subTest(profile=pid):
                for key in ("label", "summary", "requirements", "components", "accept"):
                    self.assertIn(key, p, "%s 缺 %s" % (pid, key))
                self.assertTrue(os.path.isfile(os.path.join(ROOT, p["requirements"])),
                                "%s 指的依赖清单不存在：%s" % (pid, p["requirements"]))
                self.assertTrue(p["accept"],
                                "%s 没有验收条目 —— 那这一档就只是一句口号" % pid)

    def test_no_component_belongs_to_two_profiles(self):
        seen = {}
        for pid, p in self.profiles.items():
            for cid in p["components"]:
                self.assertNotIn(cid, seen,
                                 "%s 同时属于 %s 和 %s" % (cid, seen.get(cid), pid))
                seen[cid] = pid

    def test_every_offline_component_belongs_to_a_profile(self):
        """**没有孤儿**：`offline-pack.json` 里声明的组件必须有人认领。

        不然会出现"打包脚本会打它、但没人说它属于哪一档"的情况 —— 那时它是进默认档
        （把办公本撑胖）还是根本不装，全靠读代码猜。
        """
        orphans = sorted(set(self.pack) - {c for p in self.profiles.values()
                                           for c in p["components"]})
        self.assertEqual(orphans, [], "这些组件不属于任何档：%s" % orphans)

    def test_a_profile_only_names_components_that_exist(self):
        unknown = sorted({c for p in self.profiles.values() for c in p["components"]}
                         - set(self.pack))
        self.assertEqual(unknown, [], "档位表里出现了没声明的组件：%s" % unknown)


class DefaultProfileTests(unittest.TestCase):
    """**默认档的判据**（这一档的定义就在这里）。"""

    @classmethod
    def setUpClass(cls):
        cls.profiles = {p["id"]: p for p in _load(PROFILES)["profiles"]}
        cls.pack = {c["id"]: c for c in _load(OFFLINE_PACK)}
        cls.default = cls.profiles["default"]
        with open(os.path.join(ROOT, cls.default["requirements"]), encoding="utf-8") as fh:
            cls.req_text = fh.read()
        cls.names = _package_names(cls.req_text)

    def test_the_default_profile_has_no_torch_family(self):
        """**铁律 L1**：默认档不含 torch（也不含 CUDA、不含编译步骤）。"""
        hits = sorted(n for n in self.default["forbid"] if n in self.names)
        self.assertEqual(hits, [],
                         "默认档的依赖清单里出现了 torch 系（%s）—— 那一档的全部意义就没了"
                         % hits)

    def test_the_default_profile_has_no_torch_carrying_component(self):
        """**只查依赖清单是不够的**：torch 还能从组件那条路溜回来。

        把 `stt-sensevoice`（funasr + torch，896 MB）挪进默认档，`requirements-core.txt`
        一个字都不用改 —— 而那条"不含 torch"的护栏照样绿。这条堵的就是它。
        （这是写完上一版用例、自己回头看时发现的漏洞，不是猜的。）
        """
        hits = sorted(c for c in self.default.get("forbidComponents", [])
                      if c in self.default["components"])
        self.assertEqual(hits, [],
                         "默认档里出现了这些组件（%s）—— 它们会把 torch 或几百 MB "
                         "一起带回来" % hits)

    def test_every_forbidden_component_is_actually_claimed_by_another_profile(self):
        """被禁的组件总得有人要 —— 否则它就成了"谁都不装"的死件。"""
        claimed = {c for pid, p in self.profiles.items()
                   if pid != "default" for c in p["components"]}
        orphans = sorted(set(self.default.get("forbidComponents", [])) - claimed)
        self.assertEqual(orphans, [], "这些组件被默认档禁了，却没有别的档认领：%s" % orphans)

    def test_the_default_profile_keeps_the_command_chain_local(self):
        """**铁律 L3**：唤醒与命令转写必须有本机后端。

        盯着那次真实事故（2026-09-23）：模型齐、面板说就绪，而 runtime-core 没装
        `sherpa-onnx` —— 语音指令一声不响地全废。所以它是**硬要求**。
        """
        missing = sorted(n for n in self.default["require"] if n not in self.names)
        self.assertEqual(missing, [], "默认档缺了这些（指令链路会废）：%s" % missing)

    def test_the_offline_profile_is_the_one_that_may_pull_torch(self):
        """反过来也要成立：torch 系进的是**增强档**，而且它得明说这件事。

        不成立的话，"默认档不含 torch"可能只是"哪儿都没有 torch"——
        那这个档位划分就没有意义了。
        """
        with open(os.path.join(ROOT, self.profiles["offline"]["requirements"]),
                  encoding="utf-8") as fh:
            offline = _package_names(fh.read())
        self.assertTrue(offline & {"funasr", "faster-whisper"},
                        "增强档的清单里也没有 torch 系？那两档就没区别了")

    def test_the_default_size_adds_up_and_fits_the_budget(self):
        """声明的大小 = 组件之和 + 额外项；而且要落在 §1.1 的预算里。

        这条防的是"数字变成口号"：改了组件、忘了改合计，用例会红。
        """
        comp = sum(int(self.pack[c]["pack"].get("approx_mb") or 0)
                   for c in self.default["components"])
        total = comp + int(self.default.get("extraMb") or 0)
        self.assertLessEqual(total, 700,
                             "默认档声明 %d MB，超了 §1.1 的 ≈606 MB 预算" % total)
        self.assertGreaterEqual(total, 500,
                                "默认档只有 %d MB？比 §1.1 说的少太多，八成是漏了组件"
                                % total)

    def test_the_declared_size_has_exactly_one_source(self):
        """**合计不重复存**：它由组件之和算出。

        存一份 `approxMb` 再存一份组件清单 = 同一份事实算两遍，迟早出现
        "改了组件、数字还是老的"。所以这里断言的是**没有**那个字段 ——
        要显示合计的地方（面板/文档）自己算，或者引这一条。
        """
        for pid, p in self.profiles.items():
            with self.subTest(profile=pid):
                self.assertNotIn("approxMb", p,
                                 "%s 里又存了一份合计 —— 那是第二个真相，删掉它"
                                 % pid)


class InstallerUsesTheDefaultProfileTests(unittest.TestCase):
    """安装脚本的默认路径**就是**默认档 —— 两份东西不许各说各的。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "scripts", "install.ps1"), encoding="utf-8") as fh:
            cls.install = fh.read()
        cls.default = {p["id"]: p for p in _load(PROFILES)["profiles"]}["default"]

    def test_the_installer_installs_the_default_profiles_requirements(self):
        self.assertIn(self.default["requirements"], self.install,
                      "安装脚本没在用默认档指的依赖清单（%s）"
                      % self.default["requirements"])

    def test_the_installer_does_not_install_the_heavy_list_by_default(self):
        """`requirements.txt` 是**增强档**的清单：安装脚本默认不许拿它当基础依赖。"""
        self.assertNotIn("requirements.txt", self.install.replace(
            "requirements-core.txt", ""),
            "安装脚本里出现了 requirements.txt（那是增强档的清单）")

    def test_the_multi_platform_packer_packs_the_profile_table(self):
        """档位表要**进包**：它放在 `components/` 里，而主包会打这个目录。

        不检查的话，把表挪到 `delivery/`（那目录刻意不进包）以后，
        新机器上的安装器就看不到它了 —— 而本机测试照样全绿。
        """
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "components", "profiles.json")))
        with open(os.path.join(ROOT, "scripts", "build-package.ps1"), encoding="utf-8") as fh:
            packer = fh.read()
        self.assertIn("'components'", packer,
                      "打包脚本不再打 components\\ —— 档位表进不了包")


if __name__ == "__main__":
    unittest.main()
