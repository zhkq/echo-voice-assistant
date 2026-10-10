# -*- coding: utf-8 -*-
"""切换器必须把 **harness** 一起带走（2026-10-10 用户实测两次 500）。

现场：`switch-instance.ps1` 切到稳定版时，`Get-EchoInstanceProcs` 认
router / supervisor / echo / echo-stub（外加 `Get-EchoSidebars` 的 sidebar），
**唯独不认 harness** —— 于是旧树那份 node harness 留在 43199 上。
两棵树共用这个端口，而 ECHO 的 `ensure_running()` 当时只问"端口有人应答吗"，
就把别人的 harness 认成"已在运行"；两棵树家目录不同 → 工作区 id 对不上 →
点「开始回顾」`session/create` 报 `workspace/not-found` → 500。

这条用例钉的是**切换器那一半**（ECHO 那一半在 `tests/test_harness_owner.py`）。
脚本里的进程分类是纯文本逻辑、跑起来要真起进程，所以按仓库惯例断言脚本契约。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with io.open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class SwitcherTakesTheHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = _read(os.path.join("scripts", "echo-instance-lib.ps1"))

    def test_it_classifies_a_node_harness_as_part_of_the_tree(self):
        """node + 本树路径 + `@deepseek-ai\\dsh\\lib\\bin.js` → kind = harness。"""
        self.assertIn("'harness'", self.lib, "切换器要认得出 harness 这个 Kind")
        self.assertIn("@deepseek-ai\\dsh\\lib\\bin.js", self.lib,
                      "判据要钉在 harness 的入口上（node 进程长得都一样）")
        self.assertIn("$nm -match '^node'", self.lib, "只认 node 进程，别误伤别的")

    def test_it_covers_both_layouts(self):
        """新布局 `<root>\\dsh\\app\\…` 与老布局 `<root>\\harness\\dsh\\…` 都要覆盖。

        判据是 `*$r\\*`（本树路径）**加上**入口名，所以两种布局天然都盖住 ——
        这条用例把「必须带本树路径」这件事钉住：少了它，另一棵树的 harness 会被误杀。
        """
        self.assertIn('$cl -like "*$r\\*"', self.lib,
                      "必须同时要求「命令行里有本树路径」—— 否则会去杀别人那棵树")

    def test_every_classified_kind_is_actually_killed(self):
        """`Stop-EchoInstance` 收的是 `Get-EchoInstanceProcs` 的**全部**目标（没有白名单），
        所以加 Kind 就等于会收 —— 这条防止将来有人加个"只收这些 Kind"的过滤。"""
        self.assertIn("$targets = @(Get-EchoInstanceProcs $Root $procs) + @(Get-EchoSidebars $Root $procs)",
                      self.lib)
        self.assertIn("foreach ($t in $targets)", self.lib)
        self.assertNotIn("Kind -ne 'harness'", self.lib)
        self.assertNotIn("Kind -in", self.lib)


if __name__ == "__main__":
    unittest.main()
