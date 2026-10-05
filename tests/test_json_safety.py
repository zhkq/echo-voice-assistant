# -*- coding: utf-8 -*-
"""tests/test_json_safety.py — 模型的脏数据不许把接口打成 500（2026-10-05 真机事故后补）。

现场（测试机连本机后端，会议"转写一直不完成"）：后端日志里

    ValueError: Out of range float values are not JSON compliant
      ... server/main.py:234 _record_call → starlette JSONResponse ...

根因：`server/routes.py` 的 diarize 结果里 `round(float(a), 3)` —— 极短/静音/合成音输入时
pyannote 给出 **NaN** 的 start/end，而 Starlette 的 `JSONResponse` 是 `allow_nan=False`
（合法的 JSON 里没有 NaN 这个字面量）→ 整个 `/v1/diarize` 变成 HTTP 500。同一份输入有时
出 NaN 有时不出，所以表现为**时好时坏**；就绪自测拿一秒合成音去测，就报"分离这一档不可用"。

两条规矩：
  1. `sane()` / `SafeJSONResponse`：非有限浮点 → `null`，**任何**接口都不许因此 500；
  2. diarize 的 turn 先在源头丢掉非有限值（NaN 的 turn 本来就没有意义）。
"""
import io
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.jsonx import SafeJSONResponse, sane   # noqa: E402


def _read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class SaneTests(unittest.TestCase):

    def test_non_finite_floats_become_none(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                self.assertIsNone(sane(bad))

    def test_ordinary_values_are_untouched(self):
        for good in (0, 1, -3, 0.5, -2.25, "x", "", True, False, None):
            with self.subTest(good=good):
                self.assertEqual(good, sane(good))
        self.assertIsInstance(sane(True), bool, "bool 别被 float() 化（它是 int 的子类）")
        self.assertIsInstance(sane(3), int)

    def test_it_walks_containers(self):
        got = sane({"turns": [{"start": float("nan"), "end": 2.5}], "v": [1, float("-inf")],
                    "nested": {"deep": [float("inf")]}})
        self.assertEqual({"turns": [{"start": None, "end": 2.5}], "v": [1, None],
                          "nested": {"deep": [None]}}, got)

    def test_the_response_still_renders(self):
        """这是事故的正身：原来到这一步就 `ValueError: ... not JSON compliant`。"""
        body = SafeJSONResponse(content={"a": float("nan"), "b": [1, float("inf")]}).body
        self.assertEqual(b'{"a":null,"b":[1,null]}', body)


class WiringTests(unittest.TestCase):
    """两个应用都要用上兜底 —— 不然只是"这个接口这次没炸"。"""

    def test_both_apps_use_the_safe_response(self):
        for name in ("main.py", "admin.py"):
            with self.subTest(module=name):
                src = _read("server", name)
                self.assertIn("SafeJSONResponse", src, "%s 没接兜底" % name)
                self.assertIn("default_response_class=SafeJSONResponse", src)

    def test_diarize_drops_non_finite_turns_at_the_source(self):
        src = _read("server", "routes.py")
        self.assertIn('"turns": _finite_turns(turns)', src,
                      "diarize 的 turn 要走 _finite_turns（源头就丢掉 NaN）")
        self.assertNotIn('"turns": [{"start": round(float(a), 3)', src,
                         "老写法会把 NaN 直接塞进响应")
        # 逻辑本身：把 helper 的源码抠出来单独跑一遍（不导入 routes —— 那会拉起 torch）
        start = src.index("def _finite_turns(")
        #: 切到**函数结束**（空行边界）—— 别用"下一个 def"，那会把后面的模块级代码一起 exec
        end = src.find("\n\n\n", start)
        if end < 0:
            end = src.find("\n\n", start)
        self.assertGreater(end, start, "切不出 _finite_turns 的正文")
        ns = {"math": math}
        exec(compile(src[start:end], "routes.py", "exec"), ns)   # noqa: S102 - 只跑这一个纯函数
        turns = [(0.0, 1.5, "A"), (float("nan"), 2.0, "B"), (3.0, float("inf"), "C"),
                 (4.0, 5.0, "D")]
        self.assertEqual([{"start": 0.0, "end": 1.5, "speaker": "A"},
                          {"start": 4.0, "end": 5.0, "speaker": "D"}],
                         ns["_finite_turns"](turns))


if __name__ == "__main__":
    unittest.main()
