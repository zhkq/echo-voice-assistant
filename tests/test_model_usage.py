# -*- coding: utf-8 -*-
"""模型使用账本（schema v7 的 `model_usage` + `app/model_usage.py`）。

这一层是清理功能的**前提**：没有"用没用过"的真实记录，"近期没再使用的模型"就只能靠
文件时间猜 —— 那是瞎删。所以这里钉住四件事：

1. **用过才记、没用过是空**（用户用例原话）；
2. 次数与上次使用时间都会动（同一个模型只占一行）；
3. **记在真的调用上**：`stt.transcribe_ex()` / `load_engine()` 走一次记一次，
   而"列清单、探就绪"**不算**使用（否则刚下完没用过的模型立刻变"刚用过"）；
4. 账本坏掉（写不进去）**绝不能**让一次转写失败。

测试隔离：`db.DATA_DIR/DB_FILE` 指到临时目录；账本总闸在 `tests/__init__.py` 里默认关着，
本模块按需打开（见那里的说明）。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import app.db as db
from app import model_usage


class _LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-usage-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._p1 = patch.object(db, "DATA_DIR", self.tmp)
        self._p2 = patch.object(db, "DB_FILE", os.path.join(self.tmp, "echo.db"))
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)
        self._p3 = patch.object(model_usage, "ENABLED", True)
        self._p3.start()
        self.addCleanup(self._p3.stop)
        db.init()


class LedgerFactsTests(_LedgerTestCase):
    def test_used_then_recorded_unused_then_empty(self):
        """**用过才记、没用过是空** —— 用户用例的原话。"""
        self.assertEqual(model_usage.usage_of("whisper-small"),
                         {"lastUsedAt": "", "useCount": 0, "pinned": False, "pinnedAt": ""},
                         "没用过的模型必须是空白，不许有幽灵记录")
        self.assertTrue(model_usage.note_used("whisper-small"))
        row = model_usage.usage_of("whisper-small")
        self.assertEqual(row["useCount"], 1)
        self.assertTrue(row["lastUsedAt"].startswith("20"),
                        "上次使用时间应当是本地时间字符串，实际 %r" % row["lastUsedAt"])
        self.assertEqual(model_usage.usage_of("whisper-base")["useCount"], 0)

    def test_count_and_time_advance_on_the_same_row(self):
        model_usage.note_used("sherpa", when="2026-01-01 08:00:00")
        model_usage.note_used("sherpa", when="2026-02-02 09:30:00")
        rows = db.model_usage()
        self.assertEqual(len(rows), 1, "同一个模型只占一行")
        self.assertEqual(rows[0]["use_count"], 2)
        self.assertEqual(rows[0]["last_used_at"], "2026-02-02 09:30:00")

    def test_blank_id_is_not_recorded(self):
        self.assertFalse(model_usage.note_used(""))
        self.assertFalse(model_usage.note_used(None))
        self.assertEqual(db.model_usage(), [])

    def test_pin_is_stored_and_visible(self):
        self.assertTrue(model_usage.pin("qwen3asr"))
        self.assertTrue(model_usage.usage_of("qwen3asr")["pinned"])
        model_usage.pin("qwen3asr", pinned=False)
        self.assertFalse(model_usage.usage_of("qwen3asr")["pinned"])


class EngineIdMappingTests(unittest.TestCase):
    """id 与 `/api/models` 的 item id 必须一模一样 —— 否则面板上对不上号。"""

    def test_engine_value_maps_to_catalog_id(self):
        self.assertEqual(model_usage.engine_model_id("sensevoice"), "sensevoice")
        self.assertEqual(model_usage.engine_model_id("sherpa"), "sherpa")
        self.assertEqual(model_usage.engine_model_id("qwen3asr"), "qwen3asr")
        self.assertEqual(model_usage.engine_model_id("Qwen/Qwen3-ASR-0.6B"), "qwen3asr")
        self.assertEqual(model_usage.engine_model_id("0.6B"), "qwen3asr")
        self.assertEqual(model_usage.engine_model_id("small"), "whisper-small")
        self.assertEqual(model_usage.engine_model_id("large"), "whisper-large-v3")
        self.assertEqual(model_usage.engine_model_id("nonsense"), "")

    def test_engine_model_pair_maps_to_catalog_id(self):
        self.assertEqual(model_usage.model_id_for_engine("whisper", "tiny"), "whisper-tiny")
        self.assertEqual(model_usage.model_id_for_engine("whisper", "large"), "whisper-large-v3")
        self.assertEqual(model_usage.model_id_for_engine("qwen3asr", "Qwen/Qwen3-ASR-0.6B"),
                         "qwen3asr")
        self.assertEqual(model_usage.model_id_for_engine("whisper", "sensevoice"), "")

    def test_mapping_agrees_with_the_real_catalog(self):
        """折出来的 id 必须真在出厂清单里（防止 mapping 与 CATALOG 分叉）。"""
        known = set(model_usage.known_ids())
        self.assertIn("whisper-small", known)
        for value in ("sensevoice", "sherpa", "qwen3asr", "tiny", "base", "small",
                      "medium", "large", "large-v3"):
            mid = model_usage.engine_model_id(value)
            self.assertIn(mid, known, "%s 折成了 %s，但清单里没有它" % (value, mid))


class TranscribeRecordsUseTests(_LedgerTestCase):
    """**写入时机**：真的调用才记；文件不存在、清单探测都不记。"""

    class _Seg:
        text = "喂"

    def _fake_whisper(self):
        return patch.multiple(
            "app.audio.stt",
            _get_whisper=lambda *a, **k: object(),
            transcribe_whisper=lambda *a, **k: ([self._Seg()], None),
        )

    def test_transcribe_records_the_engine_it_actually_used(self):
        from app.audio import stt
        with self._fake_whisper():
            out = stt.transcribe_ex(__file__, engine="whisper", model="small")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(model_usage.usage_of("whisper-small")["useCount"], 1)
        self.assertEqual(model_usage.usage_of("sensevoice")["useCount"], 0,
                         "没走 sensevoice 就不该有它的记录")

    def test_transcribe_repeated_calls_keep_counting(self):
        from app.audio import stt
        with self._fake_whisper():
            for _ in range(3):
                stt.transcribe_ex(__file__, engine="whisper", model="base")
        self.assertEqual(model_usage.usage_of("whisper-base")["useCount"], 3)

    def test_missing_file_records_nothing(self):
        from app.audio import stt
        out = stt.transcribe_ex(os.path.join(self.tmp, "nope.wav"), engine="whisper")
        self.assertEqual(out["status"], "missing")
        self.assertEqual(db.model_usage(), [], "文件都不存在，什么都没用上，不该记")

    def test_load_engine_records_an_explicit_preload(self):
        from app.audio import stt
        with patch.multiple("app.audio.stt", _get_sherpa=lambda: object()):
            stt.load_engine("sherpa", "")
        self.assertEqual(model_usage.usage_of("sherpa")["useCount"], 1)

    def test_ledger_failure_never_breaks_transcribe(self):
        """账本写不进去（库锁着、迁移没跑）**不许**把一次转写带崩。"""
        from app.audio import stt
        with self._fake_whisper(), \
                patch.object(db, "record_model_use", side_effect=RuntimeError("库锁着")):
            out = stt.transcribe_ex(__file__, engine="whisper", model="small")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["text"], "喂")


class InUseJudgementTests(_LedgerTestCase):
    """「现在谁在用」：当前配置选中的 + 本进程已经加载的。"""

    def _select(self, **values):
        from app import config as cfg
        return patch.object(cfg.settings, "_cache", dict(values))

    def test_selected_engines_are_in_use(self):
        with self._select(sttModel="small", meetingSttModel="qwen3asr"):
            used = model_usage.in_use_ids()
        self.assertIn("whisper-small", used)
        self.assertIn("qwen3asr", used)
        self.assertTrue(any("sttModel" in r for r in used["whisper-small"]),
                        "原因要说清是哪个设置选中的：%r" % used["whisper-small"])

    def test_wake_engine_only_counts_when_wake_is_on(self):
        with self._select(wakeEngine="kws", wakeEnabled=False):
            self.assertNotIn("kws", model_usage.in_use_ids())
        with self._select(wakeEngine="kws", wakeEnabled=True):
            self.assertIn("kws", model_usage.in_use_ids())

    def test_diarize_auto_still_protects_the_local_pyannote(self):
        """auto 可能落到本机 —— 保守取向：宁可不建议，也不要删掉还会被用到的模型。"""
        with self._select(capabilityDiarizeBackend="auto", capabilityEmbedBackend="auto"):
            self.assertIn("pyannote", model_usage.in_use_ids())
        with self._select(capabilityDiarizeBackend="echo-server",
                          capabilityEmbedBackend="echo-server",
                          sttModel="sherpa", meetingSttModel="qwen3asr"):
            self.assertNotIn("pyannote", model_usage.in_use_ids())

    def test_loaded_engines_are_in_use(self):
        from app.audio import stt
        stt._ENGINES["whisper:medium"] = object()
        self.addCleanup(stt._ENGINES.pop, "whisper:medium", None)
        with self._select(sttModel="sherpa", wakeEnabled=False):
            used = model_usage.in_use_ids()
        self.assertIn("whisper-medium", used)
        self.assertTrue(any("已加载" in r for r in used["whisper-medium"]))


class InventoryExposureTests(_LedgerTestCase):
    """/api/models 要带上使用情况（面板「本地能力」与「清理」都读它）。"""

    def test_inventory_rows_carry_usage_fields(self):
        from app import modelinfo
        model_usage.note_used("whisper-small", when="2026-08-01 10:00:00")
        model_usage.pin("sherpa")
        items = {it["id"]: it for it in modelinfo.inventory()}
        small = items["whisper-small"]
        self.assertEqual(small["lastUsedAt"], "2026-08-01 10:00:00")
        self.assertEqual(small["useCount"], 1)
        self.assertFalse(small["pinned"])
        self.assertTrue(items["sherpa"]["pinned"])
        for mid, row in items.items():
            for field in ("lastUsedAt", "useCount", "pinned", "pinnedAt", "inUse",
                          "inUseReasons"):
                self.assertIn(field, row, "%s 少了字段 %s" % (mid, field))


if __name__ == "__main__":
    unittest.main()
