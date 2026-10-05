# -*- coding: utf-8 -*-
"""tests/test_dev_badge.py — 「开发版牌子」的三条硬指标（2026-10-04 用户要求）。

用户原话："开发版有标志就行，这样不影响交付物"。

所以这里钉的是：
  1. **只有** 实例名 == dev 时，`/api/instance` 才给出 `badge`；
  2. 稳定版（名字是 stable）与"查不到名字"（客户机没有切换器配置）**都不给 badge**；
  3. 面板那半个牌子**默认隐藏**、且前端**只在 `info.badge` 非空时**才亮 —— 交付物外观零变化。
"""
import io
import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, ROOT)

from app import instance_id, paths   # noqa: E402


def _read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class InstanceIdTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-inst-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.cfg = os.path.join(self.tmp, ".echo-instances.json")

    def _write(self, obj):
        with io.open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False))

    def test_dev_tree_gets_the_badge(self):
        self._write({"current": "dev", "instances": {"dev": {"root": paths.echo_root()}}})
        info = instance_id.info(self.cfg)
        self.assertEqual("dev", info["name"])
        self.assertEqual("开发版", info["badge"])

    def test_stable_tree_gets_no_badge(self):
        """稳定版**不挂牌子**（用户要的就是这个：交付物不受影响）。"""
        self._write({"current": "stable", "instances": {"stable": {"root": paths.echo_root()}}})
        info = instance_id.info(self.cfg)
        self.assertEqual("stable", info["name"])
        self.assertEqual("", info["badge"], "稳定版不该有牌子")

    def test_missing_or_broken_config_means_no_badge_and_no_raise(self):
        """客户机上没有这份配置（或它坏了）→ 不挂牌子，而且**永不抛**。"""
        self.assertEqual("", instance_id.info(os.path.join(self.tmp, "nope.json"))["badge"])
        with io.open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual({}, instance_id.load_config(self.cfg))
        self.assertEqual("", instance_id.info(self.cfg)["badge"])

    def test_other_trees_in_the_config_do_not_leak_in(self):
        """配置里列着别的树时，本树该是**查不到**（不冒充别人的名字）。"""
        other = os.path.join(self.tmp, "elsewhere")
        self._write({"instances": {"dev": {"root": other}, "stable": {"root": other}}})
        info = instance_id.info(self.cfg)
        self.assertEqual("", info["name"])
        self.assertEqual("", info["badge"])


class PanelWiringTests(unittest.TestCase):
    """前端那半个：牌子默认隐藏 + 只在 badge 非空时亮。"""

    @classmethod
    def setUpClass(cls):
        cls.html = _read("web", "index.html")
        cls.js = _read("web", "app.js")
        cls.css = _read("web", "app.css")

    def test_badge_element_is_hidden_by_default(self):
        self.assertIn('id="instBadge"', self.html)
        self.assertRegex(self.html, r'id="instBadge"[^>]*class="badge inst hidden"|'
                                    r'class="badge inst hidden"[^>]*id="instBadge"')

    def test_the_panel_only_lights_it_when_the_backend_says_so(self):
        fn = self.js[self.js.index("async function initInstanceBadge"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn('api("/api/instance")', fn)
        self.assertIn("if (!info || !info.badge) return", fn,
                      "非开发版必须**直接返回**：不改标题、不显牌子（交付物零变化）")
        self.assertIn('el.classList.remove("hidden")', fn)
        # 牌子上的字**只能**来自后端给的 badge —— 不许在这里硬写任何版本名
        # （硬写就会在客户机上冒出来；这是"不影响交付物"的实质约束）。
        self.assertRegex(fn, r"el\.textContent\s*=\s*info\.badge")
        self.assertNotRegex(fn, r'el\.textContent\s*=\s*"(开发版|稳定版)')

    def test_the_route_exists(self):
        self.assertIn('@router.get("/instance")', _read("app", "api.py"))


if __name__ == "__main__":
    unittest.main()
