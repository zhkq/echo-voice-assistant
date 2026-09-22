# -*- coding: utf-8 -*-
"""智能体在面板上的展示：名字不能撞、状态要说清（2026-09-22 用户看着截图报的两处）。

第一处是**真 bug**：`HarnessAgent` 继承 `DshAgent`，子类里漏写 `display_name` 就会继承父类的
`"DSH Desktop"` —— 设置 → 智能体 里"标准版 harness"那行显示成 "DSH Desktop"，两行一模一样。
（当时就是这么漏的；漏掉不会报错、不会编译失败，只有眼睛能发现，所以这里钉一条测试。）

第二处是**误读**：三个智能体都显示"可用"，用户读成"都在用/都正常"，而顶部横幅又说 harness 没在运行。
其实 `available`（探测结论）、`active`（当前在用）、`enabled`（产品开关）是三件事 —— 文案要分开说。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class DisplayNameTests(unittest.TestCase):
    """每个适配器都得有自己的展示名 —— 不能悄悄继承父类的。"""

    def test_no_adapter_inherits_its_parents_display_name(self):
        from app import agents
        agents.list_agents()                     # 先触发适配器导入（注册表懒加载）
        checked = 0
        for cls in agents.specs():
            parent_name = getattr(cls.__mro__[1], "display_name", "") if cls.__mro__[1:] else ""
            self.assertTrue(cls.display_name, "%s 没有展示名" % cls.name)
            if parent_name:
                checked += 1
                self.assertNotEqual(
                    cls.display_name, parent_name,
                    "%s 漏写 display_name —— 会继承 %s 的 %r，面板上两行同名"
                    % (cls.name, cls.__mro__[1].__name__, parent_name))
        self.assertGreaterEqual(checked, 1, "预期至少有 harness 继承 dsh（测试本身要有效）")

    def test_listed_display_names_are_unique(self):
        from app import agents
        names = [a["displayName"] for a in agents.list_agents()]
        self.assertEqual(len(names), len(set(names)), "面板上出现同名行：%s" % names)

    def test_dsh_and_harness_are_told_apart(self):
        from app import agents
        got = {a["name"]: a["displayName"] for a in agents.list_agents()}
        self.assertEqual(got["dsh"], "DSH Desktop")
        self.assertIn("标准版", got["harness"])
        self.assertIn("DeepSeek Harness", got["harness"])


class PanelWordingTests(unittest.TestCase):
    """面板文案的接线（前端没有构建期检查，按仓库惯例用字符串断言守）。"""

    def setUp(self):
        self.js = _read("web", "app.js")

    def test_chip_says_whether_it_is_in_use(self):
        for text in ('"使用中"', '"可用（未使用）"', '"已选但不可用"'):
            self.assertIn(text, self.js, "状态徽标要说清'在不在用'：缺 %s" % text)
        # 探测原因要挂在 title 上，鼠标停一下能看到为什么
        self.assertIn('title="${esc(a.reason || "")}"', self.js)

    def test_install_banner_is_refreshed_after_status_changes(self):
        self.assertIn("function maybeRefreshInstallNotice(st)", self.js)
        self.assertIn("maybeRefreshInstallNotice(st);", self.js)
        self.assertIn("_installNoticeSig", self.js)     # 只在状态变化时重拉，不做成轮询


if __name__ == "__main__":
    unittest.main()
