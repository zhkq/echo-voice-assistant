# -*- coding: utf-8 -*-
"""唤醒匹配判据：**连续音节** + 贴句首（2026-10-08 用户报"误识频率很高"）。

事故形状（代码确证）：
  * 旧判据是 `_subseq()` —— 唤醒词的音节只要**按顺序出现过**就算命中，中间可以夹
    任意无关音节；
  * 唤醒词「回声回声」只有 4 个音节，而 `会/回/汇`、`生/声/省/胜` 在日常对话里高频出现
    → 闲聊极易撞上 → 误唤醒。
  * 另有两个"防误触发"的设置项 `wakeConfirmX/N` **读了但从不使用**（`hit()` 命中即
    `_reset()`，"多帧"永远凑不够）—— 所以调参也救不了，必须改判据。

修法（本条只做第 1 条）：
  * `_contiguous()` 取代 `_subseq()` 做唤醒判据 —— 音节必须**连续**；
  * 外加一道**贴句首**闸门（`_MAX_LEAD_SYL`），因为唤醒词是"喊一句"的用法，
    正常出现在话的开头；识别流是累积的，所以"前面攒了很多字"更像闲聊。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audio import wake                                          # noqa: E402


class ContiguousMatchTests(unittest.TestCase):
    """`_contiguous()` 的语义：连续 + 逐音节一个近音位。"""

    def test_exact_hit(self):
        self.assertEqual(wake._contiguous(["hui", "sheng"], ["hui", "sheng"]), (0, 2))

    def test_hit_inside_a_sentence(self):
        self.assertEqual(
            wake._contiguous(["hui", "sheng"], ["ming", "tian", "hui", "sheng"]), (2, 4))

    def test_one_near_homophone_per_syllable_is_tolerated(self):
        """识别器常给错字面（yuan≈yun），那是同一句话的另一种写法。"""
        self.assertEqual(wake._contiguous(["hui", "sheng"], ["huei", "sheng"]), (0, 2))

    def test_inserted_syllables_are_no_longer_accepted(self):
        """**这条就是事故的回归**：中间夹了别的内容，旧判据会命中，新判据必须不命中。"""
        self.assertIsNone(
            wake._contiguous(["hui", "sheng", "hui", "sheng"],
                             ["hui", "yi", "sheng", "chan", "hui", "bao", "sheng"]),
            "中间夹了无关音节仍然命中 —— 又退回子序列匹配了")

    def test_scattered_occurrences_are_not_accepted(self):
        """四个音节各自出现过、但不连续 → 不命中（旧判据会命中）。"""
        self.assertIsNone(
            wake._contiguous(["hui", "sheng", "hui", "sheng"],
                             ["hui", "a", "sheng", "b", "hui", "c", "sheng"]))

    def test_a_different_order_is_not_accepted(self):
        self.assertIsNone(wake._contiguous(["hui", "sheng"], ["sheng", "hui"]))

    def test_too_short_haystack(self):
        self.assertIsNone(wake._contiguous(["hui", "sheng", "hui"], ["hui", "sheng"]))

    def test_empty_inputs(self):
        self.assertIsNone(wake._contiguous([], ["hui"]))
        self.assertIsNone(wake._contiguous(["hui"], []))


class RealChatterDoesNotWakeTests(unittest.TestCase):
    """**用本机日志里的真实闲聊**当负例（唤醒词取稳定版库里的实际值「回声回声」）。

    这些句子都曾经被送进识别链路；旧判据在其中至少一条上会误命中。
    """

    KW = "回声回声"

    def _hits(self, text):
        kp = wake._pinyin(self.KW)
        rp = wake._pinyin(wake._norm(text))
        return wake._contiguous(kp, rp) is not None

    def test_real_chatter_does_not_trigger(self):
        for text in ("明天明天明明明明天上午开经理丽例例立会帮我把刚才收到的两个文件整理一下",
                     "明天天天气怎么样适合跑跑步吗",
                     "明天天天气怎么样",
                     "运气急怎么样",
                     "开会的时候大家都说这个方案可以",
                     "汇报生产情况",
                     "小尼小尼"):
            with self.subTest(text=text[:20]):
                self.assertFalse(self._hits(text), "闲聊被当成唤醒词了：%s" % text)

    def test_the_wake_word_still_triggers(self):
        self.assertTrue(self._hits("回声回声"), "唤醒词本身必须还能唤醒")

    def test_the_old_comment_shape_does_not_match(self):
        """老注释声称 `回升升回回回升` 能命中 —— **实测是错的**，这里钉死真相。

        `hui sheng sheng hui hui hui sheng` 里既没有连续的 `hui sheng hui sheng`，
        作子序列也不成立（`sheng` 之后紧跟 `sheng`）。
        """
        self.assertFalse(self._hits("回升升回回回升"))
        self.assertIsNone(
            wake._contiguous(wake._pinyin(self.KW),
                             wake._pinyin(wake._norm("回升升回回回升"))))


class PinyinDependencyTests(unittest.TestCase):
    """**`pypinyin` 必须进依赖清单**（2026-10-08 真机事故，这条是它的守门人）。

    现场：`wake.py::_pinyin()` 用 `from pypinyin import lazy_pinyin`，
    失败时 `except` **静默**返回 `[整段文本]`；而 `requirements-core.txt` /
    `requirements.txt` / `pyproject.toml` **三处都没有 pypinyin**。后果：
      * dev 机 venv 里恰好有 → 能唤醒；**客户机没有 → 唤不醒**，且不报错；
      * 退化后拼音容错层等于死代码，只剩逐字全等（同音字一律漏）。

    为什么必须有这条用例：这个洞**不是代码写错**，而是"机制依赖了一个从没被登记过的包" ——
    没有守门人时，它只会在**客户机**上以"唤不醒"的形式现形，开发机永远看不到。
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_requirements_core_lists_pypinyin(self):
        """**必须有一行真的声明**，不能只是注释里提到 —— 否则"注释在、包不在"照样骗过检查。

        （我第一版就是断言 `"pypinyin" in body`，结果**注释里那句说明**让用例失去意义；
        去掉真实声明行它也绿。改成解析"非注释的 requirement 行"。）
        """
        req = os.path.join(self.ROOT, "requirements-core.txt")
        with open(req, encoding="utf-8") as fh:
            declared = []
            for line in fh:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                name = s.split(">=")[0].split("==")[0].split(">")[0].strip().lower()
                declared.append(name)
        self.assertIn("pypinyin", declared,
                      "requirements-core.txt 没有**声明** pypinyin（注释里提到不算）—— "
                      "客户机上拼音层会是死代码，而且没有报错")

    def test_it_is_not_a_torch_dependency(self):
        """铁律 L1：默认档不含 torch。pypinyin 必须是纯 Python（不能顺带拖 torch）。"""
        req = os.path.join(self.ROOT, "requirements-core.txt")
        with open(req, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip().lower()
                if s.startswith("#") or not s:
                    continue
                self.assertNotIn("torch", s, "默认档清单里出现了 torch 系：%s" % line.strip())

    def test_the_module_reports_whether_pinyin_is_available(self):
        """`pinyin_available()` 必须存在 —— 否则"退化了"这件事没人能看出来。"""
        self.assertTrue(hasattr(wake, "pinyin_available"))
        self.assertIsInstance(wake.pinyin_available(), bool)

    def test_degraded_mode_does_not_break_matching(self):
        """没有 pypinyin 时：不许让"连续匹配"去比 4 个音节 vs 1 个元素（那会永不命中）。

        `hit()` 必须在 `pinyin_available()` 为假时**跳过拼音这条路**，
        只保留逐字匹配 —— 精度差但**至少能唤醒**。
        """
        import builtins
        real = builtins.__import__

        def fake(name, *a, **k):
            if name == "pypinyin":
                raise ImportError("模拟客户机：没装 pypinyin")
            return real(name, *a, **k)

        class _Rec:
            def __init__(self, text):
                self.text = text

            def create_stream(self):
                return object()

            def get_result(self, _s):
                return self.text

            def reset(self, _s):
                pass

        builtins.__import__ = fake
        try:
            self.assertFalse(wake.pinyin_available())
            # 逐字命中仍然有效（唤醒词原样出现在文本里）
            d = wake._StreamDetector(_Rec("回声回声"), ["回声回声"])
            self.assertTrue(d.hit(), "没有 pypinyin 时连逐字匹配都不工作了")
            # 同音字（拼音才认得出）此时认不出 —— 这是已知代价，不是崩溃
            d2 = wake._StreamDetector(_Rec("回生回生"), ["回声回声"])
            self.assertFalse(d2.hit())
        finally:
            builtins.__import__ = real


class DetectorWiringTests(unittest.TestCase):
    """`_StreamDetector.hit()` 必须真的走**连续**判据（而不是又退回子序列）。"""

    class _FakeRec:
        def __init__(self, text):
            self.text = text
            self.resets = 0

        def create_stream(self):
            return object()

        def get_result(self, _s):
            return self.text

        def reset(self, _s):
            self.resets += 1

    def _detector(self, text):
        return wake._StreamDetector(self._FakeRec(text), [RealChatterDoesNotWakeTests.KW])

    def test_it_hits_the_wake_word(self):
        self.assertTrue(self._detector("回声回声，今天天气怎么样").hit())

    def test_it_resets_after_a_hit(self):
        """命中后要重置识别流（否则同一段文本会反复命中）。"""
        d = self._detector("回声回声")
        self.assertTrue(d.hit())
        self.assertEqual(d.rec.resets, 1)

    def test_scattered_syllables_do_not_hit(self):
        """**事故的回归**：音节各自出现过但不连续 → 不许命中。"""
        self.assertFalse(self._detector("今天开hui的时候说sheng产，后来hui报也sheng了").hit())

    def test_real_chatter_does_not_hit(self):
        self.assertFalse(self._detector("明天明天明明明明天上午开经理丽例例立会帮我把刚才收到的两个文件整理一下").hit())
