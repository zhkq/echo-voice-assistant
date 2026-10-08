# -*- coding: utf-8 -*-
"""给"重转结束也要自动压缩"补一条用例（2026-10-08 修的那个 bug）。

背景（用户当天问"压缩没执行完吗"）：昨晚重转的 10 场（其实是 55 场老会议）
**只有 wav、没有 flac**，日志里一排
`自动压缩跳过 <会议>：正在重新转写`。

根因：`_maybe_auto_compress()` 只在**首次转写完成**那一条路（`_transcribe_meeting` 里）被调，
重转时那一刻 `compression_state()` 判到"正在重新转写"按纪律跳过 —— 那一跳之后再无补偿，
于是**重转过的会议永远压不了**。修法：`retranscribe_meeting._run()` 的 finally 里，
**清掉 `_retranscribing` 标记之后**补一次 `_maybe_auto_compress(folder)`。

这条用例钉两件事（顺序很重要，正是这个 bug 的要害）：
  ① 重转结束后**真的会去压**；
  ② 压的时候标记**已经清掉了** —— 否则 `compression_state()` 仍会判"正在重新转写"、
     照样压不成（"调了"不等于"压得成"）。
"""
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.meeting as meeting                                       # noqa: E402

P = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "test_meeting_retranscribe_guard.py")


class RetranscribeCompressesTests(unittest.TestCase):
    """用**源码判据**钉，不重复搭一遍那套隔离（本文件已有一整套 _GuardCase）。"""

    def setUp(self):
        self.src = io.open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "app", "meeting.py"), encoding="utf-8").read()

    def test_run_finally_calls_auto_compress(self):
        i = self.src.find("def retranscribe_meeting")
        self.assertGreater(i, -1)
        seg = self.src[i:i + 3000]
        # 找 `_run` 的 finally 块
        j = seg.find("def _run():")
        self.assertGreater(j, -1, "retranscribe 里没有 _run（改了结构？）")
        run = seg[j:j + 1400]
        self.assertIn("_maybe_auto_compress", run,
                      "_run 的 finally 里没有补自动压缩 → 重转过的会议永远压不了")

    def test_marker_is_discarded_before_compressing(self):
        """顺序判据：必须先 `discard`（清标记），再压。

        反过来的话 `compression_state()` 看到的还是"正在重新转写"，
        那一刀等于白补 —— 这正是必须钉住的地方。
        """
        i = self.src.find("def retranscribe_meeting")
        seg = self.src[i:i + 3000]
        j = seg.find("def _run():")
        run = seg[j:j + 1400]
        k_discard = run.find('_retranscribing["set"].discard(name)')
        k_compress = run.find("_maybe_auto_compress")
        self.assertGreater(k_discard, -1, "没清 _retranscribing 标记")
        self.assertGreater(k_compress, -1, "没补自动压缩")
        self.assertLess(k_discard, k_compress,
                        "顺序错了：必须先清标记再压，否则 compression_state() 仍判"
                        "「正在重新转写」→ 照样压不成")

    def test_compress_failure_cannot_break_the_run(self):
        """压缩失败绝不能影响"这场转写好了" —— 那一刀必须被 try 包住。"""
        i = self.src.find("def retranscribe_meeting")
        seg = self.src[i:i + 3000]
        j = seg.find("def _run():")
        run = seg[j:j + 1400]
        k = run.find("_maybe_auto_compress")
        head = run[max(0, k - 600):k]
        self.assertIn("try:", head, "自动压缩没有被 try 包住（它抛错会污染转写结果）")


if __name__ == "__main__":
    unittest.main()
