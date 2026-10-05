# -*- coding: utf-8 -*-
"""tests/test_voiceprint_card.py — 「设置 → 声纹库」卡的接线（2026-10-02）

用户要求（原话）："声纹库要有列表和删除、试听等功能"。
这张卡是**函数渲染**的（`common: () => renderVoiceprintCard()`），落点表里看不见里面的按钮，
所以单独立一组用例钉住：列表 / 删单条 / 删联系人 / 试听四件事都在，空态告诉用户去哪儿入库，
以及 `_vpLibCache` **必须先声明再赋值** —— `web/app.js` 第 2 行是 `"use strict"`，
未声明就赋值会当场 ReferenceError（表现是"功能没坏、显示坏了"，见 AGENTS ③）。
"""
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "web", "app.js")


def _fn(js, head):
    """取一段函数的正文（从 head 到它自己的收尾 "\\n}"），找不到就断言失败。"""
    i = js.find(head)
    if i < 0:
        raise AssertionError("app.js 里找不到 %s" % head)
    body = js[i:]
    j = body.find("\n}")
    return body if j < 0 else body[:j]


class VoiceprintCardTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with io.open(APP_JS, encoding="utf-8") as fh:
            cls.js = fh.read()

    def test_the_card_is_function_rendered_and_its_keys_are_declared(self):
        """卡在「设置」里、常用是函数渲染，且代管的键登记进 `covers`（否则会掉进"未归类"卡）。"""
        i = self.js.index('{ id: "vp", title: "声纹库"')
        block = self.js[i:self.js.index("},", i)]
        self.assertIn("common: () => renderVoiceprintCard()", block)
        self.assertIn('covers: ["voiceprintAutoEnroll"]', block)
        self.assertIn('adv: ["voiceprintThreshold", "voiceprintMargin"]', block)

    def test_the_list_shows_who_when_and_offers_delete_and_audition(self):
        fn = _fn(self.js, "function renderVoiceprintCard()")
        for want in ("data-vp-play=", "data-vp-del=", "data-vp-delname=", "data-vp-refresh=",
                     "sm.meeting_name", "sm.created_at"):
            self.assertIn(want, fn, "声纹库卡少了 %s" % want)

    def test_audition_hits_the_audition_endpoint_and_shows_the_reason(self):
        """试听走 `/api/voiceprints/<id>/audition`；后端回 JSON（听不了）时**原样显示原因**。"""
        fn = _fn(self.js, "async function _vpPlay(id)")
        self.assertIn("/api/voiceprints/${id}/audition", fn)
        self.assertIn("j.reason", fn, "听不了的时候要把后端给的原因显示出来")
        self.assertIn("new Audio(", fn)

    def test_empty_state_says_where_to_enroll(self):
        fn = _fn(self.js, "function renderVoiceprintCard()")
        self.assertIn("说话人管理", fn)
        self.assertIn("声纹入库", fn)

    def test_delete_calls_the_existing_endpoints(self):
        i = self.js.index('document.addEventListener("click", async (e) => {')
        # 别用 `index("});", i)` 收尾：这段里内层事件分支自己就有 `});`，会切在中间。
        # 直接取一段足够长的窗口（这个委托处理器约 1 KB）。
        handler = self.js[i:i + 2200]
        self.assertIn('method: "DELETE"', handler)
        self.assertIn("encodeURIComponent(name)", handler, "按联系人删要走 ?name=")

    def test_the_cache_is_declared_before_it_is_assigned(self):
        """`"use strict"` 的坑：模块级必须先 `let _vpLibCache` 再赋值。"""
        self.assertRegex(self.js, r"let _vpLibCache = null;")
        self.assertLess(self.js.index("let _vpLibCache = null;"),
                        self.js.index("_vpLibCache = await api("))

    def test_the_settings_tab_loads_it(self):
        """进「设置」页要拉一次（否则列表永远停在"读取中…"）。"""
        self.assertRegex(self.js, r"loadVoiceprints\(\);")

    def test_it_does_not_reuse_the_capability_card_cache(self):
        """能力卡那边另有一个**函数级** `_vpCache` —— 别混用（就是这轮踩到的重名）。"""
        fn = _fn(self.js, "async function loadVoiceprints()")
        self.assertIn("_vpLibCache", fn)
        self.assertNotIn("_vpCache", fn.replace("_vpLibCache", ""))


if __name__ == "__main__":
    unittest.main()
